// Remix deploy script. Runs automatically after compilation via the
// @custom:dev-run-script NatSpec tag on AaveV3LiquidationBot.
//
// Requires an injected wallet (MetaMask) connected to the target network,
// because deployment sends a real transaction.

import { ethers } from 'ethers'

const CONTRACT_NAME = 'AaveV3LiquidationBot'

// Aave V3 Pool. Sepolia by default — replace for other networks.
const AAVE_POOL = '0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951'

// Address allowed to call initiateLiquidation. Leave as the deployer to start.
let OPERATOR = ''

// Router the bot may use to swap seized collateral back into the debt asset.
const ROUTER = '0xc532a74256d3db42d0bf7a0400fefdbad7694008'

;(async () => {
  try {
    const artifactPath = `browser/contracts/artifacts/${CONTRACT_NAME}.json`
    const artifact = JSON.parse(
      await remix.call('fileManager', 'getFile', artifactPath)
    )

    const provider = new ethers.BrowserProvider(web3Provider)
    const signer = await provider.getSigner()
    const deployer = await signer.getAddress()
    if (!OPERATOR) OPERATOR = deployer

    console.log('Deployer:', deployer)
    console.log('Network:', (await provider.getNetwork()).chainId.toString())

    const factory = new ethers.ContractFactory(
      artifact.abi,
      artifact.data.bytecode.object,
      signer
    )

    const bot = await factory.deploy(AAVE_POOL, OPERATOR)
    await bot.waitForDeployment()

    const address = await bot.getAddress()
    console.log('Deployed at:', address)
    console.log('Pool:', AAVE_POOL)
    console.log('Operator:', OPERATOR)

    // The bot reverts with ExceedsLimit until a per-token cap is configured,
    // and with RouterNotAllowed until the swap router is allowlisted.
    const allowTx = await (bot as any).setRouterAllowed(ROUTER, true)
    await allowTx.wait()
    console.log('Router allowlisted:', ROUTER)

    console.log(
      'NEXT STEP: call setMaxDebtCover(debtToken, maxAmount) for every debt ' +
      'asset you intend to liquidate, otherwise initiateLiquidation reverts.'
    )
  } catch (e: any) {
    console.error(e.message ?? e)
  }
})()
