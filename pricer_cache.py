"""Daily cache of Quantifi DSP pricer objects (XML inside Parquet).

DSP returns each pricer as Toolkit XML that the client deserializes. Here we serialize the
loaded pricers back to XML with the Toolkit's own XmlSerialization and keep them in one Parquet
file per (calc_env, as_of), so the slow DSP round trip happens once a day.

Layout:
    pricer_cache/<calc_env>/<as_of>/pricers.parquet   one row per working pricer
    pricer_cache/<calc_env>/<as_of>/errors.parquet    trades that failed to load / price (not cached)

pricers.parquet columns: trade_id, pricer_module, pricer_class, xml, pv (at cache time), cached_at

Library use:
    from pricer_cache import load_cached_pricers
    pricers = load_cached_pricers('2026-11-12')            # {trade_id: pricer}, no DSP connection needed

CLI (needs DSP access):
    .venv\\Scripts\\python pricer_cache.py build --as-of 2026-11-12                  # trader query below
    .venv\\Scripts\\python pricer_cache.py build --as-of 2026-11-12 --ids-csv all_trades_pv.csv
    .venv\\Scripts\\python pricer_cache.py build --as-of 2026-11-12 --limit 20       # smoke test
    .venv\\Scripts\\python pricer_cache.py verify --as-of 2026-11-12 --sample 50     # reload, compare PV
"""
import argparse
import gc
import importlib
import os
import sys
import time
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

HOSTS = {'Prod': 'hb-dc1pd-qfidsp', 'UAT': 'hb-dc1ut-qfidsp'}
CACHE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pricer_cache')
DEFAULT_TRADERS = ['abhutra', 'AB', 'Abhishek Bhutra']

PRICER_SCHEMA = pa.schema([
    ('trade_id', pa.string()),
    ('pricer_module', pa.string()),
    ('pricer_class', pa.string()),
    ('xml', pa.string()),
    ('pv', pa.float64()),
    ('cached_at', pa.timestamp('us', tz='UTC')),
])
ERROR_SCHEMA = pa.schema([('trade_id', pa.string()), ('stage', pa.string()), ('error', pa.string())])


def cache_dir(as_of, calc_env='Official', root=CACHE_ROOT):
    return os.path.join(root, calc_env, str(as_of))


# --------------------------------------------------------------------------------------
# (de)serialization
# --------------------------------------------------------------------------------------
def _toolkit():
    # Toolkit import has to come before qservices (same order as the notebook)
    from Quantifi.Toolkit.Base import Dt  # noqa: F401
    from Quantifi.Toolkit.Util import XmlSerialization
    from System.Text import StringBuilder
    return XmlSerialization, StringBuilder


def pricer_to_xml(pricer):
    XmlSerialization, StringBuilder = _toolkit()
    sb = StringBuilder()
    XmlSerialization.WriteXml(pricer, sb, pricer.GetType())
    return sb.ToString()


def xml_to_pricer(xml, module, cls_name):
    XmlSerialization, _ = _toolkit()
    cls = getattr(importlib.import_module(module), cls_name)
    return XmlSerialization.ReadXmlString[cls](xml)


# --------------------------------------------------------------------------------------
# read side (no DSP connection needed)
# --------------------------------------------------------------------------------------
def load_cached_pricers(as_of, calc_env='Official', trade_ids=None, root=CACHE_ROOT):
    """Deserialize cached pricers -> {trade_id: pricer}. trade_ids restricts what is loaded."""
    path = os.path.join(cache_dir(as_of, calc_env, root), 'pricers.parquet')
    if not os.path.exists(path):
        raise FileNotFoundError(f'No pricer cache at {path}; run `python pricer_cache.py build --as-of {as_of}`')
    filters = [('trade_id', 'in', list(trade_ids))] if trade_ids is not None else None
    table = pq.read_table(path, columns=['trade_id', 'pricer_module', 'pricer_class', 'xml'], filters=filters)
    out = {}
    for tid, mod, cls, xml in zip(*(table.column(c).to_pylist()
                                    for c in ('trade_id', 'pricer_module', 'pricer_class', 'xml'))):
        out[tid] = xml_to_pricer(xml, mod, cls)
    return out


def _read_error_rows(out_dir):
    path = os.path.join(out_dir, 'errors.parquet')
    if not os.path.exists(path):
        return {}
    t = pq.read_table(path)
    return {tid: (st, er) for tid, st, er in zip(*(t.column(c).to_pylist() for c in ERROR_SCHEMA.names))}


