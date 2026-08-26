#!/usr/bin/env python3
"""Copy Morpho scanner to VPS and start systemd. Never starts Aave. Never prints secrets."""
from __future__ import annotations

import io
import sys
import tarfile
import time
from pathlib import Path

from dotenv import dotenv_values

try:
    import paramiko
except ImportError:
    print("NEED_PARAMIKO")
    sys.exit(3)

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/root/aave-liquidation-monitor-v4"

MORPHO_PY = (
    "morpho_scanner.py",
    "morpho_executor.py",
    "morpho_hf.py",
    "morpho_markets.py",
    "morpho_scoreboard.py",
    "morpho_flash_arb.py",
)


def _connect() -> paramiko.SSHClient:
    cfg = dotenv_values(ROOT / ".env")
    host = (cfg.get("VPS_HOST") or "").strip()
    user = (cfg.get("VPS_USER") or "root").strip()
    pw = (cfg.get("VPS_PASSWORD") or "").strip().strip('"')
    if not host or not pw:
        print("MISSING_CREDS")
        sys.exit(2)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    last: Exception | None = None
    for attempt in range(1, 6):
        try:
            client.connect(
                host,
                username=user,
                password=pw,
                timeout=90,
                banner_timeout=90,
                auth_timeout=90,
                allow_agent=False,
                look_for_keys=False,
            )
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(15)
            print(f"ssh_ok attempt={attempt}")
            return client
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"ssh_retry attempt={attempt} err={type(exc).__name__}")
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            time.sleep(3 * attempt)
    raise SystemExit(f"SSH_FAIL {last}")


def _run(client: paramiko.SSHClient, cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    stdout.channel.settimeout(float(timeout))
    stderr.channel.settimeout(float(timeout))
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def _add_tree(tar: tarfile.TarFile, rel: str) -> None:
    src = ROOT / rel
    if not src.exists():
        raise FileNotFoundError(rel)
    for path in src.rglob("*"):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_file():
            tar.add(path, arcname=str(path.relative_to(ROOT)).replace("\\", "/"))


def _bundle() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_tree(tar, "aave_bot")
        for name in MORPHO_PY:
            p = ROOT / "morpho_research" / name
            tar.add(p, arcname=f"morpho_research/{name}")
        tar.add(ROOT / "requirements.txt", arcname="requirements.txt")
        tar.add(
            ROOT / "scripts" / "morpho-scanner.service",
            arcname="scripts/morpho-scanner.service",
        )
        abi = ROOT / "forge-out" / "MorphoFlashLiquidator.sol" / "MorphoFlashLiquidator.json"
        tar.add(
            abi,
            arcname="forge-out/MorphoFlashLiquidator.sol/MorphoFlashLiquidator.json",
        )
        env_path = ROOT / ".env"
        info = tarfile.TarInfo(name=".env")
        data = env_path.read_bytes()
        info.size = len(data)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _sftp_put(client: paramiko.SSHClient, blob: bytes, remote: str) -> None:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("session_dead")
            sftp = client.open_sftp()
            try:
                sftp.putfo(io.BytesIO(blob), remote)
            finally:
                sftp.close()
            print(f"uploaded attempt={attempt}")
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"sftp_retry attempt={attempt} err={type(exc).__name__}")
            if "session_dead" in str(exc) or "not active" in str(exc).lower():
                raise RuntimeError("session_dead") from exc
            time.sleep(2 * attempt)
    raise RuntimeError(f"SFTP_FAIL {last}")


def main() -> int:
    print("bundling")
    blob = _bundle()
    print(f"bundle_bytes={len(blob)}")

    for round_i in range(1, 4):
        print(f"deploy_round={round_i}")
        client = _connect()
        try:
            code, out, err = _run(client, "uname -a", timeout=60)
            print(out.strip() or err.strip())
            try:
                _sftp_put(client, blob, "/tmp/morpho_vps.tgz")
            except RuntimeError as exc:
                if "session_dead" in str(exc) or "SFTP_FAIL" in str(exc):
                    print(f"reconnect after {exc}")
                    client.close()
                    continue
                raise

            setup = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl ca-certificates
mkdir -p /root/aave-liquidation-monitor-v4
tar -xzf /tmp/morpho_vps.tgz -C /root/aave-liquidation-monitor-v4
chmod 600 /root/aave-liquidation-monitor-v4/.env
cd /root/aave-liquidation-monitor-v4
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt websockets
install -m 644 scripts/morpho-scanner.service /etc/systemd/system/morpho-scanner.service
systemctl daemon-reload
systemctl enable morpho-scanner
systemctl restart morpho-scanner
rm -f /tmp/morpho_vps.tgz
systemctl is-active morpho-scanner
systemctl is-enabled morpho-scanner
systemctl is-active aave-monitor || true
"""
            code, out, err = _run(client, setup, timeout=480)
            sys.stdout.buffer.write((out[-2000:] if out else "").encode("utf-8", "replace"))
            if err.strip():
                sys.stdout.buffer.write(
                    ("\nSTDERR " + err[-800:] + "\n").encode("utf-8", "replace")
                )
            if code != 0:
                print(f"SETUP_FAIL {code}")
                return 1
            time.sleep(8)
            code, logs, _ = _run(
                client,
                "systemctl is-active morpho-scanner; "
                "grep -E 'onchain_fire|onchain_batch|hard_batch|preencode|feeder_fast' "
                "-n /root/aave-liquidation-monitor-v4/morpho_scanner.err.log "
                "/root/aave-liquidation-monitor-v4/morpho_scanner.out.log 2>/dev/null | tail -n 30; "
                "tail -n 25 /root/aave-liquidation-monitor-v4/morpho_scanner.err.log "
                "/root/aave-liquidation-monitor-v4/morpho_scanner.out.log 2>/dev/null || true",
                timeout=45,
            )
            sys.stdout.buffer.write((logs or "").encode("utf-8", "replace"))
            print("DEPLOY_OK")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"round_fail {type(exc).__name__}: {exc}")
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
            continue
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    print("DEPLOY_FAIL_ALL_ROUNDS")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
