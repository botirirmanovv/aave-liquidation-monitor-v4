#!/usr/bin/env python3
"""Deploy morpho health report + 6h systemd timer (quiet 00-08 UTC+5) to VPS."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from dotenv import dotenv_values
import paramiko

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/root/aave-liquidation-monitor-v4"


def main() -> int:
    cfg = dotenv_values(ROOT / ".env")
    host = (cfg.get("VPS_HOST") or "").strip()
    user = (cfg.get("VPS_USER") or "root").strip()
    pw = (cfg.get("VPS_PASSWORD") or "").strip().strip('"')

    last_err: Exception | None = None
    for attempt in range(4):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                host,
                username=user,
                password=pw,
                timeout=60,
                banner_timeout=60,
                allow_agent=False,
                look_for_keys=False,
            )
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(15)
            sftp = client.open_sftp()
            for d in ("tools", "scripts"):
                try:
                    sftp.stat(f"{REMOTE}/{d}")
                except OSError:
                    sftp.mkdir(f"{REMOTE}/{d}")
            for local_rel, remote_rel in (
                ("tools/morpho_health_report.py", "tools/morpho_health_report.py"),
                ("scripts/morpho-health-report.service", "scripts/morpho-health-report.service"),
                ("scripts/morpho-health-report.timer", "scripts/morpho-health-report.timer"),
            ):
                sftp.put(str(ROOT / local_rel), f"{REMOTE}/{remote_rel}")
            sftp.close()

            cmd = f"""
set -e
install -m 644 {REMOTE}/scripts/morpho-health-report.service /etc/systemd/system/morpho-health-report.service
install -m 644 {REMOTE}/scripts/morpho-health-report.timer /etc/systemd/system/morpho-health-report.timer
systemctl daemon-reload
systemctl enable morpho-health-report.timer
systemctl restart morpho-health-report.timer
systemctl start morpho-health-report.service
sleep 2
systemctl is-active morpho-health-report.timer || true
systemctl list-timers morpho-health-report.timer --no-pager || true
journalctl -u morpho-health-report.service -n 40 --no-pager || true
"""
            _, o, e = client.exec_command(cmd, timeout=120)
            sys.stdout.buffer.write(o.read())
            err = e.read()
            if err.strip():
                sys.stdout.buffer.write(b"STDERR " + err[:1200])
            return 0
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"attempt {attempt + 1} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(6)
        finally:
            client.close()
    print(f"FAILED: {last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
