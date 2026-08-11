"""Send a one-shot Telegram ping using TELEGRAM_* from .env.

    python tools/test_telegram.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(override=True)

from aave_bot.alerts import Notifier  # noqa: E402
from aave_bot.config import load_telegram_config  # noqa: E402


async def main() -> int:
    cfg = load_telegram_config()
    if not cfg.enabled:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        return 1
    notifier = Notifier(cfg, prefix="[test] ")
    ok = await notifier.send(
        "Aave монитор: Telegram OK — оповещения будут приходить в этот чат.",
        dedup_key=None,
    )
    await notifier.close()
    print("sent" if ok else "FAILED (check token/chat_id and that you /start the bot)")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
