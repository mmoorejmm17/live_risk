# Quantifi DSP / Live Risk

## PROJECT GOAL (authoritative — supersedes "Original goal" below)

The whole point of this project: **import the pricers from the DSP, import new market data
(CDX index spreads, single-name spreads, equity rates, interest rates), and recompute the risk
parameters of all of our trades against that new market data** — i.e. live/intraday risk.

**Most important product: CDX index options.** They are short-dated, so delta (and gamma) can
change a lot as index spreads move intraday. Correct, fast re-risking of these is the priority.

## Current state (as of 2026-11-13)

- `liverisk_testing.ipynb` pulls all of Abhishek Bhutra's trades (traders `abhutra`, `AB`,
  `Abhishek Bhutra`; 9,712 trades) via HQL and loads pricers. `pricer_cache.py` caches pricers
  as Toolkit XML in Parquet per (calc_env, as_of) so DSP is only hit once a day
  (`pricer_cache/<calc_env>/<as_of>/pricers.parquet`). Prod host `hb-dc1pd-qfidsp` ports 47200/47201.
- Load results (as-of 2026-11-12, Official): CDXOption 1484/1484, Bond 53/53, Stock 67/67,
  StockOption 23/24, CDO 87/92. **CDX (5520), CDS (935), CDXTranche (1537) = 0 loaded**:
  `SerializationException: _indexFactor: unknown field in type CDXPricer` (DSP server's Toolkit
  version is newer than the local one -> upgrade local Toolkit/qservices). Matters for hedges.

- **Pending (2026-11-13): qncore upgrade.** Installed in system Python312 site-packages is qncore
  22.1.1; `vendor/` + `pyproject.toml` already reference `qncore-windows-x64_DSP_22.1.4.zip`. Quantifi
  sent a link to a newer build; Matt downloads the zip to `C:\Users\mmoore\Downloads`. Plan: extract to
  its own empty dir, check version, `pip install --upgrade` into the interpreter the notebook kernel
  uses, copy to `vendor/` and update `pyproject.toml`, then retest the `_indexFactor` CDX load failure
  (cached XML from the old Toolkit may need `--refresh`).

