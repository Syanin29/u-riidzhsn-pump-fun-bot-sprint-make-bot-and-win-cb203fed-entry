// GradChase + EarlyProbe — a pump.fun paper-trading bot for agent-fun/replay/v2.
//
// Thesis (measured on the kit's dev window: 1222 coins, 24h, 128,058 events,
// split into four 6h eras for walk-forward checks; full numbers in BACKTEST.md):
//
//   - Most coins go nowhere. Median coin ends ~0.92x where it started; only
//     ~22% ever 2x, ~7% ever 5x. Buying AFTER a run-up loses, and the loss
//     grows with the run-up. Buying at the open is the only entry with a flat
//     forward median, so the early sleeve buys young coins small, before they
//     run, and cuts losers fast.
//   - The graduation leg is where the money is. Conditional on a curve's real
//     SOL crossing 30 (graduation is ~85 SOL / $69k-$100k mcap), 29% graduate;
//     conditional on crossing 40, 41% do. But crossing alone is not enough:
//     buying every cross loses (~-0.09 SOL/era). The cross only pays when the
//     coin is ALSO in a strong uptrend right now (price +15% over the last
//     ~20 slots) and the recent flow is buy-dominant (>=80% of the last 10
//     trades are buys). With those two filters the chase sleeve is positive
//     in 3 of 4 eras and the one graduation it catches pays +240%.
//   - Sizing follows the harness: a buy may spend at most 1% of the curve's
//     real SOL reserves per batch, so early probes are ~0.01-0.02 SOL and
//     chase buys are 0.25 SOL (the 1% cap at the 30 SOL trigger is 0.30). Every round trip pays ~1.25% curve fees each
//     way + 105,000 lamports tx fee each way, so stakes below 0.01 SOL are
//     never sent and exits must clear that bar.
//   - Two sleeves, one buy per coin ever:
//       A) EarlyProbe (breadth + guardrail insurance): tiny buys on young
//          coins (age <= 60 slots, >= 3 trades, price <= 1.2x create price,
//          >= 2 distinct non-creator buyers, creator has not sold, creator
//          buys <= 2 SOL, real SOL >= 0.3). Stop -25%, trail armed at +40%
//          with a 25% trail-back, 150-slot max hold. At most 3 early
//          positions open at once, reserving 2 of the 5 slots for chases.
//       B) GradChase (alpha): bigger buys when real SOL crosses 30 AND price
//          is +15% over ~20 slots ago AND >= 80% of the last 10 trades are
//          buys AND the crossing trade is <= 5 SOL (not one whale print).
//          Stop -20%, trail armed at +15% with a 25% trail-back (wide enough
//          to ride a graduation run through its pullbacks), 2000-slot
//          max hold (usually exits by trail, or the harness closes the
//          position when the curve completes/migrates).
//   - Rejected by sweep, left out: pyramiding/add-ons into strength (worse
//     at every setting), buying dips/mean reversion (-3.9 SOL house pilot),
//     momentum entries without the graduation filter (-9.5 SOL house pilot),
//     no-stop holds on chases (worse in 3 of 4 eras), chase thresholds 25
//     and 35 SOL (both much worse than 30 — the 30 SOL leg is structural).
//
// Walk-forward (own lab, faithful port of the harness sim; see tools/):
//   eras (6h each)  +0.281 / -0.009 / +0.625 / +0.489 SOL, sum +1.386
//   full 24h window +1.400 SOL on 10 SOL, 215 coins, 20% win rate,
//   max DD 2.9%. Harness ground truth (`grade-local --driver process`):
//   +1.380 SOL, 219 coins, 219 round trips, 22% win rate, max DD 2.9%,
//   0 rejected, 7 failed tx, guardrails met, L1 regrade byte-identical,
//   90% interval +0.08..+3.11 SOL, +1.12 SOL even at 2x fees.
//   Guardrails: 39-71 coins/era with round trips >= 0.01 SOL
//   (bar: 20), max drawdown <= 2.9% (bar: 50%). The era wins are a handful
//   of graduations (the biggest single round trip is +0.6 SOL on a 0.25 SOL
//   chase buy); without a graduation in a window the bot loses ~0.01 SOL
//   to fees in the lab. That is the honest shape of this edge: small fee
//   bleed on flat windows, one graduation pays for it many times over. The
//   `grade-local --stress` resampling (20 rebuilt copies of the dev window)
//   says the same thing from the other side: median -0.42 SOL, 15 of 20
//   below zero, but the dev score beats all 20 copies (rank 20/20) — the
//   copies re-draw each coin's flow from state-conditioned donor blocks,
//   which breaks the within-coin persistence a graduation run needs, so
//   they understate a graduation-chaser on real data; the real 6h eras
//   above are the better predictor of the fresh final window, and they say
//   3 of 4 eras pay. The final window is a fresh draw — nothing here
//   guarantees it contains one, and every parameter above was selected on
//   this one dev window, so the true edge is smaller than these numbers.
//   See BACKTEST.md for the variants that were tried and rejected, and for
//   the failed-exit bug the sweep caught (a failed sell must be retried,
//   never forgotten — forgetting it orphaned the position slot for the
//   rest of the window).
//
// Deterministic by construction: no clock, no randomness, no I/O, no model
// or LLM calls, no network, no real money. Iteration only over
// insertion-ordered Maps/Sets fed by the batch stream, so the grader's two
// executions produce byte-identical action logs.

