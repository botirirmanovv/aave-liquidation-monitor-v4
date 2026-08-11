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

/// @notice Aave V3 Pool — адрес разный для каждой сети (Ethereum, Arbitrum, Base, Polygon и т.д.),
/// поэтому передаётся в конструктор, а не зашивается в код.
interface IAaveV3Pool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;

    function getUserAccountData(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 availableBorrowsBase,
            uint256 currentLiquidationThreshold,
            uint256 ltv,
            uint256 healthFactor
        );
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

/*//////////////////////////////////////////////////////////////
                          ОСНОВНОЙ КОНТРАКТ
//////////////////////////////////////////////////////////////*/

/**
 * @custom:dev-run-script scripts/deploy_liquidation_bot.ts
 */
contract AaveV3LiquidationBot {
    using SafeERC20 for IERC20;

    /*//////////////////////////////////////////////////////////
                              ОШИБКИ
    //////////////////////////////////////////////////////////*/

    error OnlyOwner();
    error OnlyOperator();
    error ZeroAddress();
    error InvalidAmount();
    error ContractPaused();
    error Reentrancy();
    error EmptyBalance();
    error TransferFailed();
    error UnauthorizedPool();
    error UnauthorizedInitiator();
    error RouterNotAllowed();
    error PositionHealthy();
    error InsufficientProfit();
    error ExceedsLimit();
    error NoFlashLoanInProgress();
    error InvalidSwapPath();

    /*//////////////////////////////////////////////////////////
                              РОЛИ / СТАТУС
    //////////////////////////////////////////////////////////*/

    address public owner;
    address public operator;
    bool public paused;

    IAaveV3Pool public immutable POOL;

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

    bool private locked;

    modifier nonReentrant() {
        if (locked) revert Reentrancy();
        locked = true;
        _;
        locked = false;
    }

    /// @dev Взводится только на время нашего собственного флеш-займа. Позволяет
    /// отличить легитимный синхронный колбэк Aave от постороннего вызова
    /// executeOperation, не конфликтуя с nonReentrant внешней функции.
    bool private flashLoanActive;

    /*//////////////////////////////////////////////////////////
                         НАСТРОЙКИ БЕЗОПАСНОСТИ
    //////////////////////////////////////////////////////////*/

    /// @notice Роутеры, разрешённые для свопа полученного залога обратно в долговой актив.
    mapping(address => bool) public allowedRouters;

    /// @notice Максимальная сумма займа (debtToCover) на актив. 0 = запрещено.
    mapping(address => uint256) public maxDebtCoverPerToken;

    /*//////////////////////////////////////////////////////////
                              СОБЫТИЯ
    //////////////////////////////////////////////////////////*/

    event LiquidationStarted(address indexed user, address indexed collateralAsset, address indexed debtAsset, uint256 debtToCover);
    event LiquidationExecuted(address indexed user, uint256 collateralReceived);
    event CollateralSwapped(address indexed router, uint256 amountIn, uint256 amountOut);
    event FlashLoanRepaid(uint256 amountOwed);
    event Profit(address indexed token, uint256 amount);
    event OwnershipTransferred(address indexed oldOwner, address indexed newOwner);
    event OperatorUpdated(address indexed oldOperator, address indexed newOperator);
    event RouterAllowed(address indexed router, bool allowed);
    event MaxDebtCoverUpdated(address indexed token, uint256 amount);
    event PausedSet(bool isPaused);

    /*//////////////////////////////////////////////////////////
                             КОНСТРУКТОР
    //////////////////////////////////////////////////////////*/

    constructor(address aavePool, address _operator) {
        if (aavePool == address(0)) revert ZeroAddress();
        POOL = IAaveV3Pool(aavePool);
        owner = msg.sender;
        operator = _operator;
        emit OwnershipTransferred(address(0), msg.sender);
        emit OperatorUpdated(address(0), _operator);
    }

    receive() external payable {}

    /*//////////////////////////////////////////////////////////
                          АДМИНИСТРИРОВАНИЕ
    //////////////////////////////////////////////////////////*/

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    function setOperator(address newOperator) external onlyOwner {
        emit OperatorUpdated(operator, newOperator);
        operator = newOperator;
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit PausedSet(_paused);
    }

    function setRouterAllowed(address router, bool allowed) external onlyOwner {
        if (router == address(0)) revert ZeroAddress();
        allowedRouters[router] = allowed;
        emit RouterAllowed(router, allowed);
    }

    function setMaxDebtCover(address token, uint256 amount) external onlyOwner {
        if (token == address(0)) revert ZeroAddress();
        maxDebtCoverPerToken[token] = amount;
        emit MaxDebtCoverUpdated(token, amount);
    }

    function withdrawToken(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        if (balance == 0) revert EmptyBalance();
        IERC20(token).safeTransfer(owner, balance);
    }

    function withdrawETH() external onlyOwner {
        (bool success, ) = payable(owner).call{value: address(this).balance}("");
        if (!success) revert TransferFailed();
    }

    /*//////////////////////////////////////////////////////////
                          ЛОГИКА ЛИКВИДАЦИИ
    //////////////////////////////////////////////////////////*/

    struct LiquidationParams {
        address user;             // адрес ликвидируемого пользователя на Aave
        address collateralAsset;  // какой залог забираем
        address swapRouter;       // роутер для обмена залога обратно в debtAsset (address(0), если collateralAsset == debtAsset)
        address[] swapPath;       // путь свопа collateralAsset -> ... -> debtAsset
        uint256 amountOutMin;     // защита от проскальзывания на свопе залога
        uint256 minProfit;        // минимальная прибыль в debtAsset — если рынок уже съеден
                                   // другим ботом/сэндвичем, тут просто revert, без потери средств
        uint256 deadline;
    }

    /// @param debtAsset    Актив долга, который занимаем и гасим за пользователя
    /// @param debtToCover  Сколько долга покрываем (не больше значения из maxDebtCoverPerToken)
    /// @param params       Остальные параметры ликвидации и свопа
    function initiateLiquidation(
        address debtAsset,
        uint256 debtToCover,
        LiquidationParams calldata params
    ) external onlyOperatorOrOwner whenNotPaused nonReentrant {
        if (debtAsset == address(0)) revert ZeroAddress();
        if (params.user == address(0)) revert ZeroAddress();
        if (params.collateralAsset == address(0)) revert ZeroAddress();
        if (debtToCover == 0) revert InvalidAmount();

        uint256 limit = maxDebtCoverPerToken[debtAsset];
        if (limit == 0 || debtToCover > limit) revert ExceedsLimit();

        if (params.collateralAsset != debtAsset) {
            if (params.swapRouter == address(0) || !allowedRouters[params.swapRouter]) {
                revert RouterNotAllowed();
            }
            // Путь свопа должен начинаться залогом и заканчиваться долговым активом,
            // иначе выручка окажется в постороннем токене.
            uint256 pathLength = params.swapPath.length;
            if (pathLength < 2) revert InvalidSwapPath();
            if (params.swapPath[0] != params.collateralAsset) revert InvalidSwapPath();
            if (params.swapPath[pathLength - 1] != debtAsset) revert InvalidSwapPath();
        }

        emit LiquidationStarted(params.user, params.collateralAsset, debtAsset, debtToCover);

        bytes memory data = abi.encode(params);
        flashLoanActive = true;
        POOL.flashLoanSimple(address(this), debtAsset, debtToCover, data, 0);
        flashLoanActive = false;
    }

    /// @dev Вызывается пулом Aave синхронно внутри initiateLiquidation.
    /// Тройная проверка авторизации: вызвать может только сам Pool, инициатором
    /// флеш-займа должен быть этот контракт, и займ должен быть начат нами.
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

        LiquidationParams memory params = abi.decode(data, (LiquidationParams));

        // Финальная проверка прямо перед списанием: позиция всё ещё нездорова?
        // Если кто-то уже успел её ликвидировать до нас (или пользователь довнёс
        // залог), позиция окажется здоровой — тут просто revert, деньги целы,
        // теряем только газ на неисполненную транзакцию.
        (, , , , , uint256 healthFactor) = POOL.getUserAccountData(params.user);
        if (healthFactor >= 1e18) revert PositionHealthy();

        // Погашаем долг пользователя, забираем залог с бонусом ликвидации.
        IERC20(asset).safeApprove(address(POOL), amount);
        POOL.liquidationCall(params.collateralAsset, asset, params.user, amount, false);

        uint256 collateralReceived = IERC20(params.collateralAsset).balanceOf(address(this));
        emit LiquidationExecuted(params.user, collateralReceived);

        uint256 amountOwed = amount + premium;

        // Если залог не совпадает с занимаемым активом — меняем его обратно.
        if (params.collateralAsset != asset) {
            IERC20(params.collateralAsset).safeApprove(params.swapRouter, collateralReceived);

            uint256[] memory amounts = IUniswapV2Router02(params.swapRouter).swapExactTokensForTokens(
                collateralReceived,
                params.amountOutMin,
                params.swapPath,
                address(this),
                params.deadline
            );

            emit CollateralSwapped(params.swapRouter, collateralReceived, amounts[amounts.length - 1]);
        }

        uint256 finalBalance = IERC20(asset).balanceOf(address(this));

        // Защита от сэндвич-атак и опережения: если после всех операций денег не хватает
        // даже на возврат займа + требуемый минимальный профит — вся транзакция откатится.
        if (finalBalance < amountOwed + params.minProfit) revert InsufficientProfit();

        uint256 profit = finalBalance - amountOwed;
        emit Profit(asset, profit);

        // Aave спишет amountOwed сам через transferFrom сразу после return true —
        // approve обязателен.
        IERC20(asset).safeApprove(address(POOL), amountOwed);
        emit FlashLoanRepaid(amountOwed);

        return true;
    }
}
