"""Dry-run scoreboard: how many liquidations we detected and hypothetical P&L.

Estimates are optimistic (bonus − Aave flash 5bps, no gas/slip/competition).
Use for observation-week analysis only — not a guarantee of capture.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .chain import ChainContext, LiquidationPlan

AAVE_FLASH_PREMIUM = 0.0005  # 5 bps


@dataclass
class WouldHaveStats:
    chain: str
    path: Path
    candidates: int = 0
    unique_users: set[str] = field(default_factory=set)
    est_profit_usd: float = 0.0
    est_debt_usd: float = 0.0
    others_liq: int = 0
    others_debt_usd: float = 0.0
    others_est_profit_usd: float = 0.0
    # Detected as candidate before someone else liquidated them.
    raced_and_missed: int = 0
    raced_missed_profit_usd: float = 0.0
    # Never had a candidate hit for that user in this session.
    never_saw: int = 0
    _recent_candidates: dict[str, float] = field(default_factory=dict)  # user -> mono ts
    _candidate_profit: dict[str, float] = field(default_factory=dict)
    last_summary_at: float = 0.0

    # Arb (flash V2 / balancer V3) dry-run hits
    arb_hits: int = 0
    arb_est_profit_usd: float = 0.0
    arb_est_notional_usd: float = 0.0
    arb_by_kind: dict[str, int] = field(default_factory=dict)
    _recent_arb: dict[str, float] = field(default_factory=dict)

    def remember_candidate(self, user: str, debt_usd: float, profit_usd: float) -> bool:
        """Return True if this is a new countable candidate (dedup ~10 min)."""
        now = time.monotonic()
        prev = self._recent_candidates.get(user)
        if prev is not None and (now - prev) < 600:
            return False
        self._recent_candidates[user] = now
        self._candidate_profit[user] = profit_usd
        self.candidates += 1
        self.unique_users.add(user)
        self.est_debt_usd += max(0.0, debt_usd)
        self.est_profit_usd += max(0.0, profit_usd)
        self._save()
        return True

    def remember_arb(
        self,
        kind: str,
        route_key: str,
        notional_usd: float,
        profit_usd: float,
    ) -> bool:
        """Count a +EV arb quote. Dedup same route ~10 min."""
        now = time.monotonic()
        prev = self._recent_arb.get(route_key)
        if prev is not None and (now - prev) < 600:
            return False
        self._recent_arb[route_key] = now
        self.arb_hits += 1
        self.arb_by_kind[kind] = self.arb_by_kind.get(kind, 0) + 1
        self.arb_est_notional_usd += max(0.0, notional_usd)
        self.arb_est_profit_usd += max(0.0, profit_usd)
        self._save()
        return True

    def note_foreign_liq(
        self, user: str, debt_usd: float, profit_usd: float
    ) -> str:
        """Classify a third-party liquidation vs our recent candidates."""
        self.others_liq += 1
        self.others_debt_usd += max(0.0, debt_usd)
        self.others_est_profit_usd += max(0.0, profit_usd)
        now = time.monotonic()
        seen_at = self._recent_candidates.get(user)
        if seen_at is not None and (now - seen_at) < 3600:
            self.raced_and_missed += 1
            self.raced_missed_profit_usd += self._candidate_profit.get(
                user, max(0.0, profit_usd)
            )
            kind = "ypustili"
        else:
            self.never_saw += 1
            kind = "ne_videli"
        self._save()
        return kind

    def summary_ru(self) -> str:
        users = len(self.unique_users)
        arb_kinds = ", ".join(f"{k}={v}" for k, v in sorted(self.arb_by_kind.items())) or "—"
        return (
            f"Сводка dry-run ({self.chain})\n"
            f"— Ликвидации —\n"
            f"Кандидаты (мы увидели): {self.candidates} "
            f"({users} уник.)\n"
            f"Оценка долга: ${self.est_debt_usd:,.0f}\n"
            f"Оценка прибыли liq (бонус−5bps, без gas/slip): "
            f"${self.est_profit_usd:,.2f}\n"
            f"Чужие ликвидации: {self.others_liq} "
            f"(долг ${self.others_debt_usd:,.0f}, "
            f"их ~прибыль ${self.others_est_profit_usd:,.2f})\n"
            f"Успели бы в гонку, но упустили: {self.raced_and_missed} "
            f"(~${self.raced_missed_profit_usd:,.2f})\n"
            f"Не видели до чужого tx: {self.never_saw}\n"
            f"— Арбитраж —\n"
            f"+EV хиты: {self.arb_hits} ({arb_kinds})\n"
            f"Нотионал: ${self.arb_est_notional_usd:,.0f}\n"
            f"Оценка прибыли arb: ${self.arb_est_profit_usd:,.2f}\n"
            f"Итого liq+arb ~${self.est_profit_usd + self.arb_est_profit_usd:,.2f}\n"
            f"(гипотеза observation, не факт исполнения)"
        )

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
            "arb_hits": self.arb_hits,
            "arb_est_notional_usd": round(self.arb_est_notional_usd, 2),
            "arb_est_profit_usd": round(self.arb_est_profit_usd, 2),
            "arb_by_kind": self.arb_by_kind,
        }
        try:
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    @classmethod
    def load(cls, chain: str, path: Path) -> WouldHaveStats:
        stats = cls(chain=chain, path=path)
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
        stats.arb_hits = int(data.get("arb_hits", 0))
        stats.arb_est_notional_usd = float(data.get("arb_est_notional_usd", 0))
        stats.arb_est_profit_usd = float(data.get("arb_est_profit_usd", 0))
        kinds = data.get("arb_by_kind") or {}
        if isinstance(kinds, dict):
            stats.arb_by_kind = {str(k): int(v) for k, v in kinds.items()}
        # unique_users count only — set size restored as empty; ok for session
        n = int(data.get("unique_users", 0))
        if n:
            stats.unique_users = {f"prev-{i}" for i in range(n)}
        return stats


def estimate_plan_usd(ctx: ChainContext, plan: LiquidationPlan) -> tuple[float, float]:
    """Return (debt_usd, est_profit_usd) for a plan."""
    base = ctx.value_in_base(plan.debt_asset, plan.debt_to_cover)
    if not base:
        return 0.0, 0.0
    debt_usd = float(base) / 1e8
    bonus = ctx.liquidation_bonus(plan.collateral_asset)
    if not bonus or bonus <= 10_000:
        return debt_usd, 0.0
    gross = (bonus - 10_000) / 10_000.0
    profit = debt_usd * (gross - AAVE_FLASH_PREMIUM)
    return debt_usd, max(0.0, profit)


def estimate_amounts_usd(
    ctx: ChainContext, debt_asset: str, debt_amount: int, collateral_asset: str
) -> tuple[float, float]:
    base = ctx.value_in_base(debt_asset, debt_amount)
    if not base:
        return 0.0, 0.0
    debt_usd = float(base) / 1e8
    bonus = ctx.liquidation_bonus(collateral_asset) if collateral_asset else None
    if not bonus or bonus <= 10_000:
        return debt_usd, 0.0
    gross = (bonus - 10_000) / 10_000.0
    return debt_usd, max(0.0, debt_usd * (gross - AAVE_FLASH_PREMIUM))
