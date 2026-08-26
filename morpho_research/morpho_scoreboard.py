"""Morpho dry-run scoreboard + JSONL event log.

Tracks candidates vs foreign liquidations (race / never-saw), same idea as
aave_bot.would_have.WouldHaveStats but Morpho-specific and with append-only JSONL.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MorphoScoreboard:
    chain: str
    path: Path
    jsonl_path: Path
    candidates: int = 0
    unique_users: set[str] = field(default_factory=set)
    est_profit_usd: float = 0.0
    est_debt_usd: float = 0.0
    others_liq: int = 0
    others_debt_usd: float = 0.0
    others_est_profit_usd: float = 0.0
    raced_and_missed: int = 0
    raced_missed_profit_usd: float = 0.0
    never_saw: int = 0
    _recent_candidates: dict[str, float] = field(default_factory=dict)
    _candidate_profit: dict[str, float] = field(default_factory=dict)

    def remember_candidate(
        self,
        *,
        user: str,
        market_id: str,
        debt_usd: float,
        profit_usd: float,
        health_factor: float,
        pair: str = "",
        extra: dict[str, Any] | None = None,
    ) -> bool:
        now = time.monotonic()
        key = f"{market_id.lower()}:{user.lower()}"
        prev = self._recent_candidates.get(key)
        if prev is not None and (now - prev) < 600:
            return False
        self._recent_candidates[key] = now
        self._candidate_profit[key] = profit_usd
        self.candidates += 1
        self.unique_users.add(user.lower())
        self.est_debt_usd += max(0.0, debt_usd)
        self.est_profit_usd += max(0.0, profit_usd)
        self._append_jsonl(
            {
                "kind": "candidate",
                "ts": time.time(),
                "chain": self.chain,
                "user": user,
                "market_id": market_id,
                "pair": pair,
                "debt_usd": round(debt_usd, 4),
                "profit_usd": round(profit_usd, 4),
                "health_factor": float(health_factor),
                **(extra or {}),
            }
        )
        self._save()
        return True

    def note_foreign_liq(
        self,
        *,
        user: str,
        market_id: str,
        debt_usd: float,
        profit_usd: float,
        liquidator: str = "",
        pair: str = "",
        tx: str = "",
        block: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        self.others_liq += 1
        self.others_debt_usd += max(0.0, debt_usd)
        self.others_est_profit_usd += max(0.0, profit_usd)
        now = time.monotonic()
        key = f"{market_id.lower()}:{user.lower()}"
        seen_at = self._recent_candidates.get(key)
        if seen_at is not None and (now - seen_at) < 3600:
            self.raced_and_missed += 1
            self.raced_missed_profit_usd += self._candidate_profit.get(
                key, max(0.0, profit_usd)
            )
            kind = "ypustili"
        else:
            self.never_saw += 1
            kind = "ne_videli"
        self._append_jsonl(
            {
                "kind": "foreign_liq",
                "ts": time.time(),
                "chain": self.chain,
                "user": user,
                "market_id": market_id,
                "pair": pair,
                "liquidator": liquidator,
                "debt_usd": round(debt_usd, 4),
                "profit_usd": round(profit_usd, 4),
                "tx": tx,
                "block": block,
                "race": kind,
                **(extra or {}),
            }
        )
        self._save()
        return kind

    def summary_ru(self) -> str:
        users = len(self.unique_users)
        return (
            f"Сводка Morpho dry-run ({self.chain})\n"
            f"Кандидаты: {self.candidates} ({users} уник.)\n"
            f"Оценка долга/прибыли: ${self.est_debt_usd:,.0f} / "
            f"${self.est_profit_usd:,.2f}\n"
            f"Чужие liq: {self.others_liq} "
            f"(долг ${self.others_debt_usd:,.0f}, ~прибыль "
            f"${self.others_est_profit_usd:,.2f})\n"
            f"Успели увидеть, упустили: {self.raced_and_missed} "
            f"(~${self.raced_missed_profit_usd:,.2f})\n"
            f"Не видели до чужого tx: {self.never_saw}"
        )

    def _append_jsonl(self, row: dict[str, Any]) -> None:
        try:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _save(self) -> None:
        payload = {
            "chain": self.chain,
            "updated_at": time.time(),
            "candidates": self.candidates,
            "unique_users": len(self.unique_users),
            "est_debt_usd": round(self.est_debt_usd, 2),
            "est_profit_usd": round(self.est_profit_usd, 2),
            "others_liq": self.others_liq,
            "others_debt_usd": round(self.others_debt_usd, 2),
            "others_est_profit_usd": round(self.others_est_profit_usd, 2),
            "raced_and_missed": self.raced_and_missed,
            "raced_missed_profit_usd": round(self.raced_missed_profit_usd, 2),
            "never_saw": self.never_saw,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    @classmethod
    def load(cls, chain: str, path: Path, jsonl_path: Path) -> MorphoScoreboard:
        stats = cls(chain=chain, path=path, jsonl_path=jsonl_path)
        if not path.exists():
            return stats
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return stats
        stats.candidates = int(data.get("candidates", 0))
        stats.est_debt_usd = float(data.get("est_debt_usd", 0))
        stats.est_profit_usd = float(data.get("est_profit_usd", 0))
        stats.others_liq = int(data.get("others_liq", 0))
        stats.others_debt_usd = float(data.get("others_debt_usd", 0))
        stats.others_est_profit_usd = float(data.get("others_est_profit_usd", 0))
        stats.raced_and_missed = int(data.get("raced_and_missed", 0))
        stats.raced_missed_profit_usd = float(data.get("raced_missed_profit_usd", 0))
        stats.never_saw = int(data.get("never_saw", 0))
        n = int(data.get("unique_users", 0))
        if n:
            stats.unique_users = {f"prev-{i}" for i in range(n)}
        return stats
