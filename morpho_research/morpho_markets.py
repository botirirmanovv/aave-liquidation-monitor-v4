"""Priority Morpho Blue markets to monitor (easy to extend).

priority: lower number = higher priority (rescanned / evaluated first).
Optimism intentionally omitted (near-dead liq flow in viability scout).
"""
from __future__ import annotations

from dataclasses import dataclass


MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
DEFAULT_MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Morpho Blue math constants
WAD = 10**18
ORACLE_PRICE_SCALE = 10**36
LIQUIDATION_CURSOR = 3 * 10**17  # 0.3e18
MAX_LIQUIDATION_INCENTIVE_FACTOR = 115 * 10**16  # 1.15e18


@dataclass(frozen=True, slots=True)
class MorphoMarketConfig:
    chain: str
    market_id: str
    loan_symbol: str
    collateral_symbol: str
    loan_token: str
    collateral_token: str
    oracle: str
    irm: str
    lltv_wad: int
    priority: int = 100
    enabled: bool = True
    note: str = ""
    # 0 = MorphoExecutor MORPHO_V3_FEE default (usually 3000)
    v3_fee: int = 0


# ── Base: viability showed USDC/cbXRP + USDC/USDe as the actionable core ─────
MORPHO_MARKETS: list[MorphoMarketConfig] = [
    # Priority 1 — liquidation heat + near-HF cluster
    MorphoMarketConfig(
        chain="base",
        market_id="0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109",
        loan_symbol="USDC",
        collateral_symbol="cbXRP",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0xcb585250f852C6c6bf90434AB21A00f02833a4af",
        oracle="0x031b2EFC8d70042Ac8d9f5c793c4149eC4b60fdE",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=625_000_000_000_000_000,
        priority=1,
        note="91/98 Base liq in 30d scout",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0x54cf9be57fdfa6457a660991907434ff9d295c465a603a50126ff647d50b7354",
        loan_symbol="USDC",
        collateral_symbol="USDe",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34",
        oracle="0xF4b17C79492d68775e22e8Dd0a2Bb22854A39A47",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=915_000_000_000_000_000,
        priority=1,
        v3_fee=500,
        note="whale hunt: HF~1.005 x$20M; UniV3 USDe/USDC fee 500 (3000 liq=0)",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0x1a3e69d0109bb1be42b80e11034bb6ee98fc466721f26845dc83b2aa8d979137",
        loan_symbol="USDC",
        collateral_symbol="yoUSD",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0x0000000f2eB9f69274678c76222B35eEc7588a65",
        oracle="0x77f59C5C09e7ABEF6991B456170930324FE36222",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=915_000_000_000_000_000,
        priority=1,
        v3_fee=500,
        note="HF~1.003 ~$7k debt; swap yoUSD/USDC UniV3 fee 500",
    ),
    # Priority 10 — large TVL, lower recent liq frequency
    MorphoMarketConfig(
        chain="base",
        market_id="0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836",
        loan_symbol="USDC",
        collateral_symbol="cbBTC",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        oracle="0x663BECd10daE6C4A3Dcd89F1d76c1174199639B9",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=860_000_000_000_000_000,
        priority=10,
        note="largest Base Morpho market by TVL",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0x8793cf302b8ffd655ab97bd1c695dbd967807e8367a65cb2f4edaf1380ba1bda",
        loan_symbol="USDC",
        collateral_symbol="WETH",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0x4200000000000000000000000000000000000006",
        oracle="0xFEa2D58cEfCb9fcb597723c6bAE66fFE4193aFE4",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=860_000_000_000_000_000,
        priority=10,
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0xd7520ad198b497b6eb75bc690268f4597630dbc12e305e9d4105843bab36e41d",
        loan_symbol="USDC",
        collateral_symbol="cbADA",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0xcbADA732173e39521CDBE8bf59a6Dc85A9fc7b8c",
        oracle="0x35D87a743D1F2f7CaFb42D855dC1c5Df857Ce45f",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=625_000_000_000_000_000,
        priority=5,
        note="secondary liq flow — raised for oracle hot path",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0x6b52694164c1c86d6e834b05b8d35eb5d178ca2587a059143ac8b159a4dcf225",
        loan_symbol="USDC",
        collateral_symbol="KTA",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0xc0634090F2Fe6c6d75e61Be2b949464aBB498973",
        oracle="0x550eFC70B2d683FE8981224395B8989a452D1384",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=770_000_000_000_000_000,
        priority=1,
        note="live flow 19.08: 3 liqs ~$6.9k; UniV3 KTA/USDC fees 100/500/3000/10000",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0x73527ddd796e6d4f48387adaae36f6f3d49d606d7f2a15eb0c931416a58875d8",
        loan_symbol="USDC",
        collateral_symbol="cbDOGE",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0xcbD06E5A2B0C65597161de254AA074E489dEb510",
        oracle="0xA9D36600Fb9eba7548857e61F836Ec951e3091B2",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=625_000_000_000_000_000,
        priority=1,
        note="backtest: ~$1.7k edge / 30d",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0x09276541cfecb6920a80679a1deced4dde3ae64bf5fc2c9c1f9c21e0c152e1a5",
        loan_symbol="USDC",
        collateral_symbol="JitoSOL",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0x97bE14Dd8f994A5364573BC035D85309E7CB34de",
        oracle="0x78F772F5Fcc03256260cC1165e34Da9bb3f9BE1C",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=625_000_000_000_000_000,
        priority=5,
        note="backtest secondary SOL LST flow",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0x7dc02ff6c536b1d49d7fba770438d79f5bd1f1c78884629b7d1aaee19675782b",
        loan_symbol="USDC",
        collateral_symbol="SOL",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0x311935Cd80B76769bF2ecC9D8Ab7635b2139cf82",
        oracle="0xE9725430f3A72611ac72EdDc650625bce4F45DC7",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=625_000_000_000_000_000,
        priority=5,
        note="backtest: frequent small SOL liqs",
    ),
    # Tail combat 20.08 — overnight meat while cbXRP/cbADA/KTA were silent
    MorphoMarketConfig(
        chain="base",
        market_id="0x5dffffc7d75dc5abfa8dbe6fad9cbdadf6680cbe1428bafe661497520c84a94c",
        loan_symbol="WETH",
        collateral_symbol="cbBTC",
        loan_token="0x4200000000000000000000000000000000000006",
        collateral_token="0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        oracle="0x10b95702a0ce895972C91e432C4f7E19811D320E",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=915_000_000_000_000_000,
        priority=1,
        v3_fee=100,
        note="ETH pump squeeze vs BTC; $22k at HF~1.05; UniV3 fee 100",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0x3b3769cfca57be2eaed03fcc5299c25691b77781a1e124e7a8d520eb9a7eabb5",
        loan_symbol="WETH",
        collateral_symbol="USDC",
        loan_token="0x4200000000000000000000000000000000000006",
        collateral_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        oracle="0xD09048c8B568Dbf5f189302beA26c9edABFC4858",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=860_000_000_000_000_000,
        priority=1,
        v3_fee=500,
        note="ETH pump squeeze: WETH debt / USDC coll; UniV3 500",
    ),
    MorphoMarketConfig(
        chain="base",
        market_id="0xb3920b96dec75b6a1144b71f963f30236fb200f3e33e93c2e9c0d222c1fa53c2",
        loan_symbol="USDC",
        collateral_symbol="stkWELL",
        loan_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        collateral_token="0xe66E3A37C3274Ac24FE8590f7D84A2427194DC17",
        oracle="0xc842d95b9C35bf97842110408A8C501645Fb1722",
        irm="0x46415998764C29aB2a25CbeA6254146D50D22687",
        lltv_wad=625_000_000_000_000_000,
        priority=2,
        v3_fee=10000,
        note="tail 20.08 ~$1.9k; only UniV3 fee 10000 pool",
    ),
    # ── Arbitrum: thBILL is the only material 30d flow; TVL markets quiet ──
    MorphoMarketConfig(
        chain="arbitrum",
        market_id="0x551dbcdcceaf9322986e0cddde993d49840522a9532dc441359acd98af8badff",
        loan_symbol="USDC",
        collateral_symbol="thBILL",
        loan_token="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        collateral_token="0xfDD22Ce6D1F66bc0Ec89b20BF16CcB6670F55A5a",
        oracle="0x8e66f244F05277aAE16E1B5a586d742df932f157",
        irm="0x66F30587FB8D4206918deb78ecA7d5eBbafD06DA",
        lltv_wad=945_000_000_000_000_000,
        priority=1,
        note="backtest: 16/17 Arb liqs in 30d",
    ),
    MorphoMarketConfig(
        chain="arbitrum",
        market_id="0xe6392ff19d10454b099d692b58c361ef93e31af34ed1ef78232e07c78fe99169",
        loan_symbol="USDC",
        collateral_symbol="WBTC",
        loan_token="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        collateral_token="0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
        oracle="0x88193FcB705d29724A40Bb818eCAA47dD5F014d9",
        irm="0x66F30587FB8D4206918deb78ecA7d5eBbafD06DA",
        lltv_wad=860_000_000_000_000_000,
        priority=10,
        enabled=True,
        note="overnight watch: re-enabled for volatility",
    ),
    MorphoMarketConfig(
        chain="arbitrum",
        market_id="0xe0432ceb599fbe41defbd62fe8e914824af9d891a0a92c39de7063176c8e480b",
        loan_symbol="USDT0",
        collateral_symbol="weETH",
        loan_token="0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        collateral_token="0x35751007a407ca6FEFfE80b3cB397736D2cf4dbe",
        oracle="0xff135c74798c432B3172501a54fd56F85aA60A8A",
        irm="0x66F30587FB8D4206918deb78ecA7d5eBbafD06DA",
        lltv_wad=860_000_000_000_000_000,
        priority=10,
        enabled=True,
    ),
    MorphoMarketConfig(
        chain="arbitrum",
        market_id="0xd09404e9512e1341321c8ae3bd663fab7087582142ac61486635a6c072c2af12",
        loan_symbol="USDC",
        collateral_symbol="weETH",
        loan_token="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        collateral_token="0x35751007a407ca6FEFfE80b3cB397736D2cf4dbe",
        oracle="0x4E49d73434a78866d217BE0B542CA868495CBc77",
        irm="0x66F30587FB8D4206918deb78ecA7d5eBbafD06DA",
        lltv_wad=860_000_000_000_000_000,
        priority=10,
        enabled=True,
    ),
    MorphoMarketConfig(
        chain="arbitrum",
        market_id="0x33e0c8ab132390822b07e5dc95033cf250c963153320b7ffca73220664da2ea0",
        loan_symbol="USDC",
        collateral_symbol="wstETH",
        loan_token="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        collateral_token="0x5979D7b546E38E414F7E9822514be443A4800529",
        oracle="0x8e02a9b9Cc29d783b2fCB71C3a72651B591cae31",
        irm="0x66F30587FB8D4206918deb78ecA7d5eBbafD06DA",
        lltv_wad=860_000_000_000_000_000,
        priority=20,
        enabled=True,
    ),
    MorphoMarketConfig(
        chain="arbitrum",
        market_id="0xca83d02be579485cc10945c9597a6141e772f1cf0e0aa28d09a327b6cbd8642c",
        loan_symbol="USDC",
        collateral_symbol="WETH",
        loan_token="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        collateral_token="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        oracle="0x282FEB10549fde52bD61A6979424Ddf18A4971A2",
        irm="0x66F30587FB8D4206918deb78ecA7d5eBbafD06DA",
        lltv_wad=860_000_000_000_000_000,
        priority=20,
        enabled=True,
    ),
]


