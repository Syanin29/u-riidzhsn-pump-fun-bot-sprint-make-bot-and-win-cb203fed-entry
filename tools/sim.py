"""Faithful Python port of the agent-fun/replay/v2 engine (house harness-src/sim).

Mirrors curve.ts fill math and engine.ts resolution order, with two deliberate
approximations (documented): the bot learns of a fill in the same slot it
resolves (real bot: first batch after), and the 2% failed-tx draw uses a
sha256 of (seq,index) instead of the run seed's fail key. Both move aggregate
PnL by < ~0.005 SOL on this window (checked against grade-local).
"""
import gzip, json, glob, hashlib, math
from collections import defaultdict

LAMPORTS_PER_SOL = 1_000_000_000
BPS = 10_000
TX_FEE = 105_000  # base 5000 + priority 100000
PROTO_BPS = 95
CREATOR_BPS = 30
FAILED_TX_BPS = 200
MAX_OPEN = 5
MAX_ORDER_BPS = 100  # 1% of real reserves
MAX_ORDERS_PER_BATCH = 16
CAPITAL = 10 * LAMPORTS_PER_SOL
LATENCY = 2

def ceil_div(a, b):
    return -(-a // b)

def load_dataset():
    K = '/home/hatch/workspace/makefun/pumpbot-kit/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed-kit'
    files = sorted(glob.glob(f'{K}/dataset/events/*.ndjson.gz'))
    slots = defaultdict(list)  # slot -> [event tuples]
    for f in files:
        with gzip.open(f, 'rt') as fh:
            for line in fh:
                e = json.loads(line)
                m = e['mint']
                if e['event'] == 'CreateEvent':
                    slots[e['slot']].append(('C', m, int(e['virtual_quote_reserves']),
                        int(e['virtual_token_reserves']), 0, int(e['real_token_reserves']),
                        e['user'], e['creator'], 0, int(e['timestamp']), 0, 0))
                elif e['event'] == 'TradeEvent':
                    slots[e['slot']].append(('T', m, int(e['virtual_quote_reserves']),
                        int(e['virtual_token_reserves']), int(e['real_quote_reserves']),
                        int(e['real_token_reserves']), e['user'], e['creator'],
                        int(e['sol_amount']), int(e['timestamp']),
                        int(e['fee_basis_points']), int(e['creator_fee_basis_points']),
                        bool(e['is_buy'])))
                else:
                    slots[e['slot']].append(('X', m, 0, 0, 0, 0, e['user'], None, 0, int(e['timestamp']), 0, 0, False))
    return slots

def exact_in_split(S, pb, cb):
    net0 = S * BPS // (BPS + pb + cb)
    fee = ceil_div(net0 * pb, BPS)
    cfee = ceil_div(net0 * cb, BPS)
    over = net0 + fee + cfee - S
    if over < 0: over = 0
    return net0 - over, fee, cfee

def quote_buy(S, vq, vt, rt, pb, cb):
    q, fee, cfee = exact_in_split(S, pb, cb)
    if q <= 1:
        return None
    t = (q - 1) * vt // (vq + q - 1)
    if t > rt:
        tc = rt
        if tc == 0: return None
        q = (tc * vq) // (vt - tc) + 1
        fee = ceil_div(q * pb, BPS); cfee = ceil_div(q * cb, BPS)
        return q, tc, fee, cfee, q + fee + cfee
    return q, t, fee, cfee, q + fee + cfee

def quote_sell(t, vq, vt, pb, cb):
    q = t * vq // (vt + t)
    fee = ceil_div(q * pb, BPS); cfee = ceil_div(q * cb, BPS)
    return q, fee, cfee, q - fee - cfee

def tx_fails(seq, index):
    h = hashlib.sha256(f"fail:{seq}:{index}".encode()).digest()
    return int.from_bytes(h[:4], 'big') % BPS < FAILED_TX_BPS

class Sim:
    def __init__(self, slots, strategy, t_start=None, t_end=None):
        self.slots = slots
        self.strategy = strategy
        self.t_start = t_start; self.t_end = t_end
        self.cash = CAPITAL
        self.positions = {}  # mint -> {'tokens': int, 'cost': int}
        self.pending = []    # dicts with fill_slot
        self.coins = {}      # mint -> state dict
        self.seq = 0
        self.fills = 0; self.rejected = 0; self.failed = 0
        self.round_trips = []  # (mint, bought_lamports, pnl_lamports)
        self.open_rt = {}    # mint -> {'bought': int, 'pnl': int}
        self.equity_pts = [CAPITAL]
        self.peak = CAPITAL
        self.max_dd = 0.0
        self.orders_sent = 0

    # ---- reserves ----
    def eff_reserves(self, m):
        c = self.coins[m]
        oq, ot = c['off_q'], c['off_t']
        rq, rt = c['rq'], c['rt']
        num, den = 1, 1
        if ot > rt:
            num, den = rt, ot
        if oq < 0 and rq * den < num * (-oq):
            num, den = rq, -oq
        def scale(x):
            if num == den: return x
            return (x * num) // den if x >= 0 else -((-x * num) // den)
        q, t = scale(oq), scale(ot)
        return c['vq'] + q, c['vt'] - t, c['rq'] + q, c['rt'] - t

    def equity_now(self):
        eq = self.cash
        for mint, pos in self.positions.items():
            if mint not in self.coins: continue
            vq, vt, rq, rt = self.eff_reserves(mint)
            q, fee, cfee, proceeds = quote_sell(pos['tokens'], vq, vt, PROTO_BPS, CREATOR_BPS)
            net = proceeds - TX_FEE
            if net > 0: eq += net
        return eq

    def _track_equity(self):
        eq = self.equity_now()
        self.equity_pts.append(eq)
        if eq > self.peak: self.peak = eq
        dd = (self.peak - eq) / self.peak if self.peak > 0 else 0
        if dd > self.max_dd: self.max_dd = dd

    def run(self):
        slot_list = sorted(self.slots.keys())
        for slot in slot_list:
            evs = self.slots[slot]
            # era filter by first event timestamp
            ts = evs[0][9]
            if self.t_start is not None and ts < self.t_start: continue
            if self.t_end is not None and ts >= self.t_end: break
            self._apply_events(slot, evs)
            self._resolve_pending(slot)
            self._force_closes(slot, evs)
            orders = self.strategy.on_batch(self, slot, evs) or []
            orders = orders[:MAX_ORDERS_PER_BATCH]
            self._submit(slot, orders)
            self._track_equity()
        # flatten
        for mint in list(self.positions.keys()):
            self._force_close(mint, 'flatten', slot_list[-1])
        self._track_equity()
        return self._report()

    def _apply_events(self, slot, evs):
        for ev in evs:
            kind = ev[0]; m = ev[1]
            if kind == 'C':
                c = self.coins.get(m)
                if c is None:
                    self.coins[m] = {'vq': ev[2], 'vt': ev[3], 'rq': 0, 'rt': ev[5],
                        'off_q': 0, 'off_t': 0, 'complete': False, 'created_slot': slot,
                        'create_price': ev[2]/ev[3] if ev[3] else 0.0, 'creator': ev[7],
                        'last_fee_bps': (PROTO_BPS, CREATOR_BPS), 'last_trade_slot': slot}
                # else: duplicate create (shouldn't happen); keep first
            elif kind == 'T':
                c = self.coins.get(m)
                if c is None:
                    self.coins[m] = {'vq': ev[2], 'vt': ev[3], 'rq': ev[4], 'rt': ev[5],
                        'off_q': 0, 'off_t': 0, 'complete': False, 'created_slot': None,
                        'create_price': ev[2]/ev[3] if ev[3] else 0.0, 'creator': ev[7],
                        'last_fee_bps': (ev[10], ev[11]), 'last_trade_slot': slot}
                else:
                    c['vq'], c['vt'], c['rq'], c['rt'] = ev[2], ev[3], ev[4], ev[5]
                    c['last_fee_bps'] = (ev[10], ev[11])
                    c['last_trade_slot'] = slot
            else:  # CompleteEvent
                c = self.coins.get(m)
                if c is None:
                    self.coins[m] = {'vq': 0, 'vt': 0, 'rq': 0, 'rt': 0, 'off_q': 0, 'off_t': 0,
                        'complete': True, 'created_slot': None, 'create_price': 0.0,
                        'creator': None, 'last_fee_bps': (PROTO_BPS, CREATOR_BPS), 'last_trade_slot': slot}
                else:
                    c['complete'] = True

    def _resolve_pending(self, slot):
        due = [p for p in self.pending if p['fill_slot'] <= slot]
        self.pending = [p for p in self.pending if p['fill_slot'] > slot]
        for p in due:
            self._resolve_one(p, slot)

    def _resolve_one(self, p, slot):
        m = p['mint']
        c = self.coins.get(m)
        if c is None or c['complete']:
            self.rejected += 1
            self.strategy.on_fill(self, p, 'rejected')
            return
        vq, vt, rq, rt = self.eff_reserves(m)
        pb, cb = c['last_fee_bps']
        if p['side'] == 'sell':
            pos = self.positions.get(m)
            t = p['token_amount']
            if pos is None or pos['tokens'] < t:
                self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
            q, fee, cfee, proceeds = quote_sell(t, vq, vt, pb, cb)
            if q > rq or proceeds <= 0:
                self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
            exp_q, _, _, exp_proc = quote_sell(t, p['dec_vq'], p['dec_vt'], pb, cb)
            if proceeds * BPS < exp_proc * (BPS - p['max_slippage_bps']):
                self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
            if self.cash < TX_FEE:
                self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
            if tx_fails(p['seq'], p['index']):
                self.cash -= TX_FEE; self.failed += 1
                self._rt_pnl(m, -TX_FEE)
                self.strategy.on_fill(self, p, 'failed'); return
            self.cash += proceeds - TX_FEE
            removed = pos['cost'] if pos['tokens'] == t else pos['cost'] * t // pos['tokens']
            pos['tokens'] -= t; pos['cost'] -= removed
            c['off_q'] -= q; c['off_t'] -= t
            self._rt_pnl(m, proceeds - TX_FEE)
            self.fills += 1
            self.strategy.on_fill(self, p, 'filled', {'proceeds': proceeds, 'tokens': t})
            if pos['tokens'] == 0:
                del self.positions[m]
                self._close_rt(m)
            return
        # buy
        spend = p['quote_lamports']
        held = self.positions.get(m)
        if (held is None or held['tokens'] == 0) and sum(1 for v in self.positions.values() if v['tokens'] > 0) >= MAX_OPEN:
            self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
        if p['spent_total'] * BPS > p['dec_rq'] * MAX_ORDER_BPS:
            self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
        bq = quote_buy(spend, vq, vt, rt, pb, cb)
        if bq is None:
            self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
        q, t, fee, cfee, paid = bq
        exp = quote_buy(spend, p['dec_vq'], p['dec_vt'], p['dec_rt'], pb, cb)
        if exp is None:
            self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
        if t * BPS < exp[1] * (BPS - p['max_slippage_bps']):
            self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
        if self.cash < spend + TX_FEE:
            self.rejected += 1; self.strategy.on_fill(self, p, 'rejected'); return
        if tx_fails(p['seq'], p['index']):
            self.cash -= TX_FEE; self.failed += 1
            self._rt_pnl(m, -TX_FEE)
            self.strategy.on_fill(self, p, 'failed'); return
        self.cash -= paid + TX_FEE
        if held is None:
            held = {'tokens': 0, 'cost': 0}
            self.positions[m] = held
            if m not in self.open_rt:
                self.open_rt[m] = {'bought': 0, 'pnl': 0}
        held['tokens'] += t; held['cost'] += paid
        c['off_q'] += q; c['off_t'] += t
        self.open_rt[m]['bought'] += paid
        self._rt_pnl(m, -(paid + TX_FEE))
        self.fills += 1
        self.strategy.on_fill(self, p, 'filled', {'paid': paid, 'tokens': t})

    def _rt_pnl(self, m, delta):
        if m in self.open_rt:
            self.open_rt[m]['pnl'] += delta

    def _close_rt(self, m):
        rt = self.open_rt.pop(m, None)
        if rt is not None:
            self.round_trips.append((m, rt['bought'], rt['pnl']))

    def _force_closes(self, slot, evs):
        for ev in evs:
            if ev[0] == 'X' and ev[1] in self.positions:
                self._force_close(ev[1], 'complete', slot)

    def _force_close(self, m, why, slot):
        pos = self.positions.get(m)
        if pos is None: return
        c = self.coins.get(m)
        vq, vt, rq, rt = self.eff_reserves(m) if c else (0, 1, 0, 0)
        pb, cb = c['last_fee_bps'] if c else (PROTO_BPS, CREATOR_BPS)
        q, fee, cfee, proceeds = quote_sell(pos['tokens'], vq, vt, pb, cb)
        net = proceeds - TX_FEE
        if net <= 0:
            # writeoff: tokens abandoned, nothing paid
            self._rt_pnl(m, 0)
        else:
            self.cash += net
            c['off_q'] -= q; c['off_t'] -= pos['tokens']
            self._rt_pnl(m, net)
        t = pos['tokens']
        del self.positions[m]
        self.strategy.on_fill(self, {'mint': m, 'side': 'sell', 'client_id': f'_{why}'}, 'filled',
                              {'proceeds': max(net, 0), 'tokens': t, 'forced': why})
        self._close_rt(m)

    def _submit(self, slot, orders):
        spent_by_mint = defaultdict(int)
        for i, o in enumerate(orders):
            m = o['mint']
            c = self.coins.get(m)
            if c is None or c['complete']:
                self.rejected += 1; continue
            vq, vt, rq, rt = self.eff_reserves(m)
            p = dict(o)
            p['seq'] = self.seq; p['index'] = i
            p['fill_slot'] = slot + LATENCY
            p['dec_vq'], p['dec_vt'], p['dec_rq'], p['dec_rt'] = vq, vt, rq, rt
            if o['side'] == 'buy':
                spent_by_mint[m] += o['quote_lamports']
                p['spent_total'] = spent_by_mint[m]
            self.pending.append(p)
            self.orders_sent += 1
        self.seq += 1

    def free_cash(self):
        free = self.cash
        for p in self.pending:
            free -= TX_FEE
            if p['side'] == 'buy':
                free -= p['quote_lamports']
        return free

    def _report(self):
        pnl = self.cash - CAPITAL
        rt_ok = [r for r in self.round_trips if r[1] >= 10_000_000]
        coins_ok = len(set(r[0] for r in rt_ok))
        wins = sum(1 for r in self.round_trips if r[2] > 0)
        return {'pnl_sol': pnl / LAMPORTS_PER_SOL, 'cash_sol': self.cash / LAMPORTS_PER_SOL,
                'fills': self.fills, 'rejected': self.rejected, 'failed': self.failed,
                'round_trips': len(self.round_trips), 'coins_ge_0.01': coins_ok,
                'win_rate': wins / max(1, len(self.round_trips)),
                'max_dd': self.max_dd, 'orders_sent': self.orders_sent,
                'guardrails': coins_ok >= 20 and self.max_dd <= 0.5}
