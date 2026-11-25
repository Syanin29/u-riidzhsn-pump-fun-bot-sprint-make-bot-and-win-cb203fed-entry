"""GradChase + EarlyProbe strategy (Python mirror of strategy.ts).

Two sleeves, one buy per coin ever:
 A) EarlyProbe: tiny buys on young coins before they run up.
 B) GradChase: bigger buys when a curve organically crosses real-SOL 30
    (graduation leg: ~85 SOL / $69k-100k mcap), trailing the run to migration.
"""
import math
SOL = 1_000_000_000

DEFAULT = {
    # early sleeve
    'early_on': True, 'early_stake': 0.02, 'early_max_age': 60, 'early_min_trades': 3,
    'early_max_runup': 1.2, 'early_min_buyers': 2, 'early_max_creator_buy': 2.0,
    'early_min_real': 0.3, 'early_stop': 0.25, 'early_trail_arm': 0.40,
    'early_trail': 0.25, 'early_tp': float('inf'), 'early_max_hold': 150,
    'early_dev_sold_exit': True,
    # chase sleeve
    'chase_on': True, 'chase_thr': 30.0, 'chase_stake': 0.10, 'chase_min_trades': 10,
    'chase_max_cross_trade': 5.0, 'chase_min_buy_share': 0.5, 'chase_stop': 0.12,
    'chase_trail_arm': 0.15, 'chase_trail': 0.12, 'chase_tp': float('inf'),
    'chase_max_hold': 2000,
    'chase_add_thr': 0.0, 'chase_add_stake': 0.05,
    'chase_mom': 0.0, 'chase_min_cross_trades': 10, 'chase_min_cross_age': 0,
    # shared
    'min_buy': 0.01, 'buy_slip': 10000, 'sell_slip': 10000, 'max_open': 5,
    'early_max_open': 5,
}

