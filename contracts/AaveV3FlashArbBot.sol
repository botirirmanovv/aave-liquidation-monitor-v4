// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/*//////////////////////////////////////////////////////////////
                            ИНТЕРФЕЙСЫ
//////////////////////////////////////////////////////////////*/

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function balanceOf(address owner) external view returns (uint256);
}

interface IUniswapV2Router02 {
    function swapExactTokensForTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external returns (uint[] memory amounts);
}

interface IAaveV3Pool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

/*//////////////////////////////////////////////////////////////
                    БЕЗОПАСНАЯ РАБОТА С ERC20
//////////////////////////////////////////////////////////////*/

library SafeERC20 {
    error ERC20CallFailed();
    error ERC20OperationFailed();

    function safeApprove(IERC20 token, address spender, uint256 amount) internal {
        _call(token, abi.encodeWithSelector(token.approve.selector, spender, 0));
        if (amount > 0) {
            _call(token, abi.encodeWithSelector(token.approve.selector, spender, amount));
        }
    }

    function safeTransfer(IERC20 token, address to, uint256 amount) internal {
        _call(token, abi.encodeWithSelector(token.transfer.selector, to, amount));
    }

    function _call(IERC20 token, bytes memory data) private {
        (bool success, bytes memory returndata) = address(token).call(data);
        if (!success) revert ERC20CallFailed();
        if (returndata.length > 0) {
            if (returndata.length < 32) revert ERC20OperationFailed();
            if (!abi.decode(returndata, (bool))) revert ERC20OperationFailed();
        }
    }
}

/**
 * @title AaveV3FlashArbBot
 * @notice Two-leg UniswapV2-style flash arbitrage funded by Aave V3 flashLoanSimple.
 *
 * Flow:
 *   1. Operator calls initiateArb(borrowAsset, amount, params)
 *   2. Aave lends `amount` of borrowAsset and calls executeOperation
 *   3. We swap borrowAsset -> mid on routerBuy
 *   4. We swap mid -> borrowAsset on routerSell
 *   5. Repay amount + premium; leftover is profit (reverts if below minProfit)
 *
 * The two routers must be different venues (or differently priced pools). Same
 * router with the same path cannot be profitable after fees.
 *
 * @custom:dev-run-script scripts/deploy_flash_arb_bot.ts
 */
