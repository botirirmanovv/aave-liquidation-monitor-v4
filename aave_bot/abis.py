"""Contract ABIs. Kept minimal: only the members the bot actually calls."""
from __future__ import annotations

import json

POOL_ABI = json.loads("""
[
  {"anonymous": false, "inputs": [
    {"indexed": true, "internalType": "address", "name": "reserve", "type": "address"},
    {"indexed": false, "internalType": "address", "name": "user", "type": "address"},
    {"indexed": true, "internalType": "address", "name": "onBehalfOf", "type": "address"},
    {"indexed": false, "internalType": "uint256", "name": "amount", "type": "uint256"},
    {"indexed": true, "internalType": "uint16", "name": "referralCode", "type": "uint16"}
  ], "name": "Supply", "type": "event"},
  {"anonymous": false, "inputs": [
    {"indexed": true, "internalType": "address", "name": "reserve", "type": "address"},
    {"indexed": false, "internalType": "address", "name": "user", "type": "address"},
    {"indexed": true, "internalType": "address", "name": "onBehalfOf", "type": "address"},
    {"indexed": false, "internalType": "uint256", "name": "amount", "type": "uint256"},
    {"indexed": false, "internalType": "uint8", "name": "interestRateMode", "type": "uint8"},
    {"indexed": false, "internalType": "uint256", "name": "borrowRate", "type": "uint256"},
    {"indexed": true, "internalType": "uint16", "name": "referralCode", "type": "uint16"}
  ], "name": "Borrow", "type": "event"},
  {"anonymous": false, "inputs": [
    {"indexed": true, "internalType": "address", "name": "reserve", "type": "address"},
    {"indexed": true, "internalType": "address", "name": "user", "type": "address"},
    {"indexed": true, "internalType": "address", "name": "repayer", "type": "address"},
    {"indexed": false, "internalType": "uint256", "name": "amount", "type": "uint256"},
    {"indexed": false, "internalType": "bool", "name": "useATokens", "type": "bool"}
  ], "name": "Repay", "type": "event"},
  {"anonymous": false, "inputs": [
    {"indexed": true, "internalType": "address", "name": "reserve", "type": "address"},
    {"indexed": true, "internalType": "address", "name": "user", "type": "address"},
    {"indexed": true, "internalType": "address", "name": "to", "type": "address"},
    {"indexed": false, "internalType": "uint256", "name": "amount", "type": "uint256"}
  ], "name": "Withdraw", "type": "event"},
  {"anonymous": false, "inputs": [
    {"indexed": true, "internalType": "address", "name": "collateralAsset", "type": "address"},
    {"indexed": true, "internalType": "address", "name": "debtAsset", "type": "address"},
    {"indexed": true, "internalType": "address", "name": "user", "type": "address"},
    {"indexed": false, "internalType": "uint256", "name": "debtToCover", "type": "uint256"},
    {"indexed": false, "internalType": "uint256", "name": "liquidatedCollateralAmount", "type": "uint256"},
    {"indexed": false, "internalType": "address", "name": "liquidator", "type": "address"},
    {"indexed": false, "internalType": "bool", "name": "receiveAToken", "type": "bool"}
  ], "name": "LiquidationCall", "type": "event"},
  {"inputs": [{"internalType": "address", "name": "user", "type": "address"}],
   "name": "getUserAccountData",
   "outputs": [
     {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
     {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
     {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
     {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
     {"internalType": "uint256", "name": "ltv", "type": "uint256"},
     {"internalType": "uint256", "name": "healthFactor", "type": "uint256"}
   ], "stateMutability": "view", "type": "function"}
]
""")

