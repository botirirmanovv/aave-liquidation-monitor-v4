// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {
    MorphoFlashLiquidator,
    MarketParams
} from "../contracts/MorphoFlashLiquidator.sol";
import {MockMorphoBlue, MockERC20Lite, MockSwapRouter} from "./mocks/MockMorphoBlue.sol";

interface Vm {
    function envOr(string calldata key, string calldata defaultValue) external view returns (string memory);
    function createSelectFork(string calldata urlOrAlias) external returns (uint256);
    function expectRevert(bytes4 revertData) external;
    function expectRevert() external;
    function prank(address msgSender) external;
    function startPrank(address msgSender) external;
    function stopPrank() external;
    function deal(address account, uint256 newBalance) external;
    function label(address account, string calldata newLabel) external;
}

interface IMorphoMarketView {
    function idToMarketParams(bytes32 id) external view returns (MarketParams memory);
}

/// @dev Base-fork tests for MorphoFlashLiquidator.
/// Liquidation mechanics use a mock Morpho (transfer collateral → onMorphoLiquidate
/// → pull repay). No live USDC/cbXRP HF<1 was available; market params still match
/// morpho_research/morpho_markets.py.
contract MorphoFlashLiquidatorTest {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address internal constant MORPHO_BLUE = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address internal constant UNI_V3_SWAP_ROUTER_02 = 0x2626664c2603336E57B271c5C0b26F421741e481;
    address internal constant UNI_V3_SWAP_ROUTER_02_REPO = 0x2626664c2603336E57b271c5c0d842f2875A7dA0;
    address internal constant AERODROME_ROUTER = 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43;

    // USDC/cbXRP Morpho Blue market (Base) — morpho_markets.py
    address internal constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address internal constant CBXRP = 0xcb585250f852C6c6bf90434AB21A00f02833a4af;
    address internal constant USDC_CBXRP_ORACLE = 0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE;
    address internal constant BASE_IRM = 0x46415998764C29aB2a25CbeA6254146D50D22687;
    uint256 internal constant USDC_CBXRP_LLTV = 625_000_000_000_000_000;
    bytes32 internal constant USDC_CBXRP_MARKET_ID =
        0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109;

    address internal owner;
    address internal operator;
    address internal stranger;

    MockMorphoBlue internal morpho;
    MockERC20Lite internal loan;
    MockERC20Lite internal collateral;
    MockSwapRouter internal router;
    MorphoFlashLiquidator internal bot;

    MarketParams internal params;
    address internal borrower;

    uint256 internal constant SEIZED = 10_500e18;
    uint256 internal constant REPAID = 10_000e18;
    uint256 internal constant MIN_PROFIT = 100e18;

    event ForkBlock(uint256 chainId, uint256 blockNumber);

    function setUp() public {
        if (block.chainid != 8453) {
            vm.createSelectFork(_rpc());
        }
        emit ForkBlock(block.chainid, block.number);

        owner = address(this);
        operator = address(0xA11CE);
        stranger = address(0xB0B);
        borrower = address(0xB0B0);

        morpho = new MockMorphoBlue();
        loan = new MockERC20Lite("USDC");
        collateral = new MockERC20Lite("cbXRP");
        router = new MockSwapRouter();
        bot = new MorphoFlashLiquidator(address(morpho), operator);

        params = MarketParams({
            loanToken: address(loan),
            collateralToken: address(collateral),
            oracle: USDC_CBXRP_ORACLE,
            irm: BASE_IRM,
            lltv: USDC_CBXRP_LLTV
        });

        bot.setRouterAllowed(address(router), true);
        bot.setIntermediateTokenAllowed(address(0x4200000000000000000000000000000000000006), true);
        // Production ctor is paused (zero unpause window). Tests unpause the mock instance only.
        bot.unpause();

        // Mock Morpho holds seized collateral; mock router holds loan tokens for the swap.
        _deal(collateral, address(morpho), SEIZED * 10);
        _deal(loan, address(router), REPAID * 10);
        vm.deal(owner, 1 ether);

        vm.label(address(bot), "MorphoFlashLiquidator");
        vm.label(address(morpho), "MockMorpho");
        vm.label(address(router), "MockRouter");
        vm.label(borrower, "TestBorrower");
    }

    function test_forkMorphoBlueHasCode() public view {
        require(MORPHO_BLUE.code.length > 0, "Morpho Blue missing on Base fork");
        bool uni = UNI_V3_SWAP_ROUTER_02.code.length > 0 || UNI_V3_SWAP_ROUTER_02_REPO.code.length > 0;
        require(uni, "UniV3 SwapRouter02 missing");
        require(AERODROME_ROUTER.code.length > 0, "Aerodrome Router missing");
        require(USDC.code.length > 0, "USDC missing");
        require(CBXRP.code.length > 0, "cbXRP missing");
    }

    function test_realUsdcCbXrpMarketParamsOnFork() public view {
        MarketParams memory onchain = IMorphoMarketView(MORPHO_BLUE).idToMarketParams(USDC_CBXRP_MARKET_ID);
        require(onchain.loanToken == USDC, "loanToken");
        require(onchain.collateralToken == CBXRP, "collateralToken");
        require(onchain.oracle == USDC_CBXRP_ORACLE, "oracle");
        require(onchain.irm == BASE_IRM, "irm");
        require(onchain.lltv == USDC_CBXRP_LLTV, "lltv");
    }

    function test_constructorSetsMorphoAndOperator() public view {
        require(address(bot.MORPHO()) == address(morpho), "morpho");
        require(bot.operator() == operator, "operator");
        require(bot.owner() == owner, "owner");
        require(bot.minProfitBps() == 50, "default bps");
    }

    function test_constructorStartsPaused() public {
        MorphoFlashLiquidator fresh = new MorphoFlashLiquidator(address(morpho), operator);
        require(fresh.paused(), "ctor must start paused");
        vm.prank(operator);
        vm.expectRevert(MorphoFlashLiquidator.ContractPaused.selector);
        fresh.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
    }

    /// 1) Successful liquidation with profit above minProfit
    function test_1_successLiquidationAboveMinProfit() public {
        router.setRateBps(10_000);
        uint256 botLoanBefore = loan.balanceOf(address(bot));

        vm.prank(operator);
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);

        require(loan.balanceOf(address(morpho)) == REPAID, "morpho not repaid");
        uint256 profit = loan.balanceOf(address(bot)) - botLoanBefore;
        require(profit >= MIN_PROFIT, "profit below minProfit");
        require(profit == SEIZED - REPAID, "unexpected leftover");
    }

    /// 2) Revert on insufficient profit (simulate bad swap rate)
    function test_2_revertInsufficientProfitBadSwapRate() public {
        router.setRateBps(9_500);
        vm.prank(operator);
        vm.expectRevert(MorphoFlashLiquidator.InsufficientProfit.selector);
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
    }

    /// Leftover loanToken from a prior liquidation must not mask an unprofitable swap.
    function test_leftoverLoanTokenDoesNotRescueUnprofitableSwap() public {
        uint256 leftover = 1_000e18;
        _deal(loan, address(bot), leftover);
        require(loan.balanceOf(address(bot)) == leftover, "pre-existing leftover");

        router.setRateBps(9_500);
        vm.prank(operator);
        vm.expectRevert(MorphoFlashLiquidator.InsufficientProfit.selector);
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
    }

    /// 3) Revert if callback msg.sender is not Morpho
    function test_3_revertCallbackSenderNotMorpho() public {
        vm.expectRevert(MorphoFlashLiquidator.UnauthorizedMorpho.selector);
        bot.onMorphoLiquidate(REPAID, bytes(""));
    }

    /// 4) Revert on disallowed router
    function test_4_revertDisallowedRouter() public {
        MockSwapRouter rogue = new MockSwapRouter();
        MorphoFlashLiquidator.SwapParams memory swap = MorphoFlashLiquidator.SwapParams({
            router: address(rogue),
            swapCalldata: abi.encodeWithSelector(
                MockSwapRouter.swapAll.selector, address(collateral), address(loan), uint256(0)
            )
        });
        vm.prank(operator);
        vm.expectRevert(MorphoFlashLiquidator.RouterNotAllowed.selector);
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, swap, MIN_PROFIT);
    }

    /// 5) Pause works (`pause()`, not setPaused — match deployed ABI)
    function test_5_pauseWorks() public {
        bot.pause();
        require(bot.paused(), "paused flag");
        vm.prank(operator);
        vm.expectRevert(MorphoFlashLiquidator.ContractPaused.selector);
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
        bot.unpause();
        require(!bot.paused(), "unpaused flag");
    }

    function test_insufficientProfitAbsFloor() public {
        bot.setMinProfitBps(0);
        bot.setMinProfitAbs(address(loan), 1_000e18);
        router.setRateBps(10_000);
        vm.prank(operator);
        vm.expectRevert(MorphoFlashLiquidator.InsufficientProfit.selector);
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), 0);
    }

    function test_callbackFromMorphoWithoutOuter() public {
        vm.prank(address(morpho));
        vm.expectRevert(MorphoFlashLiquidator.NoLiquidationInProgress.selector);
        bot.onMorphoLiquidate(REPAID, bytes(""));
    }

    function test_onlyOperator() public {
        vm.prank(stranger);
        vm.expectRevert(MorphoFlashLiquidator.OnlyOperator.selector);
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
    }

    function test_setApprovalsAndRescue() public {
        address[] memory spenders = new address[](2);
        spenders[0] = address(morpho);
        spenders[1] = address(router);
        uint256 cap = 1_000_000e18;
        bot.setApprovals(address(loan), spenders, cap);
        require(loan.allowance(address(bot), address(morpho)) == cap, "morpho approve cap");
        vm.expectRevert(MorphoFlashLiquidator.InvalidAmount.selector);
        bot.setApprovals(address(loan), spenders, type(uint256).max);

        _deal(loan, address(bot), 123);
        bot.rescueTokens(address(loan), operator, 123);
        require(loan.balanceOf(operator) == 123, "rescue");
    }

    function test_swapFailed() public {
        router.setFail(true);
        vm.prank(operator);
        vm.expectRevert(MorphoFlashLiquidator.SwapFailed.selector);
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
    }

    function test_ownerCanLiquidate() public {
        bot.liquidateWithFlash(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
        require(loan.balanceOf(address(morpho)) == REPAID, "repaid");
    }

    function test_loadShot_fire() public {
        vm.prank(operator);
        bot.loadShot(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
        require(bot.shotLoaded(), "loaded");
        vm.prank(operator);
        bot.fire();
        require(!bot.shotLoaded(), "cleared");
        require(loan.balanceOf(address(morpho)) == REPAID, "repaid via fire");
    }

    function test_loadShot_beef_fallback() public {
        vm.prank(operator);
        bot.loadShot(params, borrower, SEIZED, 0, _swap(), MIN_PROFIT);
        vm.prank(operator);
        (bool ok,) = address(bot).call(hex"beef");
        require(ok, "beef call");
        require(loan.balanceOf(address(morpho)) == REPAID, "repaid via beef");
    }

    function test_fire_without_shot_reverts() public {
        vm.prank(operator);
        vm.expectRevert(MorphoFlashLiquidator.NoShotLoaded.selector);
        bot.fire();
    }

    function _swap() internal view returns (MorphoFlashLiquidator.SwapParams memory swap) {
        swap = MorphoFlashLiquidator.SwapParams({
            router: address(router),
            swapCalldata: abi.encodeWithSelector(
                MockSwapRouter.swapAll.selector, address(collateral), address(loan), uint256(0)
            )
        });
    }

    /// @dev Token analog of `vm.deal` (ETH-only cheatcode) for MockERC20Lite.
    function _deal(MockERC20Lite token, address to, uint256 amount) internal {
        uint256 cur = token.balanceOf(to);
        if (amount > cur) {
            token.mint(to, amount - cur);
        }
    }

    function _rpc() internal view returns (string memory) {
        string memory rpc = vm.envOr("BASE_HTTP_RPC_URL", string(""));
        if (bytes(rpc).length > 0) return rpc;
        rpc = vm.envOr("BASE_RPC_URL", string(""));
        if (bytes(rpc).length > 0) return rpc;
        rpc = vm.envOr("HTTP_RPC_URL", string(""));
        if (bytes(rpc).length > 0) return rpc;
        return "https://mainnet.base.org";
    }
}
