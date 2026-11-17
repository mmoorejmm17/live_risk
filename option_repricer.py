"""Live repricing of CDX/iTraxx index options on new index spreads (vol held constant, sticky strike).

    opts   = option_table(all_trades, REPORT_DATE)             # static: trade, index, strike, type, expiry, notional
    pricers= load_option_pricers(opts, REPORT_DATE)             # from the pricer cache
    base   = risk_table(opts, pricers)                          # delta / gamma / vega as of the reporting date
    live   = live_spreads(opts.index_name.unique())             # Bloomberg (bbg_utils) -> bp
    new    = risk_table(opts, pricers, spreads_bp=live_map)     # same greeks on the live spreads

Conventions (all in the option's own currency; Toolkit units):
  delta        MarketDelta (ratio, option price per index price, per 1bp)      -> delta_notional = delta x Notional
  mkt_gamma    Toolkit MarketGamma (ratio). NOT per-bp: it is ~50-80x a per-bp number for these options, so it is only
               reported for reference. Use the bump-based gammas below.
  gamma_pv     PV(s+1bp) - 2 PV(s) + PV(s-1bp), index-quote bumps (currency per bp^2) ~ official 'Credit Spread Gamma (1bp)'
  gamma_dn     change in delta_notional per +1bp of index spread (central, +/-0.5bp)
  vega         Vega(+1 vol pt); the Toolkit already includes the pricer's Notional
  theta        MarketTheta (1 day, everything else unchanged); the Toolkit already includes the pricer's Notional
  implied_vol  pricer.Volatility (annualised; unchanged when the index spread is replaced)
  fwd_spread_bp  adjusted (at-the-money) forward index spread, bp
GOTCHA: pricer.Update(MarketQuote) resets Notional to 1.0, so the notional is set back before any risk call.
"""
import re

import pandas as pd

import pricer_cache as pc

_BBG = [(re.compile(r'^CDX-NAIGS(\d+)V\d+-(\d+)Y$'), 'CDX IG CDSI S{s} {t}Y Corp'),
        (re.compile(r'^ITRAXX-EUROPES(\d+)V\d+-(\d+)Y$'), 'ITRX EUR CDSI S{s} {t}Y Corp')]


def bbg_ticker(index_name):
    """Bloomberg CDSI ticker for an index name like CDX-NAIGS47V1-5Y / ITRAXX-EUROPES46V1-5Y (None if unmapped)."""
    for rx, fmt in _BBG:
        m = rx.match(str(index_name))
        if m:
            return fmt.format(s=m.group(1), t=m.group(2))
    return None


def option_table(all_trades, as_of, strategies=('Credit', 'Manual')):
    """Live CDX/iTraxx option trades in the given strategies as of `as_of` (static fields only)."""
    ids = set(pc.live_trade_ids(all_trades, as_of, include_strategies=strategies))
    recs = [t for t in all_trades if t['TradeId'] in ids and t.get('$type') == 'CDXOptionTrade']
    df = pd.json_normalize(recs)
    out = pd.DataFrame({
        'TradeId': df['TradeId'].astype(str),
        'strategy': df['Strategy.Name'],
        'index_name': df['Product.Underlying.Name'],
        'type': df['Product.Type'],
        'strike_bp': df['Product.Strike'].astype(float) * 1e4,
        'expiry': pd.to_datetime(df['Product.Expiration']).dt.date,
        'notional': df['Amount'].astype(float),
        'counterparty': df['Counterparty.Name'],
    })
    out['bbg_ticker'] = out.index_name.map(bbg_ticker)
    return out.sort_values(['index_name', 'expiry', 'strike_bp', 'strategy']).reset_index(drop=True)


def load_option_pricers(opts, as_of, calc_env='Official'):
    return pc.load_cached_pricers(as_of, calc_env, trade_ids=opts['TradeId'].tolist())


def live_spreads(index_names, field='PX_LAST'):
    """Bloomberg index spreads (bp) via bbg_utils.bdp_mm -> DataFrame[index_name, bbg_ticker, spread_bp, as_of_time]."""
    from bbg_utils import bdp_mm
    m = pd.DataFrame({'index_name': list(index_names)})
    m['bbg_ticker'] = m.index_name.map(bbg_ticker)
    bad = m[m.bbg_ticker.isna()].index_name.tolist()
    if bad:
        raise ValueError(f'No Bloomberg ticker mapping for index(es): {bad}')
    q = bdp_mm(m.bbg_ticker.tolist(), [field, 'LAST_UPDATE_DT', 'TIME'])
    q = q.rename(columns={'security': 'bbg_ticker', field: 'spread_bp', 'TIME': 'quote_time', 'LAST_UPDATE_DT': 'quote_date'})
    out = m.merge(q, on='bbg_ticker', how='left')
    if pd.to_numeric(out.spread_bp, errors='coerce').isna().any():
        raise ValueError(f'Bloomberg returned no spread for: {out[pd.to_numeric(out.spread_bp, errors="coerce").isna()].bbg_ticker.tolist()}')
    out['spread_bp'] = out.spread_bp.astype(float)
    return out


