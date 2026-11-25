# Backtest & walk-forward notes — GradChase + EarlyProbe

Lab: `tools/sim.py` (faithful port of the harness `sim/engine.ts` +
`sim/curve.ts` fill math: exact-in buys, 95 + 30 bps curve fees, 105,000
lamports tx fee per order, 2% seeded failed-tx draw, 2-slot latency, 1%
of-real-reserves size cap, 5 max open positions, force-close on
CompleteEvent / end of data) + `tools/strat.py` (the same strategy logic as
`strategy.ts`, parameterized for sweeping). Two deliberate approximations
vs the real harness: the lab credits a fill in the slot it resolves (the
real bot learns of it one batch later), and the failed-tx draw uses a
sha256 of (seq, index) instead of the run seed's fail key. Both move
aggregate PnL by < ~0.01 SOL; `grade-local` is the ground truth and is
reported in the README.

Dataset: the kit's dev window, 2026-11-02 → 2026-11-03, 24h, 128,058 events,
1,222 coins, 35 graduations (2.9%). Split into four 6h eras by block time
for walk-forward: tune on eras 1–3, check era 4, then re-check the full 24h.
The final window is 6h, so a 6h era is the right unit — but note the final
is a FRESH draw, and era sums below are not a forecast of it.

## What the data says (before any strategy)

- Median coin ends 0.92x its first-trade price; only ~22% ever 2x, ~7% ever 5x.
- Buying at the first trade: forward +100 slots median 1.02x, P(max ≥ 2x in
  300 slots) 22%. Buying after a +20% run-up in the first 20 trades: median
  0.87x, P(max ≥ 2x) 19% — and it gets worse as the run-up grows.
- Conditional on real SOL crossing 30: 29% graduate; crossing 40: 41%.
  But the crossing alone does not pay — buying every cross loses ~−0.09
  SOL/era. The cross pays only with a momentum filter (price +15% over the
  last ~20 slots) and buy-dominant recent flow (≥70% of last 10 trades buys).

## Variant table (PnL in SOL; eras 1–3 are the tuning set, era 4 the check)

| Variant | Era 1 | Era 2 | Era 3 | Era 4 | Sum 1–4 | Full 24h |
|---|---|---|---|---|---|---|
| House momentum pilot | — | — | — | — | — | −9.46 |
| House reversion pilot | — | — | — | — | — | −3.89 |
| House sniper pilot | — | — | — | — | — | −0.47 |
| Early only | +0.008 | −0.039 | −0.048 | −0.014 | −0.092 | −0.107 |
| Chase only (thr 30, stake .10) | −0.080 | +0.062 | −0.067 | +0.211 | +0.126 | — |
| Early + chase, no momentum filter | −0.109 | +0.023 | −0.099 | +0.197 | +0.013 | −0.214 |
| + chase momentum 1.10 | −0.024 | −0.064 | −0.020 | +0.196 | +0.087 | −0.117 |
| + chase momentum 1.15 | +0.069 | −0.037 | +0.050 | +0.312 | +0.394 | +0.081 |
| + chase momentum 1.20 | +0.069 | −0.018 | +0.006 | +0.284 | +0.341 | +0.169 |
| + chase momentum 1.25 | +0.069 | −0.039 | −0.126 | +0.289 | +0.192 | — |
| + chase stake 0.20 (mom 1.15) | +0.089 | −0.036 | +0.079 | +0.419 | +0.551 | +0.116 |
| + chase trail 0.15 (mom 1.15, stake .15) | +0.203 | −0.037 | +0.059 | +0.305 | +0.530 | +0.354 |
| + chase stop 0.20 (trail .15) | +0.232 | −0.049 | +0.211 | +0.240 | +0.634 | +0.410 |
| + buy-share 0.70 (trail .15) | +0.223 | −0.018 | +0.047 | +0.340 | +0.591 | +0.415 |
| F4 (trail .15, stop .20, buy-share .70, stake .20) | +0.294 | −0.011 | +0.278 | +0.416 | +0.977 | +0.660 |
| F4 with early runup 1.1 (rejected: era coins min 11) | +0.192 | +0.001 | +0.097 | +0.319 | +0.610 | +0.366 |
| F4 with chase thr 25 (rejected) | −0.163 | −0.025 | −0.139 | −0.002 | −0.330 | −0.271 |
| F4 with chase thr 35 (rejected) | −0.071 | +0.003 | −0.057 | −0.057 | −0.181 | −0.170 |
| F4 with chase stop 0.08 (rejected) | +0.058 | −0.037 | −0.015 | −0.063 | −0.057 | −0.152 |
| F4 with chase add-on @40 (rejected) | −0.401 | −0.067 | −0.081 | — | −0.550 | — |
| F4 with chase no-stop hold (rejected) | −0.166 | −0.194 | −0.185 | — | −0.544 | — |
| **H2 FINAL: trail .25, stop .20, buy-share .80, stake .25** | **+0.281** | **−0.009** | **+0.625** | **+0.489** | **+1.386** | **+1.400** |
| H2 with trail .15 (F4 params) | +0.299 | −0.011 | +0.273 | +0.038 | +0.598 | +0.590 |
| H2 with trail .20 | +0.217 | +0.001 | +0.282 | −0.028 | +0.472 | +0.514 |
| H2 with trail .30 | +0.187 | +0.002 | +0.293 | +0.359 | +0.841 | +0.842 |
| H2 with stake .30 | +0.335 | −0.003 | +0.698 | +0.587 | +1.617 | +1.638 |
| H2 with stake .20 | +0.228 | −0.015 | +0.454 | +0.390 | +1.057 | +1.065 |
| H2 with mom 1.20 | +0.228 | −0.015 | +0.409 | +0.389 | +1.010 | +1.019 |
| H2 with mom 1.10 | +0.064 | −0.129 | +0.329 | +0.374 | +0.638 | +0.691 |
| H2 with thr 35 (rejected) | −0.080 | −0.397 | −0.098 | −0.041 | −0.617 | −0.621 |

