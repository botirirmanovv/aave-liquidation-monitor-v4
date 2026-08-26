// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/*//////////////////////////////////////////////////////////////
                            INTERFACES
//////////////////////////////////////////////////////////////*/

/// @dev Minimal ERC-20. `allowance` is required for gas-efficient max-approvals.
interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function balanceOf(address owner) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
}

/// @dev Canonical Morpho Blue `MarketParams` (morpho-org/morpho-blue IMorpho.sol).
struct MarketParams {
    address loanToken;
    address collateralToken;
    address oracle;
    address irm;
    uint256 lltv;
}

/// @dev Morpho Blue `liquidate` — built-in flash callback, not Balancer flashLoan.
interface IMorpho {
    function liquidate(
        MarketParams memory marketParams,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes memory data
    ) external returns (uint256, uint256);
}

/// @dev morpho-org/morpho-blue IMorphoCallbacks.sol — exact signature.
interface IMorphoLiquidateCallback {
    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external;
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
 * @title MorphoFlashLiquidator
 * @notice Single-call Morpho Blue liquidation using `liquidate(..., data)` →
 *         `onMorphoLiquidate` → swap seized collateral → Morpho pulls repay.
 *
 * Flow (Morpho Blue, verified against morpho-org/morpho-blue):
 *   1. Operator calls liquidateWithFlash (one external tx)
 *   2. Morpho seizes collateral to this contract, then callbacks
 *   3. Swap via allowlisted router + operator-built calldata (UniV3 / Aerodrome / …)
 *   4. Require profit (abs + bps floor); Morpho `transferFrom`s repaidAssets
 *
 * Base Morpho Blue: 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb
 *
 * Typical Base routers to allowlist (NOT hardcoded as the swap implementation):
 *   - Uniswap V3 SwapRouter02: 0x2626664c2603336E57B271c5C0b26F421741e481
 *   - SwapRouter02 used by this repo's Balancer V3 bot:
 *       0x2626664c2603336E57b271c5c0d842f2875A7dA0
 *   - Aerodrome Router:        0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
 *   - BaseSwap V2:             0x327Df1E6de05895d2ab08513aaDD9313Fe505d86
 *   - Sushi V2:                0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891
 *
 * Reentrancy: nonReentrant is on the outer entrypoint ONLY. Morpho callbacks
 * into onMorphoLiquidate while the lock is held (same pattern as AaveV3LiquidationBot).
 */
contract MorphoFlashLiquidator is IMorphoLiquidateCallback {
    using SafeERC20 for IERC20;

    uint256 public constant BPS_DENOMINATOR = 10_000;
    uint256 public constant DEFAULT_MIN_PROFIT_BPS = 50;

    error OnlyOwner();
    error OnlyOperator();
    error ZeroAddress();
    error InvalidAmount();
    error ContractPaused();
    error Reentrancy();
    error UnauthorizedMorpho();
    error RouterNotAllowed();
    error IntermediateTokenNotAllowed();
    error InsufficientProfit();
    error SwapFailed();
    error NoLiquidationInProgress();
    error TransferFailed();
    error InvalidBps();
    error NoShotLoaded();
    address public owner;
    address public operator;
    bool public paused;

    IMorpho public immutable MORPHO;

    mapping(address => bool) public allowedRouters;
    mapping(address => bool) public allowedIntermediateTokens;
    mapping(address => uint256) public minProfitAbs;
    uint256 public minProfitBps = DEFAULT_MIN_PROFIT_BPS;

    bool private locked;
    bool private liquidationActive;

    /// @dev Preloaded shot for short fire()/0xbeef path (whale-style).
    struct LoadedShot {
        address loanToken;
        address collateralToken;
        address oracle;
        address irm;
        uint256 lltv;
        address borrower;
        uint256 seizedAssets;
        uint256 repaidShares;
        address router;
        uint256 minProfit;
        bool active;
    }

    LoadedShot private _loaded;
    bytes private _loadedSwapCalldata;

    struct SwapParams {
        address router;
        bytes swapCalldata;
    }

    struct CallbackData {
        address loanToken;
        address collateralToken;
        address router;
        uint256 minProfit;
        bytes swapCalldata;
    }

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event OperatorUpdated(address indexed previousOperator, address indexed newOperator);
    event RouterAllowlistUpdated(address indexed router, bool allowed);
    event IntermediateTokenAllowlistUpdated(address indexed token, bool allowed);
    event MinProfitAbsUpdated(address indexed loanToken, uint256 amount);
    event MinProfitBpsUpdated(uint256 bps);
    event Paused(address indexed by);
    event Unpaused(address indexed by);
    event LiquidationStarted(
        address indexed borrower,
        address indexed loanToken,
        address indexed collateralToken,
        uint256 seizedAssets,
        uint256 repaidShares
    );
    event LiquidationExecuted(address indexed borrower, uint256 seizedAssets, uint256 repaidAssets);
    event CollateralSwapped(address indexed router, address tokenIn, uint256 amountIn);
    event Profit(address indexed asset, uint256 amount);
    event TokensRescued(address indexed token, address indexed to, uint256 amount);
    event ApprovalsSet(address indexed token, address[] spenders, uint256 amount);
    event ShotLoaded(address indexed borrower, address indexed loanToken, address indexed collateralToken);
    event ShotFired(address indexed borrower);

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    modifier onlyOperator() {
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

    constructor(address morpho, address initialOperator) {
        if (morpho == address(0)) revert ZeroAddress();
        owner = msg.sender;
        operator = initialOperator == address(0) ? msg.sender : initialOperator;
        MORPHO = IMorpho(morpho);
        paused = true;
        emit OwnershipTransferred(address(0), msg.sender);
        emit OperatorUpdated(address(0), operator);
        emit Paused(msg.sender);
    }

    receive() external payable {}

    /*//////////////////////////////////////////////////////////////
                              ADMIN
    //////////////////////////////////////////////////////////////*/

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

    function setIntermediateTokenAllowed(address token, bool allowed) external onlyOwner {
        if (token == address(0)) revert ZeroAddress();
        allowedIntermediateTokens[token] = allowed;
        emit IntermediateTokenAllowlistUpdated(token, allowed);
    }

    function setMinProfitAbs(address loanToken, uint256 amount) external onlyOwner {
        if (loanToken == address(0)) revert ZeroAddress();
        minProfitAbs[loanToken] = amount;
        emit MinProfitAbsUpdated(loanToken, amount);
    }

    function setMinProfitBps(uint256 bps) external onlyOwner {
        if (bps > BPS_DENOMINATOR) revert InvalidBps();
        minProfitBps = bps;
        emit MinProfitBpsUpdated(bps);
    }

    function pause() external onlyOwner {
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external onlyOwner {
        paused = false;
        emit Unpaused(msg.sender);
    }

    /// @notice Finite-approve `spenders` on `token` (Morpho + routers). Not `type(uint256).max`.
    /// @dev Cap examples on Base: 25_000e6 USDC (6 dec), 30_000e6 cbXRP (6 dec, covers ~15% bonus).
    function setApprovals(address token, address[] calldata spenders, uint256 amount) external onlyOwner {
        if (token == address(0)) revert ZeroAddress();
        uint256 len = spenders.length;
        if (len == 0 || amount == 0) revert InvalidAmount();
        if (amount == type(uint256).max) revert InvalidAmount();
        for (uint256 i; i < len; ++i) {
            address spender = spenders[i];
            if (spender == address(0)) revert ZeroAddress();
            IERC20(token).safeApprove(spender, amount);
        }
        emit ApprovalsSet(token, spenders, amount);
    }

    function rescueTokens(address token, address to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        if (amount == 0) revert InvalidAmount();
        if (token == address(0)) {
            (bool ok,) = to.call{value: amount}("");
            if (!ok) revert TransferFailed();
        } else {
            IERC20(token).safeTransfer(to, amount);
        }
        emit TokensRescued(token, to, amount);
    }

    /*//////////////////////////////////////////////////////////////
                           LIQUIDATION
    //////////////////////////////////////////////////////////////*/

    /// @param p             Morpho market (loan/collateral/oracle/irm/lltv)
    /// @param borrower      Unhealthy position owner
    /// @param seizedAssets  Collateral to seize; 0 = derive from repaidShares
    /// @param repaidShares  Debt shares to repay; 0 = derive from seizedAssets
    /// @param swap          Allowlisted router + pre-built swap calldata
    /// @param minProfit     Extra absolute floor (combined with storage abs/bps)
    function liquidateWithFlash(
        MarketParams calldata p,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        SwapParams calldata swap,
        uint256 minProfit
    ) external onlyOperator whenNotPaused nonReentrant {
        if (borrower == address(0)) revert ZeroAddress();
        if (p.loanToken == address(0) || p.collateralToken == address(0)) revert ZeroAddress();
        if ((seizedAssets == 0) == (repaidShares == 0)) revert InvalidAmount();
        if (swap.router == address(0) || !allowedRouters[swap.router]) revert RouterNotAllowed();
        if (swap.router == address(this) || swap.router == address(MORPHO)) revert RouterNotAllowed();
        if (swap.swapCalldata.length == 0) revert SwapFailed();

        emit LiquidationStarted(borrower, p.loanToken, p.collateralToken, seizedAssets, repaidShares);

        bytes memory data = abi.encode(
            CallbackData({
                loanToken: p.loanToken,
                collateralToken: p.collateralToken,
                router: swap.router,
                minProfit: minProfit,
                swapCalldata: swap.swapCalldata
            })
        );

        liquidationActive = true;
        (uint256 seized, uint256 repaid) = MORPHO.liquidate(p, borrower, seizedAssets, repaidShares, data);
        liquidationActive = false;

        emit LiquidationExecuted(borrower, seized, repaid);
    }

    /// @notice Same-market multi-borrower liquidation (cascade soft→hard batch).
    /// @dev Max 8 items. Each item gets its own Morpho callback + swap.
    function liquidateWithFlashBatch(
        MarketParams calldata p,
        address[] calldata borrowers,
        uint256[] calldata seizedAssets,
        uint256[] calldata repaidShares,
        SwapParams[] calldata swaps,
        uint256[] calldata minProfits
    ) external onlyOperator whenNotPaused nonReentrant {
        uint256 len = borrowers.length;
        if (len == 0 || len > 8) revert InvalidAmount();
        if (
            seizedAssets.length != len || repaidShares.length != len || swaps.length != len
                || minProfits.length != len
        ) {
            revert InvalidAmount();
        }
        if (p.loanToken == address(0) || p.collateralToken == address(0)) revert ZeroAddress();

        for (uint256 i; i < len; ++i) {
            _liquidateOne(p, borrowers[i], seizedAssets[i], repaidShares[i], swaps[i], minProfits[i]);
        }
    }

    function _liquidateOne(
        MarketParams calldata p,
        address borrower,
        uint256 seizedIn,
        uint256 repaidIn,
        SwapParams calldata swap,
        uint256 minProfit
    ) private {
        if (borrower == address(0)) revert ZeroAddress();
        if ((seizedIn == 0) == (repaidIn == 0)) revert InvalidAmount();
        if (swap.router == address(0) || !allowedRouters[swap.router]) revert RouterNotAllowed();
        if (swap.router == address(this) || swap.router == address(MORPHO)) revert RouterNotAllowed();
        if (swap.swapCalldata.length == 0) revert SwapFailed();

        emit LiquidationStarted(borrower, p.loanToken, p.collateralToken, seizedIn, repaidIn);

        bytes memory data = abi.encode(
            CallbackData({
                loanToken: p.loanToken,
                collateralToken: p.collateralToken,
                router: swap.router,
                minProfit: minProfit,
                swapCalldata: swap.swapCalldata
            })
        );

        liquidationActive = true;
        (uint256 seized, uint256 repaid) = MORPHO.liquidate(p, borrower, seizedIn, repaidIn, data);
        liquidationActive = false;

        emit LiquidationExecuted(borrower, seized, repaid);
    }

    /// @notice Store a full liquidation for a later short `fire()` / `0xbeef` call.
    function loadShot(
        MarketParams calldata p,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        SwapParams calldata swap,
        uint256 minProfit
    ) external onlyOperator whenNotPaused {
        if (borrower == address(0)) revert ZeroAddress();
        if (p.loanToken == address(0) || p.collateralToken == address(0)) revert ZeroAddress();
        if ((seizedAssets == 0) == (repaidShares == 0)) revert InvalidAmount();
        if (swap.router == address(0) || !allowedRouters[swap.router]) revert RouterNotAllowed();
        if (swap.router == address(this) || swap.router == address(MORPHO)) revert RouterNotAllowed();
        if (swap.swapCalldata.length == 0) revert SwapFailed();

        _loaded = LoadedShot({
            loanToken: p.loanToken,
            collateralToken: p.collateralToken,
            oracle: p.oracle,
            irm: p.irm,
            lltv: p.lltv,
            borrower: borrower,
            seizedAssets: seizedAssets,
            repaidShares: repaidShares,
            router: swap.router,
            minProfit: minProfit,
            active: true
        });
        _loadedSwapCalldata = swap.swapCalldata;
        emit ShotLoaded(borrower, p.loanToken, p.collateralToken);
    }

    function clearShot() external onlyOperator {
        _loaded.active = false;
        delete _loadedSwapCalldata;
    }

    function shotLoaded() external view returns (bool) {
        return _loaded.active;
    }

    /// @notice Execute preloaded shot (4-byte selector). Prefer this over fat calldata in the race.
    function fire() external onlyOperator whenNotPaused nonReentrant {
        _fireLoaded();
    }

    /// @dev Whale-style 2-byte trigger: calldata == 0xbeef → fire loaded shot.
    fallback() external {
        if (msg.sender != operator && msg.sender != owner) revert OnlyOperator();
        if (paused) revert ContractPaused();
        if (msg.data.length != 2 || bytes2(msg.data) != 0xbeef) revert InvalidAmount();
        if (locked) revert Reentrancy();
        locked = true;
        _fireLoaded();
        locked = false;
    }

    function _fireLoaded() private {
        if (!_loaded.active) revert NoShotLoaded();
        LoadedShot memory s = _loaded;
        bytes memory swapCd = _loadedSwapCalldata;
        _loaded.active = false;
        delete _loadedSwapCalldata;

        MarketParams memory p = MarketParams({
            loanToken: s.loanToken,
            collateralToken: s.collateralToken,
            oracle: s.oracle,
            irm: s.irm,
            lltv: s.lltv
        });
        SwapParams memory swap = SwapParams({router: s.router, swapCalldata: swapCd});
        _liquidateOneMemory(p, s.borrower, s.seizedAssets, s.repaidShares, swap, s.minProfit);
        emit ShotFired(s.borrower);
    }

    function _liquidateOneMemory(
        MarketParams memory p,
        address borrower,
        uint256 seizedIn,
        uint256 repaidIn,
        SwapParams memory swap,
        uint256 minProfit
    ) private {
        if (borrower == address(0)) revert ZeroAddress();
        if ((seizedIn == 0) == (repaidIn == 0)) revert InvalidAmount();
        if (swap.router == address(0) || !allowedRouters[swap.router]) revert RouterNotAllowed();
        if (swap.router == address(this) || swap.router == address(MORPHO)) revert RouterNotAllowed();
        if (swap.swapCalldata.length == 0) revert SwapFailed();

        emit LiquidationStarted(borrower, p.loanToken, p.collateralToken, seizedIn, repaidIn);

        bytes memory data = abi.encode(
            CallbackData({
                loanToken: p.loanToken,
                collateralToken: p.collateralToken,
                router: swap.router,
                minProfit: minProfit,
                swapCalldata: swap.swapCalldata
            })
        );

        liquidationActive = true;
        (uint256 seized, uint256 repaid) = MORPHO.liquidate(p, borrower, seizedIn, repaidIn, data);
        liquidationActive = false;

        emit LiquidationExecuted(borrower, seized, repaid);
    }

    /// @inheritdoc IMorphoLiquidateCallback
    /// @dev NOT nonReentrant — Morpho calls this while the outer lock is held.
    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external override {
        if (msg.sender != address(MORPHO)) revert UnauthorizedMorpho();
        if (!liquidationActive) revert NoLiquidationInProgress();
        if (repaidAssets == 0) revert InvalidAmount();

        CallbackData memory decoded = abi.decode(data, (CallbackData));
        _swapAndRequireProfit(repaidAssets, decoded);
    }

    function _swapAndRequireProfit(uint256 repaidAssets, CallbackData memory decoded) private {
        if (!allowedRouters[decoded.router]) revert RouterNotAllowed();

        uint256 seized = IERC20(decoded.collateralToken).balanceOf(address(this));
        if (seized == 0) revert InvalidAmount();

        _ensureApprove(IERC20(decoded.collateralToken), decoded.router, seized);
        uint256 balBefore = IERC20(decoded.loanToken).balanceOf(address(this));
        (bool ok,) = decoded.router.call(decoded.swapCalldata);
        if (!ok) revert SwapFailed();
        emit CollateralSwapped(decoded.router, decoded.collateralToken, seized);

        uint256 required = _requiredProfit(decoded.loanToken, repaidAssets, decoded.minProfit);
        uint256 balAfter = IERC20(decoded.loanToken).balanceOf(address(this));
        if (balAfter < balBefore) revert InsufficientProfit();
        uint256 delta = balAfter - balBefore;
        if (delta < repaidAssets + required) revert InsufficientProfit();

        emit Profit(decoded.loanToken, delta - repaidAssets);
        _ensureApprove(IERC20(decoded.loanToken), address(MORPHO), repaidAssets);
    }

    function _requiredProfit(address loanToken, uint256 repaidAssets, uint256 minProfit)
        private
        view
        returns (uint256 required)
    {
        required = minProfit;
        uint256 absMin = minProfitAbs[loanToken];
        if (absMin > required) required = absMin;
        uint256 bpsMin = (repaidAssets * minProfitBps) / BPS_DENOMINATOR;
        if (bpsMin > required) required = bpsMin;
    }

    /// @dev Approve exactly `needed` if current allowance is short. Never `type(uint256).max`.
    function _ensureApprove(IERC20 token, address spender, uint256 needed) private {
        if (needed == 0) revert InvalidAmount();
        if (token.allowance(address(this), spender) >= needed) return;
        token.safeApprove(spender, needed);
    }
}