- **UPDATE 2026-11-13 (supersedes the 26.0 notes below): Quantifi says use v24.0.1 (matches Matt's other
  Quantifi subscription).** The correct DSP file is `Downloads\qncoreDSPClient-24.0.1.zip` — an OUTER
  wrapper holding `qncore-windows-x64.zip` (inner; qncore 24.0.1 + `qpy/lib/qservices/`, verified to contain
  `get_pricer_by_trade_id`, `data_query`, etc.) plus `requirements*.txt` (old pins, ignore). The inner zip is
  what pip/`pyproject.toml` needs, not the outer. `qncore-windows-x64-24.0.1.zip` (plain) has no qservices
  and is superseded. qservices files differ trivially from the installed 22.1.4 ones (same functions).
  **Venv test PASSED 2026-11-13** (throwaway venv + extracted source in the session scratchpad; system
  Python still on 22.1.1): with qncore 24.0.1 the previously failing CDX (CDXPricer), CDS
  (CDSCashflowPricer) and CDXTranche (SyntheticCDOPricer) trades load and price from Prod DSP; 4 live CDX
  option PVs identical to the cache; 60 sampled cached pricers (written by 22.1.x) deserialize and match
  under 24.0.1. Next: install into the real env, copy inner zip to `vendor/`, update `pyproject.toml`,
  rebuild the cache for CDX/CDS/tranche trades (and add the live-option/Done filter).
  **INSTALLED into system Python312 on 2026-11-13** via `pip install --upgrade <extracted inner zip dir>`;
  inner zip saved as `vendor/qncore-windows-x64_DSP_24.0.1.zip`, `pyproject.toml` updated (poetry.lock still
  references the old zip hash; regenerate with `poetry lock` if/when poetry is used). Files on disk are 24.0.1
  and verified (CDX/CDS/tranche load, 60/60 cached pricers match). **Cosmetic leftover:** stale metadata dirs
  `site-packages\qncore-22.1.4.dist-info` and `~ncore-22.1.1.dist-info` make `pip list` show 22.1.4 and warn
  about `~ncore`; deleting them is harmless (Matt declined an automated rm, so do it by hand or ask).
  **Where the zip lives:** `vendor/` is gitignored (licensed Quantifi binary, don't commit). Canonical copies are on
  the shared drive: `S:\Structured Credit\Matt Stuff\quantifi_vendor\` (24.0.1 and the old 22.1.4); `pyproject.toml`
  points there via the RELATIVE path `../quantifi_vendor/...zip` (an absolute `S:/...` path made `poetry lock`
  crash with an AssertionError in Poetry 2.4.1, so keep it relative). Works only if the repo is cloned as a
  sibling of `quantifi_vendor` (i.e. under `S:\Structured Credit\Matt Stuff\`).
  Remaining TODO: rebuild cache for CDX/CDS/tranche trades; add live-options + `Done` filter.
- (older) **qncore 26.0 downloaded 2026-11-13, NOT installed.** In
  `C:\Users\mmoore\Downloads`: `qncore-windows-x64-26.0.zip` (the Python/CLR package; the `(1).zip` is a
  byte-identical duplicate) and `Quantifi.SDK-26.0.exe` (SDK installer, not needed for Python, don't run).
  **The 26.0 zip has no `qservices`** (old 22.1.4 "DSP" build bundled it), so upgrading in place would
  remove the DSP client. Matt emailed Quantifi asking for a DSP build of 26.0. Fallback plan: test in a
  throwaway venv, copy the existing `qservices` folder in, retest CDX load and CDX option PVs; only then
  upgrade the real environment and update `vendor/` + `pyproject.toml`.

## CDX option findings (verified 2026-11-13)

- Loading is correct: all 1484 load as `CreditIndexOptionPricer`, PV non-null, and
  `FairPrice * Notional` reproduces the cached `Pv()`. Strike/type/expiry/notional/factor/recovery
  (40%) look right. Product `Receiver` -> Toolkit `Type=Call`, `Payer` -> `Put`. Sign of PV comes
  from signed Notional (short = negative).
- **Only 8 of 1484 are live (expiry >= as-of); 1476 are expired and PV=0.** Filter on
  `Product.Expiration >= as_of` (and `TradeStatus == 'Done'`; there are Pending/Canceled trades)
  before loading/caching to avoid wasted DSP calls.
- **The 8 live options are 4 pairs of the same trade booked twice** (`abhutra`/Strategy Manual and
  `AB`/Strategy Credit, same counterparty/strike/expiry/notional, identical PVs). Decide whether
  both books belong in the risk aggregate or it double counts.
- **GOTCHA — repricing on new spreads:** loaded pricers use full replication (125 per-name
  survival curves). Setting `pricer.Spread = x` is a NO-OP for Pv/forward/delta (readback changes,
  PV does not; also with `SurvivalCurves=None`). Working approach:
  `q = p.Update(MarketQuote(spread, QuotingConvention.CreditSpread))` then
  `CreditIndexOptionPricerFactory.FairPrice(q) * notional`. **`Update` resets `Notional` to 1.0 and
  `q.Pv()` returns 0 — always multiply by / re-set the original Notional.** Forward does respond to
  the new quote; vol surface is held fixed (sticky strike) — confirm that is the desired intraday
  convention.
- Toolkit risk functions that work on the loaded pricers: `CreditIndexOptionPricerFactory`
  `.MarketDelta(p, 0.0001, False)` (x Notional = index-notional hedge), `.Spread01`, `.MarketGamma`,
  `.Vega(p, 0.01)`, `.MarketTheta`; also `Sensitivities.Spread(...)` (see Toolkit sample
  `Api1/Python/Credit/CDX Options.ipynb`, sections 6 and 14).
- Illustration (4 unique live options, Nov-12 EOD): short 6-day payer IG46 K60 (spread 50.7bp):
  PV -6k but +10bp spread -> about -495k; so intraday re-risking clearly matters.
- Still to verify against Matt's Quantifi Risk app: the delta/PV numbers, and that survival-curve
  scaling to the new index spread (`AdjustSpread` flags in the pricer's `CDXOptionModelData`) behaves
  as intended when only the index quote is moved. Single-name spread overrides are not yet built.

---

# Background (written 2026-09-29, before this project had any code)

This section hands off context from research done elsewhere (in
`S:\Structured Credit\Matt Stuff\quantifi_usethis\Quantifi_Dev`).

## Original goal (superseded by PROJECT GOAL above)

Matt wanted to import trades into Quantifi via the **DSP (Data Science Platform)** — Quantifi's
Python integration layer — and asked for help writing that in Python, referencing the Quantifi
Toolkit MCP server for API grounding.

## What "DSP" actually is

Quantifi DSP = **Data Science Platform**. It's not a separate product from the Quantifi Toolkit;
it's the Python/Jupyter integration layer that sits on top of the Quantifi Risk application and
database, letting you:
- pull product/trade/curve/pricer objects live out of the Quantifi Risk DB into Python,
- run Toolkit pricing/risk calcs (Sensitivities, etc.) on those objects,
- manipulate results with pandas/matplotlib.

It is **not** covered by the `quantifi-mcp-toolkit` MCP server (that server only grounds the
core Toolkit pricing API — `Quantifi.Toolkit.*` — not the DSP/`qservices` REST client). Searches
there for "DSP", "trade import", "TradeLoader", etc. all came back empty — this is expected,
not a gap to keep chasing in that server.

## Where the real examples live

`S:\Structured Credit\Matt Stuff\quantifi_usethis\Quantifi_Dev\` has Quantifi-provided demo
notebooks that are the actual source of truth for DSP usage:

- **`DSP Demo - Products and Trades.ipynb`** — the main reference. Demonstrates:
  - env setup (`Q_DATA_SERVICE_URL_PREFIX`, `Q_MARKET_ENV_SERVICE_URL_PREFIX`, `Q_LOCAL_INSTALL`)
  - `from qservices.QuantifiApiClient import *` — this is the DSP client package
    (installed at `...\Python312\Lib\site-packages\qservices\QuantifiApiClient.py`)
  - `Connect.get_bond_data(name=..., ds_url=..., verify_https_cert=False)` — static data by HQL
  - `Connect.get_legal_entity_data(name=...)` — issuer/legal entity lookup
  - `Connect.data_query(hql, max_results=..., verify_https_cert=False)` — arbitrary HQL queries,
    e.g. `"FROM Trade b WHERE b.Strategy.Name = 'Manual'"` or `"...WHERE b.TradeId LIKE 'FXForward%'"`
  - `Connect.get_pricer_by_trade_id(as_of=..., trade_id=..., calc_env='Official', verify_https_cert=False)`
    — loads a single fully-formed pricer object (bond, CDX, FX forward/option, equity option,
    commodity future/option, CDS, swap, CDO tranche, etc.) for a given trade, as-of date, and
    calc environment
  - `Connect.get_pricers_by_trade_ids(as_of=..., trade_ids=[...], calc_env='Official', verify_https_cert=False)`
    — batch version, returns a dict keyed by trade ID
  - `Connect.get_discount_curve(...)` / `Connect.get_survival_curve(...)` — load curves by name/as-of/calc_env
  - Once loaded, pricers are regular Toolkit pricer objects — `pricer.Pv()`, `.Delta()`, `.Accrued()`,
    reassigning `.MarketQuote`, `.DiscountCurve`, `.SurvivalCurve` for scenario runs, and running
    `Sensitivities.Rate(...)` / `Sensitivities.Spread(...)` / `Sensitivities.Theta(...)` across a
    portfolio of pricers.
  - **This notebook only reads/loads existing trades — it does not demonstrate creating/importing
    new trades.** No `create_trade`, `add_trade`, `save_trade`, or similar write call appears in it.
  - The notebook run in that folder shows a live connectivity error: `500` from
    `hb-dc1ut-qfiap1.nyc.gerbercapital.com` ("No IPEndpoints were found for host ...") — that's a
    stale/wrong internal hostname baked into that demo env, not a code bug — needs the correct HBC
    DSP host before this will run live.

- **`DSP_Testing_Trades_Pricer.ipynb`** — companion notebook, not yet fully read (large file).
  Also worth checking for trade-loading patterns.

- **`QuantifiAPI_Utils.py`**, **`quantifi_literals.py`** — local helper/wrapper modules Matt
  built on top of the DSP client. Worth checking for any trade-write helpers before assuming
  none exist.

- Also present in that folder: `DSP Demo - Curves_MM.ipynb`, `DSP_Tranche_Risk.ipynb`,
  `getting_started.ipynb`, `Questions_For_Alexei.md` (likely open questions Matt already sent to
  a Quantifi contact named Alexei — check this for anything about trade import specifically),
  `ReportingServiceAPIExample.py` / `ReportingServiceAPIExample_MM.py`.

## Conclusion: the installed DSP client is read-only — no trade-import path exists in it

Checked every angle available locally and confirmed this is not a search gap — it's real:

1. **`DSP_Testing_Trades_Pricer.ipynb`** — grepped for `Connect.*` calls across the whole notebook
   (it's mostly output-heavy, hence large). It uses the exact same read-only calls as the other
   notebook: `get_bond_data`, `get_legal_entity_data`, `get_pricer_by_trade_id`,
   `get_pricers_by_trade_ids`, `get_discount_curve`, `get_survival_curve`, `data_query`. No
   create/add/save/import/write call anywhere.

2. **`QuantifiAPI_Utils.py`** — turned out to be unrelated to DSP. It's an MSAL/Azure AD auth helper
   for calling Quantifi's separate **ReportingService REST API** (`generate_report(...)` — pulls a
   named report for a date/risk-run). Not a trade-import path either.

3. **`quantifi_literals.py`** — just `typing.Literal` string-enum definitions (DayCount, Frequency,
   QuotingConvention, Currency, BondType, etc.) mirroring the Toolkit's C# enums, for type hints /
   valid-value reference in Python. Not related to trade import.

4. **`Questions_For_Alexei.md`** — confirms this is a genuinely open question Matt already raised
   with Quantifi support (a contact named Alexei), specifically item 2: *"Creating products/pricers
   from scratch ... e.g. a 25delta, 50delta, and 75delta put/call for each expiry in cdx"* — i.e.
   Matt already flagged that the local samples don't show how to construct new
   trades/products/pricers, not just load existing ones.

5. **Definitive check — the installed client itself:**
   `C:\Users\mmoore\AppData\Local\Programs\Python\Python312\Lib\site-packages\qservices\QuantifiApiClient.py`
   is the actual `qservices` package all these notebooks import (`from qservices.QuantifiApiClient
   import *`). Listed every function defined in it (~40 total) and every one is a `get_*`,
   `find_*`, or `data_query*` — e.g. `get_products`, `get_trades`, `get_trade_by_trade_id`,
   `get_trades_by_trade_ids`, `get_pricer_by_trade_id`/`_by_isin`/`_by_cusip`/`_by_figi`/
   `_by_externalid` and their plural/batch forms, `get_rate_curve(s)`, `get_discount_curve(s)`,
   `get_survival_curve(s)`, `get_fx_curve`, `get_inflation_curve(s)`, `data_query`,
   `data_query_risk_results`, `data_query_VaR_losses`. There is **no** `post`/`put`/`patch`/`delete`
   HTTP call anywhere in the file (grepped directly) and no create/add/save/import function of any
   kind. **This version of the DSP Python client is read-only by design** — it's built to pull
   static data, trades, curves and pricers *out* of the Quantifi Risk DB into Python for analysis,
   not to push new trades *in*.

## What this means for "importing trades via DSP"

Trade creation/import is **not a Python/DSP-client capability** in this installation — it has to
happen elsewhere, most likely:
- through the Quantifi Risk **application UI** (manual trade entry / trade blotter import), or
- through a separate **trade-booking/back-office feed** (e.g. an STP/FpML/CSV file feed, or the
  Quantifi front-office API rather than the DSP read client) that loads into the same Risk DB these
  `get_*` calls read from, or
- a newer/different Quantifi component not installed here.

**Next step should be asking Quantifi (Alexei or support) directly** how trades get created/loaded
into the Risk DB that DSP reads from — don't keep searching the DSP Python client for it, since the
installed client provably has no write surface. The `quantifi-mcp-toolkit` MCP server can't help
here either — it only grounds the Toolkit *pricing* API (`Quantifi.Toolkit.*`), not the
`qservices` DSP REST client or the Risk application's trade-booking/import mechanism.

## Live trades & Credit-vs-Manual reconciliation (2026-11-15, as-of 2026-11-13, Official)

- **ROOT CAUSE of the 2026-11-13 notebook failures (5530 `_indexFactor`, 2500 `traded_`): the Jupyter kernel
  `quantifi-live-risk` runs in the POETRY VENV** (`...\pypoetry\Cacheirtualenvs\quantifi-live-risk-Bi6SF7dF-py3.12`),
  which still has **qncore 22.1.4**. The 24.0.1 install went into system Python312 only (CLI `python pricer_cache.py` uses
  that one and works). Fix: `poetry install` in the repo (poetry.lock already says 24.0.1), restart kernel, verify
  `Quantifi.Toolkit` assembly version 24.0.1.0.
- `credit_manual_rec.py` + `credit_manual_rec.ipynb`: Credit-vs-Manual rec by risk bucket (net notional, PV, `Sensitivities.Spread01`
  from `Quantifi.Toolkit.Sensitivity`). First run (379 live Credit/Manual trades, all priced): 71 buckets OK, 8 BREAK,
  14 ONLY_Credit, 18 ONLY_Manual; all 4 CDX option buckets tie exactly. Run from system Python; notebook needs the venv fixed.
- `pricer_cache.py` now has `is_live` / `live_trade_ids` and `build --live-only` (Done, no Termination <= as_of,
  Product Maturity/Expiration >= as_of). The notebook's `trade_ids` cell has NOT been switched to it yet.
- **Abhishek live trades: 501 of 9,722 -> 482 priced, 19 fail** (BindError: missing survival curve scaling for
  HY37/39/40/BB37 & iTraxx XO42 on 13-Nov, missing IRiskCorrelation for BNP Fairmont/Ashburn, Nomura Main37 ZR). All 8 live CDX options price.
- By strategy (live / priced / PV): Manual 220/220/-100.6M; Credit 159/159/-115.9M; CV Draft Trades 73/62; TradeIdeas 29/21; Standard Risk Calc 20/20.
- **TO DO (Matt's requirement): "Credit" = official booking, flows downstream from official booking systems; "Manual" = trades
  we book ourselves. The two strategies must tie out in trades AND risk.** Live counts today do NOT tie:
  CDX 51 vs 109, CDS 43 vs 50, CDXTranche 55 vs 51, CDO 6 vs 6, CDXOption 4 vs 4. PV -115.9M vs -100.6M.
  Build a Credit-vs-Manual reconciliation (match on product/counterparty/strike/expiry/notional, then compare PV and risk).
  Draft/TradeIdeas/Standard Risk Calc strategies are not part of that tie-out.

## CDX option risk vs Quantifi official (started 2026-11-16)

- `option_risk.py` computes per live option: PV, CS01 (`Sensitivities.Spread01(p,1.0,0.0,None)`), market delta (`MarketDelta(p,0.0001,False)` x Notional),
  gamma (`MarketGamma` x Notional), vega, theta. Toolkit delta/gamma are per-unit ratios; **Vega/MarketTheta already include Notional**.
  Saved `option_risk_2026-11-13.csv` (4 unique Manual options).
- Quantifi official risk: **RiskRun** HQL entity exists per date (`RiskData` = official, `AB Risk Run (Manual)`, `CV Risk Run (Draft Trades)`, `RiskData-WhatIf`; e.g. AsOf 2026-11-13).
  **RiskMeasure** entity has `CS01`, `Spread01`, `Delta`, `Gamma`, `Vega`, `Theta`, `Pv`, `CS 1%_Delta`, ... Results are fetched with
  `Connect.data_query_risk_results(min_date, measures, risk_run_name=, trade_id=, tsds_url=)` (TimeSeriesDataService, env `Q_TIMESERIES_DATA_SERVICE_URL_PREFIX`) --
  **that service URL is not found yet**: ports 47200/47201/47202 on hb-dc1pd-qfidsp all 404 (47202 = ReportingService).
  `ReportGenerator.generate_report` on 47202 returns 500 "No IPEndpoints were found for host hbc-dc1-qfiap.nyc.gerbercapital.com" (stale host in server config, same as old demo).
  HQL `FROM RiskResult/Position/Report` are invalid. Need the TimeSeries service URL (or a working report) from Quantifi/infra.

### Official risk comparison (2026-11-17) -- ReportingService works now
- `ReportGenerator.generate_report(date, r'Users\abhutra\Structured Credit Risk Guidelines', None, 'https://hb-dc1pd-qfidsp:47202', False)` returns 200 (use a RAW string: `\a` in
  'Users\abhutra' is a bell char). `run_name=None` = official run, trade-level rows (430 for 2026-11-13, saved `official_risk_report_2026-11-13.csv`).
  `'AB Risk Run (Manual)'` returns only an aggregate row ('*').  Columns: Option Delta, Delta Equivalent Notional, CS 1%, Credit Spread Delta (1bp), Credit Spread Gamma (1bp), Theta, Vega, Vega 1%, Fair Value, ...
- Official report is in **USD**: EUR (iTraxx) options are x1.1023 vs our local-ccy numbers (PV, vega, delta all). Compare after FX conversion.
- Ours matches official EXACTLY (USD options): PV/Fair Value, Option Delta (0.028109), Vega, Theta ~.  Official "Delta Equivalent Notional" sign is opposite to our MarketDelta x Notional.
- **Official CS delta (1bp) = CENTRAL difference of option PV for index quote +/-1bp** (`p.Update(MarketQuote(s0+-1bp, CreditSpread))` -> `FairPrice x Notional`), within ~0.7% of official.
  `Sensitivities.Spread01` (all-curve bump) and forward-difference bumps do NOT tie (gamma is large). "CS 1%" = relative 1% bump of index spread (s0 x 1%).

## options_risk.ipynb (2026-11-17)
- `option_repricer.py` + `options_risk.ipynb`: live CDX/iTraxx options (Credit + Manual strategies, Abhishek traders) -> table of trade, index, strike, delta/gamma/vega as of
  `REPORT_DATE` (2026-11-16, from the pricer cache), then the same greeks re-computed on LIVE Bloomberg index spreads (vol constant, valuation date fixed, no theta).
- Bloomberg via the user's `bbg_utils` (`bdp_mm`/`bdh_mm`, xbbg; installed in the poetry venv): tickers `CDX IG CDSI S<n> 5Y Corp`, `ITRX EUR CDSI S<n> 5Y Corp`; PX_LAST is the spread in bp.
  Index-name map: `CDX-NAIGS47V1-5Y` -> S47, `ITRAXX-EUROPES46V1-5Y` -> S46. HY / Crossover are NOT mapped (raises) -- add patterns in `option_repricer._BBG` when options on them appear.
- Reprice = `p.Update(MarketQuote(bp*1e-4, CreditSpread))`, then **set `Notional` back** (Update resets to 1.0), then MarketDelta/MarketGamma/Vega. Repricing at the booked spread reproduces the base greeks exactly.
- Quantifi vs Bloomberg 11-16 close basis is tiny (52.11 vs 52.20, 59.91 vs 59.98, 60.28 vs 60.28 bp). `SPREAD_MODE='change'` uses booked + (live - BBG 11-16 close) to strip it.
- Local currency (EUR iTraxx NOT converted to USD). Credit and Manual hold the same 4 options twice -- do not sum both.
- Databricks (`claude_repos/databricks_utils`) is planned for tranche work later; not used yet.