DATA_PROVIDER_ABI = json.loads("""
[
  {"inputs": [], "name": "getAllReservesTokens",
   "outputs": [{"components": [
     {"internalType": "string", "name": "symbol", "type": "string"},
     {"internalType": "address", "name": "tokenAddress", "type": "address"}
   ], "internalType": "struct TokenData[]", "name": "", "type": "tuple[]"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [
     {"internalType": "address", "name": "asset", "type": "address"},
     {"internalType": "address", "name": "user", "type": "address"}
   ],
   "name": "getUserReserveData",
   "outputs": [
     {"internalType": "uint256", "name": "currentATokenBalance", "type": "uint256"},
     {"internalType": "uint256", "name": "currentStableDebt", "type": "uint256"},
     {"internalType": "uint256", "name": "currentVariableDebt", "type": "uint256"},
     {"internalType": "uint256", "name": "principalStableDebt", "type": "uint256"},
     {"internalType": "uint256", "name": "scaledVariableDebt", "type": "uint256"},
     {"internalType": "uint256", "name": "stableBorrowRate", "type": "uint256"},
     {"internalType": "uint256", "name": "liquidityRate", "type": "uint256"},
     {"internalType": "uint40", "name": "stableRateLastUpdated", "type": "uint40"},
     {"internalType": "bool", "name": "usageAsCollateralEnabled", "type": "bool"}
   ], "stateMutability": "view", "type": "function"},
  {"inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
   "name": "getReserveConfigurationData",
   "outputs": [
     {"internalType": "uint256", "name": "decimals", "type": "uint256"},
     {"internalType": "uint256", "name": "ltv", "type": "uint256"},
     {"internalType": "uint256", "name": "liquidationThreshold", "type": "uint256"},
     {"internalType": "uint256", "name": "liquidationBonus", "type": "uint256"},
     {"internalType": "uint256", "name": "reserveFactor", "type": "uint256"},
     {"internalType": "bool", "name": "usageAsCollateralEnabled", "type": "bool"},
     {"internalType": "bool", "name": "borrowingEnabled", "type": "bool"},
     {"internalType": "bool", "name": "stableBorrowRateEnabled", "type": "bool"},
     {"internalType": "bool", "name": "isActive", "type": "bool"},
     {"internalType": "bool", "name": "isFrozen", "type": "bool"}
   ], "stateMutability": "view", "type": "function"}
]
""")

ORACLE_ABI = json.loads("""
[
  {"inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
   "name": "getAssetPrice",
   "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
   "name": "getSourceOfAsset",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "BASE_CURRENCY_UNIT",
   "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
   "stateMutability": "view", "type": "function"}
]
""")

MULTICALL3_ABI = json.loads("""
[
  {"inputs": [
     {"internalType": "bool", "name": "requireSuccess", "type": "bool"},
     {"components": [
       {"internalType": "address", "name": "target", "type": "address"},
       {"internalType": "bytes", "name": "callData", "type": "bytes"}
     ], "internalType": "struct Multicall3.Call[]", "name": "calls", "type": "tuple[]"}
   ],
   "name": "tryAggregate",
   "outputs": [{"components": [
     {"internalType": "bool", "name": "success", "type": "bool"},
     {"internalType": "bytes", "name": "returnData", "type": "bytes"}
   ], "internalType": "struct Multicall3.Result[]", "name": "returnData", "type": "tuple[]"}],
   "stateMutability": "payable", "type": "function"}
]
""")

# Matches contracts/AaveV3LiquidationBot.sol: initiateLiquidation(debtAsset,
# debtToCover, LiquidationParams). The legacy flat `liquidate(...)` signature
# from the original monitor does not exist on that contract.
LIQUIDATION_BOT_ABI = json.loads("""
[
  {"inputs": [
     {"internalType": "address", "name": "debtAsset", "type": "address"},
     {"internalType": "uint256", "name": "debtToCover", "type": "uint256"},
     {"components": [
       {"internalType": "address", "name": "user", "type": "address"},
       {"internalType": "address", "name": "collateralAsset", "type": "address"},
       {"internalType": "address", "name": "swapRouter", "type": "address"},
       {"internalType": "address[]", "name": "swapPath", "type": "address[]"},
       {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
       {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
       {"internalType": "uint256", "name": "deadline", "type": "uint256"}
     ], "internalType": "struct LiquidationParams", "name": "params", "type": "tuple"}
   ], "name": "initiateLiquidation", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
  {"inputs": [], "name": "owner",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "operator",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "paused",
   "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [{"internalType": "address", "name": "", "type": "address"}],
   "name": "allowedRouters",
   "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [{"internalType": "address", "name": "", "type": "address"}],
   "name": "maxDebtCoverPerToken",
   "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
   "stateMutability": "view", "type": "function"}
]
""")

# Custom errors declared by AaveV3LiquidationBot, used to turn a bare 4-byte
# revert selector from a simulation into something readable in the logs.
LIQUIDATION_BOT_ERROR_SIGNATURES = [
    "OnlyOwner()", "OnlyOperator()", "ZeroAddress()", "InvalidAmount()",
    "ContractPaused()", "Reentrancy()", "EmptyBalance()", "TransferFailed()",
    "UnauthorizedPool()", "UnauthorizedInitiator()", "RouterNotAllowed()",
    "PositionHealthy()", "InsufficientProfit()", "ExceedsLimit()",
    "NoFlashLoanInProgress()", "InvalidSwapPath()",
    "ERC20CallFailed()", "ERC20OperationFailed()",
]

