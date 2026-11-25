// GradChase + EarlyProbe — pump.fun paper-trading bot for agent-fun/replay/v2.
// The grader runs `node /bot/main.ts` with this directory mounted at /bot.

import { runBot } from './vendor/makefun-bot.ts';
import { createStrategy, DEFAULT_PARAMS } from './strategy.ts';

// Fail fast on a misconfigured sizing table: a stray zero or a swapped pair
// of limits here would otherwise trade at the wrong size for a whole window
// before anyone noticed, so the entrypoint refuses to start instead. The
// smallest buy must also clear the round-trip floor — anything below it could
// never register as a round trip once fees are taken out.
const cfg = DEFAULT_PARAMS;
if (!(cfg.minBuySol >= 0.01 && cfg.minBuySol <= cfg.earlyStakeSol && cfg.earlyStakeSol <= cfg.chaseStakeSol)) {
  throw new Error('entrypoint refusing to start: buy-size limits are not ordered smallest to largest');
}
if (
  !(
    cfg.buySlippageBps >= 0 &&
    cfg.buySlippageBps <= 10_000 &&
    cfg.sellSlippageBps >= 0 &&
    cfg.sellSlippageBps <= 10_000 &&
    cfg.earlyStop > 0 &&
    cfg.earlyStop < 1 &&
    cfg.chaseStop > 0 &&
    cfg.chaseStop < 1 &&
    cfg.earlyMaxHoldSlots > 0 &&
    cfg.chaseMaxHoldSlots > 0 &&
    cfg.earlyMaxOpen > 0 &&
    cfg.earlyMaxOpen <= cfg.maxOpen &&
    cfg.maxOpen > 0 &&
    cfg.chaseThresholdSol > 0 &&
    cfg.chaseMomentum >= 1
  )
) {
  throw new Error('entrypoint refusing to start: slippage, exit or position-count limit outside its usable range');
}

await runBot(createStrategy(cfg));