import type { Batch, Bot, BotState, OrderInput, Pubkey } from './vendor/makefun-bot.ts';

export interface Params {
  // Early sleeve.
  earlyStakeSol: number;
  earlyMaxAgeSlots: number;
  earlyMinTrades: number;
  earlyMaxRunup: number;
  earlyMinBuyers: number;
  earlyMaxCreatorBuySol: number;
  earlyMinRealSol: number;
  earlyStop: number;
  earlyTrailArm: number;
  earlyTrail: number;
  earlyMaxHoldSlots: number;
  earlyDevSoldExit: boolean;
  earlyMaxOpen: number;
  // Chase sleeve.
  chaseThresholdSol: number;
  chaseStakeSol: number;
  chaseMinTrades: number;
  chaseMaxCrossTradeSol: number;
  chaseMinBuyShare: number;
  chaseMomentum: number;
  chaseStop: number;
  chaseTrailArm: number;
  chaseTrail: number;
  chaseMaxHoldSlots: number;
  // Shared.
  minBuySol: number;
  buySlippageBps: number;
  sellSlippageBps: number;
  maxOpen: number;
}

export const DEFAULT_PARAMS: Params = {
  earlyStakeSol: 0.02,
  earlyMaxAgeSlots: 60,
  earlyMinTrades: 3,
  earlyMaxRunup: 1.2,
  earlyMinBuyers: 2,
  earlyMaxCreatorBuySol: 2.0,
  earlyMinRealSol: 0.3,
  earlyStop: 0.25,
  earlyTrailArm: 0.4,
  earlyTrail: 0.25,
  earlyMaxHoldSlots: 150,
  earlyDevSoldExit: true,
  earlyMaxOpen: 3,
  chaseThresholdSol: 30,
  chaseStakeSol: 0.25,
  chaseMinTrades: 10,
  chaseMaxCrossTradeSol: 5.0,
  chaseMinBuyShare: 0.8,
  chaseMomentum: 1.15,
  chaseStop: 0.2,
  chaseTrailArm: 0.15,
  chaseTrail: 0.25,
  chaseMaxHoldSlots: 2000,
  minBuySol: 0.01,
  buySlippageBps: 10_000,
  sellSlippageBps: 10_000,
  maxOpen: 5,
};

const LAMPORTS_PER_SOL = 1_000_000_000n;
const TX_FEE_LAMPORTS = 105_000n; // base 5,000 + priority 100,000 per order.
const PROTO_BPS = 95n;
const CREATOR_BPS = 30n;
const BPS = 10_000n;