FLASH_ARB_BOT_ABI = json.loads("""
[
  {"inputs": [
     {"internalType": "address", "name": "borrowAsset", "type": "address"},
     {"internalType": "uint256", "name": "amount", "type": "uint256"},
     {"components": [
       {"internalType": "address", "name": "routerBuy", "type": "address"},
       {"internalType": "address", "name": "routerSell", "type": "address"},
       {"internalType": "address[]", "name": "pathBuy", "type": "address[]"},
       {"internalType": "address[]", "name": "pathSell", "type": "address[]"},
       {"internalType": "uint256", "name": "amountOutMinBuy", "type": "uint256"},
       {"internalType": "uint256", "name": "amountOutMinSell", "type": "uint256"},
       {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
       {"internalType": "uint256", "name": "deadline", "type": "uint256"}
     ], "internalType": "struct ArbParams", "name": "params", "type": "tuple"}
   ], "name": "initiateArb", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
  {"inputs": [], "name": "owner",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "operator",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [{"internalType": "address", "name": "", "type": "address"}],
   "name": "allowedRouters",
   "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [{"internalType": "address", "name": "", "type": "address"}],
   "name": "maxBorrowPerToken",
   "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
   "stateMutability": "view", "type": "function"}
]
""")

ERC20_ABI = json.loads("""
[
  {"inputs": [], "name": "decimals",
   "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "symbol",
   "outputs": [{"internalType": "string", "name": "", "type": "string"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [{"internalType": "address", "name": "account", "type": "address"}],
   "name": "balanceOf",
   "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
   "stateMutability": "view", "type": "function"}
]
""")

BALANCER_V3_ARB_BOT_ABI = json.loads("""
[
  {"inputs": [
     {"internalType": "address", "name": "borrowAsset", "type": "address"},
     {"internalType": "uint256", "name": "amount", "type": "uint256"},
     {"components": [
       {"internalType": "address", "name": "midAsset", "type": "address"},
       {"internalType": "uint24", "name": "feeBuy", "type": "uint24"},
       {"internalType": "uint24", "name": "feeSell", "type": "uint24"},
       {"internalType": "uint256", "name": "amountOutMinBuy", "type": "uint256"},
       {"internalType": "uint256", "name": "amountOutMinSell", "type": "uint256"},
       {"internalType": "uint256", "name": "minProfit", "type": "uint256"}
     ], "internalType": "struct ArbParams", "name": "params", "type": "tuple"}
   ], "name": "initiateArb", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
  {"inputs": [], "name": "owner",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "operator",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "v3Router",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [{"internalType": "address", "name": "", "type": "address"}],
   "name": "maxBorrowPerToken",
   "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
   "stateMutability": "view", "type": "function"}
]
""")

# Uniswap V3 QuoterV2 — same CREATE2 address on Ethereum / Base / Arbitrum / Optimism.
UNISWAP_V3_QUOTER_V2_ABI = json.loads("""
[
  {"inputs": [
     {"components": [
       {"internalType": "address", "name": "tokenIn", "type": "address"},
       {"internalType": "address", "name": "tokenOut", "type": "address"},
       {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
       {"internalType": "uint24", "name": "fee", "type": "uint24"},
       {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}
     ], "internalType": "struct IQuoterV2.QuoteExactInputSingleParams", "name": "params", "type": "tuple"}
   ], "name": "quoteExactInputSingle",
   "outputs": [
     {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
     {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
     {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
     {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"}
   ], "stateMutability": "nonpayable", "type": "function"}
]
""")

ROUTER_QUOTE_ABI = json.loads("""
[
  {"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"},
              {"internalType": "address[]", "name": "path", "type": "address[]"}],
   "name": "getAmountsOut",
   "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "factory",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "WETH",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"}
]
""")

FACTORY_ABI = json.loads("""
[
  {"inputs": [{"internalType": "address", "name": "tokenA", "type": "address"},
              {"internalType": "address", "name": "tokenB", "type": "address"}],
   "name": "getPair",
   "outputs": [{"internalType": "address", "name": "pair", "type": "address"}],
   "stateMutability": "view", "type": "function"}
]
""")

# Chainlink proxy: aggregator() reveals the contract that actually emits
# AnswerUpdated, which is what we must subscribe to.
CHAINLINK_PROXY_ABI = json.loads("""
[
  {"inputs": [], "name": "aggregator",
   "outputs": [{"internalType": "address", "name": "", "type": "address"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "description",
   "outputs": [{"internalType": "string", "name": "", "type": "string"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [], "name": "decimals",
   "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
   "stateMutability": "view", "type": "function"}
]
""")
