#!/usr/bin/env python3
"""Send the locked combine A-to-Z spec to Morpho Telegram.

Does not start Morpho live or Aave. Needs MORPHO_TELEGRAM_* or TELEGRAM_*.

  python tools/send_combine_strategy_morpho.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(override=True)

from aave_bot.alerts import Notifier  # noqa: E402
from aave_bot.config import load_morpho_telegram_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "xau_jam" / "reports" / "STRATEGY_A_TO_YA.txt"
CHUNK = 3500
HEADER = (
    "Combine / импульс семьи — копия для восстановления.\n"
    "Live закрыт. Aave и Morpho live не трогал. Деньги не вносим."
)


def chunk_text(text: str, limit: int = CHUNK) -> list[str]:
    parts: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if buf and len(buf) + len(line) > limit:
            parts.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        parts.append(buf)
    return parts


async def send_spec(path: Path = SPEC) -> int:
    if not path.is_file():
        print(f"missing spec: {path}")
        return 1
    cfg = load_morpho_telegram_config()
    if not cfg.enabled:
        print(
            "Morpho Telegram not configured. "
            "Set MORPHO_TELEGRAM_BOT_TOKEN + MORPHO_TELEGRAM_CHAT_ID "
            "(or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) on this machine, then rerun."
        )
        return 1
    text = path.read_text(encoding="utf-8")
    n = Notifier(cfg, prefix="")
    try:
        ok_doc = await n.send_document(path, caption=HEADER)
        print(f"document: {'ok' if ok_doc else 'FAIL'}")
        if ok_doc:
            return 0
        parts = chunk_text(text)
        total = len(parts)
        failed = 0
        for i, part in enumerate(parts, 1):
            title = f"[combine {i}/{total}]\n" if total > 1 else "[combine]\n"
            ok = await n.send(title + part, dedup_key=None, cooldown=0)
            print(f"chunk {i}/{total}: {'ok' if ok else 'FAIL'}")
            if not ok:
                failed += 1
            await asyncio.sleep(0.4)
        return 0 if failed == 0 else 2
    finally:
        await n.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(send_spec()))