def _bumped(p, notional, bp):
    from Quantifi.Toolkit.Base import MarketQuote, QuotingConvention
    q = p.Update(MarketQuote(bp * 1e-4, QuotingConvention.CreditSpread))
    q.Notional = notional                      # Update() resets Notional to 1.0
    return q


def _fd_gammas(p, notional, spread_bp):
    from Quantifi.Toolkit.Pricers import CreditIndexOptionPricerFactory as F
    pv = lambda bp: float(F.FairPrice(_bumped(p, notional, bp))) * notional
    dn = lambda bp: float(F.MarketDelta(_bumped(p, notional, bp), 0.0001, False)) * notional
    gamma_pv = pv(spread_bp + 1.0) - 2.0 * pv(spread_bp) + pv(spread_bp - 1.0)
    gamma_dn = (dn(spread_bp + 0.5) - dn(spread_bp - 0.5)) / 1.0
    return gamma_pv, gamma_dn


def _greeks(p, notional, spread_bp):
    from Quantifi.Toolkit.Pricers import CreditIndexOptionPricerFactory as F
    d = F.MarketDelta(p, 0.0001, False)
    g = F.MarketGamma(p)
    gamma_pv, gamma_dn = _fd_gammas(p, notional, spread_bp)
    return dict(fwd_spread_bp=float(F.AdjustedForwardSpread(p)) * 1e4, implied_vol=float(p.Volatility),
                pv=float(F.FairPrice(p)) * notional,
                delta=float(d), delta_notional=float(d) * notional,
                gamma_pv=gamma_pv, gamma_dn=gamma_dn, mkt_gamma=float(g),
                theta=float(F.MarketTheta(p)))


def risk_table(opts, pricers, spreads_bp=None, bump_vol=0.01):
    """Greeks per option. spreads_bp=None -> as booked/cached for the reporting date;
    spreads_bp={index_name: bp} -> repriced with the index quote replaced (vol held constant)."""
    from Quantifi.Toolkit.Base import MarketQuote, QuotingConvention
    from Quantifi.Toolkit.Pricers import CreditIndexOptionPricerFactory as F
    rows = []
    for r in opts.itertuples():
        p = pricers[r.TradeId]
        n = float(p.Notional)
        base_bp = float(p.Spread) * 1e4
        use_bp = base_bp
        if spreads_bp is not None:
            use_bp = float(spreads_bp[r.index_name])
            p = p.Update(MarketQuote(use_bp * 1e-4, QuotingConvention.CreditSpread))
            p.Notional = n                       # Update() resets Notional to 1.0
        row = dict(TradeId=r.TradeId, index_spread_bp=use_bp, **_greeks(p, n, use_bp), vega=float(F.Vega(p, bump_vol)))
        rows.append(row)
    return pd.DataFrame(rows)


def bbg_close(index_names, date, field='PX_LAST'):
    """Bloomberg closing index spread (bp) on `date` via bbg_utils.bdh_mm -> {index_name: bp}."""
    from bbg_utils import bdh_mm
    names = list(index_names)
    tick = {n: bbg_ticker(n) for n in names}
    d = pd.Timestamp(date).strftime('%Y-%m-%d')
    q = bdh_mm(list(tick.values()), field, d, d)
    px = dict(zip(q.security, q[field].astype(float)))
    return {n: px[t] for n, t in tick.items()}


def shifted_spreads(opts, pricers, live, report_date):
    """'Change' mode: Quantifi's own booked spread + (Bloomberg live - Bloomberg close on report_date), bp.
    Strips the small level basis between Bloomberg and the Quantifi quote source."""
    live_map = dict(zip(live.index_name, live.spread_bp))
    close = bbg_close(live.index_name, report_date)
    base = {r.index_name: float(pricers[r.TradeId].Spread) * 1e4 for r in opts.itertuples()}
    return {n: base[n] + live_map[n] - close[n] for n in live.index_name}
