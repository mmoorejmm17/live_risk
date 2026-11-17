"""Toolkit-computed risk for live CDX options (to compare with Quantifi's official numbers).

Per option: PV, CS01 (Sensitivities.Spread01, +1bp on all survival curves, full replication), Market delta
(index-notional hedge = MarketDelta x Notional), gamma (MarketGamma x Notional), vega (+1 vol pt) and theta.
Toolkit delta/gamma are per-unit ratios (x signed Notional here); Vega/MarketTheta already include the pricer's Notional.
"""
import pandas as pd

import pricer_cache as pc


def option_risk(as_of, calc_env='Official', trade_ids=None):
    from Quantifi.Toolkit.Base import Dt  # noqa: F401  (Toolkit import order)
    from Quantifi.Toolkit.Pricers import CreditIndexOptionPricerFactory as F
    from Quantifi.Toolkit.Sensitivity import Sensitivities
    idx = pc.cache_index(as_of, calc_env)
    idx = idx[idx.pricer_class == 'CreditIndexOptionPricer']
    if trade_ids is not None:
        idx = idx[idx.trade_id.isin(trade_ids)]
    pricers = pc.load_cached_pricers(as_of, calc_env, trade_ids=idx.trade_id.tolist())
    rows = []
    for tid, p in pricers.items():
        n = float(p.Notional)
        row = dict(TradeId=tid, notional=n, pv=float(p.Pv()), cs01=None, mkt_delta=None, mkt_gamma=None, vega=None, theta=None)
        for k, f in [('cs01', lambda: Sensitivities.Spread01(p, 1.0, 0.0, None)),
                     ('mkt_delta', lambda: F.MarketDelta(p, 0.0001, False) * n),
                     ('mkt_gamma', lambda: F.MarketGamma(p) * n),
                     ('vega', lambda: F.Vega(p, 0.01)),
                     ('theta', lambda: F.MarketTheta(p))]:
            try:
                row[k] = float(f())
            except Exception as ex:
                row[k + '_err'] = str(ex)[:80]
        rows.append(row)
    return pd.DataFrame(rows)