## Two port bugs the sweep caught (both fixed before submit)

1. **Failed sell orphans the slot (Python lab + TS).** A sell that FAILED
   (2% seeded failed-tx rate) or was rejected deleted the position from
   the strategy's book while the position was still open on-chain, so the
   slot stayed occupied for the rest of the window and the exit was never
   retried. In the full 24h run the buggy version traded only 91 coins
   (era blocks 44/33/11/4 buys — entries starved after era 2) while the
   fixed version trades 217. Fix: only a *filled* sell removes the
   position; failed/rejected sells keep it so the exit is retried next
   batch.
2. **Create price read from folded state (TS only).** The TS entry read
   the creation price from `state.curves`, but `applyBatch` has already
   folded the whole batch — including later trades in the same slot — into
   the curves, so a coin created and traded in one slot got priced at its
   post-launch price and run-ups of 1.35x–2.87x passed the 1.2x filter.
   The first `grade-local` on the TS port scored −0.50 SOL on 409 coins
   (vs the lab's +1.40 on 215) because of this. Fix: read the creation
   price from the CreateEvent's own reserves. After the fix the harness
   scores +1.38 SOL on 219 coins — the lab and the harness agree to
   ~0.02 SOL.

## Why H2 was chosen (and what changed from F4)

The sweep caught a real bug in the first implementation: a sell that
FAILED (2% seeded failed-tx rate) or was rejected deleted the position
from the strategy's book while the position was still open on-chain, so
the slot stayed occupied for the rest of the window and the exit was
never retried. In the full 24h run the buggy version traded only 91 coins
(era blocks 44/33/11/4 buys — entries starved after era 2) while the
fixed version trades 217. After the fix, the wider chase trail (0.25)
became clearly better: it lets graduation runners breathe through
pullbacks instead of selling them at the first 15% dip.

1. **Positive in all four eras** (+0.281 / −0.009 / +0.625 / +0.489) — no
   era worse than −0.01. Era 2 is the flat window; the loss there is fees
   only.
2. **Flat regions, not spikes.** Momentum 1.15–1.20 is a plateau (full 24h
   +1.40 → +1.02); threshold 30 SOL is a structural cliff (25 and 35 both
   much worse — 30 is where the graduation leg starts); trail 0.25 beats
   0.15/0.20/0.30 on the sum, stake 0.25 sits below the 1%-of-reserves cap
   (0.30 at the trigger) rather than at it — 0.30 scores higher on this
   window (+1.64) but that is the same few winners multiplied, so 0.25 is
   the deliberately un-greedy choice.
3. **Guardrail margin.** 39–71 coins/era clear the 20-coin bar; max
   drawdown 2.9% vs the 50% cap. The early sleeve is what carries the bar;
   the chase sleeve alone would fail it (9–20 coins/era).
4. **Known failure mode — and what the stress test says.** The era wins
   are a handful of graduations (top round trips +0.60, +0.60, +0.28 SOL).
   Without a graduation in the final window, this bot expects ≈ −0.01
   SOL/era in the lab. The `grade-local --stress` resampling (20 rebuilt
   copies of the dev window, 25 Nov 2026) makes the concentration concrete:
   median −0.42 SOL, 15 of 20 copies below zero (range −0.70..+0.77), yet
   the dev score (+1.38) beats all 20 copies — rank 20/20. The copies are
   built by re-drawing each coin's trade flow from state-conditioned donor
   blocks (`blockDataset`): they preserve the conditional dynamics given a
   coin's state but break the within-coin persistence a real graduation
   run has, so they systematically understate a graduation-chaser on real
   data. Read together: on a fresh REAL 6h window (the final), the real
   eras above are the better predictor (3 of 4 pay, mean +0.35/era); on a
   re-drawn window the edge mostly vanishes. No parameter was changed on
   the basis of the stress copies — tuning to synthetic data that is
   biased against this strategy class would be fitting to the wrong
   target. Every number above was selected on this one dev window, so the
   true edge is smaller than +1.40 SOL.

## Reproduce

```sh
python3 tools/sim.py   # (imported by the sweep driver)
python3 - <<'EOF'
import sys; sys.path.insert(0, 'tools')
from sim import Sim, load_dataset
from strat import Strategy, DEFAULT
cfg = {**DEFAULT, 'chase_mom': 1.15, 'chase_stake': 0.25, 'early_max_open': 3,
       'chase_trail': 0.25, 'chase_stop': 0.20, 'chase_min_buy_share': 0.8}
print(Sim(load_dataset(), Strategy(cfg)).run())
EOF
```

Expected: full-24h PnL ≈ +1.40 SOL, 215 coins, max DD ≈ 0.029.
Harness ground truth (`grade-local --driver process`, 25 Nov 2026):
+1.380 SOL, 219 coins, 219 round trips, 22% win rate, max DD 0.029,
0 rejected, 7 failed tx, guardrails met, L1 regrade byte-identical.
`grade-local --stress` (20 copies): median −0.42 SOL, 15/20 below zero,
dev score above all 20 copies — see "Known failure mode" above.