def markets_for_chain(chain: str, *, enabled_only: bool = True) -> list[MorphoMarketConfig]:
    chain = chain.lower()
    rows = [m for m in MORPHO_MARKETS if m.chain == chain]
    if enabled_only:
        rows = [m for m in rows if m.enabled]
    return sorted(rows, key=lambda m: (m.priority, m.loan_symbol, m.collateral_symbol))


def market_by_id(market_id: str) -> MorphoMarketConfig | None:
    key = market_id.lower()
    for m in MORPHO_MARKETS:
        if m.market_id.lower() == key:
            return m
    return None


def parse_allowed_markets(chain: str, spec: str | None = None) -> list[MorphoMarketConfig]:
    """Filter `markets_for_chain` by MORPHO_ALLOWED_MARKETS.

    `spec` is a comma-separated list of market ids, `LOAN/COLL` pairs, or
    collateral symbols. Empty / unset → all enabled markets for `chain`.
    """
    rows = markets_for_chain(chain, enabled_only=True)
    if not spec or not spec.strip():
        return rows
    wanted = {part.strip().lower() for part in spec.split(",") if part.strip()}
    out: list[MorphoMarketConfig] = []
    for m in rows:
        keys = {
            m.market_id.lower(),
            f"{m.loan_symbol}/{m.collateral_symbol}".lower(),
            f"{m.loan_symbol.lower()}-{m.collateral_symbol.lower()}",
            m.collateral_symbol.lower(),
        }
        if wanted & keys:
            out.append(m)
    return out
