# GradChase + EarlyProbe — Pump.fun Bot Sprint

Paper-trades pump.fun on the `pumpfun-replay/v2` harness. Node 22, `main.ts` at
the repo root. No model/LLM calls, no real money, no network — a pure function
of the replay batches. Fully deterministic, so the grader's two executions
produce byte-identical action logs.

## Strategy (one paragraph)

Two sleeves, one buy per coin ever. **EarlyProbe** buys young coins small
before they run (age ≤ 60 slots, ≥ 3 trades, price ≤ 1.2× create price, ≥ 2
distinct non-creator buyers, creator hasn't sold, creator buys ≤ 2 SOL, real
SOL ≥ 0.3), 0.02 SOL a probe, stop −25%, trail armed at +40% with a 25%
trail-back, 150-slot max hold, at most 3 early positions at once. **GradChase**
buys the graduation leg: when a curve's real SOL crosses 30 (graduation is
~85 SOL) AND the price is +15% over ~20 slots ago AND ≥ 80% of the last 10
trades are buys AND the crossing trade is ≤ 5 SOL — 0.25 SOL, stop −20%,
trail armed at +15% with a 25% trail-back, 2000-slot max hold (usually exits
by trail, or the harness closes the position when the curve migrates).

## Files

- `main.ts` — entrypoint: `runBot(createStrategy(DEFAULT_PARAMS))`.
- `strategy.ts` — the strategy + parameters + sweep evidence in its header.
- `vendor/makefun-bot.ts` — the kit's SDK shim, copied byte-identical from
  `sdk/ts/makefun-bot.ts` (same file the equal-weight baseline vendors).
- `tools/sim.py` + `tools/strat.py` — the Python lab that reproduces the
  harness sim and sweeps variants; see `BACKTEST.md`.
- `BACKTEST.md` — variant table, walk-forward numbers, and why these
  parameters were chosen.

## Results on the dev window (full 24h dataset, 128,058 events)

Harness ground truth (`grade-local --driver process`, 25 Nov 2026):

| Config | Net PnL (SOL) | Coins | Win rate | Max DD |
|---|---|---|---|---|
| **GradChase + EarlyProbe (this entry)** | **+1.38** | 219 | 22% | 0.029 |
| house pilot sniper | −0.47 | 351 | ~13% | 0.052 |
| house pilot mean reversion | −3.89 | 195 | ~26% | 0.482 |
| house pilot momentum | −9.46 | 46 | ~6% | 0.956 |
| equal-weight buy-and-hold | −0.02 | 5 | 0% | 0.009 |

Own lab (faithful port of the harness sim) agrees to ~0.02 SOL: +1.40 SOL,
215 coins, 20% win rate, max DD 0.029. L1 regrade reproduced byte for byte;
90% interval +0.08..+3.11 SOL; +1.12 SOL even at 2× fees; 0 rejected
orders, 7 failed tx (2% seeded rate, exits retried).

Walk-forward (four 6h eras, lab): **+0.281 / −0.009 / +0.625 / +0.489 SOL**.
Guardrails on dev: 39–71 coins/era hold round trips ≥ 0.01 SOL (bar: 20),
max drawdown 2.9% (bar: 50%) — met with margin.

Robustness (`grade-local --stress`, 20 rebuilt copies of the dev window):
median **−0.42 SOL**, 15 of 20 copies below zero, best +0.77, worst −0.70 —
but the dev score beats all 20 copies (rank 20/20). The copies re-draw
each coin's flow from state-conditioned donor blocks, which breaks the
within-coin persistence a graduation run needs, so they understate a
graduation-chaser on real data; the real 6h eras above are the better
predictor of the fresh final window. The concentration is real either way:
3 coins made 77% of the dev gains; without them the dev window is −0.10.

## Honest note

The edge is one shape: small fee bleed on flat windows (~−0.01 SOL/era in
the lab; the stress copies imply up to ~−0.10 SOL per 6h when no runner
appears), a few graduations pay for it many times over (the two biggest
round trips are +0.60 and +0.60 SOL on 0.25 SOL chase buys, +240% each).
The final window is a fresh 6h draw — nothing here guarantees it contains a
graduation, and if it doesn't, this bot expects to finish a few hundredths
to a tenth of a SOL below zero. Every parameter was selected on this one
dev window, so the true edge is smaller than the lab numbers; parameters
were chosen in flat regions of the sweep grid (momentum 1.15 sits on a
1.15–1.20 plateau; threshold 30 SOL is a structural cliff — 25 and 35 are
both much worse), not at single best cells. See `BACKTEST.md` for the full
variant table, including the variants that were tried and rejected — and
the failed-exit bug the sweep caught (a failed sell must be retried, never
forgotten).
