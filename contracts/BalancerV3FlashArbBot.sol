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

interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

/// @notice Uniswap V3 SwapRouter02-style exactInputSingle (no deadline field).
interface ISwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params)
        external
        payable
        returns (uint256 amountOut);
}

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
 * @title BalancerV3FlashArbBot
 * @notice Two-leg Uniswap V3 flash arbitrage funded by a Balancer V2 Vault flash loan.
 *
 * Why Balancer: flash fee is 0%. Aave's 5 bps premium alone killed round-trips on
 * tight L2 majors; removing it is the precondition for any on-chain DEX arb edge.
 *
 * Flow:
 *   1. Operator calls initiateArb(borrowAsset, midAsset, amount, fees, minProfit)
 *   2. Vault lends `amount` and calls receiveFlashLoan (feeAmounts[i] == 0)
 *   3. exactInputSingle borrow -> mid at feeBuy
 *   4. exactInputSingle mid -> borrow at feeSell
 *   5. Repay exact `amount` to the Vault; leftover is profit
 *
 * @custom:dev-run-script scripts/deploy_balancer_v3_arb_bot.ts
 */
contract BalancerV3FlashArbBot {
    using SafeERC20 for IERC20;

    error OnlyOwner();
    error OnlyOperator();
    error ZeroAddress();
    error InvalidAmount();
    error ContractPaused();
    error Reentrancy();
    error UnauthorizedVault();
    error RouterNotAllowed();
    error InsufficientProfit();
    error ExceedsLimit();
    error NoFlashLoanInProgress();
    error SameFee();

    address public owner;
    address public operator;
    bool public paused;

    IBalancerVault public immutable VAULT;
    address public v3Router;

    mapping(address => uint256) public maxBorrowPerToken;
    mapping(address => bool) public allowedRouters;

    bool private flashLoanActive;
    bool private locked;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event OperatorUpdated(address indexed previousOperator, address indexed newOperator);
    event RouterUpdated(address indexed router);
    event RouterAllowlistUpdated(address indexed router, bool allowed);
    event MaxBorrowUpdated(address indexed token, uint256 amount);
    event Paused(address indexed by);
    event Unpaused(address indexed by);
    event ArbStarted(
        address indexed borrowAsset,
        address indexed midAsset,
        uint256 amount,
        uint24 feeBuy,
        uint24 feeSell
    );
    event ArbLeg(address tokenIn, uint256 amountIn, uint256 amountOut, uint24 fee);
    event Profit(address indexed asset, uint256 amount);
    event FlashLoanRepaid(uint256 amount);
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

    /// @param vault Balancer V2 Vault (0xBA12…F2C8 on Base/Arb/OP/Ethereum)
    /// @param swapRouter Uniswap V3 SwapRouter02
    constructor(address vault, address swapRouter, address initialOperator) {
        if (vault == address(0) || swapRouter == address(0)) revert ZeroAddress();
        owner = msg.sender;
        operator = initialOperator == address(0) ? msg.sender : initialOperator;
        VAULT = IBalancerVault(vault);
        v3Router = swapRouter;
        allowedRouters[swapRouter] = true;
        emit OwnershipTransferred(address(0), msg.sender);
        emit OperatorUpdated(address(0), operator);
        emit RouterUpdated(swapRouter);
        emit RouterAllowlistUpdated(swapRouter, true);
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

    function setV3Router(address router) external onlyOwner {
        if (router == address(0)) revert ZeroAddress();
        v3Router = router;
        allowedRouters[router] = true;
        emit RouterUpdated(router);
        emit RouterAllowlistUpdated(router, true);
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
        address midAsset;
        uint24 feeBuy;
        uint24 feeSell;
        uint256 amountOutMinBuy;
        uint256 amountOutMinSell;
        uint256 minProfit; // in borrowAsset; Balancer fee is 0 so repay == amount
    }

    function initiateArb(
        address borrowAsset,
        uint256 amount,
        ArbParams calldata params
    ) external onlyOperatorOrOwner whenNotPaused nonReentrant {
        if (borrowAsset == address(0) || params.midAsset == address(0)) revert ZeroAddress();
        if (amount == 0) revert InvalidAmount();
        if (params.feeBuy == params.feeSell) revert SameFee();

        uint256 limit = maxBorrowPerToken[borrowAsset];
        if (limit == 0 || amount > limit) revert ExceedsLimit();
        if (!allowedRouters[v3Router]) revert RouterNotAllowed();

        emit ArbStarted(borrowAsset, params.midAsset, amount, params.feeBuy, params.feeSell);

        address[] memory tokens = new address[](1);
        tokens[0] = borrowAsset;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = amount;

        flashLoanActive = true;
        VAULT.flashLoan(address(this), tokens, amounts, abi.encode(borrowAsset, amount, params));
        flashLoanActive = false;
    }

    /// @dev Balancer callback. feeAmounts are 0 on current Vault deployments.
    function receiveFlashLoan(
        address[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external {
        if (msg.sender != address(VAULT)) revert UnauthorizedVault();
        if (!flashLoanActive) revert NoFlashLoanInProgress();
        if (tokens.length != 1 || amounts.length != 1) revert InvalidAmount();

        (address borrowAsset, uint256 amount, ArbParams memory params) =
            abi.decode(userData, (address, uint256, ArbParams));

        if (tokens[0] != borrowAsset || amounts[0] != amount) revert InvalidAmount();

        uint256 midOut = _swapV3(
            borrowAsset,
            params.midAsset,
            amount,
            params.feeBuy,
            params.amountOutMinBuy
        );
        _swapV3(
            params.midAsset,
            borrowAsset,
            midOut,
            params.feeSell,
            params.amountOutMinSell
        );

        // Balancer pulls `amount + fee` via transferFrom; fee is currently 0.
        uint256 repay = amount + feeAmounts[0];
        uint256 finalBalance = IERC20(borrowAsset).balanceOf(address(this));
        if (finalBalance < repay + params.minProfit) revert InsufficientProfit();

        emit Profit(borrowAsset, finalBalance - repay);
        IERC20(borrowAsset).safeApprove(address(VAULT), repay);
        emit FlashLoanRepaid(repay);
    }

    function _swapV3(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24 fee,
        uint256 amountOutMin
    ) private returns (uint256 amountOut) {
        IERC20(tokenIn).safeApprove(v3Router, amountIn);
        amountOut = ISwapRouter02(v3Router).exactInputSingle(
            ISwapRouter02.ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOut,
                fee: fee,
                recipient: address(this),
                amountIn: amountIn,
                amountOutMinimum: amountOutMin,
                sqrtPriceLimitX96: 0
            })
        );
        emit ArbLeg(tokenIn, amountIn, amountOut, fee);
    }
}
