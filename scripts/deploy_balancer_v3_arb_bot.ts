/**
 * Deploy BalancerV3FlashArbBot.
 *
 * Vault (same on Base/Arb/OP/Ethereum): 0xBA12222222228d8Ba445958a75a0704d566BF2C8
 * SwapRouter02 Base: 0x2626664c2603336E57B271c5C0d842F2875A7dA0
 * SwapRouter02 Arb/OP: 0x68b3465833fb72A710864c33b2b4F9C8c7C6c7E5
 *
 * After deploy: setOperator(bot EOA), setMaxBorrow per token, set
 * BALANCER_ARB_BOT_ADDRESS in .env. Keep AUTO_EXECUTE=false until dry-run finds hits.
 */
export {};
