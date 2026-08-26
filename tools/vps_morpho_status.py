#!/usr/bin/env python3
"""Check morpho-scanner systemd on VPS. Never prints secrets."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values
import paramiko

ROOT = Path(__file__).resolve().parents[1]
cfg = dotenv_values(ROOT / ".env")
host = (cfg.get("VPS_HOST") or "").strip()
user = (cfg.get("VPS_USER") or "root").strip()
pw = (cfg.get("VPS_PASSWORD") or "").strip().strip('"')
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=pw, timeout=20, allow_agent=False, look_for_keys=False)
cmd = (
    "systemctl is-active morpho-scanner; "
    "systemctl is-active aave-monitor || true; "
    "echo '---GREP---'; "
    "grep -E 'alive:|liq_exec:|arb:|foreign|ERROR|Traceback|subscribed|sent=|would_send|dry-run|USDC/KTA|USDC/cbXRP|USDC/cbADA|live send' "
    "/root/aave-liquidation-monitor-v4/morpho_scanner.err.log 2>/dev/null | tail -n 40; "
    "echo '---TAIL---'; "
    "tail -n 25 /root/aave-liquidation-monitor-v4/morpho_scanner.err.log 2>/dev/null || true"
)
_, stdout, stderr = client.exec_command(cmd, timeout=30)
out = stdout.read().decode("utf-8", "replace")
err = stderr.read().decode("utf-8", "replace")
sys.stdout.buffer.write(out.encode("utf-8", "replace"))
if err.strip():
    sys.stdout.buffer.write(("STDERR " + err[:500] + "\n").encode("ascii", "replace"))
client.close()
