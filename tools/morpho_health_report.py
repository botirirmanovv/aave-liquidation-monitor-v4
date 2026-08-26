#!/usr/bin/env python3
"""Morpho health-отчёт → Telegram (на русском).

Каждые 6 часов (08/14/20 UTC+5). С 00:00 до 08:00 (UTC+5) не шлём.
Запуск на VPS через systemd timer. Ловит деградацию latency / рынков / live.
Секреты не печатает.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Local wall clock for Botir (UTC+5). Quiet: [00:00, 08:00).
LOCAL_TZ = timezone(timedelta(hours=5))
QUIET_START_HOUR = 0
QUIET_END_HOUR = 8

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from aave_bot.alerts import Notifier  # noqa: E402
from aave_bot.config import load_morpho_telegram_config  # noqa: E402

LOG = ROOT / "morpho_scanner.err.log"
ENV = ROOT / ".env"

EVAL_WARN_MS = 200.0
EVAL_CRIT_MS = 800.0
WAIT_WARN_MS = 500.0
WAIT_CRIT_MS = 2000.0

EXPECTED_MARKETS = {"USDC/cbXRP", "USDC/WETH", "USDC/yoUSD"}

# Format (feed_ev optional for older log lines):
# alive: ... oracle_moves=N [feed_ev=N] batch=N eval_ms=N wait_ms=N ...
ALIVE_RE = re.compile(
    r"alive:\s*chain=\S+\s+up=(?P<up>[\d.]+)s\s+.*?candidates=(?P<cand>\d+)\s+"
    r"foreign_liq=(?P<foreign>\d+)\s+discovery_liq=(?P<disc>\d+)\s+"
    r"tracked=(?P<tr>\d+)\s+hot=(?P<hot>\d+)\s+.*?oracle_moves=(?P<om>\d+)\s+"
    r"(?:feed_ev=(?P<feed>\d+)\s+)?"
    r"(?:feeder_ev=(?P<feeder>\d+)\s+)?"
    r"(?:feeder_rec=(?P<frec>\d+)\s+)?"
    r"(?:feeder_fast=(?P<ffast>\d+)\s+)?"
    r".*?batch=(?P<batch>\d+)\s+"
    r"eval_ms=(?P<eval>[\d.]+)\s+wait_ms=(?P<wait>[\d.]+)",
    re.I,
)
LIQ_RE = re.compile(
    r"liq_exec:\s*mode=(?P<mode>\S+)\s+would_send=(?P<ws>\d+)\s+"
    r"sim_ok=(?P<sok>\d+)\s+sim_fail=(?P<sfail>\d+)\s+"
    r"sent=(?P<sent>\d+)\s+landed=(?P<landed>\d+)\s+"
    r"lost_foreign=(?P<lost>\d+)",
    re.I,
)
SUB_RE = re.compile(r"subscribed morpho logs.*?markets=(?P<m>\d+)", re.I)
EVAL_OPT_RE = re.compile(r"eval-opt:\s*(?P<body>.+)$", re.I)
SEED_RE = re.compile(r"registry=(?P<reg>\d+)\s+markets=(?P<m>\d+)", re.I)


@dataclass
class Report:
    flags: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def level(self) -> str:
        if any(f.startswith("КРАСНЫЙ") for f in self.flags):
            return "КРАСНЫЙ"
        if any(f.startswith("ЖЁЛТЫЙ") for f in self.flags):
            return "ЖЁЛТЫЙ"
        return "ОК"


def _env_val(key: str) -> str:
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return (os.environ.get(key) or "").strip()


def _systemctl_active(unit: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return (r.stdout or "unknown").strip()
    except Exception as exc:  # noqa: BLE001
        return f"ошибка:{type(exc).__name__}"


def _svc_ru(status: str) -> str:
    return {
        "active": "активен",
        "inactive": "выключен",
        "failed": "упал",
        "activating": "запускается",
        "deactivating": "останавливается",
    }.get(status, status)


def _yes_no(val: str) -> str:
    low = val.lower()
    if low in {"1", "true", "yes", "on"}:
        return "да"
    if low in {"0", "false", "no", "off"}:
        return "нет"
    return val or "?"


def _on_off(val: str) -> str:
    low = val.lower()
    if low in {"1", "true", "yes", "on"}:
        return "вкл"
    if low in {"0", "false", "no", "off"}:
        return "выкл"
    return val or "?"


def _read_tail_text(path: Path, max_bytes: int = 400_000) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", "replace")


def _last_match(text: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    last: re.Match[str] | None = None
    for line in text.splitlines():
        m = pattern.search(line)
        if m:
            last = m
    return last


def _last_n(text: str, pattern: re.Pattern[str], n: int) -> list[re.Match[str]]:
    out: list[re.Match[str]] = []
    for line in text.splitlines():
        m = pattern.search(line)
        if m:
            out.append(m)
    return out[-n:]


def build_report() -> Report:
    rep = Report()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    markets = _env_val("MORPHO_ALLOWED_MARKETS") or "?"
    flash = _env_val("MORPHO_FLASH_ARB_ENABLED") or "?"
    diagnostic = _env_val("MORPHO_DIAGNOSTIC_MODE") or "?"
    auto = _env_val("MORPHO_AUTO_EXECUTE") or "?"
    svc = _systemctl_active("morpho-scanner")
    aave = _systemctl_active("aave-monitor")

    text = _read_tail_text(LOG)
    alive = _last_match(text, ALIVE_RE)
    liq = _last_match(text, LIQ_RE)
    sub = _last_match(text, SUB_RE)
    opt = _last_match(text, EVAL_OPT_RE)
    seed = _last_match(text, SEED_RE)

    if svc != "active":
        rep.flags.append(f"КРАСНЫЙ morpho-scanner={_svc_ru(svc)}")
    if aave == "active":
        rep.flags.append("ЖЁЛТЫЙ aave-monitor ещё активен (должен быть выкл)")

    eval_ms = float(alive.group("eval")) if alive else None
    wait_ms = float(alive.group("wait")) if alive else None
    if eval_ms is None:
        rep.flags.append("КРАСНЫЙ нет строк alive: в логах")
    elif eval_ms >= EVAL_CRIT_MS:
        rep.flags.append(f"КРАСНЫЙ eval_ms={eval_ms:.0f} (≥ {EVAL_CRIT_MS:.0f})")
    elif eval_ms >= EVAL_WARN_MS:
        rep.flags.append(f"ЖЁЛТЫЙ eval_ms={eval_ms:.0f} (≥ {EVAL_WARN_MS:.0f})")

    if wait_ms is not None:
        if wait_ms >= WAIT_CRIT_MS:
            rep.flags.append(f"КРАСНЫЙ wait_ms={wait_ms:.0f}")
        elif wait_ms >= WAIT_WARN_MS:
            rep.flags.append(f"ЖЁЛТЫЙ wait_ms={wait_ms:.0f}")

    have = {p.strip() for p in markets.split(",") if p.strip()}
    if have and have != EXPECTED_MARKETS:
        missing = EXPECTED_MARKETS - have
        extra = have - EXPECTED_MARKETS
        bits: list[str] = []
        if missing:
            bits.append("нет " + ",".join(sorted(missing)))
        if extra:
            bits.append("лишнее " + ",".join(sorted(extra)))
        rep.flags.append("ЖЁЛТЫЙ рынки: " + "; ".join(bits))

    if flash.lower() in {"1", "true", "yes", "on"}:
        rep.flags.append("ЖЁЛТЫЙ flash_arb=вкл (на free RPC обычно выкл)")

    sub_m = int(sub.group("m")) if sub else None
    if sub_m is not None and have and sub_m != len(have):
        rep.flags.append(f"ЖЁЛТЫЙ подписано рынков={sub_m}, в env={len(have)}")

    seed_m = int(seed.group("m")) if seed else None
    if seed_m is not None and have and seed_m != len(have):
        rep.flags.append(f"ЖЁЛТЫЙ seed рынков={seed_m}, в env={len(have)}")

    if not rep.flags:
        rep.flags.append("ОК все проверки зелёные")

    rep.lines.append(f"Morpho здоровье ({rep.level})")
    rep.lines.append(now)
    rep.lines.append("")
    rep.lines.append("флаги:")
    for f in rep.flags:
        rep.lines.append(f"• {f}")
    rep.lines.append("")
    rep.lines.append(f"сервис morpho={_svc_ru(svc)} aave={_svc_ru(aave)}")
    rep.lines.append(f"рынки={markets}")
    rep.lines.append(
        f"автоотправка={_yes_no(auto)} диагностика={_yes_no(diagnostic)} "
        f"flash_arb={_on_off(flash)}"
    )
    if opt:
        rep.lines.append(f"eval-opt: {opt.group('body').strip()}")
    if sub_m is not None:
        rep.lines.append(f"подписка рынков={sub_m}")
    if seed:
        rep.lines.append(f"книга={seed.group('reg')} рынков={seed.group('m')}")
    if alive:
        up = float(alive.group("up"))
        rep.lines.append(
            f"alive аптайм={up / 3600:.1f}ч tracked={alive.group('tr')} "
            f"hot={alive.group('hot')} eval_ms={alive.group('eval')} "
            f"wait_ms={alive.group('wait')}"
        )
        feed = alive.groupdict().get("feed")
        feeder = alive.groupdict().get("feeder")
        feed_bit = f" feed_ev={feed}" if feed is not None else ""
        feeder_bit = f" feeder_ev={feeder}" if feeder is not None else ""
        rep.lines.append(
            f"сессия кандидаты={alive.group('cand')} чужие={alive.group('foreign')} "
            f"discovery={alive.group('disc')} oracle_moves={alive.group('om')}"
            f"{feed_bit}{feeder_bit} batch={alive.group('batch')}"
        )
    if liq:
        mode = liq.group("mode")
        mode_ru = {"live": "live", "paper": "бумага", "off": "выкл"}.get(mode, mode)
        rep.lines.append(
            f"liq_exec режим={mode_ru} would={liq.group('ws')} "
            f"sim_ok={liq.group('sok')} sim_fail={liq.group('sfail')} "
            f"отправлено={liq.group('sent')} село={liq.group('landed')} "
            f"ушло_чужим={liq.group('lost')}"
        )
    recent = _last_n(text, ALIVE_RE, 5)
    if len(recent) >= 2:
        evs = [float(m.group("eval")) for m in recent]
        rep.lines.append(
            f"тренд eval_ms (последние {len(evs)}): "
            + " → ".join(f"{v:.0f}" for v in evs)
        )
    rep.lines.append("")
    rep.lines.append(
        f"пороги: eval warn≥{EVAL_WARN_MS:.0f} crit≥{EVAL_CRIT_MS:.0f} | "
        f"wait warn≥{WAIT_WARN_MS:.0f} crit≥{WAIT_CRIT_MS:.0f}"
    )
    rep.lines.append(
        "интервал: каждые 6ч в 08/14/20 (UTC+5); ночь 00–08 без отчёта"
    )
    return rep


def format_jiv_message() -> str:
    """Plain-language on-demand status for Telegram /jiv."""
    rep = build_report()
    text = _read_tail_text(LOG)
    alive = _last_match(text, ALIVE_RE)
    liq = _last_match(text, LIQ_RE)
    svc = _systemctl_active("morpho-scanner")

    now = datetime.now(LOCAL_TZ).strftime("%d.%m.%Y %H:%M UTC+5")

    if svc != "active":
        verdict = "Сканер НЕ работает — упал или выключен."
    elif rep.level == "КРАСНЫЙ":
        verdict = "Сканер жив, но есть серьёзная проблема."
    elif rep.level == "ЖЁЛТЫЙ":
        verdict = "Сканер живой, есть предупреждение (не обязательно поломка)."
    else:
        verdict = "Сканер живой и работает нормально."

    lines = [f"Morpho — {rep.level}", now, "", verdict, ""]

    if alive:
        up_h = float(alive.group("up")) / 3600.0
        tracked = alive.group("tr")
        hot = alive.group("hot")
        eval_ms = float(alive.group("eval"))
        wait_ms = float(alive.group("wait"))
        cand = alive.group("cand")
        lines.append(f"• работает уже ~{up_h:.1f} ч")
        lines.append(f"• смотрим {tracked} заёмщиков на 3 рынках")
        lines.append(f"• из них ~{hot} близко к ликвидации (hot) — проверяем чаще")
        if eval_ms < EVAL_WARN_MS:
            speed = "это хорошо"
        elif eval_ms < EVAL_CRIT_MS:
            speed = "чуть медленно"
        else:
            speed = "плохо, тормозит"
        lines.append(f"• одна проверка ~{eval_ms:.0f} мс ({speed})")
        lines.append(f"• очередь ~{wait_ms:.0f} мс — {'почти пустая' if wait_ms < WAIT_WARN_MS else 'забита'}")
        lines.append(f"• готовых к выстрелу за сессию: {cand}")
    else:
        lines.append("• в логах нет свежих строк alive — возможно только что упал")

    if liq:
        sent = liq.group("sent")
        would = liq.group("ws")
        mode = liq.group("mode")
        if mode == "live":
            lines.append(f"• режим live, отправлено={sent}, почти-выстрелов={would}")
            if sent == "0" and would == "0" and (not alive or alive.group("cand") == "0"):
                lines.append("• ликвидаций пока нет — рынок спокойный, не поломка")
        else:
            lines.append(f"• режим={mode} (отправка не live)")

    bad = [f for f in rep.flags if not f.startswith("ОК")]
    if bad:
        lines.append("")
        lines.append("флаги:")
        for f in bad[:4]:
            lines.append(f"• {f}")

    lines.append("")
    lines.append("команды: /jiv /status /restart /help")
    return "\n".join(lines)


def format_help_message() -> str:
    return (
        "Morpho бот — команды\n\n"
        "/jiv или /жив — жив ли сканер (простыми словами)\n"
        "/status — то же\n"
        "/restart или /рестарт — перезапуск morpho-scanner (VPS)\n"
        "/help — эта справка\n\n"
        "Полный отчёт каждые 6ч (08/14/20 UTC+5)."
    )


def _in_quiet_hours(now: datetime | None = None) -> bool:
    local = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    return QUIET_START_HOUR <= local.hour < QUIET_END_HOUR


async def main() -> int:
    if _in_quiet_hours():
        local = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
        msg = (
            f"тихий час {local.strftime('%H:%M')} UTC+5 "
            f"(00–08) — отчёт не шлём"
        )
        print(msg)
        return 0
    rep = build_report()
    text = "\n".join(rep.lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))
    cfg = load_morpho_telegram_config()
    if not cfg.enabled:
        print("Telegram не настроен — только печать", file=sys.stderr)
        return 1
    n = Notifier(cfg, prefix="")
    ok = await n.send(text, dedup_key=None, cooldown=0.0)
    await n.close()
    print("tg_отправлено" if ok else "tg_ОШИБКА")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
