#!/usr/bin/env python3
"""Write Morpho evening plan + send to Telegram (chunked)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(override=True)

from aave_bot.alerts import Notifier  # noqa: E402
from aave_bot.config import load_morpho_telegram_config  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "morpho_research" / "out" / "MORPHO_EVENING_PLAN.txt"

PLAN = r"""MORPHO FLASH-LIQ — план (без RPC/сервера)
========================================
Контекст: сканер уже видит hot-set/oracle. Не хватает
контракта liquidate+flash и тонкого execute-пути.
Цель вечера: только Morpho flash-liq, без arb-шума.

A) MAX ускорения НЕ про RPC и НЕ про VPS
----------------------------------------
Ранжировано по влиянию на win-rate гонки:

1. Контракт liquidate+callback (1 внешний вызов)
   Morpho: liquidate(..., data) -> коллбек -> своп -> repay.
   Быстрее чем flashLoan + liquidate отдельно.
   Меньше calldata, меньше gas, меньше revert-поверхностей.

2. Стрелять с локального HF по кешу shares
   На oracle tick: HF из cached supply/borrow shares +
   новый oracle price — БЕЗ ожидания Multicall.
   Multicall = подтверждение/коррекция, не триггер.
   (Сейчас очередь часто ждёт батч — это главный лаг «глаза».)

3. Pre-build calldata / нулевые аллокации на hot path
   Для каждой hot-позиции заранее:
   MarketParams, borrower, approx repay, router path,
   minOut, gasLimit. На тике — только подставить price/
   seized/repaid и sign/send.

4. Не звать eth_estimateGas в гонке
   Фиксированный gasLimit (калибровка off-path).
   eth_call simulate — только в dry-run / фоне.

5. Nonce pipeline + gas escalate
   Держать next_nonce локально; не ждать receipt перед
   следующей попыткой на другом borrower (с лимитом
   in-flight).
   Если tx не проходит (pending / чужой выиграл блок /
   underpriced): sticky replacement тот же nonce, tip↑
   и maxFee↑ по ступеням до лимита (см. конфиг).

6. Убрать I/O с hot path
   TG/JSONL/scoreboard — через asyncio.Queue, не await
   в том же тике что encode+send.

7. Преаппрувы в контракте (один раз)
   approve Morpho + routers на max; в колбэке не делать
   approve(0)/approve(x) каждый раз (экономия gas+время).

8. Узкий рынок + размер
   Не гонять китов >$25k (топ-2 забирают ~38–60%).
   Цель: $300–$25k, рынки с жарой (cbXRP и т.п.).
   Меньше конкурентов = выше «эффективная скорость».

9. Приватная отправка tx (если URL уже есть)
   Не «быстрее RPC», а меньше публичного mempool-снайпа.
   Опционально; без нового сервера.

10. Не трогать сейчас: Cython, реврайт на Rust, свой
    sequencer — ROI низкий относительно 1–6.

B) Контракт — подробный план
----------------------------
Имя: MorphoFlashLiquidator.sol (новый; arb не мешать)

Интерфейсы:
- IMorpho.liquidate(MarketParams, borrower, seizedAssets,
  repaidShares, bytes data)
- IMorphoFlashLoanCallback / onMorphoLiquidate (по доке Blue)
- Router: UniV2 + опционально UniV3 exactInput (cb* пары)

Хранение:
- owner, operator, paused
- immutable MORPHO
- allowedRouters / allowed intermediate tokens
- minProfitBps или minProfitAbs per loanAsset
- pre-approvals helper setApprovals(token, spenders[])

Вход оператора (один вызов):
  liquidateWithFlash(
    MarketParams p,
    address borrower,
    uint256 seizedAssets,   // 0 = max by repaid
    uint256 repaidShares,   // 0 = by seized
    SwapParams swap,        // router, path, minOut, deadline
    uint256 minProfit
  )

Колбэк onMorphoLiquidate:
  1) seized collateral уже у контракта
  2) swap -> loanAsset
  3) require balance >= repaidAssets + minProfit
  4) approve Morpho на repaidAssets (или уже infinite)
  Morpho сам pull'ит repaid.