contract AaveV3FlashArbBot {
    using SafeERC20 for IERC20;

    error OnlyOwner();
    error OnlyOperator();
    error ZeroAddress();
    error InvalidAmount();
    error ContractPaused();
    error Reentrancy();
    error UnauthorizedPool();
    error UnauthorizedInitiator();
    error RouterNotAllowed();
    error InsufficientProfit();
    error ExceedsLimit();
    error NoFlashLoanInProgress();
    error InvalidPath();
    error SameRouter();

    address public owner;
    address public operator;
    bool public paused;

    IAaveV3Pool public immutable POOL;

    mapping(address => bool) public allowedRouters;
    /// @notice Cap per borrowed asset. 0 means the asset is not enabled.
    mapping(address => uint256) public maxBorrowPerToken;

    bool private flashLoanActive;
    bool private locked;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event OperatorUpdated(address indexed previousOperator, address indexed newOperator);
    event RouterAllowlistUpdated(address indexed router, bool allowed);
    event MaxBorrowUpdated(address indexed token, uint256 amount);
    event Paused(address indexed by);
    event Unpaused(address indexed by);
    event ArbStarted(address indexed borrowAsset, uint256 amount, address routerBuy, address routerSell);
    event ArbLeg(address indexed router, address tokenIn, uint256 amountIn, uint256 amountOut);
    event Profit(address indexed asset, uint256 amount);
    event FlashLoanRepaid(uint256 amountOwed);
    event TokensRescued(address indexed token, address indexed to, uint256 amount);

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    modifier onlyOperatorOrOwner() {
        if (msg.sender != operator && msg.sender != owner) revert OnlyOperator();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    modifier nonReentrant() {
        if (locked) revert Reentrancy();
        locked = true;
        _;
        locked = false;
    }

    constructor(address pool, address initialOperator) {
        if (pool == address(0)) revert ZeroAddress();
        owner = msg.sender;
        operator = initialOperator == address(0) ? msg.sender : initialOperator;
        POOL = IAaveV3Pool(pool);
        emit OwnershipTransferred(address(0), msg.sender);
        emit OperatorUpdated(address(0), operator);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    function setOperator(address newOperator) external onlyOwner {
        if (newOperator == address(0)) revert ZeroAddress();
        emit OperatorUpdated(operator, newOperator);
        operator = newOperator;
    }

    function setRouterAllowed(address router, bool allowed) external onlyOwner {
        if (router == address(0)) revert ZeroAddress();
        allowedRouters[router] = allowed;
        emit RouterAllowlistUpdated(router, allowed);
    }

    function setMaxBorrow(address token, uint256 amount) external onlyOwner {
        if (token == address(0)) revert ZeroAddress();
        maxBorrowPerToken[token] = amount;
        emit MaxBorrowUpdated(token, amount);
    }

    function pause() external onlyOwner {
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external onlyOwner {
        paused = false;
        emit Unpaused(msg.sender);
    }

    function rescueTokens(address token, address to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        IERC20(token).safeTransfer(to, amount);
        emit TokensRescued(token, to, amount);
    }

    struct ArbParams {
        address routerBuy;     // first leg: borrowAsset -> mid
        address routerSell;    // second leg: mid -> borrowAsset
        address[] pathBuy;     // starts with borrowAsset
        address[] pathSell;    // ends with borrowAsset
        uint256 amountOutMinBuy;
        uint256 amountOutMinSell;
        uint256 minProfit;     // in borrowAsset, after premium
        uint256 deadline;
    }

    function initiateArb(
        address borrowAsset,
        uint256 amount,
        ArbParams calldata params
    ) external onlyOperatorOrOwner whenNotPaused nonReentrant {
        if (borrowAsset == address(0)) revert ZeroAddress();
        if (amount == 0) revert InvalidAmount();

        uint256 limit = maxBorrowPerToken[borrowAsset];
        if (limit == 0 || amount > limit) revert ExceedsLimit();

        if (params.routerBuy == address(0) || params.routerSell == address(0)) revert ZeroAddress();
        if (params.routerBuy == params.routerSell) revert SameRouter();
        if (!allowedRouters[params.routerBuy] || !allowedRouters[params.routerSell]) {
            revert RouterNotAllowed();
        }

        uint256 buyLen = params.pathBuy.length;
        uint256 sellLen = params.pathSell.length;
        if (buyLen < 2 || sellLen < 2) revert InvalidPath();
        if (params.pathBuy[0] != borrowAsset) revert InvalidPath();
        if (params.pathSell[sellLen - 1] != borrowAsset) revert InvalidPath();
        // Mid token must match across legs, otherwise the second swap has nothing useful.
        if (params.pathBuy[buyLen - 1] != params.pathSell[0]) revert InvalidPath();

        emit ArbStarted(borrowAsset, amount, params.routerBuy, params.routerSell);

        bytes memory data = abi.encode(params);
        flashLoanActive = true;
        POOL.flashLoanSimple(address(this), borrowAsset, amount, data, 0);
        flashLoanActive = false;
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata data
    ) external returns (bool) {
        if (msg.sender != address(POOL)) revert UnauthorizedPool();
        if (initiator != address(this)) revert UnauthorizedInitiator();
        if (!flashLoanActive) revert NoFlashLoanInProgress();

        ArbParams memory params = abi.decode(data, (ArbParams));
        uint256 midAmount = _swapBuy(asset, amount, params);
        _swapSell(params, midAmount);

        uint256 amountOwed = amount + premium;
        uint256 finalBalance = IERC20(asset).balanceOf(address(this));
        if (finalBalance < amountOwed + params.minProfit) revert InsufficientProfit();

        emit Profit(asset, finalBalance - amountOwed);
        IERC20(asset).safeApprove(address(POOL), amountOwed);
        emit FlashLoanRepaid(amountOwed);
        return true;
    }

    function _swapBuy(
        address asset,
        uint256 amount,
        ArbParams memory params
    ) private returns (uint256 midAmount) {
        IERC20(asset).safeApprove(params.routerBuy, amount);
        uint256[] memory bought = IUniswapV2Router02(params.routerBuy).swapExactTokensForTokens(
            amount,
            params.amountOutMinBuy,
            params.pathBuy,
            address(this),
            params.deadline
        );
        midAmount = bought[bought.length - 1];
        emit ArbLeg(params.routerBuy, asset, amount, midAmount);
    }

    function _swapSell(ArbParams memory params, uint256 midAmount) private {
        address mid = params.pathBuy[params.pathBuy.length - 1];
        IERC20(mid).safeApprove(params.routerSell, midAmount);
        uint256[] memory sold = IUniswapV2Router02(params.routerSell).swapExactTokensForTokens(
            midAmount,
            params.amountOutMinSell,
            params.pathSell,
            address(this),
            params.deadline
        );
        emit ArbLeg(params.routerSell, mid, midAmount, sold[sold.length - 1]);
    }
}
