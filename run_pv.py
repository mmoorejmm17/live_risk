"""Price every trade in Quantifi DSP and write PVs to CSV, chunk by chunk.

Read-only. Pricers are discarded after each chunk so memory stays flat, results are
appended to the CSV as the run goes, and a rerun resumes where it left off.

    .venv\\Scripts\\python run_pv.py --limit 100            # smoke test
    .venv\\Scripts\\python run_pv.py                        # everything (resumes)
    .venv\\Scripts\\python run_pv.py --retry-errors         # re-attempt trades that failed before
    .venv\\Scripts\\python run_pv.py --fresh                # ignore existing output

If the process dies, the last chunk's trade ids are in inflight_chunk.txt, and
faulthandler prints the native stack to stderr on a hard crash.
"""
import argparse
import csv
import faulthandler
import gc
import os
import sys
import time

faulthandler.enable()

import requests

# Toolkit import before qservices, same order as the notebook
from Quantifi.Toolkit.Base import Dt  # noqa: F401
from qservices.QuantifiApiClient import Connect

HOSTS = {'Prod': 'hb-dc1pd-qfidsp', 'UAT': 'hb-dc1ut-qfidsp'}
FIELDS = ['TradeId', 'Pricer', 'Product', 'PV', 'Error']
INFLIGHT_FILE = 'inflight_chunk.txt'


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--env', choices=HOSTS, default='Prod')
    ap.add_argument('--as-of', default='2026-09-29')
    ap.add_argument('--calc-env', default='Official')
    ap.add_argument('--hql', default='FROM Trade b', help='HQL used to select trades')
    ap.add_argument('--max-trades', type=int, default=100000)
    ap.add_argument('--limit', type=int, default=None, help='only price the first N trades')
    ap.add_argument('--chunk', type=int, default=10, help='trades per request')
    ap.add_argument('--out', default='pv_results.csv')  # not all_trades_pv.csv: the notebook writes that one
    ap.add_argument('--retry-errors', action='store_true', help='re-attempt trades whose previous row has an Error')
    ap.add_argument('--fresh', action='store_true', help='ignore and overwrite existing output')
    return ap.parse_args()


def load_done(path, retry_errors):
    """TradeIds already in the output file (skipped on this run)."""
    done = set()
    if not os.path.exists(path):
        return done
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            if retry_errors and row.get('Error'):
                continue
            done.add(row['TradeId'])
    return done


def price_one(tid, pricer):
    row = {'TradeId': tid, 'Pricer': type(pricer).__name__, 'Product': '', 'PV': '', 'Error': ''}
    try:
        row['Product'] = str(pricer.Product)
    except Exception:
        pass
    try:
        row['PV'] = pricer.Pv()
    except Exception as e:
        row['Error'] = f'PvError: {e}'
    return row


def error_row(tid, msg):
    return {'TradeId': tid, 'Pricer': '', 'Product': '', 'PV': '', 'Error': msg}


def fmt_errors(v):
    return '; '.join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)


def load_chunk(chunk, args):
    """Return CSV rows for one chunk. Falls back to one-by-one if the whole request fails."""
    rows = []
    try:
        pricers, errs = Connect.get_pricers_by_trade_ids(
            as_of=args.as_of, trade_ids=chunk, calc_env=args.calc_env,
            batch_size=len(chunk), with_errors=True, verify_https_cert=False)
        for tid in chunk:
            if tid in pricers:
                rows.append(price_one(tid, pricers[tid]))
            elif tid in errs:
                rows.append(error_row(tid, fmt_errors(errs[tid])))
            else:
                rows.append(error_row(tid, 'No pricer and no error returned'))
        del pricers, errs
        return rows
    except Exception as e:
        print(f'  chunk request failed ({e}); retrying one by one', flush=True)

    for tid in chunk:
        try:
            pricer, errs = Connect.get_pricer_by_trade_id(
                as_of=args.as_of, trade_id=tid, calc_env=args.calc_env,
                with_errors=True, verify_https_cert=False)
            if pricer is not None:
                rows.append(price_one(tid, pricer))
            else:
                rows.append(error_row(tid, fmt_errors(errs) if errs else 'No pricer returned'))
            del pricer
        except Exception as e2:
            rows.append(error_row(tid, str(e2)))
    return rows


def main():
    args = parse_args()
    host = HOSTS[args.env]
    os.environ['Q_DATA_SERVICE_URL_PREFIX'] = f'https://{host}:47200'
    os.environ['Q_MARKET_ENV_SERVICE_URL_PREFIX'] = f'https://{host}:47201'
    os.environ['Q_LOCAL_INSTALL'] = 'True'
    requests.packages.urllib3.disable_warnings()  # unverified HTTPS cert

    print(f'Env={args.env} host={host} as_of={args.as_of} calc_env={args.calc_env}', flush=True)
    trades = Connect.data_query(args.hql, max_results=args.max_trades, verify_https_cert=False)
    print('Trades returned:', len(trades), flush=True)
    if len(trades) >= args.max_trades:
        print('WARNING: hit --max-trades, results are probably truncated; raise it.')
    trade_ids = [t['TradeId'] for t in trades]
    del trades
    if args.limit:
        trade_ids = trade_ids[:args.limit]

    if args.fresh and os.path.exists(args.out):
        os.remove(args.out)
    done = load_done(args.out, args.retry_errors)
    todo = [t for t in trade_ids if t not in done]
    print(f'{len(done)} already in {args.out}, {len(todo)} to price', flush=True)
    if not todo:
        return

    # when retrying errors, drop the old failed rows so each trade appears once
    if args.retry_errors and os.path.exists(args.out):
        with open(args.out, newline='') as f:
            kept = [r for r in csv.DictReader(f) if r['TradeId'] in done]
        with open(args.out, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(kept)

    new_file = not os.path.exists(args.out)
    chunks = [todo[i:i + args.chunk] for i in range(0, len(todo), args.chunk)]
    n_ok = n_err = 0
    t0 = time.time()

    with open(args.out, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for i, chunk in enumerate(chunks, 1):
            with open(INFLIGHT_FILE, 'w') as inflight:
                inflight.write('\n'.join(chunk))
            rows = load_chunk(chunk, args)
            w.writerows(rows)
            f.flush()
            n_err += sum(1 for r in rows if r['Error'])
            n_ok += sum(1 for r in rows if not r['Error'])
            gc.collect()

            if i % 10 == 0 or i == len(chunks):
                elapsed = time.time() - t0
                eta = elapsed / i * (len(chunks) - i)
                print(f'{i}/{len(chunks)} chunks | {n_ok} ok, {n_err} errors | '
                      f'{elapsed:.0f}s elapsed, ~{eta:.0f}s left', flush=True)

    if os.path.exists(INFLIGHT_FILE):
        os.remove(INFLIGHT_FILE)
    print(f'Done: {n_ok} ok, {n_err} errors -> {args.out} ({time.time() - t0:.0f}s)')


if __name__ == '__main__':
    sys.exit(main())