class Strategy:
    def __init__(self, params=None):
        self.p = dict(DEFAULT)
        if params: self.p.update(params)
        self.coins = {}   # mint -> feature dict
        self.bought = set()
        self.pos = {}     # mint -> {'cost': lamports, 'tokens': int, 'slot': int, 'sleeve': str, 'peak': float, 'armed': bool}
        self.pending_buy = {}   # client_id -> (mint, sleeve, slot)
        self.added = set()
        self.dbg = {'eval': 0, 'early_pass': 0, 'chase_pass': 0, 'size_fail': 0, 'cash_fail': 0, 'age_fail': 0, 'trades_fail': 0, 'runup_fail': 0, 'buyers_fail': 0, 'csold_fail': 0, 'cbuy_fail': 0, 'real_fail': 0, 'no_create': 0}
        self.pending_sell = {}  # client_id -> mint
        self.nid = 0

    def _cid(self, prefix):
        self.nid += 1
        return f"{prefix}{self.nid}"

    def _coin(self, sim, m):
        c = self.coins.get(m)
        if c is None:
            sc = sim.coins.get(m)
            c = {'created_slot': sc['created_slot'] if sc else None,
                 'create_price': sc['create_price'] if sc else 0.0,
                 'creator': sc['creator'] if sc else None,
                 'trades': 0, 'buys': 0, 'buy_sol': 0.0, 'sell_sol': 0.0,
                 'buyers': set(), 'creator_buy': 0.0, 'creator_sold': False,
                 'price': 0.0, 'real_sol': 0.0, 'prev_real_sol': 0.0,
                 'last10': [], 'last_trade_sol': 0.0, 'last_is_buy': False,
                 'hist': []}
            self.coins[m] = c
        return c

    def on_batch(self, sim, slot, evs):
        p = self.p
        orders = []
        touched = set()
        for ev in evs:
            kind, m = ev[0], ev[1]
            c = self._coin(sim, m)
            touched.add(m)
            if kind == 'C':
                sc = sim.coins[m]
                c['created_slot'] = sc['created_slot']
                c['create_price'] = sc['create_price']
                c['creator'] = sc['creator']
            elif kind == 'T':
                vq, vt, rq = ev[2], ev[3], ev[4]
                c['prev_real_sol'] = c['real_sol']
                c['real_sol'] = rq / SOL
                c['price'] = vq / vt if vt else 0.0
                c['trades'] += 1
                c['last_trade_sol'] = ev[8] / SOL
                c['last_is_buy'] = ev[12]
                if ev[12]:
                    c['buys'] += 1; c['buy_sol'] += ev[8] / SOL
                    if ev[6] != c['creator']: c['buyers'].add(ev[6])
                    if ev[6] == c['creator']: c['creator_buy'] += ev[8] / SOL
                else:
                    c['sell_sol'] += ev[8] / SOL
                    if ev[6] == c['creator']: c['creator_sold'] = True
                c['last10'].append(ev[12])
                if len(c['last10']) > 10: c['last10'].pop(0)
                c['hist'].append((slot, c['price']))
                if len(c['hist']) > 60: c['hist'].pop(0)
            # CompleteEvent: nothing (sim force-closes)
        # ---- exits ----
        for mint in list(sim.positions.keys()):
            pos = sim.positions[mint]
            if mint in [v for v in self.pending_sell.values()]:
                continue
            st = self.pos.get(mint)
            if st is None:
                continue
            value = self._mark_value(sim, mint, pos)
            cost = st['cost']
            if value is None:
                continue
            if value > st['peak']: st['peak'] = value
            pre = 'early_' if st['sleeve'] == 'early' else 'chase_'
            stop, arm, trail, tp, mh = (self.p[pre + k] for k in ('stop', 'trail_arm', 'trail', 'tp', 'max_hold'))
            sell = False
            if value <= cost * (1 - stop): sell = True
            elif value >= cost * (1 + arm): st['armed'] = True
            if st['armed'] and value <= st['peak'] * (1 - trail): sell = True
            if value >= cost * (1 + tp): sell = True
            if slot - st['slot'] >= mh: sell = True
            if st['sleeve'] == 'early' and p['early_dev_sold_exit']:
                c = self.coins.get(mint)
                if c and c['creator_sold'] and slot > st['slot']: sell = True
            if sell:
                cid = self._cid('s')
                self.pending_sell[cid] = mint
                orders.append({'side': 'sell', 'mint': mint, 'token_amount': pos['tokens'],
                               'max_slippage_bps': p['sell_slip'], 'client_id': cid})
        # ---- entries ----
        open_n = len([1 for v in sim.positions.values() if v['tokens'] > 0]) + len(self.pending_buy)
        for m in touched:
            if open_n >= p['max_open']: break
            # sleeve-aware slot reservation is enforced at buy time below
            if m in self.bought and not (p['chase_add_thr'] > 0 and m in self.pos and self.pos[m]['sleeve'] == 'chase' and m not in self.added):
                continue
            c = self.coins.get(m)
            sc = sim.coins.get(m)
            if c is None or sc is None or sc['complete']: continue
            self.dbg['eval'] += 1
            buy = None
            # chase first (bigger, rarer)
            if p['chase_on'] and c['prev_real_sol'] < p['chase_thr'] <= c['real_sol']:
                mom_ok = True
                if p['chase_mom'] > 0:
                    old = [pr for (sl, pr) in c['hist'] if sl <= slot - 20]
                    mom_ok = bool(old) and c['price'] >= old[-1] * p['chase_mom']
                age_ok = c['created_slot'] is None or (slot - c['created_slot']) >= p['chase_min_cross_age']
                if (c['trades'] >= max(p['chase_min_trades'], p['chase_min_cross_trades'])
                        and c['last_trade_sol'] <= p['chase_max_cross_trade']
                        and sum(1 for b in c['last10'] if b) >= p['chase_min_buy_share'] * len(c['last10'])
                        and mom_ok and age_ok):
                    buy = ('chase', p['chase_stake'])
            # add-on: a held chase position whose coin crosses the add threshold
            if buy is None and p['chase_on'] and p['chase_add_thr'] > 0 and m in self.pos and self.pos[m]['sleeve'] == 'chase':
                if c['prev_real_sol'] < p['chase_add_thr'] <= c['real_sol'] and m not in self.added:
                    buy = ('chase_add', p['chase_add_stake'])
            elif p['early_on'] and c['created_slot'] is not None:
                age = slot - c['created_slot']
                if age > p['early_max_age']: self.dbg['age_fail'] += 1
                elif c['trades'] < p['early_min_trades']: self.dbg['trades_fail'] += 1
                elif c['create_price'] > 0 and c['price'] > c['create_price'] * p['early_max_runup']: self.dbg['runup_fail'] += 1
                elif len(c['buyers']) < p['early_min_buyers']: self.dbg['buyers_fail'] += 1
                elif c['creator_sold']: self.dbg['csold_fail'] += 1
                elif c['creator_buy'] > p['early_max_creator_buy']: self.dbg['cbuy_fail'] += 1
                elif c['real_sol'] < p['early_min_real']: self.dbg['real_fail'] += 1
                else:
                    self.dbg['early_pass'] += 1
                    buy = ('early', p['early_stake'])
            elif p['early_on']:
                self.dbg['no_create'] += 1
            if buy is None: continue
            sleeve, stake = buy
            if sleeve == 'early' and open_n >= p['early_max_open']: continue
            vq, vt, rq, rt = sim.eff_reserves(m)
            cap = rq * 100 // 10000  # 1% of real reserves
            amt = min(int(stake * SOL), cap)
            if amt < int(p['min_buy'] * SOL):
                self.dbg['size_fail'] += 1; continue
            if sim.free_cash() < amt + 105_000:
                self.dbg['cash_fail'] += 1; continue
            cid = self._cid('b')
            self.pending_buy[cid] = (m, sleeve, slot)
            self.bought.add(m)
            if sleeve == 'chase_add': self.added.add(m)
            orders.append({'side': 'buy', 'mint': m, 'quote_lamports': amt,
                           'max_slippage_bps': p['buy_slip'], 'client_id': cid})
            open_n += 1
            if len(orders) >= 16: break
        return orders

    def _mark_value(self, sim, mint, pos):
        c = sim.coins.get(mint)
        if c is None: return None
        vq, vt, rq, rt = sim.eff_reserves(mint)
        from sim import quote_sell, PROTO_BPS, CREATOR_BPS, TX_FEE
        q, fee, cfee, proceeds = quote_sell(pos['tokens'], vq, vt, PROTO_BPS, CREATOR_BPS)
        net = proceeds - TX_FEE
        return float(net)

    def on_fill(self, sim, order, status, info=None):
        cid = order.get('client_id', '')
        if cid in self.pending_buy:
            m, sleeve, eslot = self.pending_buy.pop(cid)
            if status == 'filled' and info:
                if sleeve == 'chase_add' and m in self.pos:
                    old = self.pos[m]
                    old['cost'] += info['paid']; old['tokens'] += info['tokens']
                    old['peak'] = max(old['peak'], float(old['cost']))
                else:
                    self.pos[m] = {'cost': info['paid'], 'tokens': info['tokens'],
                                   'slot': eslot, 'sleeve': sleeve,
                                   'peak': float(info['paid']), 'armed': False}
        if cid in self.pending_sell:
            m = self.pending_sell.pop(cid)
            if status == 'filled':
                self.pos.pop(m, None)
            # failed/rejected: the position is STILL OPEN (nothing traded).
            # Keep it in self.pos so the exit is retried next batch; popping
            # it here would orphan the slot for the rest of the window.
        # forced closes (client_id starts with _)
        if cid.startswith('_') and info and info.get('forced'):
            self.pos.pop(order['mint'], None)
