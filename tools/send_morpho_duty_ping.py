#!/usr/bin/env python3
"""One-shot Morpho duty ping via MORPHO_TELEGRAM_BOT_TOKEN. No secrets in the message."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(override=True)

from aave_bot.alerts import Notifier  # noqa: E402
from aave_bot.config import load_morpho_telegram_config  # noqa: E402

TEXT = """Morpho на связи

режим=live  отправка включена
порог чистыми $0.50
рынки USDC/cbXRP + USDC/cbADA + USDC/KTA

Сообщения теперь на русском."""


async def main() -> int:
    cfg = load_morpho_telegram_config()
    if not cfg.enabled:
        print("Telegram not configured")
        return 1
    n = Notifier(cfg, prefix="[morpho/duty] ")
    ok = await n.send(TEXT, dedup_key=None)
    await n.close()
    print("sent" if ok else "FAILED")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
