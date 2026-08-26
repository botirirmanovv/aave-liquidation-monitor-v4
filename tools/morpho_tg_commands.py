#!/usr/bin/env python3
"""Telegram on-demand commands for Morpho status (/jiv, /жив, /status, /restart).

Long-polls getUpdates; replies only to configured chat_id.
Run on VPS via morpho-tg-commands.service.

Usage:
  python tools/morpho_tg_commands.py
  python tools/morpho_tg_commands.py --once /jiv   # local test, print only
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import aiohttp  # noqa: E402

from aave_bot.config import load_morpho_telegram_config  # noqa: E402
from tools.morpho_health_report import format_help_message, format_jiv_message  # noqa: E402

LOG = logging.getLogger("morpho_tg_commands")
API = "https://api.telegram.org"
POLL_TIMEOUT = 50
RESTART_COOLDOWN_SEC = 90.0
SCANNER_UNIT = "morpho-scanner"
SCANNER_LOG = ROOT / "morpho_scanner.err.log"
COMMANDS = {
    "/jiv",
    "/жив",
    "/alive",
    "/status",
    "/help",
    "/start",
    "/restart",
    "/рестарт",
}
_last_restart_mono = 0.0


def _normalize_cmd(text: str) -> str:
    parts = (text or "").strip().split()
    if not parts:
        return ""
    part = parts[0].lower()
    if "@" in part:
        part = part.split("@", 1)[0]
    return part


async def _api(session: aiohttp.ClientSession, token: str, method: str, **params: object) -> dict:
    url = f"{API}/bot{token}/{method}"
    async with session.post(url, json=params) as resp:
        data = await resp.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description", data))[:200])
    return data


async def send_reply(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: str | int,
    text: str,
    *,
    reply_to: int | None = None,
) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    await _api(session, token, "sendMessage", **payload)


def _run(cmd: list[str], *, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode, out
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def restart_morpho_scanner() -> str:
    """Restart morpho-scanner via systemd. VPS only."""
    global _last_restart_mono
    now = time.monotonic()
    if now - _last_restart_mono < RESTART_COOLDOWN_SEC:
        left = int(RESTART_COOLDOWN_SEC - (now - _last_restart_mono))
        return f"Подожди {left}с — restart не чаще раза в {int(RESTART_COOLDOWN_SEC)}с"

    code, out = _run(["systemctl", "is-active", SCANNER_UNIT])
    before = out.splitlines()[0] if out else "?"

    code, out = _run(["systemctl", "restart", SCANNER_UNIT], timeout=60)
    if code != 0:
        return f"Restart FAIL (code={code})\n{out[:300]}"
    _last_restart_mono = now

    time.sleep(8)
    code, out = _run(["systemctl", "is-active", SCANNER_UNIT])
    after = out.splitlines()[0] if out else "?"

    alive_hint = ""
    if SCANNER_LOG.exists():
        tail = SCANNER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(tail[-80:]):
            if "alive:" in line:
                alive_hint = line[line.index("alive:") :][:120]
                break
        if not alive_hint:
            for line in reversed(tail[-30:]):
                if "subscribed morpho" in line or "seeded" in line:
                    alive_hint = line.strip()[:120]
                    break

    lines = [
        "Morpho scanner — restart",
        f"было={before} → сейчас={after}",
    ]
    if after != "active":
        lines.append("КРАСНЫЙ: сервис не active после restart")
    else:
        lines.append("OK: сервис поднялся")
    if alive_hint:
        lines.append(alive_hint)
    lines.append("")
    lines.append("через ~30с: /jiv")
    return "\n".join(lines)


def _response_for(cmd: str) -> str:
    if cmd in {"/help", "/start"}:
        return format_help_message()
    if cmd in {"/jiv", "/жив", "/alive", "/status"}:
        return format_jiv_message()
    if cmd in {"/restart", "/рестарт"}:
        return restart_morpho_scanner()
    return ""


async def handle_update(
    session: aiohttp.ClientSession,
    token: str,
    allowed_chat: str,
    update: dict,
) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or str(chat_id) != str(allowed_chat):
        LOG.debug("ignore chat %s", chat_id)
        return
    text = msg.get("text") or ""
    cmd = _normalize_cmd(text)
    if cmd not in COMMANDS:
        return
    if cmd in {"/restart", "/рестарт"}:
        await send_reply(
            session,
            token,
            chat_id,
            "Перезапускаю morpho-scanner…",
            reply_to=msg.get("message_id"),
        )
    body = await asyncio.to_thread(_response_for, cmd)
    if not body:
        return
    await send_reply(session, token, chat_id, body, reply_to=msg.get("message_id"))
    LOG.info("replied %s to chat %s", cmd, chat_id)


async def _delete_webhook(session: aiohttp.ClientSession, token: str) -> None:
    try:
        await _api(session, token, "deleteWebhook", drop_pending_updates=False)
        LOG.info("telegram webhook cleared")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("deleteWebhook: %s", exc)


async def poll_forever() -> int:
    cfg = load_morpho_telegram_config()
    if not cfg.enabled:
        LOG.error("Telegram not configured (MORPHO_TELEGRAM_* or TELEGRAM_*)")
        return 1
    allowed = str(cfg.chat_id)
    timeout = aiohttp.ClientTimeout(total=POLL_TIMEOUT + 15)
    offset = 0
    LOG.info("morpho tg commands listening chat_id=%s", allowed)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _delete_webhook(session, cfg.bot_token)
        while True:
            try:
                data = await _api(
                    session,
                    cfg.bot_token,
                    "getUpdates",
                    offset=offset,
                    timeout=POLL_TIMEOUT,
                    allowed_updates=["message", "edited_message"],
                )
                for upd in data.get("result") or []:
                    offset = max(offset, int(upd["update_id"]) + 1)
                    try:
                        await handle_update(session, cfg.bot_token, allowed, upd)
                    except Exception as exc:  # noqa: BLE001
                        LOG.error("handle update: %s", exc, exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "Conflict" in msg and "webhook" in msg.lower():
                    LOG.warning("webhook conflict — clearing and retrying")
                    await _delete_webhook(session, cfg.bot_token)
                else:
                    LOG.warning("poll error: %s", exc)
                await asyncio.sleep(5)


async def main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.once:
        text = _response_for(_normalize_cmd(args.once))
        sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))
        if args.send:
            cfg = load_morpho_telegram_config()
            if not cfg.enabled:
                return 1
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await send_reply(session, cfg.bot_token, cfg.chat_id, text)
        return 0
    return await poll_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description="Morpho Telegram /jiv commands")
    ap.add_argument("--once", metavar="CMD", help="print response locally, e.g. /jiv")
    ap.add_argument("--send", action="store_true", help="with --once, also send to TG")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