def load_cache_errors(as_of, calc_env='Official', root=CACHE_ROOT):
    """{trade_id: error} for trades the last build could not load/price."""
    path = os.path.join(cache_dir(as_of, calc_env, root), 'errors.parquet')
    if not os.path.exists(path):
        return {}
    t = pq.read_table(path)
    return dict(zip(t.column('trade_id').to_pylist(), t.column('error').to_pylist()))


def cache_index(as_of, calc_env='Official', root=CACHE_ROOT):
    """Metadata only (no xml column, so it's cheap): trade_id, pricer_class, pv, cached_at."""
    path = os.path.join(cache_dir(as_of, calc_env, root), 'pricers.parquet')
    return pq.read_table(path, columns=['trade_id', 'pricer_class', 'pv', 'cached_at']).to_pandas()


# --------------------------------------------------------------------------------------
# write side (needs DSP)
# --------------------------------------------------------------------------------------
def _connect(env):
    import requests
    host = HOSTS[env]
    os.environ['Q_DATA_SERVICE_URL_PREFIX'] = f'https://{host}:47200'
    os.environ['Q_MARKET_ENV_SERVICE_URL_PREFIX'] = f'https://{host}:47201'
    os.environ['Q_LOCAL_INSTALL'] = 'True'
    requests.packages.urllib3.disable_warnings()
    _toolkit()
    from qservices.QuantifiApiClient import Connect
    return Connect


def _fmt(v):
    return '; '.join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)


def select_trade_ids(Connect, hql=None, ids_csv=None, max_trades=100_000):
    if ids_csv:
        import pandas as pd
        df = pd.read_csv(ids_csv)
        if 'PV' in df:                       # keep only the ones that priced last time
            df = df[df['PV'].notna()]
        if 'PvError' in df:
            df = df[df['PvError'].isna()]
        return df['TradeId'].astype(str).tolist()
    if hql is None:
        names = ', '.join(f"'{t}'" for t in DEFAULT_TRADERS)
        hql = f'FROM Trade b WHERE b.Trader.Name IN ({names})'
    trades = Connect.data_query(hql, max_results=max_trades, verify_https_cert=False)
    if len(trades) >= max_trades:
        print(f'WARNING: hit max_trades={max_trades}; results probably truncated')
    return [t['TradeId'] for t in trades]


