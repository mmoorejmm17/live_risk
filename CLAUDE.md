# Quantifi DSP / Live Risk — background

This folder is empty as of 2026-09-29. This file exists to hand off context from research
done elsewhere (in `S:\Structured Credit\Matt Stuff\quantifi_usethis\Quantifi_Dev`) to
whatever gets built here.

## Original goal

Matt wants to import trades into Quantifi via the **DSP (Data Science Platform)** — Quantifi's
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
