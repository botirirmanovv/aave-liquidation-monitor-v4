// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MorphoFlashLiquidator} from "../contracts/MorphoFlashLiquidator.sol";

interface Vm {
    function envOr(string calldata key, address defaultValue) external view returns (address);
    function envOr(string calldata key, bool defaultValue) external view returns (bool);
    function envAddress(string calldata key) external view returns (address);
    function startBroadcast() external;
    function stopBroadcast() external;
}

/// @notice Foundry deploy for MorphoFlashLiquidator (+ hard-batch).
///
/// Base mainnet:
///   forge script scripts/DeployMorphoFlashLiquidator.s.sol:DeployMorphoFlashLiquidator \
///     --rpc-url $BASE_HTTP_RPC_URL --broadcast --chain 8453
///
/// Set MORPHO_DEPLOY_UNPAUSE=true to unpause in the same broadcast (combat).
contract DeployMorphoFlashLiquidator {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address internal constant MORPHO_BLUE = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    // Uni V3 SwapRouter02 (repo / alt) + Aerodrome
    address internal constant UNI_V3_REPO = 0x2626664c2603336E57b271c5c0d842f2875A7dA0;
    address internal constant UNI_V3 = 0x2626664c2603336E57B271c5C0b26F421741e481;
    address internal constant AERO = 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43;
    address internal constant WETH = 0x4200000000000000000000000000000000000006;
    address internal constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address internal constant CBXRP = 0xcb585250f852C6c6bf90434AB21A00f02833a4af;
    address internal constant YOUSD = 0x0000000f2eB9f69274678c76222B35eEc7588a65;

    uint256 internal constant BASE_MAINNET = 8453;
    uint256 internal constant USDC_APPROVAL_CAP = 25_000 * 1e6;
    uint256 internal constant CBXRP_APPROVAL_CAP = 30_000 * 1e6;
    uint256 internal constant YOUSD_APPROVAL_CAP = 30_000 * 1e18;
    uint256 internal constant WETH_APPROVAL_CAP = 20 * 1e18;

    event Deployed(address bot, address operator, uint256 chainId, bool paused);

    function run() external {
        address operator = vm.envOr("MORPHO_OPERATOR_ADDRESS", address(0));
        address morpho = vm.envOr("MORPHO_BLUE_ADDRESS", MORPHO_BLUE);
        bool doUnpause = vm.envOr("MORPHO_DEPLOY_UNPAUSE", false);

        vm.startBroadcast();
        MorphoFlashLiquidator bot = new MorphoFlashLiquidator(morpho, operator);
        bot.pause();

        bot.setRouterAllowed(UNI_V3, true);
        bot.setRouterAllowed(AERO, true);
        bot.setRouterAllowed(UNI_V3_REPO, true);
        bot.setIntermediateTokenAllowed(WETH, true);

        if (block.chainid == BASE_MAINNET && USDC.code.length > 0) {
            address[] memory loanSpenders = new address[](1);
            loanSpenders[0] = morpho;
            bot.setApprovals(USDC, loanSpenders, USDC_APPROVAL_CAP);

            address[] memory collSpenders = new address[](3);
            collSpenders[0] = UNI_V3;
            collSpenders[1] = UNI_V3_REPO;
            collSpenders[2] = AERO;
            if (CBXRP.code.length > 0) {
                bot.setApprovals(CBXRP, collSpenders, CBXRP_APPROVAL_CAP);
            }
            if (YOUSD.code.length > 0) {
                bot.setApprovals(YOUSD, collSpenders, YOUSD_APPROVAL_CAP);
            }
            if (WETH.code.length > 0) {
                bot.setApprovals(WETH, collSpenders, WETH_APPROVAL_CAP);
            }
        }

        if (doUnpause) {
            bot.unpause();
        }

        emit Deployed(address(bot), bot.operator(), block.chainid, bot.paused());
        vm.stopBroadcast();
    }
}
