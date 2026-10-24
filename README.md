# 🍌 Banana Patterns — personal VCP swing-trading screener (NSE)

A personal end-of-day screener for NSE stocks, modelled on the ideas behind
[bananapatterns.com/screens](https://bananapatterns.com/screens) (especially its
**VCP — Volatility Contraction Pattern** screen), built on the **Zerodha Kite Connect** API.

> **Status: Phase 1 (Foundation) complete; Phase 2 in progress.** You can log in to Kite, load the
> Nifty 500, backfill ~8 years of adjusted daily candles into SQLite and browse them in the dashboard.
> Phase 2 so far: **base detection** (B1…Bn boxes, pivot, breakout markers, breakout card on every chart) and
> the **stage board** home page (Forming / Fresh breakouts / Climbing / Played out + "what changed since last
> close"), and **RS rating + Trend Template** on every stock. The VCP screen (PR #4) and card view (PR #5) are next. See [Roadmap](#roadmap) and [Development log](#development-log).

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
8. [Base detection (engine)](#base-detection-engine)
9. [Stage board (Screens home)](#stage-board-screens-home)
10. [RS rating and Trend Template](#rs-rating-and-trend-template)
11. [REST API](#rest-api)
12. [CLI](#cli)
13. [Configuration](#configuration)
14. [Frontend](#frontend)
15. [Testing](#testing)
16. [Troubleshooting](#troubleshooting)
17. [Roadmap](#roadmap)
18. [Decisions log](#decisions-log)
19. [Development log](#development-log)

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
│  └─ engine/
│     ├─ indicators.py        causal SMA, trailing stats
│     ├─ bases.py             base (consolidation box) detector + breakout metrics
│     ├─ stages.py            stage of a stock: forming / fresh / climbing / played_out
│     ├─ rs.py                RS score/rating 1-99, industry ranks
│     ├─ trend_template.py    Minervini Trend Template (8 checks)
│     ├─ scan.py              universe scan, RS/trend metrics, what-changed feed, stores results
│     └─ (next) swings, vcp, scoring
├─ scripts/sync.py            CLI over the same pipeline code
├─ tests/                     offline pytest suite with a fake Kite SDK
├─ frontend/
│  ├─ vite.config.js          :3000, proxies /api → :8000, builds to frontend/build
│  └─ src/App.jsx, App.css, services/api.js,
│     chart/basesPrimitive.js (canvas plugin drawing base boxes),
│     components/ScreensHome (stage board), StageList, TrendPanel, KiteBar, UniverseTable, StockChart, BaseCard,
│     DataPanel, SettingsPanel, LogsPanel
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
| `scan_results` | **PK (as_of, symbol)**, stage, payload JSON (base, pivot, breakout, gains, reasons) |
| `scan_changes` | per as-of date: symbol, kind (breakout / below_pivot / played_out / forming / left / climbing), sentence |
| `stock_metrics` | **PK (as_of, symbol)**, rs, rs_score, industry, industry rank/of, Trend Template passed/of, payload (all 8 checks) |
| `scan_runs` | per scan: as_of, ran_at, scanned, stale, counts per stage, dials used |
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

## Base detection (engine)

`backend/engine/bases.py` finds the **B1…Bn base boxes** drawn on the chart, modelled on the boxes on
bananapatterns.com's breakout cards. It is a pure function over daily candles: no DB, no Kite.

**Rules** (all of them are dials in **Settings → bases**):
- A **base** is a run of ≥ `min_base_bars` sessions (default 5 = 1 week) after a closing high, in which
  **no close goes above the pivot** and the drop from the pivot to the lowest low stays within
  `max_base_depth` (25%). It must follow an advance: the base high is ≥ `min_prior_advance` (20%) above the
  lowest low of the previous `prior_advance_bars` (63) sessions. Longer than `max_base_bars` (65 weeks) or
  deeper than allowed means a restart (deeper also restarts the count).
- **Pivot = highest close in the base** (`pivot_basis="close"`). Alternative: `"high"`, where wicks may lift
  the pivot at most `max_pivot_drift` (3%) above the base's first high; past that it is a slow grind up (a trend).
- **Breakout = the first close above the pivot.** It ends the base and is marked on the chart even on low
  volume (as the site does). `volume_ok` says whether volume ≥ `bo_vol_mult` (1.5×) × the baseline
  (`vol_baseline`: `sma50` per your brief, or `median20` as on the site's card). The screen's BREAKOUT stage
  (next step) requires `volume_ok`.
- **Numbering** B1, B2, … restarts at B1 after a close below the `reset_ma` (200) SMA, a base low that
  undercuts the previous base's low (`reset_on_undercut`), or a decline deeper than `max_base_depth`.
- **Weeks** = calendar days ÷ 7, rounded down (site convention: 24 days → "3w").
- **Causal:** the forward walk means a base and its breakout depend only on earlier bars, and a test checks that
  results don't change when later bars are added. The only outcome field is `squat` (closed back below the pivot
  within `squat_bars` sessions after the breakout). It is `null` ("pending") until those sessions exist.

**Breakout card metrics** (same definitions as the site's card): breakout date, breakout through %
(close ÷ pivot − 1), breakout volume vs the 50-day average **and** vs the 20-day median, close-in-range
((close − low) ÷ (high − low), NA when high = low), 1-day gain, breakout-day turnover (₹ cr), base depth,
and base volume vs the prior pace (mean volume in the base ÷ the `dryup_lookback` sessions before it). A forming base
shows the pivot, distance under the pivot, length, depth, dry-up and the base low.

**Checked against the site (23 Oct 2026, manual spot-check from screenshots; public daily prices used locally,
nothing scraped or committed):**

| | bananapatterns.com | this engine |
|---|---|---|
| ORIENTHOT latest base | 12w, pivot ₹146.54, BO 16/10/2026, through 3.59%, vol 8.12× (20-d median), CIR 0.82 | **identical** |
| NRL latest base | 3w, pivot ₹166.26, BO 16/10/2026, through 1.56%, vol 0.44×, CIR NA | **identical** |
| ORIENTHOT earlier boxes | B2 (Apr), B3 5w (May–Jun), B4 (Jun–Jul) | same boxes, same dates and pivots |
| Base number | ORIENTHOT **B6**, NRL **B5** | ours B3 and B6: the site's counting origin isn't published; no simple rule reproduced both |
| "squat" pill | shown (hollow) on both cards, though ORIENTHOT never closed back under its pivot | ours: closed under the pivot within 5 sessions (dial); the site's meaning is unclear |

Two findings from this check changed the defaults: the site's **pivot is the highest close**, not the
highest high, and its boxes are **tight** (≤ ~20% deep, from 1 week long).

## Stage board (Screens home)

The **Screens** tab is the home page, modelled on the site's VCP screen header. A headline ("N fresh breakouts in
the last 5 sessions") sits above four **stage cards** with counts. Tap a card to list its stocks, and tap a stock
to open its chart with the base boxes. The **What changed since last close** feed sits on the right.

**Stages** (`backend/engine/stages.py`, every threshold in **Settings → screen**). Each stock is classified on its
last bar using only past bars:

| Card | Rule |
|---|---|
| **Forming** | latest base still active, ≥ `min_forming_weeks` (3) weeks, close within `max_dist_to_pivot` (5%) under the pivot |
| **Fresh breakouts** | closed above a base pivot within the last `fresh_sessions` (5) sessions, still above it, run not ended, breakout volume-confirmed (`require_volume`; ≥ 1.5× the baseline) |
| **Climbing** | broke out earlier, still above the pivot, run not ended, volume-confirmed |
| **Played out** | the last breakout's run ended within `played_out_window` (20) sessions: a close below the `played_out_ma` (50) SMA or `played_out_drop` (8%) below the pivot |

- **Precedence:** Fresh > Forming > Climbing > Played out. A climber building its next base shows as Forming (e.g. B7).
- **Gates for every card:** close above the `trend_ma` (200) SMA, and median turnover over `turnover_window`
  (50) sessions ≥ `min_turnover_cr` (₹5 cr). Kite has no market cap, so this is turnover only.
- **Low-volume breakouts** (e.g. NRL at 0.44×) are not counted. They are listed under Fresh breakouts as
  "low-volume breakouts" so nothing is hidden. The site counts them, so our Fresh number can be lower.
- **Back under the pivot** without being played out is a `below_pivot` state: no card, but it appears in the feed.

**Scan** (`backend/engine/scan.py`): classifies every universe stock on its full history (**today**) and on the same
candles minus the last bar (**yesterday**). The feed is the difference, written as sentences:
"X broke out of B4: closed ₹…, +2.3% over the ₹… pivot on 2.1x volume", "X closed ₹…, back below the ₹… pivot it
broke out of", "X played out. Closed ₹…, closed below its 50-day line, +23.0% from the ₹… pivot (peak +31%)",
"X is newly forming B3: 2.4% under the ₹… pivot", "X left Forming: below its 200-day line".
Because yesterday is recomputed from candles, the feed never depends on an earlier scan having run.
Results are stored per as-of date (`scan_results`, `scan_changes`, `scan_runs`).

**When it runs:** automatically after every candle sync, at backend startup when the board is older than the data,
via **Rescan** (after changing dials), or with `python -m scripts.sync scan`.

## RS rating and Trend Template

Computed for **every** stock on each scan (`backend/engine/rs.py`, `backend/engine/trend_template.py`) and stored
in `stock_metrics`. All dials are in **Settings → trend**.

- **RS score** = 0.4·ROC(63) + 0.2·ROC(126) + 0.2·ROC(189) + 0.2·ROC(252), i.e. 3/6/9/12-month returns. This is the
  community approximation of IBD's RS, not IBD's formula. Young listings with ≥ 63 bars use the periods they have,
  with the weights renormalised.
- **RS rating** = the score's percentile among all scored stocks that day, 1–99 (ties share a rating).
  We rank against the Nifty 500, the site against ~5,000 stocks, so values differ (PNB: ours 76, site 71).
- **Industry RS**: NSE industries (20) ranked by the median RS of their stocks, e.g. "Healthcare: 2nd of 20".
  The site uses a finer industry map.
- **Trend Template**: eight checks, each shown with its value: price above the 150- and 200-day; 150-day above 200-day;
  200-day rising over `slow_rising_bars` (21); 50-day above 150 and 200; price above the 50-day; ≥ 30% above the
  52-week low; within 25% of the 52-week high; RS ≥ `min_rs` (70). Too little history fails a check rather than
  passing it.
- **In the UI:** RS and Trend (x/8) columns in Universe and the stage lists (sortable by RS); the chart shows an RS pill,
  industry rank, 52-week distances and the 8-check list.
- On 23 Oct 2026 (2 years of public prices, local QA) 16 of 497 stocks passed all eight checks.

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
| GET | `/api/screens` | available screens (`bases` now, `vcp` next) |
| GET | `/api/screens/{screen}/summary` | as-of date, counts per stage, number of changes, stale flag |
| GET | `/api/screens/{screen}/results?stage=` | stocks in `forming`, `fresh`, `fresh_low_volume`, `climbing` or `played_out` |
| POST | `/api/screens/scan` | rescan now (409 while a candle sync runs; the sync scans when it finishes) |
| GET | `/api/changes` | what changed since the last close (latest as-of date) |
| GET | `/api/stock/{symbol}/metrics` | latest RS rating, industry rank, Trend Template checks |
| GET | `/api/stock/{symbol}/bases?since=YYYY-MM-DD` | bases over full history (correct numbering), latest base, dials used |
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
.venv/bin/python -m scripts.sync candles [--full] [SYMBOL ...]   # sync all or some symbols, then scan
.venv/bin/python -m scripts.sync scan                   # recompute the stage board from stored candles
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

| `bases.*` | Default | Meaning |
|---|---|---|
| `pivot_basis` | close | pivot = highest close (matches site) or highest high |
| `min_base_bars` / `max_base_bars` | 5 / 325 | base length limits in sessions |
| `max_base_depth` | 0.30 | max drop from pivot to base low |
| `max_pivot_drift` | 0.03 | high basis only: wick drift allowed before it counts as a trend |
| `prior_advance_bars` / `min_prior_advance` | 63 / 0.10 | required advance into the base |
| `reset_ma` / `reset_on_undercut` | 200 / true | base-count restart rules |
| `vol_baseline` / `bo_vol_mult` | sma50 / 1.5 | breakout volume rule |
| `weak_close_cir` | 0.5 | close-in-range below this = weak close |
| `squat_bars` | 5 | close back under the pivot within N sessions = squat |
| `dryup_lookback` | 50 | sessions before the base for "volume vs prior pace" |

| `screen.*` | Default | Meaning |
|---|---|---|
| `min_forming_weeks` / `max_dist_to_pivot` | 3 / 0.05 | Forming card |
| `fresh_sessions` | 5 | Fresh breakouts window |
| `require_volume` | true | Fresh/Climbing need a volume-confirmed breakout |
| `played_out_ma` / `played_out_drop` / `played_out_window` | 50 / 0.08 / 20 | when a run ends, and how long it stays on the card |
| `trend_ma` | 200 | all cards: close above this SMA |
| `min_turnover_cr` / `turnover_window` | 5.0 / 50 | all cards: median turnover floor |

| `trend.*` | Default | Meaning |
|---|---|---|
| `rs_weights` / `rs_periods` | 0.4, 0.2, 0.2, 0.2 / 63, 126, 189, 252 | RS score weights and periods (sessions) |
| `rs_min_bars` | 63 | minimum history for an RS rating |
| `ma_fast` / `ma_mid` / `ma_slow` | 50 / 150 / 200 | Trend Template SMAs |
| `slow_rising_bars` | 21 | slow SMA rising over this many sessions (63 is stricter) |
| `min_above_low52` / `max_below_high52` | 0.30 / 0.25 | 52-week low/high checks |
| `min_rs` | 70 | RS check |

---

## Frontend

React 19 + Vite + lightweight-charts 5, with a dark theme. Vite replaces create-react-app (deprecated)
but keeps the same React stack as AlgoTrade-v3. The dev server is on :3000 with `/api` proxied to :8000.
`npm run build` writes `frontend/build`, which FastAPI serves at :8000.

Tabs: **Screens** (home: stage board, see above), **Universe** (sortable/filterable table + chart), **Data** (refresh, sync, full refetch, progress,
errors, run history), **Settings** (config sections `screen`, `bases`, `trend` and `ingest`; `vcp` next), **Logs**.

The **chart** shows daily candles, volume, SMA 50/150/200 and the **base overlay**:
- base boxes drawn by a canvas plugin (`chart/basesPrimitive.js`, lightweight-charts v5 series primitive):
  the latest base is green-shaded, earlier ones grey, each labelled "B6 · 12w"
- a dashed **pivot line** for the latest base ("pivot ₹146.54")
- **▲ breakout markers**: green when volume-confirmed, amber when below the volume rule
- a **"bases" checkbox** to hide or show the overlay, and under the chart the **breakout card** ("Broke out of B6" or "Forming B3")
  with the metrics above and tags (below volume rule, weak close, squat / squat pending). The top bar shows Kite status and token expiry, and re-checks every minute.

## Testing

```bash
.venv/bin/python -m pytest          # 58 tests, offline, ~10 s
```

A `FakeKite` (tests/conftest.py) mimics the SDK, and each test gets its own temporary SQLite DB.
Base detector tests (tests/test_bases.py) use synthetic series with known answers: a staircase of two bases,
too-short pauses, deep declines, no prior advance, a slow grind (high basis), a forming base, squat and pending,
the MA reset, the high-basis pivot, JSON-serialisable output and **causality** (prefix-stable results).
Stage tests (tests/test_stages.py) cover each card's rule, low-volume breakouts, both played-out triggers and the
window expiring, below-pivot, the climber-forming-next-base precedence, the liquidity/history gates, feed sentences,
and a scan that stores results and changes. Other coverage: throttle spacing, the 06:00 IST rule, date chunking, retry/abort classification,
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

### Phase 2: VCP engine *(in progress)*
- ✅ PR #3: RS rating, industry RS, Trend Template, base dials recalibrated against the site.
- Next, PR #4: VCP screen (tightening, dry-up, U/D ratio, contractions, quality filter, scoring). Then PR #5:
  card view with mini charts, a cards/list toggle, and contraction swings on the chart.
- ✅ `engine/bases.py` + chart overlay + breakout card (base boxes, pivot, breakout markers): see
  [Base detection](#base-detection-engine).
- `engine/indicators.py`: SMA 50/150/200, ATR%, 52-week high/low, volume SMA 50, median turnover.
- `engine/rs.py`: RS score = 0.4·ROC63 + 0.2·ROC126 + 0.2·ROC189 + 0.2·ROC252, percentile-ranked 1–99
  across the ingested universe (community approximation, not IBD's formula); industry RS from the NSE industry column.
- `engine/trend_template.py`: the Minervini Trend Template (all conditions as dials; 200-DMA slope lookback 21–63).
- `engine/swings.py`: **pluggable, causal** swing detection (ZigZag with threshold max(zz_pct, k·ATR%),
  plus fractal pivots), with no look-ahead (a pivot is confirmed only after the reversal).
- `engine/vcp.py`: inside each base, 2–N contractions, each ≤ shrink × previous, max depth, final tightness,
  volume dry-up, pivot = high of the last contraction, stop = last contraction low − buffer (capped at 8%).
- `engine/scoring.py`: weighted sort (RS 30, tightness 20, dry-up 15, pivot proximity 15, 52-week-high proximity 10, industry RS 10).
- A `vcp` config section with every threshold as a dial. A liquidity gate on median turnover ≥ ₹5 cr
  (Kite has no market cap, so the MVP is turnover-only).
- Unit tests on synthetic VCP / non-VCP series; visual QA by drawing detected contractions on real charts
  (toggle inside the base boxes).
- ✅ Stage board: `engine/stages.py`, `engine/scan.py`, Screens home with four cards and the what-changed feed.
- VCP screen in the screen selector: the same four cards, restricted to bases that pass the VCP checks.

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
| 2026-10-23 | Mark every close above the pivot as a breakout (tag weak/low-volume ones); the BREAKOUT stage and the screen keep the ≥ 1.5× volume rule |
| 2026-10-23 | Phase 2 starts with base detection + chart overlay so later detections can be checked by eye |
| 2026-10-23 | Pivot = highest **close** in the base and tight boxes (≤ 25%, from 1 week), following the spot-check against the site |
| 2026-10-23 | Base numbering kept as our own documented rule; the site's numbering origin is unpublished |
| 2026-10-24 | Home page = stage board (Forming / Fresh / Climbing / Played out + what-changed feed) for the base screen; VCP screen joins the selector next |
| 2026-10-24 | Played out = close below the 50-day SMA or 8% under the pivot, shown for 20 sessions; low-volume breakouts listed but not counted |
| 2026-10-24 | NSE `DUMMY*` placeholder rows in the index file are excluded from the universe |
| 2026-10-24 | Remaining Phase 2 split into three PRs: #3 RS + Trend Template, #4 VCP screen, #5 card view |
| 2026-10-24 | Base dials recalibrated to the site's VCP Forming bases: `max_base_depth` 0.30, `min_prior_advance` 0.10 |
| 2026-10-24 | The site's Forming isn't "within 5% of pivot" (its names sit 9–21% under); the VCP screen will use a quality filter |
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

### 2026-10-23: Phase 2, step 1: base boxes on the chart
- Gagan shared two breakout cards from the site (ORIENTHOT "B6 · 12w", NRL "B5 · 3w") and asked for the same base
  boxes. Agreed plan: start Phase 2 with base detection + chart overlay; mark every close above the pivot as a breakout and
  tag low-volume ones, while the BREAKOUT stage keeps the 1.5× rule.
- Merged PR #1 into `main` (Phase 1); restarted the branch from `main`, carrying over the `setup.sh` Python-discovery fix.
- Built `engine/bases.py` (causal forward walk), a `bases` config section, `/api/stock/{symbol}/bases`, a lightweight-charts
  canvas plugin for the boxes, a pivot price line, breakout markers, the breakout card, and a section switch in Settings.
- Spot-checked against the site using public daily prices locally (not committed). First run used highest-high pivots
  and 15-session/40% bases: the breakout dates, the 20-day-median volume ratios (8.12, 0.44) and close-in-range (0.82, NA)
  matched exactly, but the pivots were higher than the site's. The site's pivots equal the **highest close** in the base,
  to the paisa. After switching to close basis, calendar weeks and tighter boxes (≤ 25%, ≥ 1 week), every number on both
  cards matches and the earlier ORIENTHOT boxes line up. Base numbering still differs (site's rule unpublished).
- Bugs caught: highest-high pivots let a slow grind look like a base (fixed with the drift rule, then close basis); numpy
  bools broke JSON serialisation of the bases endpoint (fixed + test); config bools parsed "false" as true (fixed + test).
- 41 offline tests passing.


### 2026-10-24: Phase 2, step 2: stage board home page
- Gagan's first live run on the Mac: 499/499 symbols synced through 23 Oct. Two findings: the chart had no boxes
  because the Mac was on `main` (PR #2 not merged yet), and `DUMMYHEG` (an NSE placeholder row) showed as unmatched.
  `DUMMY*` rows are now excluded.
- Asked for the site's home cards (Forming / Fresh breakouts / Climbing / Played out + "what changed since last
  close"). Agreed rules: Fresh needs the 1.5× volume rule (low-volume ones listed separately); a run plays out on a
  close below the 50-day SMA or 8% under the pivot and stays on the card for 20 sessions.
- Built `engine/stages.py`, `engine/scan.py`, the `screen` config section, scan tables, screens/changes endpoints,
  scan after sync, at startup and on Rescan, the CLI `scan`, and the Screens home tab.
- QA on 2 years of public daily prices for the Nifty 500, loaded locally only: 497 stocks scanned in 2.5 s
  (5 forming, 7 fresh + 1 low-volume, 2 climbing, 34 played out, 10 moves as of 23 Oct). The stage lists and feed
  read correctly. One feed line matches the site's exactly: "PTCIL closed ₹23,455.00, back below the ₹23,830.00
  pivot it broke out of" (site: "PTC Industries closed ₹23,455, back below the ₹23,830 pivot it broke out of").
  HFCL's pivot also matches the site's (₹251.47). The site lists HFCL as played out; we don't track that run
  because its breakout was 0.41× volume (`require_volume`). Setting it to false would follow the site.
- Bug caught by the screenshot: scan rows were stored without their symbol (empty column, rows not clickable).
  Fixed, with tests.
- 52 offline tests passing.

### 2026-10-24: Phase 2, step 3 (PR #3): RS rating and Trend Template
- PR #2 merged. Gagan shared the site's VCP screen (Forming list with base depth, tightening, dry-up, now vs pivot,
  U/D ratio, RS; Fresh list with NRL and ORIENTHOT). Agreed: build the rest of Phase 2 as three separate PRs.
- Calibration on six of the site's eight VCP Forming names (BHAGERIA, SYNCOMF, BPLPHARMA, PREMEXPLN, TGVSL, PNB;
  public prices, local only): with `max_base_depth` 0.30 and `min_prior_advance` 0.10 our latest base matches the site's
  **pivot, base depth and distance under the pivot exactly on all six** (e.g. PNB: ₹132.36 pivot, 25.58%, −9.1%, a 40-week
  base from 16 Jan). ORIENTHOT and NRL still match too. The site's tightening / dry-up / U-D formulas could not be
  reproduced from ~60 candidates, so the VCP screen will use our own documented definitions.
- Built `engine/rs.py`, `engine/trend_template.py`, a `trend` config section, the `stock_metrics` table,
  `/api/stock/{symbol}/metrics`, RS/Trend columns and RS sort, and the trend panel on the chart. Config list values sent
  as text from Settings are now coerced to numbers.
- QA on 497 stocks: scan 3.5 s; PNB RS 76 (site 71, different universe); 16 stocks pass all 8 trend checks.
- 58 offline tests passing.

