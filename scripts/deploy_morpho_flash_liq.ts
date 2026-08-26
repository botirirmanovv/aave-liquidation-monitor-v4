/**
 * Deploy MorphoFlashLiquidator (Remix / Hardhat / ethers).
 * Constructor starts paused. Call pause() immediately. NEVER unpause() / NEVER live liquidate.
 *
 * Foundry (preferred, from repo root):
 *   powershell -ExecutionPolicy Bypass -File scripts/deploy_morpho_one_command.ps1 -Network sepolia
 *
 * Base Morpho Blue (CREATE2, Base + Base Sepolia): 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb
 * Uni V3 SwapRouter02 (user-listed): 0x2626664c2603336E57B271c5C0b26F421741e481
 * Uni V3 SwapRouter02 (executor default): 0x2626664c2603336E57b271c5c0d842f2875A7dA0
 * Aerodrome Router: 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
 *
 * Approvals: finite caps, not uint256.max.
 *   USDC (6 dec): 25_000e6
 *   cbXRP (6 dec on Base, not 18): 30_000e6
 */

const MORPHO = '0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb'
const OPERATOR = '0x0000000000000000000000000000000000000000' // placeholder; owner if zero
const UNI_V3 = '0x2626664c2603336E57B271c5C0b26F421741e481'
const UNI_V3_REPO = '0x2626664c2603336E57b271c5c0d842f2875A7dA0'
const AERO = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43'
const WETH = '0x4200000000000000000000000000000000000006'
const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
const CBXRP = '0xcb585250f852C6c6bf90434AB21A00f02833a4af'
const USDC_CAP = 25_000n * 1_000_000n
const CBXRP_CAP = 30_000n * 1_000_000n

async function main() {
  const bot = await ethers.deployContract('MorphoFlashLiquidator', [MORPHO, OPERATOR])
  await bot.waitForDeployment()
  const addr = await bot.getAddress()
  console.log('MorphoFlashLiquidator:', addr)

  await (await bot.pause()).wait()
  await (await bot.setRouterAllowed(UNI_V3, true)).wait()
  await (await bot.setRouterAllowed(AERO, true)).wait()
  await (await bot.setRouterAllowed(UNI_V3_REPO, true)).wait()
  await (await bot.setIntermediateTokenAllowed(WETH, true)).wait()

  const chainId = Number((await ethers.provider.getNetwork()).chainId)
  if (chainId === 8453) {
    await (await bot.setApprovals(USDC, [MORPHO], USDC_CAP)).wait()
    await (await bot.setApprovals(CBXRP, [UNI_V3, UNI_V3_REPO, AERO], CBXRP_CAP)).wait()
    console.log('mainnet: routers + finite USDC/cbXRP approvals. still paused.')
  } else {
    console.log('non-mainnet: skipped token approvals (tokens may not exist). still paused.')
  }
  console.log('NEVER unpause in this script. Add address to MORPHO_LIQ_CONTRACT(_SEPOLIA).')
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