function ceilDiv(a: bigint, b: bigint): bigint {
  return (a + b - 1n) / b;
}

/** Net SOL (lamports) a full sale of `tokens` would return at these reserves. */
function sellProceeds(tokens: bigint, vq: bigint, vt: bigint): bigint {
  const q = (tokens * vq) / (vt + tokens);
  const fee = ceilDiv(q * PROTO_BPS, BPS);
  const creatorFee = ceilDiv(q * CREATOR_BPS, BPS);
  return q - fee - creatorFee - TX_FEE_LAMPORTS;
}

interface CoinFeatures {
  createdSlot: number | null;
  createPrice: number;
  creator: Pubkey | null;
  trades: number;
  buyers: Set<Pubkey>;
  creatorBuyLamports: bigint;
  creatorSold: boolean;
  price: number;
  realSol: number;
  prevRealSol: number;
  last10Buys: boolean[];
  lastTradeSol: number;
  /** (slot, price) of recent trades, oldest first, capped at 60. */
  hist: Array<[number, number]>;
}

interface HeldPosition {
  costLamports: bigint;
  sleeve: 'early' | 'chase';
  entrySlot: number;
  peakValueLamports: bigint;
  armed: boolean;
}

export function createStrategy(params: Params): Bot {
  const coins = new Map<Pubkey, CoinFeatures>();
  const bought = new Set<Pubkey>();
  const held = new Map<Pubkey, HeldPosition>();
  const pendingBuy = new Map<string, { mint: Pubkey; sleeve: 'early' | 'chase'; slot: number }>();
  const pendingSell = new Map<string, Pubkey>();
  let nextId = 1;

  const freshCoin = (state: BotState, mint: Pubkey): CoinFeatures => {
    const curve = state.curves.get(mint);
    const c: CoinFeatures = {
      createdSlot: curve?.created_slot ?? null,
      createPrice:
        curve && curve.virtual_token_reserves > 0n
          ? Number(curve.virtual_quote_reserves) / Number(curve.virtual_token_reserves)
          : 0,
      creator: null,
      trades: 0,
      buyers: new Set(),
      creatorBuyLamports: 0n,
      creatorSold: false,
      price: 0,
      realSol: 0,
      prevRealSol: 0,
      last10Buys: [],
      lastTradeSol: 0,
      hist: [],
    };
    coins.set(mint, c);
    return c;
  };

  const coinOf = (state: BotState, mint: Pubkey): CoinFeatures =>
    coins.get(mint) ?? freshCoin(state, mint);

  /** Fold this batch's fills into the held-position map. */
  const applyFills = (state: BotState, batch: Batch): void => {
    for (const fill of batch.fills) {
      const buy = pendingBuy.get(fill.client_id);
      if (buy !== undefined) {
        pendingBuy.delete(fill.client_id);
        if (fill.status === 'filled') {
          const existing = held.get(buy.mint);
          const paid = BigInt(fill.quote_lamports);
          if (existing !== undefined) {
            // Add-on to an open position (not used by the current params,
            // kept so a future param change cannot corrupt the book).
            existing.costLamports += paid;
            if (paid > existing.peakValueLamports) existing.peakValueLamports = paid;
          } else {
            held.set(buy.mint, {
              costLamports: paid,
              sleeve: buy.sleeve,
              entrySlot: buy.slot,
              peakValueLamports: paid,
              armed: false,
            });
          }
        }
        continue;
      }
      const sellMint = pendingSell.get(fill.client_id);
      if (sellMint !== undefined) {
        pendingSell.delete(fill.client_id);
        // Only a filled sell closes the position. A failed sell traded
        // nothing and a rejected sell changed nothing: the position is
        // still open, so keep it in `held` — deleting it here would orphan
        // the slot for the rest of the window (a failed exit must be
        // retried on the next batch, not forgotten).
        if (fill.status === 'filled') held.delete(sellMint);
        continue;
      }
      // Harness closes (_complete_ / _flatten_ / _writeoff_) arrive with no
      // pending order: the position is gone either way.
      if (fill.client_id.startsWith('_')) held.delete(fill.mint);
    }
    // Reconcile with the authoritative account: a position the engine no
    // longer holds is closed; cost comes from the engine, not our memory.
    for (const mint of [...held.keys()]) {
      if (!state.positions.has(mint)) held.delete(mint);
    }
  };

  const markValue = (state: BotState, mint: Pubkey): bigint | null => {
    const curve = state.curves.get(mint);
    const pos = state.positions.get(mint);
    if (curve === undefined || pos === undefined) return null;
    return sellProceeds(pos.token_amount, curve.virtual_quote_reserves, curve.virtual_token_reserves);
  };

  return {
    onBatch(state: BotState, batch: Batch): OrderInput[] {
      applyFills(state, batch);
      const orders: OrderInput[] = [];
      const touched = new Set<Pubkey>();

      for (const ev of batch.events) {
        const c = coinOf(state, ev.mint);
        touched.add(ev.mint);
        if (ev.event === 'CreateEvent') {
          // Use the EVENT's own reserves, never the curve's current state:
          // applyBatch has already folded this whole batch (including later
          // trades in the same slot) into state.curves, so reading the curve
          // here would price the creation at the post-launch price and let
          // run-ups through the filter. The event carries the true initial
          // reserves.
          c.createdSlot = ev.slot;
          c.createPrice =
            BigInt(ev.virtual_token_reserves) > 0n
              ? Number(ev.virtual_quote_reserves) / Number(ev.virtual_token_reserves)
              : c.createPrice;
          c.creator = ev.creator;
        } else if (ev.event === 'TradeEvent') {
          c.prevRealSol = c.realSol;
          c.realSol = Number(ev.real_quote_reserves) / Number(LAMPORTS_PER_SOL);
          c.price =
            BigInt(ev.virtual_token_reserves) > 0n
              ? Number(ev.virtual_quote_reserves) / Number(ev.virtual_token_reserves)
              : c.price;
          c.trades += 1;
          c.lastTradeSol = Number(ev.sol_amount) / Number(LAMPORTS_PER_SOL);
          if (ev.is_buy) {
            if (ev.user !== c.creator) c.buyers.add(ev.user);
            if (ev.user === c.creator) c.creatorBuyLamports += BigInt(ev.sol_amount);
          } else {
            if (ev.user === c.creator) c.creatorSold = true;
          }
          c.last10Buys.push(ev.is_buy);
          if (c.last10Buys.length > 10) c.last10Buys.shift();
          c.hist.push([ev.slot, c.price]);
          if (c.hist.length > 60) c.hist.shift();
        }
        // CompleteEvent: the harness force-closes any position; nothing to do.
      }

      // ---- exits ----
      for (const [mint, pos] of [...state.positions.entries()]) {
        if ([...pendingSell.values()].includes(mint)) continue;
        const st = held.get(mint);
        if (st === undefined) continue;
        const value = markValue(state, mint);
        if (value === null) continue;
        if (value > st.peakValueLamports) st.peakValueLamports = value;
        const pre = st.sleeve === 'early' ? 'early' : 'chase';
        const stop = pre === 'early' ? params.earlyStop : params.chaseStop;
        const arm = pre === 'early' ? params.earlyTrailArm : params.chaseTrailArm;
        const trail = pre === 'early' ? params.earlyTrail : params.chaseTrail;
        const maxHold = pre === 'early' ? params.earlyMaxHoldSlots : params.chaseMaxHoldSlots;
        const cost = st.costLamports;
        // Work in scaled integers to avoid float drift: compare value/cost
        // against (1 +/- x) with x in thousandths.
        const stopBps = BigInt(Math.round(stop * 10_000));
        const armBps = BigInt(Math.round(arm * 10_000));
        const trailBps = BigInt(Math.round(trail * 10_000));
        let sell = false;
        if (value * BPS <= cost * (BPS - stopBps)) sell = true;
        else if (value * BPS >= cost * (BPS + armBps)) st.armed = true;
        if (st.armed && value * BPS <= st.peakValueLamports * (BPS - trailBps)) sell = true;
        if (batch.slot - st.entrySlot >= maxHold) sell = true;
        if (st.sleeve === 'early' && params.earlyDevSoldExit) {
          const c = coins.get(mint);
          if (c !== undefined && c.creatorSold && batch.slot > st.entrySlot) sell = true;
        }
        if (sell) {
          const cid = `s${nextId++}`;
          pendingSell.set(cid, mint);
          orders.push({
            side: 'sell',
            mint,
            token_amount: pos.token_amount,
            max_slippage_bps: params.sellSlippageBps,
            client_id: cid,
          });
        }
      }

      // ---- entries ----
      let openCount =
        [...state.positions.values()].filter((p) => p.token_amount > 0n).length + pendingBuy.size;
      for (const mint of touched) {
        if (openCount >= params.maxOpen) break;
        if (bought.has(mint)) continue;
        const c = coins.get(mint);
        const curve = state.curves.get(mint);
        if (c === undefined || curve === undefined || curve.complete) continue;
        let sleeve: 'early' | 'chase' | null = null;
        let stakeSol = 0;

        // Chase first: bigger, rarer, and the only sleeve with a real edge.
        if (
          c.prevRealSol < params.chaseThresholdSol &&
          params.chaseThresholdSol <= c.realSol
        ) {
          let momentumOk = true;
          if (params.chaseMomentum > 0) {
            const old = c.hist.filter(([slot]) => slot <= batch.slot - 20);
            momentumOk =
              old.length > 0 && c.price >= old[old.length - 1][1] * params.chaseMomentum;
          }
          const buyCount = c.last10Buys.filter(Boolean).length;
          const buyShareOk =
            c.last10Buys.length > 0 &&
            buyCount >= params.chaseMinBuyShare * c.last10Buys.length;
          if (
            c.trades >= params.chaseMinTrades &&
            c.lastTradeSol <= params.chaseMaxCrossTradeSol &&
            buyShareOk &&
            momentumOk
          ) {
            sleeve = 'chase';
            stakeSol = params.chaseStakeSol;
          }
        }
        if (sleeve === null && c.createdSlot !== null) {
          const age = batch.slot - c.createdSlot;
          if (
            age <= params.earlyMaxAgeSlots &&
            c.trades >= params.earlyMinTrades &&
            c.createPrice > 0 &&
            c.price <= c.createPrice * params.earlyMaxRunup &&
            c.buyers.size >= params.earlyMinBuyers &&
            !c.creatorSold &&
            c.creatorBuyLamports <= BigInt(Math.round(params.earlyMaxCreatorBuySol * 1_000_000_000)) &&
            c.realSol >= params.earlyMinRealSol
          ) {
            sleeve = 'early';
            stakeSol = params.earlyStakeSol;
          }
        }
        if (sleeve === null) continue;
        if (sleeve === 'early' && openCount >= params.earlyMaxOpen) continue;
        // Size cap: at most 1% of the curve's real SOL reserves per batch
        // (hello.caps.max_order_bps_of_real_reserve = 100 bps).
        const cap = (curve.real_quote_reserves * 100n) / BPS;
        const stake = BigInt(Math.round(stakeSol * 1_000_000_000));
        const amount = stake < cap ? stake : cap;
        const minBuy = BigInt(Math.round(params.minBuySol * 1_000_000_000));
        if (amount < minBuy) continue;
        if (state.freeCashLamports() < amount + TX_FEE_LAMPORTS) continue;
        const cid = `b${nextId++}`;
        pendingBuy.set(cid, { mint, sleeve, slot: batch.slot });
        bought.add(mint);
        orders.push({
          side: 'buy',
          mint,
          quote_lamports: amount,
          max_slippage_bps: params.buySlippageBps,
          client_id: cid,
        });
        openCount += 1;
        if (orders.length >= 16) break;
      }
      return orders;
    },
  };
}
