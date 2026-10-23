# 🍌 Banana Patterns — personal VCP swing-trading screener (NSE)

A personal end-of-day screener for NSE stocks, modelled on the ideas behind
[bananapatterns.com/screens](https://bananapatterns.com/screens) (especially its
**VCP — Volatility Contraction Pattern** screen), built on the **Zerodha Kite Connect** API.

> **Status: Phase 1 (Foundation) complete.** You can log in to Kite, load the Nifty 500,
> backfill ~8 years of adjusted daily candles into SQLite and browse them in the dashboard.
> The VCP engine (Phase 2) is next. See [Roadmap](#roadmap) and [Development log](#development-log).

**Personal use only.** Screen-only mode: there is **no order or GTT code** in phases 1–4.
This is not investment advice; neither this project nor bananapatterns.com is a SEBI-registered
RA/IA. Offering signals to others would fall under a different regulatory regime.
bananapatterns.com's terms forbid scraping: it is used only for design inspiration and manual spot-checks.

---

## Contents
1. [Quick start (Mac)](#quick-start-mac)
2. [Daily workflow](#daily-workflow)
3. [Architecture](#architecture)
4. [Project layout (code guide)](#project-layout-code-guide)
5. [Database schema](#database-schema)
6. [Kite Connect integration](#kite-connect-integration)
7. [Data pipeline](#data-pipeline)
8. [REST API](#rest-api)
9. [CLI](#cli)
10. [Configuration](#configuration)
11. [Frontend](#frontend)
12. [Testing](#testing)
13. [Troubleshooting](#troubleshooting)
14. [Roadmap](#roadmap)
15. [Decisions log](#decisions-log)
16. [Development log](#development-log)

---

## Quick start (Mac)

Requirements: Python 3.12+, Node.js 18+, a Kite Connect app (the **same app as AlgoTrade-v3**).

```bash
git clone https://github.com/randan96mk/bananapatterns.git && cd bananapatterns
./setup.sh                      # venv, pip + npm install, .env from template, DB init
# edit .env: KITE_API_KEY, KITE_API_SECRET
./start.sh                      # backend :8000 + dashboard :3000
```

Or in VS Code: **Terminal → Run Task → "BP: Setup (first time)"**, then **"BP: Start (dev)"**.

Then in the dashboard (http://localhost:3000):
1. **Login with Kite** (top bar). You are redirected back, and the status turns *connected*.
2. **Data → 1. Refresh universe**. This downloads the Nifty 500 list from NSE and the Kite instruments.
3. **Data → 2. Sync candles**. The first run is a full backfill of ~1,000 calls at 3/s, roughly 6–7 minutes.
4. **Universe** tab: about 498 stocks (REITs excluded) plus the NIFTY 500 / NIFTY 50 indices. Click a row to open its chart.

> ⚠️ AlgoTrade-v3 shares the Kite app and port 8000. Only one token per app is active,
> so logging in here logs AlgoTrade out (and the other way round). Run one at a time.
> The Kite developer console redirect URL stays `http://localhost:8000/api/kite/callback`.

## Daily workflow

| When (IST) | What |
|---|---|
| Any time after 06:00 | Log in to Kite (tokens die at **06:00 IST** every day) |
| After ~15:45 | **Sync candles**. Before 15:45 today's bar is still forming, so the sync stops at yesterday. |
| Weekly / on index rebalances | **Refresh universe** (stored as a dated snapshot) |

Phase 4 automates the evening sync and alerts you if the day's login is missing.

---

## Architecture

```
 Browser (React 19 + Vite, :3000 dev / served by FastAPI in prod)
     │  /api/*
     ▼
 FastAPI (backend/main.py, :8000) ── api/routes.py
     │                    │
     │                    ├── ingest/universe.py ── NSE constituents CSV (httpx)
     │                    ├── ingest/candles.py  ── background worker thread (resumable)
     │                    │
     │                    └── kite_api/client.py ── token bucket (3 req/s) → pykiteconnect → Kite API
     ▼
 SQLite WAL  data/bananapatterns.db  (instruments, universe snapshots, candles, ingest status/runs, settings, logs)
```

**Stack:** Python 3.12+, FastAPI, SQLite (WAL), pandas/numpy (Phase 2), `kiteconnect` 5.2.2,
httpx, pytest; React 19, Vite 6, lightweight-charts 5.

**Shape borrowed from AlgoTrade-v3** (copied as patterns, nothing imported): Kite login and
callback routes, the dataclass config persisted to SQLite, the WAL DB helpers, logger,
FastAPI lifespan + CORS shell, `run.py`/`start.sh`/`setup.sh`/`.vscode/tasks.json`, the dark React shell.

**Deliberately not carried over:** the trendline strategy, order executor, intraday scheduler,
NFO-only instruments loader, and the old `historical_data` wrapper (it swallowed errors).
Also fixed: AlgoTrade treated a token as valid for the whole calendar day. Kite tokens die at 06:00 IST.

---

## Project layout (code guide)

```
bananapatterns/
├─ run.py                     uvicorn entry (BP_DEV=1 → auto-reload)
├─ setup.sh / start.sh        one-time setup / launcher (dev, --prod, --backend)
├─ requirements.txt  .env.example  pytest.ini  .vscode/tasks.json
├─ backend/
│  ├─ main.py                 FastAPI app, lifespan restores today's token, serves frontend/build
│  ├─ config.py               dataclass config sections; IST tz; db_path(); save/load to app_settings
│  ├─ api/routes.py           all REST endpoints + AppState (config, client, job, token source)
│  ├─ kite_api/
│  │  ├─ client.py            KiteClient: login, profile, instruments, historical_daily; retry/classify
│  │  ├─ throttle.py          thread-safe token bucket
│  │  └─ auth.py              06:00 IST expiry rule, store/load token
│  ├─ ingest/
│  │  ├─ universe.py          NSE CSV fetch/parse, Kite instrument filter, symbol→token join, snapshots
│  │  └─ candles.py           sync_symbol (backfill/incremental/adjustment refetch), run_sync, JobState
│  ├─ database/models.py      schema + data-access helpers
│  ├─ logging_config/logger.py file+console (all) and SQLite (WARNING+) logging
│  └─ engine/                 (Phase 2) indicators, rs, swings, trend_template, vcp, stages, scoring, diff
├─ scripts/sync.py            CLI over the same pipeline code
├─ tests/                     offline pytest suite with a fake Kite SDK
├─ frontend/
│  ├─ vite.config.js          :3000, proxies /api → :8000, builds to frontend/build
│  └─ src/App.jsx, App.css, services/api.js,
│     components/KiteBar, UniverseTable, StockChart, DataPanel, SettingsPanel, LogsPanel
├─ config/                    optional local inputs (fallback Nifty 500 CSV)
└─ data/  logs/               gitignored: SQLite DB, rotating log file
```

### Key modules in more detail

**`kite_api/client.py`**. Every call goes through `_call()`, which waits on a token bucket,
calls the SDK, and classifies failures using pykiteconnect's own exception types
(checked against the 5.2.2 source):

| SDK error | Handling |
|---|---|
| `TokenException` | `KiteAuthError`, never retried. The whole sync run aborts with "log in again". |
| `PermissionException` | `KitePermissionError`. Fatal (e.g. no historical data on the Personal plan). |
| `NetworkException`, HTTP 429, `DataException`, `GeneralException` ≥500, requests connection/timeout | Retried `max_retries` times with backoff `base·2^n` + 0–25% jitter, then `KiteRequestError` |
| `InputException` and anything else | `KiteRequestError` immediately. The run records it against the symbol and continues. |

`historical_daily()` splits ranges into ≤`chunk_days` (2,000) windows. 8 years means 2 calls per symbol.
It normalises dates to IST `YYYY-MM-DD` and de-duplicates. An empty list means "no bars", not an error.

**`kite_api/auth.py`**. `last_expiry_boundary(now)` is the most recent 06:00 IST, and a token is fresh
if it was issued after it. A token issued at 01:00 dies at 06:00 the same morning. One issued at 19:00 lives until 06:00 next day.

**`ingest/candles.py`**. See [Data pipeline](#data-pipeline).

**`api/routes.py`**. `AppState.token_source` is `db` (browser login or pasted token, expiry known),
`env` (`KITE_ACCESS_TOKEN`, expiry unknown) or `None`. `token_valid()` drops a DB token as soon as
06:00 passes, even if the server has been running all night.

---

## Database schema

SQLite in WAL mode at `data/bananapatterns.db` (override with `BP_DB_PATH`).

| Table | Purpose / key columns |
|---|---|
| `candles_daily` | **PK (instrument_token, date)**, open, high, low, close, volume. Adjusted daily OHLCV, `WITHOUT ROWID`. ~500 × 2,000 ≈ 1M rows. |
| `instruments` | Kite NSE dump filtered to `segment=NSE, instrument_type=EQ` and `segment=INDICES`. Replaced on each universe refresh. |
| `universe_snapshots` | **PK (snapshot_date, index_name, symbol)**, company, industry, series, isin. Dated membership history (survivorship-bias protection). |
| `ingest_status` | Per instrument: first/last date, bars, last full fetch, last incremental, last error |
| `ingest_runs` | Per sync run: kind, status (`running/ok/partial/failed/aborted`), counts, message |
| `app_settings` | key/value: `config.<section>` JSON, `kite_access_token`, `kite_token_issued_at` |
| `system_logs` | WARNING+ log lines for the Logs tab |

---

## Kite Connect integration

- **Plan:** historical candles need the paid **Kite Connect** plan (₹500/month per app). The free
  Personal plan has no market data. A `PermissionException` is surfaced if historical data isn't enabled.
- **Login flow:** `/api/kite/login-url` → Kite → `/api/kite/callback?request_token=…` →
  `generate_session(request_token, api_secret)` → token stored with its issue time → redirect to the dashboard.
  Manual alternative: paste a token in the top bar (`POST /api/kite/token`). TOTP or credential login is
  deliberately **not automated**.
- **Rate limits:** historical API 3 req/s (shared bucket; capacity 1, so calls are spaced evenly);
  other endpoints are limited to 5 req/s here (Kite allows 10). Kite allows 2,000 days per `day` call.
- **Static IP** (SEBI retail-algo framework, Apr 2026) applies only to order endpoints, so this
  screen-only app runs fine from a home Mac.
- **Verified in SDK 5.2.2 source** (for later phases): `place_order/modify_order` take `market_protection`
  (`-1` auto or 1–100%), and `place_gtt` exists. Not used before the gated automation phase.

---

## Data pipeline

### Universe (`ingest/universe.py`)
1. Download `ind_nifty500list.csv` (columns: Company Name, Industry, Symbol, Series, ISIN Code) from
   `nsearchives.nseindia.com`, falling back to `niftyindices.com`, then to `config/ind_nifty500list.csv`.
   Both URLs were checked working on 23 Oct 2026, but NSE URLs change, so they are config dials.
2. Store it as a dated snapshot in `universe_snapshots`.
3. Fetch `kite.instruments("NSE")` and keep EQ cash equities and indices.
4. Filter by `include_series` (default `EQ`, `BE`). The live list (23 Oct 2026) has **501** rows: 496 EQ,
   2 BE (HFCL, STLTECH: trade-for-trade) and 3 RR (REITs: Bagmane, Brookfield, Embassy). REITs are
   excluded as non-equities but stay in the snapshot.
5. Join on **tradingsymbol**: `SYMBOL` for EQ, `SYMBOL-SERIES` (e.g. `HFCL-BE`, Kite's convention) for other series.
   The Kite dump has no ISIN, which comes from the NSE CSV only. Unmatched symbols (renames,
   suspensions) are reported in the UI and API.
6. Index tokens for `NIFTY 500` and `NIFTY 50` (config `index_symbols`) are resolved for RS and regime work in Phase 2.

### Candles (`ingest/candles.py`)
Backfill and incremental sync share **one code path**, `sync_symbol()`:

- **No bars stored** → full fetch of `history_years` (8) ending at the last complete session.
- **Bars stored** → re-fetch from the date of the last `overlap_bars` (5) stored bars:
  - If any overlapping close differs by more than `adjust_tolerance` (0.5%), Kite has **re-adjusted history**
    (split, bonus, …). The symbol is wiped and refetched in full (`refetched`). This keeps the whole
    store corporate-action-adjusted without a separate corporate-actions feed.
  - Otherwise the new bars are upserted (`incremental`, or `uptodate` if there was nothing new).
- **Never** replaces stored history with an empty response (guard raises instead).
- **End date** = today if the time is ≥ `eod_ready_time` (15:45 IST), else yesterday, so a mid-day
  sync never stores a half-formed daily bar. Weekends and holidays need no calendar: Kite returns no bar.

`run_sync()` walks indices first, then the stocks. Each symbol commits on its own, so a run is
**resumable** (re-run and finished symbols are just `uptodate`). Per-symbol failures go to
`ingest_status.last_error` and the run ends `partial`. Auth or permission failures abort the run
immediately. A cancel flag stops it between symbols.

**Cost:** about 500 stocks + 2 indices × 2 chunks ≈ 1,004 calls ≈ 6 minutes for the first backfill.
An evening incremental run is about 502 calls ≈ 3 minutes. (Phase 4 can cut this further: Kite's
`quote` endpoint returns the day's OHLC for many symbols per call.)

---

## REST API

Interactive docs: http://localhost:8000/docs

| Method | Path | Description |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/api/kite/login-url` | Kite login URL (400 if keys missing) |
| GET | `/api/kite/callback` | OAuth redirect target; stores token, redirects to dashboard |
| POST | `/api/kite/token` | `{access_token}`: store a token obtained elsewhere |
| GET | `/api/kite/status?verify=true` | token state, `expires_at`, `needs_login`; `verify` calls `profile()` |
| GET/PUT | `/api/config/{section}` | read / update a config section (`ingest` now, `vcp` in Phase 2) |
| POST | `/api/universe/refresh` | download Nifty 500 + Kite instruments; returns mapping summary |
| GET | `/api/universe` | latest snapshot with per-symbol coverage, last close, change %, last error |
| GET | `/api/stock/{symbol}/candles?limit=750` | daily candles (stocks or indices, e.g. `NIFTY 500`) |
| POST | `/api/ingest/sync` | `{force_full?, symbols?}`: start background sync (409 if running, 401 if no token) |
| POST | `/api/ingest/cancel` | stop after the current symbol |
| GET | `/api/ingest/status` | live job progress + last 10 runs |
| GET | `/api/logs?level=` | WARNING/ERROR log lines |

---

## CLI

The same pipeline without the UI (log in via the dashboard first, or store a token):

```bash
.venv/bin/python -m scripts.sync status                 # token validity + data summary + last runs
.venv/bin/python -m scripts.sync token <ACCESS_TOKEN>   # store a token
.venv/bin/python -m scripts.sync universe               # refresh Nifty 500 + instruments
.venv/bin/python -m scripts.sync candles [--full] [SYMBOL ...]   # sync all or some symbols
```

---

## Configuration

**`.env`** (never committed): `KITE_API_KEY`, `KITE_API_SECRET`, optional `KITE_ACCESS_TOKEN`,
`KITE_REDIRECT_URL` (default `http://localhost:8000/api/kite/callback`), optional `BP_DB_PATH`,
`BP_DEV=1` (reload), `BP_FRONTEND_URL` (post-login redirect target).

**Runtime dials:** edited in **Settings**, saved in `app_settings`, applied without a restart.

| `ingest.*` | Default | Meaning |
|---|---|---|
| `history_years` | 8 | backfill depth |
| `hist_rate_per_sec` | 3.0 | historical API rate |
| `max_retries` / `backoff_base_sec` | 5 / 1.0 | retry policy |
| `chunk_days` | 2000 | days per historical call |
| `overlap_bars` / `adjust_tolerance` | 5 / 0.005 | re-adjustment detection |
| `min_bars_warn` | 260 | flag young listings (future IPO-base screen) |
| `eod_ready_time` | 15:45 | IST time after which today's bar is final |
| `universe_urls` | NSE, niftyindices | constituents CSV sources (tried in order) |
| `include_series` | EQ, BE | constituent series to ingest (REITs = RR excluded) |
| `index_symbols` | NIFTY 500, NIFTY 50 | indices to ingest |

---

## Frontend

React 19 + Vite + lightweight-charts 5, with a dark theme. Vite replaces create-react-app (deprecated)
but keeps the same React stack as AlgoTrade-v3. The dev server is on :3000 with `/api` proxied to :8000.
`npm run build` writes `frontend/build`, which FastAPI serves at :8000.

Tabs: **Universe** (sortable/filterable table + chart with volume and SMA 50/150/200), **Data**
(refresh, sync, full refetch, progress, errors, run history), **Settings** (generic config editor,
reused for the VCP dials in Phase 2), **Logs**. The top bar shows Kite status and token expiry, and re-checks every minute.

## Testing

```bash
.venv/bin/python -m pytest          # 29 tests, offline, ~8 s
```

A `FakeKite` (tests/conftest.py) mimics the SDK, and each test gets its own temporary SQLite DB.
Coverage includes: throttle spacing, the 06:00 IST rule, date chunking, retry/abort classification,
universe parsing/fallback/joining, full → incremental → up-to-date sync, split → full refetch, the
empty-response guard, per-symbol failure recording, auth abort, and the API end to end
(login callback → universe → background sync → universe/candles endpoints → token expiry while running).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `setup.sh`: "Python 3.12+ not found" | macOS's `python3` is 3.9. `brew install python@3.12`, or `BP_PYTHON=/path/to/python3.12 ./setup.sh` |
| "Kite login required" after a night | Expected: tokens die at 06:00 IST. Log in again. |
| Run `aborted`: permission denied | Historical data needs the Kite Connect plan on this API key. |
| AlgoTrade-v3 suddenly logged out | Same Kite app, and only one token is active. Re-login in whichever you are using. |
| Universe refresh fails (NSE 403) | Save the CSV to `config/ind_nifty500list.csv` (see config/README.md). |
| Symbols listed as *excluded* | Their series isn't in `include_series` (REITs by default). Edit in Settings. |
| Symbols listed as *unmatched* | Usually a rename or suspension. Check the NSE symbol vs Kite's tradingsymbol. |
| Port 8000 busy | `start.sh` frees 8000/3000. Otherwise `lsof -ti tcp:8000 \| xargs kill`. |
| Details of a failure | Logs tab (WARNING+), full log in `logs/bananapatterns.log` |

---

## Roadmap

Each phase is explained first, built after approval, and shown before the next one starts.

### Phase 2: VCP engine *(next)*
- `engine/indicators.py`: SMA 50/150/200, ATR%, 52-week high/low, volume SMA 50, median turnover.
- `engine/rs.py`: RS score = 0.4·ROC63 + 0.2·ROC126 + 0.2·ROC189 + 0.2·ROC252, percentile-ranked 1–99
  across the ingested universe (community approximation, not IBD's formula); industry RS from the NSE industry column.
- `engine/trend_template.py`: the Minervini Trend Template (all conditions as dials; 200-DMA slope lookback 21–63).
- `engine/swings.py`: **pluggable, causal** swing detection (ZigZag with threshold max(zz_pct, k·ATR%),
  plus fractal pivots), with no look-ahead (a pivot is confirmed only after the reversal).
- `engine/vcp.py`: base window, 2–N contractions, each ≤ shrink × previous, max depth, final tightness,
  volume dry-up, pivot = high of the last contraction, stop = last contraction low − buffer (capped at 8%).
- `engine/stages.py`: FORMING / BREAKOUT (close > pivot, previous close ≤ pivot, volume ≥ 1.5× SMA50 volume) / CLIMBING (≤ 5% above pivot).
- `engine/scoring.py`: weighted sort (RS 30, tightness 20, dry-up 15, pivot proximity 15, 52-week-high proximity 10, industry RS 10).
- A `vcp` config section with every threshold as a dial. A liquidity gate on median turnover ≥ ₹5 cr
  (Kite has no market cap, so the MVP is turnover-only).
- Unit tests on synthetic VCP / non-VCP series; visual QA by drawing detected contractions on real charts.

### Phase 3: React UI
Pipeline board (forming → breakout → climbing); stock drawer with annotated chart (contractions, pivot
line, breakout marker, stop) and a plain-English summary (base length, depth, distance to pivot, RS rank,
contraction ratio, volume vs prior pace); dial sliders with named presets; what-changed-since-yesterday
feed; watchlist ("waiting well").

### Phase 4: daily EOD job and alerts
Scheduler (APScheduler in-process, or launchd so it survives restarts) around 18:00–19:00 IST on trading
days: sync → scan → persist results and diffs. NSE holiday calendar. Alert when the day's Kite login is
missing. Optional NSE CM-UDiFF bhavcopy cross-check later in the evening (unadjusted, so compare
only on days without corporate actions). Note: a sleeping Mac will not run jobs (`pmset` wake schedule or keep awake).

### Later (only after approval)
- **Market context:** breadth, sector rotation, industry map, all breakouts today, regime filter
  (Nifty 500 vs its 50-DMA, breakout counts).
- **More screens:** Blue sky (ATH bases), Multi-year breakout, IPO base (young listings with < 260 bars).
  VCP ∩ Blue sky as the top-quality subset. X-ray of every base a stock ever built.
- **Backtester:** next-day entry, 8% stop or 50-DMA trail, Indian costs (STT, stamp duty, DP, slippage),
  walk-forward, survivorship-aware using `universe_snapshots`. Score weights tuned only here.
- **Paper trading:** qty = floor(equity·risk% / (entry − stop)), risk 0.5–1.5%, ≤ 5 positions, ≤ 2 new per day,
  no entry > 5% above pivot, regime filter.
- **Gated GTT automation:** static IP, CONFIRM mode before AUTO, idempotency keys, kill switch, audit log,
  LIMIT orders with `market_protection`.
- **Scale:** all liquid NSE stocks; DuckDB/Parquet for analytics; bulk EOD via quotes.

---

## Decisions log

| Date | Decision |
|---|---|
| 2026-10-23 | Repo `randan96mk/bananapatterns`; universe = Nifty 500 (NSE EQ); React frontend; phases 1–4 first |
| 2026-10-23 | Breakout = **daily close** above pivot with volume ≥ 1.5× 50-day average volume (not intraday) |
| 2026-10-23 | No order/GTT code in phases 1–4; default mode screen-only; secrets only in `.env` |
| 2026-10-23 | Reuse the **same Kite app** as AlgoTrade-v3 → same port 8000 and callback; run one at a time |
| 2026-10-23 | Backfill depth **8 years** (2 calls/symbol), so the later backtester has history |
| 2026-10-23 | Phase 1 ships a minimal React UI (login, data pipeline, universe table, chart) |
| 2026-10-23 | Vite instead of create-react-app (CRA deprecated); still React 19 + lightweight-charts |
| 2026-10-23 | Token validity = issued after the last **06:00 IST** (fixes AlgoTrade's calendar-day check) |
| 2026-10-23 | Corporate actions handled by overlap re-check → full refetch, not a separate CA feed |
| 2026-10-23 | Ingest series EQ + BE (trade-for-trade equities); exclude REITs (RR) from the stock universe (dial) |
| 2026-10-23 | `main` started with an initial `.gitignore` commit so Phase 1 can be reviewed as a PR into it |

## Development log

### 2026-10-23: Phase 1: Foundation
- Read AlgoTrade-v3 and pykiteconnect 5.2.2 source. Confirmed: `historical_data(token, from, to, interval, continuous, oi)`,
  the typed exceptions, `market_protection` on orders, `place_gtt`. Corrected two brief assumptions:
  8 years needs 2 historical calls per symbol (2,000-day limit), and the Kite instruments dump has no ISIN.
- Checked that both NSE Nifty 500 CSV URLs respond. The live file has 501 rows including 2 BE and 3 REIT (RR)
  rows, which led to the `include_series` dial and the `SYMBOL-BE` join.
- Built: config sections persisted to SQLite; WAL schema; logger (WARNING+ to DB); Kite client with token
  bucket, retries and error classification; the 06:00 IST token rule; universe snapshots and symbol join;
  candle sync with adjustment detection, empty-response guard and EOD cutoff; REST API; CLI; React shell.
- Bugs found while testing and fixed: a token-bucket float-drift infinite loop (elapsed·rate landing at
  0.9999999 with a sub-ULP sleep); the run progress counter counting an aborted symbol as done.
- Visual QA with Playwright against a seeded demo DB (universe table, chart, data tab).
- First Mac run: `setup.sh` rejected the system Python 3.9 and didn't look in Homebrew paths. It now searches
  Homebrew (Apple silicon + Intel), pyenv and python.org locations for 3.12–3.14, accepts `BP_PYTHON`,
  and rebuilds a `.venv` made with an old Python.
- 29 offline tests passing. **Not yet run against live Kite**: the first real login and backfill happen on
  Gagan's Mac (see the checklist in the PR).
