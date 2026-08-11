"""Persistent monitor state with an asset -> users reverse index.

The original code answered "who is affected by this price update?" by iterating
every tracked user, so one AnswerUpdated event cost a full re-scan of the book.
The reverse index is derived from user_reserves on load, which keeps the two
views from ever drifting apart.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger("aave_bot.state")


class MonitorState:
    def __init__(self, path: Path, save_interval_seconds: float = 5.0) -> None:
        self.path = Path(path)
        self.tracked_users: set[str] = set()
        self.user_reserves: dict[str, set[str]] = {}
        self.asset_users: dict[str, set[str]] = {}
        self.svr_reserves: set[str] = set()

        self._save_interval = save_interval_seconds
        self._dirty = False
        # Anchored at construction so the interval applies from the very first
        # save; otherwise monotonic() - 0.0 dwarfs any interval and a startup
        # burst of events writes the file on every single event.
        self._last_save = time.monotonic()

    # ── persistence ──────────────────────────────────────────────────────
    def load(self) -> None:
        if not self.path.exists():
            log.info("no state file at %s, starting empty", self.path)
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read %s (%s), starting empty", self.path, exc)
            return

        self.tracked_users = set(payload.get("tracked_users", []))
        self.user_reserves = {
            user: set(reserves) for user, reserves in payload.get("user_reserves", {}).items()
        }
        self.svr_reserves = set(payload.get("svr_reserves", []))
        self._rebuild_index()
        log.info(
            "loaded %d users, %d assets indexed, %d known SVR reserves",
            len(self.tracked_users), len(self.asset_users), len(self.svr_reserves),
        )

    def save(self, force: bool = False) -> None:
        """Atomic write, throttled so a burst of events does not thrash the disk."""
        if not self._dirty and not force:
            return
        if not force and (time.monotonic() - self._last_save) < self._save_interval:
            return

        payload = {
            "tracked_users": sorted(self.tracked_users),
            "user_reserves": {u: sorted(r) for u, r in self.user_reserves.items()},
            "svr_reserves": sorted(self.svr_reserves),
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not persist state to %s: %s", self.path, exc)
            return

        self._dirty = False
        self._last_save = time.monotonic()

    def _rebuild_index(self) -> None:
        self.asset_users = {}
        for user, reserves in self.user_reserves.items():
            for asset in reserves:
                self.asset_users.setdefault(asset, set()).add(user)

    # ── mutation ─────────────────────────────────────────────────────────
    def track(self, user: str, reserve: str) -> bool:
        """Record that `user` interacted with `reserve`. True if the user is new."""
        is_new = user not in self.tracked_users
        self.tracked_users.add(user)

        reserves = self.user_reserves.setdefault(user, set())
        if reserve not in reserves:
            reserves.add(reserve)
            self.asset_users.setdefault(reserve, set()).add(user)
            self._dirty = True
        if is_new:
            self._dirty = True
        return is_new

    def forget(self, user: str) -> None:
        """Drop a user, e.g. once their debt is fully repaid."""
        self.tracked_users.discard(user)
        for asset in self.user_reserves.pop(user, set()):
            holders = self.asset_users.get(asset)
            if holders:
                holders.discard(user)
                if not holders:
                    self.asset_users.pop(asset, None)
        self._dirty = True

    def mark_svr(self, asset: str) -> None:
        if asset not in self.svr_reserves:
            self.svr_reserves.add(asset)
            self._dirty = True

    # ── queries ──────────────────────────────────────────────────────────
    def users_for_asset(self, asset: str) -> set[str]:
        return set(self.asset_users.get(asset, ()))

    def users_for_assets(self, assets: Iterable[str]) -> set[str]:
        affected: set[str] = set()
        for asset in assets:
            affected |= self.asset_users.get(asset, set())
        return affected

    def reserves_of(self, user: str) -> set[str]:
        return set(self.user_reserves.get(user, ()))

    def __len__(self) -> int:
        return len(self.tracked_users)