def build_cache(as_of, calc_env='Official', env='Prod', trade_ids=None, hql=None, ids_csv=None,
                chunk=10, limit=None, refresh=False, retry_errors=False, root=CACHE_ROOT):
    """Load pricers from DSP, keep those that price, write them to the Parquet cache.

    Incremental by default: trade ids already in this (calc_env, as_of) cache are NOT re-requested, and
    neither are ids that failed on an earlier run (their error stays in errors.parquet). Only new ids go
    to DSP and the new rows are appended.
      retry_errors=True  re-request the previously failed ids too (e.g. after fixing the cause)
      refresh=True       ignore the existing cache and re-pull everything (also picks up amended trades)
    Existing rows are kept even if the trade has since left the id list.
    """
    Connect = _connect(env)
    ids = list(trade_ids) if trade_ids is not None else select_trade_ids(Connect, hql, ids_csv)
    ids = list(dict.fromkeys(ids))           # de-dupe, keep order
    if limit:
        ids = ids[:limit]
    out_dir = cache_dir(as_of, calc_env, root)
    final_p = os.path.join(out_dir, 'pricers.parquet')
    existing = None if refresh or not os.path.exists(final_p) else pq.ParquetFile(final_p)
    have = set() if existing is None else set(existing.read(columns=['trade_id']).column('trade_id').to_pylist())
    old_errs = {} if refresh else _read_error_rows(out_dir)      # trade_id -> (stage, error)
    skip = have | (set() if retry_errors else set(old_errs))
    todo = [t for t in ids if t not in skip]
    print(f'{len(ids)} trades requested: {len(have & set(ids))} cached, '
          f'{len(set(old_errs) & set(ids) - have) if not retry_errors else 0} known failures skipped, '
          f'{len(todo)} to fetch')
    if not todo:
        return 0, []                         # nothing to do; leave the cache untouched
    ids = todo
    os.makedirs(out_dir, exist_ok=True)
    tmp_p, tmp_e = (os.path.join(out_dir, n) for n in ('pricers.parquet.tmp', 'errors.parquet.tmp'))
    now = datetime.now(timezone.utc)
    n_ok, errors, t0 = 0, [], time.time()

    with pq.ParquetWriter(tmp_p, PRICER_SCHEMA, compression='zstd') as writer:
        if existing is not None:             # carry the already-cached rows over, batch by batch
            for rb in existing.iter_batches(batch_size=200):
                writer.write_batch(rb)
        for i in range(0, len(ids), chunk):
            batch = ids[i:i + chunk]
            try:
                pricers, errs = Connect.get_pricers_by_trade_ids(
                    as_of=as_of, trade_ids=batch, calc_env=calc_env, batch_size=len(batch),
                    with_errors=True, verify_https_cert=False)
            except Exception as ex:           # whole request failed (e.g. 400 on every id in the chunk)
                pricers, errs = {}, {tid: str(ex) for tid in batch}
            errors += [(tid, 'load', _fmt(m)) for tid, m in errs.items()]
            missing = set(batch) - set(pricers) - set(errs)
            errors += [(tid, 'load', 'not returned by DSP') for tid in missing]

            rows = []
            for tid, p in pricers.items():
                try:
                    pv = float(p.Pv())        # "currently working" = loads AND prices
                    xml = pricer_to_xml(p)
                except Exception as ex:
                    errors.append((tid, 'pv/serialize', str(ex)))
                    continue
                cls = type(p)
                rows.append((tid, cls.__module__, cls.__name__, xml, pv, now))
            if rows:
                writer.write_table(pa.Table.from_pylist(
                    [dict(zip(PRICER_SCHEMA.names, r)) for r in rows], schema=PRICER_SCHEMA))
                n_ok += len(rows)
            del pricers, rows
            gc.collect()
            done = min(i + chunk, len(ids))
            if (done // chunk) % 20 == 0 or done == len(ids):
                print(f'{done}/{len(ids)} requested, {n_ok} cached, {len(errors)} failed '
                      f'({time.time() - t0:.0f}s)', flush=True)

    fetched = set(ids)                       # old errors for ids we just re-requested are superseded
    merged = {t: v for t, v in old_errs.items() if t not in fetched}
    merged.update({t: (st, er) for t, st, er in errors})
    pq.write_table(pa.Table.from_pylist([dict(zip(ERROR_SCHEMA.names, (t, *v))) for t, v in merged.items()],
                                        schema=ERROR_SCHEMA), tmp_e, compression='zstd')
    existing = None                          # release the handle on the old file before replacing it
    # atomic-ish swap so a crash mid-build never leaves a half-written "good" cache
    for tmp in (tmp_p, tmp_e):
        os.replace(tmp, tmp[:-4])
    size_mb = os.path.getsize(os.path.join(out_dir, 'pricers.parquet')) / 1e6
    print(f'Done: {n_ok} new pricers cached, {len(errors)} not cached; file is {size_mb:.1f} MB -> {out_dir}')
    return n_ok, errors


def verify_cache(as_of, calc_env='Official', sample=None, rel_tol=1e-9, root=CACHE_ROOT):
    """Reload from the cache and compare Pv() with the PV stored when it was cached."""
    idx = cache_index(as_of, calc_env, root)
    if sample:
        idx = idx.sample(min(sample, len(idx)), random_state=0)
    pricers = load_cached_pricers(as_of, calc_env, trade_ids=idx['trade_id'], root=root)
    bad = []
    for tid, pv0 in zip(idx['trade_id'], idx['pv']):
        try:
            pv1 = float(pricers[tid].Pv())
        except Exception as ex:
            bad.append((tid, pv0, None, str(ex)))
            continue
        if abs(pv1 - pv0) > rel_tol * max(1.0, abs(pv0)):
            bad.append((tid, pv0, pv1, 'PV mismatch'))
    print(f'Verified {len(idx)} pricers: {len(idx) - len(bad)} match, {len(bad)} differ')
    for b in bad[:20]:
        print('  ', b)
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--as-of', required=True)
    b.add_argument('--calc-env', default='Official')
    b.add_argument('--env', choices=HOSTS, default='Prod')
    b.add_argument('--hql', help='HQL selecting trades (default: Abhishek trader query)')
    b.add_argument('--ids-csv', help='CSV with TradeId (+PV) column; only rows with a PV are used')
    b.add_argument('--chunk', type=int, default=10)
    b.add_argument('--limit', type=int)
    b.add_argument('--retry-errors', action='store_true', help='re-request trades that failed on an earlier run')
    b.add_argument('--refresh', action='store_true', help='ignore the existing cache and re-pull every trade')
    v = sub.add_parser('verify')
    v.add_argument('--as-of', required=True)
    v.add_argument('--calc-env', default='Official')
    v.add_argument('--sample', type=int)
    a = ap.parse_args()
    if a.cmd == 'build':
        build_cache(a.as_of, a.calc_env, a.env, hql=a.hql, ids_csv=a.ids_csv, chunk=a.chunk, limit=a.limit,
                    refresh=a.refresh, retry_errors=a.retry_errors)
    else:
        sys.exit(1 if verify_cache(a.as_of, a.calc_env, a.sample) else 0)


if __name__ == '__main__':
    main()
