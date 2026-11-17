"""Credit (official booking) vs Manual (self-booked) reconciliation of live trades, by RISK bucket.

Trade counts need not match (2 x 50mm of HY39 protection == 1 x 100mm); signed notional, PV and
Spread01 summed per risk bucket must. Trades are netted per (product type, bucket); a bucket whose trades
did not all load/price is flagged INCOMPLETE rather than a break.

    import credit_manual_rec as rec
    live = rec.live_static(all_trades, AS_OF)                 # Done, live, strategy in Credit/Manual
    px   = rec.load_pricers(live, AS_OF, CALC_ENV)             # from the pricer cache
    risk = rec.trade_risk(live, px)                            # per trade: notional, PV, Spread01
    rep  = rec.reconcile(risk)                                 # per bucket: Credit | Manual | diff | status
"""
import ast
import time

import pandas as pd

import pricer_cache as pc

STRATS = ('Credit', 'Manual')


def _name(r, key):
    """Name of a nested DSP object: flattened 'key.Name' column, or dict / its str() repr (CSV export)."""
    if r.get(key + '.Name') is not None and pd.notna(r.get(key + '.Name')):
        return r[key + '.Name']
    v = r.get(key)
    if isinstance(v, str) and v.startswith('{'):
        try:
            v = ast.literal_eval(v)
        except Exception:
            return v
    return v.get('Name') if isinstance(v, dict) else v


def _g(r, key):
    return r.get(key)


def bucket(r):
    """(ptype, bucket description) -- the fields that drive RISK, not PV-only terms like coupon/counterparty."""
    t = str(r['Product.$type'])
    mat = pd.Timestamp(r.get('Product.Maturity')).date() if pd.notna(r.get('Product.Maturity')) else None
    if t == 'CDX':
        k = (r['Product.Name'],)
    elif t == 'CDS':
        k = (_name(r, 'Product.ReferenceEntity'), r.get('Product.Seniority'), mat)
    elif t == 'CDXOption':
        k = (_name(r, 'Product.Underlying'), r.get('Product.Type'), r.get('Product.Strike'),
             pd.Timestamp(r.get('Product.Expiration')).date())
    elif t == 'CDXTranche':
        k = (_name(r, 'Product.CDSIndex'), r.get('Product.Attachment'), r.get('Product.Detachment'), mat)
    elif t == 'CDO':      # underlying basket not compared: same attach/detach/maturity can be different baskets
        k = (r.get('Product.Attachment'), r.get('Product.Detachment'), mat)
    else:
        k = (r.get('Product.Name'),)
    return t, ' | '.join(map(str, k))


def live_static(all_trades, as_of):
    """Flatten data_query trade records, keep live Credit/Manual trades."""
    live = [t for t in all_trades if pc.is_live(t, as_of) and (t.get('Strategy') or {}).get('Name') in STRATS]
    df = pd.json_normalize(live)
    df['TradeId'] = df['TradeId'].astype(str)
    df['strategy'] = df['Strategy.Name']
    b = df.apply(bucket, axis=1, result_type='expand')
    df['ptype'], df['bucket'] = b[0], b[1]
    return df


def load_pricers(live, as_of, calc_env='Official'):
    return pc.load_cached_pricers(as_of, calc_env, trade_ids=live['TradeId'].tolist())


def trade_risk(live, pricers, bump_bp=1.0):
    """One row per live trade: signed notional (Amount), PV, Spread01 (PV change for +1bp, all curves)."""
    from Quantifi.Toolkit.Sensitivity import Sensitivities
    out, t0 = [], time.time()
    for r in live.itertuples():
        row = dict(TradeId=r.TradeId, strategy=r.strategy, ptype=r.ptype, bucket=r.bucket,
                   notional=r.Amount, pv=None, spread01=None, error=None)
        p = pricers.get(r.TradeId)
        if p is None:
            row['error'] = 'not loaded'
        else:
            try:
                row['pv'] = float(p.Pv())
                row['spread01'] = float(Sensitivities.Spread01(p, bump_bp, 0.0, None))
            except Exception as ex:
                row['error'] = str(ex)[:120]
        out.append(row)
    print(f'risk for {len(out)} trades in {time.time() - t0:.0f}s')
    return pd.DataFrame(out)


def reconcile(risk, tol_pct=0.005, tol_abs=1.0):
    """Per (ptype, bucket): Credit vs Manual net notional / PV / Spread01. Status: OK, BREAK, INCOMPLETE, ONLY_<strategy>."""
    d = risk.copy()
    d['missing'] = d['spread01'].isna()
    g = d.groupby(['ptype', 'bucket', 'strategy']).agg(
        trades=('TradeId', 'size'), notional=('notional', 'sum'), pv=('pv', 'sum'),
        spread01=('spread01', 'sum'), unpriced=('missing', 'sum')).unstack('strategy')
    g.columns = [f'{m}_{s}' for m, s in g.columns]
    for m in ('trades', 'notional', 'pv', 'spread01', 'unpriced'):
        for s in STRATS:
            if f'{m}_{s}' not in g:
                g[f'{m}_{s}'] = 0.0
    g = g.fillna(0.0)
    for m in ('notional', 'pv', 'spread01'):
        g[f'{m}_diff'] = g[f'{m}_Credit'] - g[f'{m}_Manual']

    def status(r):
        if r.trades_Credit == 0:
            return 'ONLY_Manual'
        if r.trades_Manual == 0:
            return 'ONLY_Credit'
        if r.unpriced_Credit + r.unpriced_Manual > 0:
            return 'INCOMPLETE'
        ok = all(abs(r[f'{m}_diff']) <= max(tol_abs, tol_pct * max(abs(r[f'{m}_Credit']), abs(r[f'{m}_Manual'])))
                 for m in ('notional', 'spread01'))
        return 'OK' if ok else 'BREAK'
    g['status'] = g.apply(status, axis=1)
    return g.reset_index().sort_values(['status', 'ptype', 'bucket'])
