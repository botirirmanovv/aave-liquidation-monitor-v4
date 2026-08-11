// Deploy AaveV3FlashArbBot on Remix / Hardhat.
// After deploy: setRouterAllowed for each venue, setMaxBorrow for each asset.

const POOL = '0x...'           // Aave V3 Pool on this chain
const OPERATOR = '0x...'       // EOA / bot key that will call initiateArb
const ROUTER_A = '0x...'       // UniswapV2-compatible
const ROUTER_B = '0x...'       // second venue
const BORROW_ASSET = '0x...'   // e.g. USDC
const MAX_BORROW = 1_000_000n * 10n ** 6n

const bot = await ethers.deployContract('AaveV3FlashArbBot', [POOL, OPERATOR])
await bot.waitForDeployment()
console.log('FlashArbBot:', await bot.getAddress())

await (await bot.setRouterAllowed(ROUTER_A, true)).wait()
await (await bot.setRouterAllowed(ROUTER_B, true)).wait()
await (await bot.setMaxBorrow(BORROW_ASSET, MAX_BORROW)).wait()
console.log('routers allowlisted, maxBorrow set')