Защиты:
- onlyOperator на вход
- msg.sender == MORPHO в колбэке
- reentrancy lock (не на колбэке если дедлок; как в Aave-фиксе)
- paused, rescueTokens (owner)
- revert InsufficientProfit / SwapFailed

Деплой Base first (8453). Constructor(morpho, operator).
Скрипт Remix/foundry: scripts/deploy_morpho_flash_liq.ts
Тесты: fork Base — mock liquidatable position или
записанный tx replay.

НЕ делать в v1: мульти-borrower batch, cross-chain,
сложные aggregator'ы (1inch) — latencу убьют.

C) Бот — подробный план
-----------------------
Новый тонкий модуль (не раздувать scanner):

morpho_research/morpho_executor.py
  - encode liquidateWithFlash
  - sign + send (PRIVATE_TX если задан, иначе HTTP)
  - gas: maxFee / tip из env (агрессивно на Base)
  - fixed gasLimit
  - min_debt / max_debt / min_edge фильтры
  - AUTO_EXECUTE=false по умолчанию

Wiring в morpho_scanner:
  при HF < 1.0 и debt в окне и market enabled:
    -> executor.try_liq(pos) fire-and-forget task
  oracle path: использовать cached shares (п.A2)
  candidate уже в scoreboard — не блокировать send

Конфиг (.env Base):
  MORPHO_LIQ_CONTRACT=
  MORPHO_AUTO_EXECUTE=false
  MORPHO_MIN_DEBT_USD=300
  MORPHO_MAX_DEBT_USD=25000
  MORPHO_MIN_EDGE_USD=20
  MORPHO_GAS_LIMIT=800000
  MORPHO_PRIORITY_FEE_GWEI=...      # стартовый tip
  MORPHO_PRIORITY_FEE_MAX_GWEI=...  # потолок escalate
  MORPHO_GAS_BUMP_PCT=25            # +% tip/maxFee на retry
  MORPHO_GAS_BUMP_MAX=4             # макс. замен того же nonce
  PRIVATE_TX_RPC_URL=  (опционально)

Режимы:
  1) observe: только лог «would send»
  2) paper: eth_call simulate, TG результат
  3) live: send (после 1–2 дней paper на каскадах)

Метрики TG (раз в N часов + on hit):
  would_send / simulated_ok / sent / landed /
  lost_to_foreign / revert_reason

D) Порядок работ вечером (чеклист)
---------------------------------
[ ] 1. Solidity MorphoFlashLiquidator + compile
[ ] 2. Deploy Base (operator = bot EOA)
[ ] 3. setApprovals USDC/WETH/cb* + routers
[ ] 4. morpho_executor.py + wire scanner
[ ] 5. Paper mode на live hot-set
[ ] 6. Сверить с foreign_liq scoreboard
[ ] 7. Только потом AUTO_EXECUTE на мелком окне

E) Чего НЕ делать сегодня
-------------------------
- Платный RPC / переезд VPS (ты исключил)
- Morpho DEX-arb (edge копеечный)
- Правки monitor_v4 / Aave
- Гонка за китами >$25k

F) Ожидание
-----------
Скорость «узнать+нажать» без нового железа растёт за счёт
локального HF + 1-call контракт + no-estimateGas.
Окно исполнения: debt $300–$25k.
Gas: стартовый tip + escalate (same nonce) пока не
пройдёт или не упрёмся в MAX / foreign win.
Доля рынка: мид/крупный мид на каскадах; киты >$25k — skip.
"""


async def send_chunks(text: str) -> None:
    cfg = load_morpho_telegram_config()
    if not cfg.enabled:
        print("Telegram disabled")
        return
    n = Notifier(cfg, prefix="")
    # TG limit 4096; keep margin
    limit = 3500
    parts: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            parts.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        parts.append(buf)
    total = len(parts)
    for i, part in enumerate(parts, 1):
        header = f"[Morpho plan {i}/{total}]\n" if total > 1 else "[Morpho plan]\n"
        ok = await n.send(header + part, dedup_key=None, cooldown=0)
        print(f"chunk {i}/{total}: {'ok' if ok else 'FAIL'}")
        await asyncio.sleep(0.4)
    await n.close()


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(PLAN, encoding="utf-8")
    print(f"wrote {OUT}")
    asyncio.run(send_chunks(PLAN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
