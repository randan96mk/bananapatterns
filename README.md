# 🍌 Banana Patterns — personal VCP swing-trading screener (NSE)

A personal end-of-day screener for NSE stocks, modelled on the ideas behind
[bananapatterns.com/screens](https://bananapatterns.com/screens) (especially its
**VCP — Volatility Contraction Pattern** screen), built on the **Zerodha Kite Connect** API.

> **Status: Phase 1 (Foundation) complete; Phase 2 in progress.** You can log in to Kite, load the
> Nifty 500, backfill ~8 years of adjusted daily candles into SQLite and browse them in the dashboard.
> Phase 2 so far: **base detection** (B1…Bn boxes, pivot, breakout markers, breakout card on every chart) and
> the **stage board** home page (Forming / Fresh breakouts / Climbing / Played out + "what changed since last
> close"), **RS rating + Trend Template** on every stock, and the **VCP screen** (classic VCP rules, scoring, VCP check on
> every chart), plus **Telegram alerts and the automatic evening job** (Phase 4 core). The card view with mini
> charts is next. See [Roadmap](#roadmap) and [Development log](#development-log).

**Personal use only.** Screen-only mode: there is **no order or GTT code** in phases 1–4.
This is not investment advice; neither this project nor bananapatterns.com is a SEBI-registered
RA/IA. Offering signals to others would fall under a different regulatory regime.
bananapatterns.com's terms forbid scraping: it is used only for design inspiration and manual spot-checks.

---

## Contents
1. [Quick start (Mac)](#quick-start-mac)
2. [Run with Docker](#run-with-docker-mac-later-a-cloud-server)
   - [Cloud (Oracle + Tailscale)](#cloud-oracle--tailscale): full runbook in [docs/CLOUD.md](docs/CLOUD.md)
3. [Daily workflow](#daily-workflow)
4. [Architecture](#architecture)
5. [Project layout (code guide)](#project-layout-code-guide)
6. [Database schema](#database-schema)
7. [Kite Connect integration](#kite-connect-integration)
8. [Data pipeline](#data-pipeline)
9. [Base detection (engine)](#base-detection-engine)
10. [Stage board (Screens home)](#stage-board-screens-home)
11. [RS rating and Trend Template](#rs-rating-and-trend-template)
12. [VCP screen](#vcp-screen)
13. [Cup & handle screen](#cup--handle-screen)
14. [Index ticker bar](#index-ticker-bar)
15. [Bull flag screen](#bull-flag-screen)
16. [Results calendar](#results-calendar)
17. [Exchange filings (news)](#exchange-filings-news)
18. [Red flags and fund buying](#red-flags-and-fund-buying)
19. [Telegram alerts and automatic jobs](#telegram-alerts-and-automatic-jobs)
20. [REST API](#rest-api)
21. [CLI](#cli)
22. [Configuration](#configuration)
23. [Frontend](#frontend)
24. [Testing](#testing)
25. [Troubleshooting](#troubleshooting)
26. [Roadmap](#roadmap)
27. [Decisions log](#decisions-log)
28. [Development log](#development-log)
---

## Quick start (Mac)

Requirements: Python 3.12+, Node.js 18+, a Kite Connect app with historical data.

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

> The Kite developer console redirect URL is `http://localhost:8000/api/kite/callback`. Only one token per Kite
> app is active: if another program uses the same API key, a login in either one logs the other out.

## Cloud (Oracle + Tailscale)

The app runs 24×7 on a free **Oracle Cloud** server (Ubuntu 24.04 ARM, Docker) and is opened from the Mac,
iPhone and Android at `https://banana.<tailnet>.ts.net`, a private **Tailscale** address with HTTPS that nobody
outside your Tailscale account can reach. **[docs/CLOUD.md](docs/CLOUD.md)** is the complete runbook: first-time
setup step by step (Oracle account, network, server, Tailscale, deploy key, switch-over from the Mac, phones),
daily use, updating after a merged PR, rollback, backups and restore, maintenance, troubleshooting and a command
cheat sheet.

The short version, on the server (`ssh ubuntu@banana`, `cd ~/bananapatterns`):
```bash
./docker.sh update                 # after a PR is merged: pull + rebuild + restart
./docker.sh status | logs          # is it healthy / what is it doing
scripts/backup.sh                  # backup now (nightly at 23:30 via cron: scripts/backup.sh --install-cron)
```
Scripts: `scripts/server-setup.sh` (prepares a fresh Ubuntu server: updates, IST clock, swap, Docker, Tailscale,
deploy key, reachability check), `scripts/backup.sh` (database + `.env` + `config/`, integrity-checked, 14 days,
Telegram on failure), `scripts/pull-backups.sh` (copies the server's backups to the Mac).
Run **one copy** only: the cloud and the Mac share one Kite API key, one Kite redirect URL and one Telegram bot.

---

## Run with Docker (Mac, later a cloud server)

The same app in one container: the dashboard is built inside the image and served by the backend on port 8000,
with the scheduler, Telegram bot and alerts running as usual. Your existing `data/`, `logs/` and `config/`
folders are mounted in, so it uses the **same database, settings and Kite login** as `./start.sh`.

**One time:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/) or
[OrbStack](https://orbstack.dev) (both free for personal use; pick the Apple Silicon or Intel build) and open it.
Keep your `.env` as it is: it is read at start-up and never copied into the image.

```bash
./docker.sh up        # start in the background (the first time it builds the image, ~3-5 min)
./docker.sh logs      # follow what it is doing (Ctrl+C stops following, not the app)
./docker.sh status    # running / healthy?
./docker.sh update    # after a PR is merged: git pull + rebuild + restart (~1 min)
./docker.sh rebuild   # rebuild from the code as it is (no pull), e.g. after checking out an older version
./docker.sh down      # stop
```

- Dashboard: **http://localhost:8000**. The port is open to this Mac only, and the Kite redirect URL stays
  `http://localhost:8000/api/kite/callback` (no Kite console change).
- It restarts on its own after a crash, and starts again whenever Docker Desktop / OrbStack starts (set it to
  open at login).
- **Run one copy only.** `./docker.sh up` refuses while port 8000 is busy, and `./start.sh` refuses while the
  Docker copy runs: two copies would send every alert twice. If you use the launchd agent
  (`scripts/macos/`), unload it when switching to Docker.
- A sleeping Mac pauses Docker as well; for alerts with the Mac off, the same image runs on a cloud server.
- Back to the normal way at any time: `./docker.sh down`, then `./start.sh`.
- Files: `Dockerfile` (Node stage builds the dashboard, Python 3.12 slim runs the app as a non-root user,
  IST time zone, `/health` check), `docker-compose.yml` (restart policy, `.env`, the three folders, log
  rotation), `.dockerignore` (keeps `.env`, `data/`, `logs/`, `node_modules`, `.venv` out of the image),
  `docker.sh`. `BP_HOST=0.0.0.0` inside the container; `./start.sh` keeps `127.0.0.1`. The container runs as
  your user (`docker.sh` passes `BP_UID` / `BP_GID`), so it can always write `data/` and `logs/`; only
  `update` rebuilds, so a plain `up` needs no internet.

## Daily workflow

| When (IST) | What |
|---|---|
| Any time after 6:00 AM | Log in to Kite (tokens die at **6:00 AM IST** every day) |
| After ~3:45 PM | **Sync candles**. Before 3:45 PM today's bar is still forming, so the sync stops at yesterday. |
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

Banana Patterns is standalone: it reads market data only and places no orders. Kite tokens die at 6:00 AM IST,
whenever they were issued.

---

## Project layout (code guide)

```
bananapatterns/
├─ run.py                     uvicorn entry (BP_DEV=1 → auto-reload)
├─ setup.sh / start.sh        one-time setup / launcher (dev, --prod, --backend)
├─ Dockerfile, docker-compose.yml, .dockerignore, docker.sh   run in Docker (see "Run with Docker")
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
│  │  ├─ holidays.py          NSE trading calendar (config/nse_holidays.json, weekly refresh from NSE)
│  │  ├─ universe.py          NSE CSV fetch/parse, Kite instrument filter, symbol→token join, snapshots
│  │  └─ candles.py           sync_symbol (backfill/incremental/adjustment refetch), run_sync, JobState
│  ├─ database/models.py      schema + data-access helpers
│  ├─ logging_config/logger.py file+console (all) and SQLite (WARNING+) logging
│  ├─ alerts/
│  │  ├─ telegram.py          Telegram Bot API client (send text/photo, detect chat), 1 msg/s, retries
│  │  ├─ charts.py            optional matplotlib loader + simple chart PNG
│  │  ├─ decision.py          decision chart: numbered markers + verdict/checklist/levels panel, message lines
│  │  ├─ messages.py          alert / digest text (HTML, escaped, chunked)
│  │  ├─ dispatch.py          scan -> alerts, sent once (sent_alerts)
│  │  ├─ priority.py          A/B gate, market line + caution on alerts, market-aware size
│  │  ├─ intraday.py          NSE volume curve, volume / close verdicts, per-day intraday flags
│  │  ├─ login.py             Kite login-link alert, session check (profile), login confirmation
│  │  ├─ commands.py          Telegram /login and /status (long-poll getUpdates, own chat only)
│  │  └─ jobs.py              scheduler: login reminder, evening job, intraday heads-up, weekly refresh
│  └─ engine/
│     ├─ indicators.py        causal SMA, trailing stats
│     ├─ bases.py             base (consolidation box) detector + breakout metrics
│     ├─ stages.py            stage of a stock: forming / fresh / climbing / played_out
│     ├─ market.py            market health signals + status, A/B tier
│     ├─ rs.py                RS score/rating 1-99, industry ranks
│     ├─ trend_template.py    Minervini Trend Template (8 checks)
│     ├─ scan.py              universe scan, RS/trend metrics, what-changed feed, stores results
│     ├─ vcp.py               VCP metrics (tightening, dry-up, U/D, ZigZag contractions, stop) + rules
│     └─ scoring.py           0-100 sort score for the VCP screen
├─ scripts/sync.py            CLI over the same pipeline code
├─ tests/                     offline pytest suite with a fake Kite SDK
├─ frontend/
│  ├─ vite.config.js          :3000, proxies /api → :8000, builds to frontend/build
│  └─ src/App.jsx, App.css, services/api.js,
│     chart/basesPrimitive.js (canvas plugin drawing base boxes),
│     components/AlertsPanel (Telegram + jobs), ScreensHome (stage board), StageList, TrendPanel, VcpPanel, KiteBar, UniverseTable, StockChart, BaseCard,
│     DataPanel, SettingsPanel, LogsPanel
├─ config/                    nse_holidays.json (official 2026 list), optional fallback Nifty 500 CSV
├─ scripts/macos/             optional launchd agent to start the app at login
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

**`kite_api/auth.py`**. `last_expiry_boundary(now)` is the most recent 6:00 AM IST, and a token is fresh
if it was issued after it. A token issued at 1:00 AM dies at 6:00 AM the same morning. One issued at 7:00 PM lives until 6:00 AM next day.

**`ingest/candles.py`**. See [Data pipeline](#data-pipeline).

**`api/routes.py`**. `AppState.token_source` is `db` (browser login or pasted token, expiry known),
`env` (`KITE_ACCESS_TOKEN`, expiry unknown) or `None`. `token_valid()` drops a DB token as soon as
6:00 AM passes, even if the server has been running all night.

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
| `scan_results` | **PK (as_of, screen, symbol)**, stage, payload JSON (base, pivot, breakout, gains, RS, trend, VCP metrics, score) |
| `scan_changes` | per as-of date and screen: symbol, kind (breakout / below_pivot / played_out / forming / left / climbing), sentence |
| `stock_metrics` | **PK (as_of, symbol)**, rs, rs_score, industry, industry rank/of, Trend Template passed/of, payload (all 8 checks) |
| `scan_runs` | per scan: as_of, ran_at, scanned, stale, counts per stage, dials used |
| `sent_alerts` | **PK key** (dedup), sent_at, kind, symbol, text, ok, error: every alert sent or attempted |
| `job_runs` | per automatic job run: job, run_date, started/finished, status (`ok/partial/failed/no_session/waiting/retry`), message |
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
- **One token per Kite app:** a new login replaces the previous token, so don't run another program on the
  same API key at the same time.
- **Login from Telegram:** see [Kite login link](#kite-login-link).
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

**Grid view** (default; **▦ Grid / ☰ Table** toggle, remembered per browser): each stock is a card with a
~6-month mini chart: candles, the base box (shaded), pivot line, 50-day line, ▲ breakout (amber when below the
volume rule), ✕ the day a run ended. Under the chart: price and change, then stage facts (forming: distance under
the pivot and depth; fresh: breakout date, % over the pivot, volume; climbing: sessions, gain, peak; played out: end
date, reason, result), RS, trend and, on the VCP screen, score, pullbacks, dry-up and stop. Forming names within 3%
of the pivot get a highlighted border. Click a card for the full chart. Mini charts are SVG
(`components/StageGrid.jsx`) fed by `GET /api/screens/{screen}/sparks?stage=…&bars=120`. The base box needs the
`base_start` / `base_end` / `base_low` fields saved by the scan: press **Rescan** once after updating.

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

## VCP screen

The default screen in the **Screens** dropdown (**VCP** / **Bases (all)**): the same four cards and feed, but only for
bases that pass the VCP rules. Defaults follow the classic Minervini VCP from the project brief, and every rule is a
dial in **Settings → vcp**.

**Metrics** (`backend/engine/vcp.py`; our own definitions, because the site's formulas are unpublished and ~60 candidates
failed to reproduce them):

| Metric | Definition |
|---|---|
| Pullbacks (contractions) | Inside the base, causal ZigZag swings: a swing is confirmed only after price reverses by max(5%, 2 × ATR%). Each pullback's depth = (swing high − swing low) ÷ swing high. The current, still-open pullback counts once it is that deep. |
| Tightening (ATR ratio) | ATR over the last 10 sessions ÷ the base's mean true range (< 1 = narrowing) |
| Volume dry-up | mean volume of the last 10 sessions ÷ mean volume of the 50 sessions before the base |
| Up/down volume ratio | volume on up days ÷ volume on down days over 50 sessions (shown, not a rule) |
| Stop | last pullback's low − 1%, never more than 8% under the pivot |
| Score (0–100) | RS 30, tightening 20, dry-up 15, pivot proximity 15, 52-week-high proximity 10, industry RS 10 (weights are dials, untuned until the backtester) |

**Rules (all must pass):** base ≥ 3 weeks and ≤ 30% deep; 2–6 pullbacks; last pullback ≤ 12% and ≤ 0.8× the largest
earlier one; tightening ≤ 1.0; dry-up ≤ 0.85; RS ≥ 70; at least 5 of the 8 Trend Template checks. Plus the board's gates
(above the 200-day, turnover ≥ ₹5 cr).

**Stages on the VCP screen:** Forming = the active base is a VCP and the close is within 25% under the pivot (the
site's VCP names sit 9–21% under theirs). Fresh / Climbing / Played out = the last breakout came out of a base that was a
VCP **on the bar before the breakout**, so the judgement is causal. Lists sort by score; the Bases screen tags VCP names.
The breakout volume rule (≥ 1.5×) is kept; low-volume breakouts stay listed separately.

**Why stricter than the site:** five of the six site VCP names checked aren't classic VCPs. Their latest pullback is the
deepest (BHAGERIA, SYNCOMF ~20%), or the base has one contraction (TGVSL). With the classic rules, PREMEXPLN (pullbacks
7.7 → 16 → 14 → 5.7%) passes and the others don't. To approximate the site, relax `min_contractions`,
`require_shrinking` and `max_final_contraction`. On 23 Oct 2026 (Nifty 500, 2 years of public prices, local QA) the VCP
screen finds 7 forming (CUB, PAYTM, CRAFTSMAN, STARHEALTH, ATHERENERG, DLF, SOLARINDS), 0 fresh, 0 climbing and 1 played out.

On every chart, **VCP check** shows the verdict, any failed rules ("last pullback 14.0% > 12%"), the pullback
sequence, tightening, dry-up, U/D ratio and stop.

## Cup & handle screen

`backend/engine/cups.py` finds William O'Neil's cup with handle, looking only at past bars. Pick **Cup & handle** in
the screen dropdown; it uses the same stage cards, feed, grid, alerts, decision charts, A/B tiers, daily plan and
position guard as VCP. Every rule is in **Settings → cup**:

| Part | Default rule |
|---|---|
| Prior uptrend | ≥ 30% rise into the left lip (from the low of the 130 sessions before it) |
| Cup | 7–65 weeks lip to lip, 12–33% deep, nothing above the rim, right lip within −10% / +5% of the left |
| U-shape | ≥ 5 sessions within 5% of the bottom **and** ≥ 25% of closes in the cup's bottom third (rejects spike / V bottoms) |
| Handle | 1–5 weeks, 2–12% deep, low in the cup's upper half, volume ≤ 1.0× the 50-day average |
| Pivot / stop | pivot = the handle's intraday high; stop = handle low − 1%, never more than 8% under the pivot |
| Screen | Forming = in the handle within 5% of the pivot; RS ≥ 70 and Trend Template ≥ 5/8, as for VCP |

Score (0–100): handle tightness 30, handle volume dry-up 20, U-shape 15, cup depth 15, RS 20. The decision chart and
the grid cards draw the cup as a smooth green **U** from lip to lip (each side's roundness fitted to the stock's lows,
so a fast drop and a long flat bottom draw a wide, flat U) and the handle as a yellow curve back up to the pivot, with
**L** / **U** / **R** marks. On the cup screen a grid card reaches back to just before the left lip. The checklist
shows cup depth and length, the U-shape, the handle and its volume. Cups appear on the Bases screen with a **C&H**
tag. Only cups *with* a handle count (no cup-without-handle).

**Alerts.** Cup & handle is on in **Alerts → screens** by default next to VCP (an existing saved setting gets it added
once; untick it to turn it off). Cups get the same alerts as VCPs: breakout, near-pivot, intraday heads-up, the 3:15 PM
close check, the daily plan and the position guard, with the cup's own stop (handle low − 1%) and a
"Cup · handle · vol · stop" line. A name on several screens alerts once per kind, from the most specific screen
(VCP, then Cup & handle, then Bases).

## Index ticker bar

A strip under the top menu on every page: **Nifty 50 · Nifty 500 · Bank Nifty · India VIX · Sensex** with the
price, today's change (points and %), a small line of today's 5-minute moves against the previous close (dashed),
and a **▲ / ▼ 50D** tag for above / below the 50-day average. India VIX is amber with **fear ↑** when it rises (a
rising VIX is a warning, not good news). The corner shows **● Live · NSE open** and the update time, or
Pre-open / Closed / Holiday. Click an index to open its chart, like a stock. On a phone the indices are small chips (about three in view, % change
only) that scroll sideways, with the status line under them.

How (`backend/ingest/indices.py`, `GET /api/indices`): one Kite quote call for all five, cached ~25 s; the
dashboard polls every 30 s while the market is open (5 min otherwise), so nothing runs when it isn't open in a
browser. Sparklines come from today's 5-minute candles (refreshed every 5 min while open). Daily candles for each
index are kept in `candles_daily` and topped up once a day (three years the first time), which gives the 50-day
average and the chart; Sensex (BSE) gets its own instruments row. Without a Kite login the bar shows the last
stored closes ("log in for live"). Charts hide the volume pane for indices (they have none).

## Bull flag screen

`backend/engine/flags.py` finds bull flags and high-tight flags, looking only at past bars: a sharp **pole**, then a
short, quiet **flag**. Pick **Bull flag** in the screen dropdown; it uses the same stage cards, feed, grid, alerts,
decision charts, A/B tiers, daily plan, position guard and News tab as VCP and Cup & handle. Rules in
**Settings → flag**:

| Part | Default rule |
|---|---|
| Pole | ≥ 20% rise in ≤ 30 sessions (from where the sharp move starts), on ≥ 1.2× the 50-day volume |
| Flag | 5–25 sessions, ≤ 25% under the pole top and giving back ≤ 38% of the pole, drifting sideways / down |
| Volume | flag volume ≤ 0.8× the pole's |
| Pivot / stop | pivot = the flag's high (the pole top); stop = flag low − 1%, never more than 10% under the pivot |
| Screen | Forming = in the flag within 6% of the pivot; RS ≥ 80 and Trend Template ≥ 5/8 |
| Tags | **tight** (≤ 25% of the pole given back) · **HTF** high-tight flag (pole ≥ +90% within 8 weeks) · **post-results** (results came out during the pole, so the event risk is behind it) |

Score (0–100): flag tightness 30, volume dry-up 20, pole strength 20, RS 20, flag length 10. The decision chart draws
the pole as a glowing arrow from **P** to **T** and the flag as a shaded channel between two parallel lines, with
pole and flag volume shaded underneath; grid cards draw the same in miniature. Flags are fast: the chart says to act
within 1–3 sessions of the breakout. Bases cards get a **Flag** tag. Bull flag is an alert screen by default (saved
settings get it once, after Cup & handle). Calibrated on the demo history: 107 flag breakouts across the Nifty 500 in
~300 sessions (median pole +32% in 18 sessions, flag 8 sessions and 7% deep on 0.5× volume).

## Results calendar

`backend/ingest/events.py` keeps NSE's board-meeting calendar (`/api/event-calendar`: the exchange's own public
data; results, dividends, fund raising) in the `corp_events` table. The evening job refreshes it before the alerts
(10 days back to 45 days ahead); **Data → Refresh results calendar** does it by hand. A failed fetch keeps the stored
calendar and never blocks the scan or alerts. Holding a swing trade through results risks a gap past the stop either
way, so the next results date shows up where you decide:

| Where | What |
|---|---|
| Grid cards, stage tables, positions, plan card | 📅 **Results 28 Oct** chip for results within 15 sessions; amber within `results_warn_sessions` (5) |
| Stock alerts (breakout, near-pivot, forming, intraday, 3:15 PM check) | ⚠️ **Results Wed 28 Oct (in 3 sessions)**: a results gap can jump past the stop either way |
| Decision chart | "R  Results Wed 28 Oct (in 3 sessions): gap risk" under Levels |
| Top setups / morning plan | ⚠️ results line under the setup |
| Position guard | 📅 once per results date, `results_guard_sessions` (1) before: decide whether to hold, trim or sell; the daily summary flags results within 5 sessions |

Sessions count NSE trading days (Friday → Monday is the next session). Settings → alerts: `results_calendar`,
`results_warn_sessions`, `results_guard_sessions`. API: `GET /api/events` (status), `GET /api/events?symbol=X`,
`POST /api/events/refresh`.

## Exchange filings (news)

`backend/ingest/filings.py` keeps NSE's corporate announcements (`/api/corporate-announcements`: what each company
has told the exchange) in the `filings` table for 120 days. The `filings` job fetches new ones every
`filings_interval_min` (15) from 7:00 AM to 11:00 PM on every day (results often come out in the evening or on a
Saturday); **Data → Refresh filings** does it by hand. A failed fetch keeps what is stored. Each filing gets a kind
from NSE's category and text:

| Kind | From | Pushed? |
|---|---|---|
| 📊 Results | board-meeting outcome / press release with the financial results | yes |
| 📦 Order win | orders / contracts received | yes |
| 🤝 Deal | acquisition, merger, agreement, buyback, fund raising, capacity / production start | yes |
| ⚖️ Regulatory / legal | actions or orders passed, insolvency, litigation, default, penalty | yes |
| 🏷️ Credit rating | new / revised rating | yes |
| ❓ Exchange query | NSE asked about a news item, a volume spurt or a price move | yes |
| 👔 Management change | management, director / KMP or auditor change or resignation | yes |
| Update | press release, investor presentation, other updates | shown only |
| (noise) | newspaper copies, analyst-meet schedules, trading window, ESOPs, certificates | hidden |

What you get:
- **News line on stock alerts** (breakout, near-pivot, forming, intraday, 3:15 PM check): "📰 Results 24 Oct · Order win
  22 Oct" from the last `news_days` (3), with the most telling filing's text. A breakout with none says
  "📰 No exchange filings in 3 days: a move on no news."
- **Filing alerts** (`filings_alerts`): every important filing (last 24 h) on a stock you **hold** (position guard),
  **watch** (📌 button / `/watch`) or have in tonight's **plan**, once per filing, with a link to the filing PDF.
- **📰 chips** on grid cards, stage tables and positions (newest important filing in `news_days`; red for legal), and
  an **Exchange filings (30 days)** list in the stock chart window.

API: `GET /api/filings` (status), `GET /api/filings?symbol=X&days=30`, `POST /api/filings/refresh`.

## Red flags and fund buying

`backend/ingest/holders.py` adds who is buying and selling, fetched with the filings job: NSE's daily **bulk / block
deals** (`bulk.csv`, `block.csv`) and **big-holder disclosures** (SEBI SAST Reg 29: a holder crossing 5% or moving 2%+),
stored in the `deals` table. **Data → Refresh deals** fetches them by hand. NSE's pledge and insider-trading feeds
return empty data to scripts, so pledges are not covered.

| Signal | Rule (Settings → alerts) | Effect |
|---|---|---|
| 🚩 Promoter sold X% | promoter sale ≥ `stake_change_min_pct` (1%) in `red_flag_days` (30) | **tier B**, alert line, chip |
| 🚩 Regulatory / legal | a legal filing (orders passed, insolvency, litigation …), not one "in favour" of the company | tier B … |
| 🚩 Auditor resigned / Rating downgrade | from the filings | tier B … |
| 🏦 Fund bought ₹X Cr | a fund / institution net-bought ≥ `deal_min_cr` (₹5 Cr) in bulk / block deals in `deal_days` (10) | +4 plan rank, alert line, green chip |
| 🏦 Fund stake up to X% | SAST: a fund raised its stake by ≥ 1% | as above |
| 🏦 Fund sold … / stake down | the same, selling | caution chip and alert line |

Funds are told apart from prop traders by name (mutual fund, insurance, pension, FPI houses …); a client who buys
and sells the same stock on the same day nets out. A red flag keeps a stock at **tier B** (so it only appears in the
digest unless watched) until it ages out. Stock alerts carry the 🚩 / 🏦 lines; held, watched and plan stocks get a
message for each new one ("🚩 DDD · Promoter sold 2.4% (you hold it)"); chips show on cards, tables, positions and
the plan card; the stock window's **Filings & deals** list shows every deal and disclosure.

**News tab.** The "What changed" box on the Screens page has three tabs: **Moves** (today's stage moves), **News** and
**Results**. News lists the last `news_days` (3) of exchange news on the *board stocks*: every stock in the latest
scan on any screen (VCP, Cup & handle, Bases) at any stage, plus held and watched stocks. 📊 results, 📦 orders /
🤝 deals, 🚩 red flags, 🏦 fund moves and other filings (ratings, management, exchange queries), newest first, with
filter chips; a results filing and its press release show once; a red-flag filing shows once, as 🚩. Each line says
why the stock is in scope ("VCP forming", "held" …) and links to the filing; clicking it opens the chart. Results lists
board stocks reporting within `results_warn_sessions`, nearest first (held, watched and live setups ahead of the rest).
Built from stored data (`backend/ingest/news.py`, `GET /api/news`), no extra NSE calls. `red_flags` and `deals`
switch them off. API: `GET /api/deals` (status), `GET /api/deals?symbol=X`, `POST /api/deals/refresh`.

## Telegram alerts and automatic jobs

**Setup (once)**
1. In Telegram, message **@BotFather** → `/newbot` → copy the bot token.
2. Add `TELEGRAM_BOT_TOKEN=…` to `.env` and restart (`./start.sh`). The token is never stored in the database or shown in the UI.
3. Send your bot any message, then **Alerts → Detect chat ID → Send test message**.

**Testing the bot any time:** **Check bot** asks Telegram who the bot is (shows `@your_bot ✓`, or the error, such as
"Unauthorized" for a wrong token) without sending anything. **Sample alert → Send sample** sends one or all alert types,
each starting with "🧪 TEST —", built from your latest scan (or a built-in example when there's no data) with chart
images where the real alert has them. Samples work on weekends and holidays without a Kite login, can be repeated freely,
never block a real alert, and show in the Sent-alerts log as `test`. A wrong token stops after the first failure.

**Alerts** (each a switch in the **Alerts** tab; sent for the screens chosen there, VCP by default)

| Alert | When | Default |
|---|---|---|
| 🚀 Fresh breakout (+ chart image) | a VCP base closed above its pivot on ≥ 1.5× volume; includes close-in-range, stop and risk % from the close | on |
| 🔻 Back below pivot | a breakout closed back under its pivot (and "reclaimed the pivot" when it recovers) | on |
| 🏁 Played out | the run ended: close below the 50-day line or 8% under the pivot | on |
| 🔵 Newly forming | a base became a VCP Forming setup; includes the pullback sequence and stop | on |
| 🎯 Near pivot (+ chart image) | Forming and within `near_pivot_pct` (3%) under the pivot, once per base | on |
| 📋 Daily digest | the four counts + the whole "what changed since last close" list | on |
| ⚡ Intraday heads-up | during market hours the price trades above a Forming name's pivot; **unconfirmed** until the close. Says 🟢 strong / 🟡 average / 🟡 weak volume (day's projected volume vs `intraday_volume_mult`) or ⛔ extended (above the buy zone: don't chase) | on |
| ⚠️ Failed breakout | a name that crossed its pivot today falls `failed_buffer_pct` (1%) back under it: don't buy / honour the stop | on (with intraday) |
| 🕒 Act-before-close check | at 3:15 PM, for every Forming name above its pivot: ✅ **holding** (volume on pace ≥ 1.5× and price in the upper half of the day's range), 🟡 weak, or ⛔ extended, with the decision chart and buy zone, so you can act before 3:30 PM. Up to 3 names get their own message; more go in one summary. At 6:30 PM the breakout alert says "✓ flagged at 3:15 PM", and a 3:15 PM "holding" name that didn't confirm gets "✗ didn't confirm on the close" | on |
| ⚙️ System | Kite login missing (3:30 PM), evening job waiting / failed / partial, no bar from Kite, Nifty 500 additions/removals | on |
| ⚠️ Low-volume breakout · ▫️ Left a card | below the volume rule · dropped off a card (noisy) | off |

Every alert is sent **once** (`sent_alerts` key), so rescans and restarts don't repeat it. A failed send is retried on
the next run.

**Decision charts** (`backend/alerts/decision.py`). Each stock alert carries an annotated image plus matching text lines,
so you can decide at a glance:

| Marker | Meaning |
|---|---|
| ① pivot line + shaded **buy zone** | the trigger level, and pivot → +`buy_zone_pct` (5%): above it the stock is extended |
| ② ③ ④ … | the base's pullbacks with depths (they should shrink) |
| **S** stop line | last pullback low minus a buffer, capped at `vcp.max_stop` under the pivot |
| volume dry-up band | the base's volume vs the 50-day average |
| breakout callout | date, % through the pivot, volume ratio |
| **E** exit day | played-out alerts: the day and the reason the run ended |
| now / **LIVE** | the latest close, or today's still-forming candle for intraday heads-ups (unconfirmed) |

The right-hand panel shows a **verdict** (e.g. "VCP breakout ✓", "Breakout ✓ · not a VCP", "Failed breakout so far"),
a ✓/✗ checklist (Trend Template, RS, shrinking pullbacks, dry-up, tightening, breakout volume, close-in-range,
not extended), the levels, and what to watch. The caption repeats it as `Verdict / Why / Caution / Levels / Watch`
lines. With `capital` > 0 it also shows a position size: `capital × risk_pct ÷ (entry − stop)` shares. The size is
off by default. Choose which alert kinds get images in **Alerts → Decision charts**. Without matplotlib the text lines
still go out. On any stock chart, **Preview alert chart** shows the exact image (auto-picks the kind from the stock's
current stage, or choose one). These are the screen's rules, not advice.

### Kite login link

Kite sessions end at **6:00 AM IST** whatever time you log in, so a session that lasts the day can only
start after 6:00 AM. The bot sends a **🔑 Log in to Kite** button. Tap it on the Mac or phone, log in on Zerodha's
page, and the token is saved. You get a "✅ Kite connected" page and a Telegram
confirmation.

| When (IST, trading days) | Sent if |
|---|---|
| 5:45 AM heads-up "session ends at 6:00 AM" *(off by default)* | a token is active |
| 6:01 AM link: "new Kite day" | Kite doesn't accept a session |
| 8:45 AM link: "still not logged in" | the 6:01 AM link wasn't used |
| 3:30 PM reminder and the 6:30 PM evening job | still no login (now with the link) |
| any time 6:00 AM–11:00 PM (checked every 30 min) | Kite rejects the held session (logged out / revoked) |
| `/login` to the bot, or **Send login link now** | on demand (`/status` shows the session) |

- **Checked, not assumed:** before each link the app asks Kite (`profile`) whether the session works. A
  network error doesn't count as logged out.
- **Mac asleep:** missed times catch up as **one** link when the app wakes.
- **Safe links:** each works once, for 15 minutes; a newer link cancels older ones (a stale button shows
  "link expired"). It's the normal Kite URL plus `redirect_params=n=<one-time code>`, checked on the way back.
  Only your chat's `/login` is answered.
- **From a phone:** Kite returns to the redirect URL registered on the Kite developer console, so that
  address must be reachable from the phone. On the Mac alone it is `http://127.0.0.1:8000/…` (links work on
  the Mac only). With Tailscale or the cloud deployment, set `PUBLIC_BASE_URL`, then copy the URL shown in
  **Alerts → Kite login link** into the Kite console. Set `BP_DASHBOARD_PASSWORD` whenever the app is
  reachable from outside: the dashboard then asks for it, while Telegram links prove themselves with
  their one-time code.
- Settings: `login_links`, `login_times`, `login_heads_up`, `login_heads_up_time`, `login_every_day`,
  `login_check_min`.

### Market health and A/B priority

Each scan computes one **market status** (`backend/engine/market.py`, thresholds in **Settings → market**):

| Signal | Healthy when |
|---|---|
| Nifty 500 vs its 50- and 200-day averages, 50-day rising | above both, rising |
| Breadth: % of the 500 above their own 50-day line | ≥ 50% |
| Distribution days: index down ≥ 0.2% on higher volume, last 25 sessions (volume = total of the 500 stocks, since Kite index candles have no volume) | ≤ 4 |
| Breakouts working: of breakouts in the last 20 sessions, the share still above the pivot | ≥ 50% |

- **🟢 Uptrend**: all of the above healthy.
- **🔴 Correction**: index below its 200-day, or breadth under 35% while the index is below its 50-day (or the
  index is unknown), or 7+ distribution days.
- **🟡 Under pressure**: everything in between.

The status shows on the Screens home (market card with each signal and a 60-day strip). Every alert starts with a
market line, e.g. "Market 🟢 Uptrend · breadth 64% · 3 dist. days · breakouts working 7/10". Intraday alerts add
today's Nifty 500 move, and the digest has a fuller block.

**A/B priority** (`backend/alerts/priority.py`):
- A setup: VCP qualifies, RS ≥ 85, Trend Template ≥ 7/8, industry in the top third, market not in correction.
- A setups alert at once, tagged 🅰️. B setups go to the digest only (or send, tagged 🅱️, or off).
- Tiered alerts: breakout, near pivot, forming, intraday heads-up, 3:15 PM check (B names appear in its summary).
- Sell-side alerts (below pivot, played out, failed breakout, didn't confirm) are always sent.
- In a correction, buy alerts carry a caution (or are muted with `correction_mode: mute_buys`).
- With `capital` set, sizes follow the market: ×1 uptrend, ×½ under pressure, ×¼ in correction.
- A/B badges appear on the stage cards and table rows.
- Long photo captions are now split at a line break: the rest follows as a message, instead of being cut (which
  could break the HTML).

### Daily plan: Top setups, morning plan, opening check

`backend/alerts/plan.py`. After the evening scan, the Forming setups are ranked:
rank = VCP score (or 0.6 × RS) − 2 per % under the pivot + 10 for an A setup + 8 when **coiled**. Coiled means the
last bar is an NR7 (narrowest range of the last 7) or an inside day, on volume ≤ 0.5× average, within 3% of the pivot.

| When (trading days) | Message |
|---|---|
| 7:00 PM (waits for the evening scan) | 🎯 Top 5 for the next session: trigger, buy zone, stop, size, tier, coiled; then a decision chart each. The list is saved (`daily_plan`) |
| 8:50 AM | ☀️ Today's plan: market line and the saved triggers |
| 9:10 AM | 🔔 Opening check for the plan names: ⛔ gapping above the buy zone (don't chase), 🟢 opening above the pivot, 🔻 below the stop (skip) |

The Screens home shows the plan; Alerts → Daily plan has the switches and times, plus **Top setups now**.

### Position guard

`backend/alerts/guard.py`. After each evening scan, the app reads your **Kite holdings and today's CNC buys
(read-only, no order code)**. It follows each one with the setup's pivot and stop (VCP stop, else pivot −8%; or
average −8% when there's no setup); you can change a stop on the Screens home. Rules:

| Alert | When | Repeats |
|---|---|---|
| 🛑 Stop | close below your stop | every day while true |
| 📉 50-day | close below the 50-day line | once |
| 💰 Profit zone | +20% from your average (`profit_zone_pct`): consider selling part | once |
| 🎈 Extended | close 25% above the 50-day line (`extended_pct`): consider tightening | once |
| ⚠️ Failed | back under the pivot within 3 sessions of buying (`failed_days`) | once |

Plus one evening line per position: price, gain %, R multiple, days held, stop. Sold positions drop off.
**Run guard now** (Alerts tab) syncs and checks on demand.

### Market hours: live guard, entry checklist, Today page

`backend/alerts/live.py`, run from the 5-minute intraday job, the 9:10 AM opening check and the 3:15 PM close check
(read-only Kite quotes and history, never orders).

**Live position guard** (`live_guard`; each alert once per stock per day, kept across restarts in `intraday_flags`):

| Alert | When |
|---|---|
| 🔻 Under stop | the price trades below your stop. Unconfirmed until the close (the 3:15 PM check says whether it recovered); with `hard_stop` on it says "exit now" |
| ⚠️ Heavy selling | down `live_drop_pct` (4%) or more today on `live_drop_volume` (2×) normal volume for the time of day, still above the stop |
| 🌅 Opened under stop | at the 9:10 AM opening check: a gap the stop can't protect against |
| 🕒 Positions at 3:15 PM | names that were under the stop or sold off today: still under → "your rule says exit, act before 3:30 PM"; back above → ✅. Sent only when something needs you |
| 🆕 Now following | Kite holdings are re-read every `holdings_refresh_min` (30) in market hours, so today's buy is guarded within the half hour |

The live alerts carry **📈 Chart** and **🔕 Quiet today** (no more live alerts on that stock today; the evening guard
still runs). One extra Kite quote call per 5-minute tick covers every position.

**Entry checklist** (`entry_checklist`) on every intraday heads-up and the 3:15 PM check:

| Check | Passes when |
|---|---|
| Buy zone | within pivot … +`buy_zone_pct` (5%) |
| VWAP | above Kite's average traded price for the day |
| Volume | on pace for ≥ `intraday_volume_mult` (1.5×) the 50-day average (NSE volume curve) |
| Leader | up more than the Nifty 500 today |
| Opening range | over the high of the first `opening_range_min` (30) minutes (one 5-minute history call per stock per day) |

🟢 **Valid entry** = all five · 🔴 **Skip** = beyond the buy zone, or under VWAP on weak volume (pace < 1×) ·
🟡 **Wait** = the rest (a check without data yet counts as not passed). A valid entry adds **🎯 Buy up to** (top of
the zone), the stop (the setup's, else pivot −8%) and, with `capital` set, the **quantity** and ₹ at risk
(capital × `risk_pct` × the market size factor: ½ under pressure, ¼ in a correction). A 🟡 that turns 🟢 later
the same day gets one **⬆️ now a valid entry** note. Breakouts still count only on a close above the pivot.

**Today page** (⚡ Today tab; on a phone it takes the Data slot in the bottom bar, Data moves to More):
market strip (status, Nifty 500 and VIX today, size note), your positions (live price, day %, R, stop distance,
under stop / recovered / heavy selling) and the plan plus Forming names within `today_near_pct` (3%) of their pivot
(live price, % vs pivot, VWAP ▲/▼, volume pace, vs Nifty, the verdict and quantity; tap a setup for its checklist).
Today's 5-minute line next to each name, with the stop or pivot dashed. It refreshes every 30 s while NSE is open
(`GET /api/today`: one Kite quote call, cached 25 s; sparklines `GET /api/today/sparks`, cached 5 min) and shows the
names without prices when there is no Kite login. The Screens positions card shows the same live price, chip and a
stop-distance bar while the market is open.

### Buttons and bot commands

Stock alerts carry four buttons (`alert_buttons`), answered by the bot poller (`backend/alerts/commands.py`, your
chat only):

| Button | Effect |
|---|---|
| 📌 Watch | always alert on this stock, even as a B setup |
| 🔕 Mute | no buy-side alerts for `mute_days` (30); sell-side alerts still come |
| 📈 Chart | sends the decision chart for its current stage |
| ✅ I bought | the position guard follows it from the last close (stop from the setup); it becomes Kite-backed once your holdings show it |
| 🔕 Quiet today | (live guard alerts) no more live alerts on that stock today |

Commands: `/login`, `/status`, `/top` (next session's plan), `/watch [SYM]` (list or add), `/unwatch SYM`,
`/mute SYM`, `/chart SYM`, `/help`.

**Automatic jobs** (`backend/alerts/jobs.py`, a background thread; times are IST and editable in Alerts → Timing)

| Job | When | What |
|---|---|---|
| Login reminder | trading days 3:30 PM → 6:30 PM | alerts once if today's Kite token is missing |
| Evening job | trading days from 6:30 PM | sync candles → scan → alerts. Without a login it alerts once and retries every 15 min until 11:00 PM; if Kite has no bar for today yet it retries every 30 min until 10:30 PM ("no session" after). Catches up if the app is opened later that evening. |
| Intraday heads-up | trading days 9:20 AM–3:25 PM, every 5 min | one Kite quote call for the Forming names (quote API 1 req/s); heads-up and failed-breakout alerts |
| Close check | trading days 3:15 PM | one quote call; holding / weak / extended verdicts (see the alerts table) |
| Weekly refresh | Sundays from 10:00 AM | NSE holiday list + Nifty 500 refresh, index-change alert |
| Kite login links | 6:01 AM / 8:45 AM, session check every 30 min | see [Kite login link](#kite-login-link) |

Trading days = weekdays minus NSE equity holidays (`config/nse_holidays.json`, the official NSE holiday-master list,
refreshed weekly; special sessions such as Diwali Muhurat are handled by checking that Kite returned a bar).
**Run evening job now / Intraday check now / Weekly refresh now** buttons and the job history are in the Alerts tab.

**Keeping the Mac ready** (jobs run only while the app is open and the Mac is awake)
- `./start.sh` keeps the Mac from idle-sleeping while the backend runs (`caffeinate -i -w <pid>`, macOS only).
  Closing the lid on battery still sleeps it.
- Start the app at login: `scripts/macos/com.bananapatterns.app.plist` (edit the path, copy to
  `~/Library/LaunchAgents/`, `launchctl load …`; instructions inside the file).
- Wake the Mac before market open on weekdays: `sudo pmset repeat wakeorpoweron MTWRF 09:10:00`
  (check with `pmset -g sched`, remove with `sudo pmset repeat cancel`).

Personal use only: send alerts to your own chat. Broadcasting signals to others may count as investment advice (SEBI).

## REST API

Interactive docs: http://localhost:8000/docs

| Method | Path | Description |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/api/kite/login-url` | Kite login URL (400 if keys missing) |
| GET | `/api/kite/callback` | OAuth redirect target; stores the token, confirms in Telegram. With `n` (Telegram link) the one-time code must be valid and a phone page is shown; without it, redirects to the dashboard |
| POST | `/api/kite/token` | `{access_token}`: store a token obtained elsewhere |
| GET | `/api/kite/status?verify=true` | token state, `expires_at`, `needs_login`; `verify` calls `profile()` |
| GET/PUT | `/api/config/{section}` | read / update a config section (`ingest` now, `vcp` in Phase 2) |
| POST | `/api/universe/refresh` | download Nifty 500 + Kite instruments; returns mapping summary |
| GET | `/api/universe` | latest snapshot with per-symbol coverage, last close, change %, last error |
| GET | `/api/stock/{symbol}/candles?limit=750` | daily candles (stocks or indices, e.g. `NIFTY 500`) |
| GET | `/api/screens` | available screens (`vcp`, `cup`, `bases`) |
| GET | `/api/screens/{screen}/summary` | as-of date, counts per stage, number of changes, stale flag |
| GET | `/api/screens/{screen}/sparks?stage=&bars=120` | last N candles + 50-day line per stock in a stage (grid view) |
| GET | `/api/screens/{screen}/results?stage=` | stocks in `forming`, `fresh`, `fresh_low_volume`, `climbing` or `played_out` |
| POST | `/api/screens/scan` | rescan now (409 while a candle sync runs; the sync scans when it finishes) |
| GET | `/api/market/health?days=60` | latest market status + signals, the previous day, daily history, thresholds |
| GET | `/api/positions` · PUT `/api/positions/{symbol}` `{stop}` | positions the guard follows (with `live` price and guard state while the market is open); change a stop |
| GET | `/api/today` · `/api/today/sparks` | the Today page: market strip, live positions, plan and near-pivot names with the entry checklist; today's 5-minute closes |
| GET | `/api/plan` | the newest saved plan (ranked setups) |
| GET | `/api/changes?screen=vcp` | what changed since the last close for a screen (`bases` default) |
| GET | `/api/alerts/status` | bot token set?, chat ID, ready, today's trading status, upcoming holidays |
| POST | `/api/alerts/detect-chat` | read the bot's recent chats; saves the chat ID when there is exactly one |
| POST | `/api/alerts/test` | send a test message |
| GET | `/api/alerts/bot` | check the bot token with Telegram (getMe), no message sent |
| POST | `/api/alerts/sample` | `{kind: all \| breakout \| near_pivot \| forming \| below_pivot \| played_out \| intraday \| digest \| system}`: send TEST samples |
| POST | `/api/alerts/login-link` | send a one-time Kite login link to Telegram now |
| GET | `/api/alerts/log` | sent / failed alerts |
| GET | `/api/jobs` | automatic job history and schedule |
| POST | `/api/jobs/{eod,intraday,weekly,reminder}/run` | run a job now (evening job forced even on a non-trading day) |
| GET | `/api/stock/{symbol}/alert-chart.png?kind=auto` | the decision chart an alert would carry (`auto` = from the latest scan stage; or breakout, near_pivot, forming, below_pivot, played_out, climbing) |
| GET | `/api/stock/{symbol}/vcp` | VCP check of the latest base (breakouts judged on the bar before) |
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
`TELEGRAM_BOT_TOKEN` (alerts), optional `TELEGRAM_CHAT_ID`, `BP_NO_SCHEDULER=1` (disable automatic jobs),
`KITE_REDIRECT_URL` (default `http://localhost:8000/api/kite/callback`), optional `BP_DB_PATH`,
`BP_DEV=1` (reload), `BP_FRONTEND_URL` (post-login redirect target),
`PUBLIC_BASE_URL` (address Kite redirects to: Tailscale or cloud; default from `KITE_REDIRECT_URL`),
`BP_DASHBOARD_PASSWORD` (password for the dashboard; set it before exposing the app beyond the Mac).

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

| `vcp.*` | Default | Meaning |
|---|---|---|
| `max_depth` / `min_weeks` | 0.30 / 3 | base depth and length |
| `min_contractions` / `max_contractions` | 2 / 6 | number of pullbacks |
| `require_shrinking` / `shrink` | true / 0.8 | last pullback ≤ shrink × largest earlier |
| `max_final_contraction` | 0.12 | last pullback depth |
| `max_tightening` / `max_dryup` | 1.0 / 0.85 | ATR ratio and volume ratio limits |
| `min_rs` / `min_trend_checks` | 70 / 5 | strength and trend |
| `max_dist_to_pivot` | 0.25 | Forming card distance on the VCP screen |
| `zz_min_pct` / `zz_atr_mult` | 0.05 / 2.0 | swing confirmation threshold |
| `atr_recent` / `dryup_recent` / `dryup_lookback` / `ud_window` | 10 / 10 / 50 / 50 | metric windows |
| `stop_buffer` / `max_stop` | 0.01 / 0.08 | stop placement |
| `score_weights` | 30, 20, 15, 15, 10, 10 | RS, tightening, dry-up, pivot, 52w high, industry RS |

| `alerts.*` | Default | Meaning |
|---|---|---|
| `enabled` / `chat_id` / `screens` | true / "" / vcp | master switch, Telegram chat, screens to alert on |
| `breakout`, `below_pivot`, `played_out`, `forming`, `near_pivot`, `digest`, `system`, `intraday` | true | alert types on |
| `low_volume_breakout`, `left` | false | alert types off |
| `near_pivot_pct` | 0.03 | near-pivot distance |
| `chart_images` | true | attach decision-chart images |
| `chart_kinds` | all stock alert kinds | which kinds get an image (breakout, breakout_low_volume, near_pivot, forming, below_pivot, played_out, climbing, intraday) |
| `buy_zone_pct` | 0.05 | buy zone above the pivot; above it = extended |
| `capital` / `risk_pct` | 0 / 0.01 | position size on the chart and in the message (0 = off) |
| `job_enabled` / `job_time` / `reminder_time` | true / 18:30 / 15:30 | evening job and login reminder (IST) |
| `intraday_start` / `intraday_end` / `intraday_interval_min` | 09:20 / 15:25 / 5 | intraday heads-up window |
| `intraday_volume_mult` | 1.5 | projected day volume (× 50-day average) for "strong" and "holding" |
| `failed_buffer_pct` | 0.01 | a crossed name this far under the pivot = failed breakout |
| `close_check` / `close_check_time` / `close_min_range` | true / 15:15 / 0.5 | act-before-close check; price must sit in the upper half of the day's range |
| `universe_refresh_weekday` | 6 (Sunday) | weekly refresh day |
| `tiering` / `b_alerts` / `correction_mode` | true / digest / caution | A/B priority; B: digest, push or off; correction: caution or mute_buys |
| `a_min_rs` / `a_min_trend` / `a_top_industry_frac` | 85 / 7 / 0.33 | what makes an A setup |
| `market_line` / `market_sizing` | true / true | market line on alerts; size by market status |
| `top_setups` / `top_setups_time` / `plan_top_n` | true / 19:00 / 5 | ranked setups after the evening scan |
| `morning_plan` / `morning_plan_time` · `gap_check` / `gap_check_time` | true / 08:50 · true / 09:10 | morning plan and opening check |
| `coiled_volume` / `coiled_within_pct` | 0.5 / 3.0 | coiled: volume ≤ 0.5× average, within 3% of the pivot |
| `position_guard` / `profit_zone_pct` / `extended_pct` / `failed_days` | true / 20 / 25 / 3 | position guard rules |
| `live_guard` / `live_drop_pct` / `live_drop_volume` / `holdings_refresh_min` / `hard_stop` | true / 4 / 2 / 30 / false | live position guard in market hours |
| `entry_checklist` / `opening_range_min` / `today_near_pct` | true / 30 / 3 | entry checklist on heads-ups; Today page near-pivot range |
| `alert_buttons` / `mute_days` | true / 30 | buttons under stock alerts; how long Mute lasts |
| `login_links` / `login_times` | true / 06:01, 08:45 | Kite login link when no session (times must be ≥ 06:00) |
| `login_heads_up` / `login_heads_up_time` | false / 05:45 | "session ends at 06:00" note |
| `login_every_day` / `login_check_min` | false / 30 | weekends + holidays too / session check interval |

---

## Frontend

React 19 + Vite + lightweight-charts 5, with a dark theme. Vite replaces create-react-app (deprecated)
The dev server is on :3000 with `/api` proxied to :8000.
`npm run build` writes `frontend/build`, which FastAPI serves at :8000.

Tabs: **Screens** (home: stage board, see above), **Alerts** (Telegram setup, switches, timing, job buttons and
history, sent-alerts log), **Universe** (sortable/filterable table + chart), **Data** (refresh, sync, full refetch, progress,
errors, run history), **Settings** (config sections `vcp`, `screen`, `bases`, `trend` and `ingest`), **Logs**.

**Light / dark mode**: the header switch ◐ Auto / ☀ Light / ☾ Dark (remembered per browser; Auto follows the
system setting). Colours are CSS variables in `App.css` (`:root` dark, `:root[data-theme="light"]` light, tints via
`color-mix`), all text colours pass WCAG AA contrast in both. `src/theme.js` applies the mode; `index.html` sets it
before the first paint (no flash). The candle chart reads the theme colours and is rebuilt when you switch. Telegram
chart images stay dark.

**On a phone** (under 600 px wide) the app switches to a mobile layout:
- A bottom tab bar (Screens · ⚡ Today · Alerts · More). More opens a sheet with Universe, Data, Settings, Logs and the
  theme.
- The Kite bar shrinks to a status pill and Login, with the rest behind "⋯".
- The Screens page is reordered for a phone: index ticker → screen pills (VCP · Cup · Flag · Bases) and ↻ rescan →
  the market on one line (status, breadth, 52-week highs / lows; tap for the signals) → the four stage cards as a
  compact 2×2 grid → the first 3 names in the selected stage right under them ("Show all" opens the full grid /
  table) → Moves / News / Results → the plan and the positions as one row per stock (stop still editable). It opens
  on the first stage that has names. Tablets and desktops keep the original order.
- The feed shows 3 items and the grid one card per row.
- Tables keep the symbol column pinned while the rest scrolls sideways.
- The stock chart opens full-screen, with the Trend / VCP / Base panels folded.
- Touch targets are at least 44 px, inputs use 16 px text (no iOS zoom), time fields use the phone's time picker and
  number fields its keypad, and the layout leaves room for the notch and home bar.
- Checked at 390 px: no page scrolls sideways.

The **chart** shows daily candles, volume, SMA 50/150/200 and the **base overlay**:
- base boxes drawn by a canvas plugin (`chart/basesPrimitive.js`, lightweight-charts v5 series primitive):
  the latest base is green-shaded, earlier ones grey, each labelled "B6 · 12w"
- a dashed **pivot line** for the latest base ("pivot ₹146.54")
- **▲ breakout markers**: green when volume-confirmed, amber when below the volume rule
- a **"bases" checkbox** to hide or show the overlay, and under the chart the **breakout card** ("Broke out of B6" or "Forming B3")
  with the metrics above and tags (below volume rule, weak close, squat / squat pending). The top bar shows Kite status and token expiry, and re-checks every minute.

## Testing

```bash
.venv/bin/python -m pytest          # 85 tests, offline, ~20 s
```

A `FakeKite` (tests/conftest.py) mimics the SDK, and each test gets its own temporary SQLite DB.
Base detector tests (tests/test_bases.py) use synthetic series with known answers: a staircase of two bases,
too-short pauses, deep declines, no prior advance, a slow grind (high basis), a forming base, squat and pending,
the MA reset, the high-basis pivot, JSON-serialisable output and **causality** (prefix-stable results).
Alert tests (tests/test_alerts.py): Telegram client against a fake server (429 retry, 401 not retried, chat detection),
HTML escaping and digest splitting, chart PNG, the schedule (fake clock), volume pace, holiday refresh merge, dispatch
(sent once, toggles, failed send retried), the evening job (waits for login, retries without today's bar) and the intraday
heads-up (once per day). VCP tests (tests/test_vcp.py): a textbook 20 → 10 → 5% VCP qualifies, an expanding pattern fails, each rule can fail,
swings are causal, the VCP classifier judges breakouts on the bar before, scoring order, and the scan-table migration.
Stage tests (tests/test_stages.py) cover each card's rule, low-volume breakouts, both played-out triggers and the
window expiring, below-pivot, the climber-forming-next-base precedence, the liquidity/history gates, feed sentences,
and a scan that stores results and changes. Other coverage: throttle spacing, the 6:00 AM IST rule, date chunking, retry/abort classification,
universe parsing/fallback/joining, full → incremental → up-to-date sync, split → full refetch, the
empty-response guard, per-symbol failure recording, auth abort, and the API end to end
(login callback → universe → background sync → universe/candles endpoints → token expiry while running).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `setup.sh`: "Python 3.12+ not found" | macOS's `python3` is 3.9. `brew install python@3.12`, or `BP_PYTHON=/path/to/python3.12 ./setup.sh` |
| "Kite login required" after a night | Expected: tokens die at 6:00 AM IST. Log in again. |
| Run `aborted`: permission denied | Historical data needs the Kite Connect plan on this API key. |
| Kite logged out during the day | Another program logged in with the same API key (one token per app). Log in again. |
| Universe refresh fails (NSE 403) | Save the CSV to `config/ind_nifty500list.csv` (see config/README.md). |
| Symbols listed as *excluded* | Their series isn't in `include_series` (REITs by default). Edit in Settings. |
| Symbols listed as *unmatched* | Usually a rename or suspension. Check the NSE symbol vs Kite's tradingsymbol. |
| Backend won't start: `ModuleNotFoundError` (e.g. matplotlib) after `git pull` | A new dependency: `./start.sh` now installs it automatically when `requirements.txt` changes; or run `.venv/bin/pip install -r requirements.txt`. Without matplotlib the app still runs and alerts go out as text. |
| No Telegram alerts | Alerts tab: token set? chat set? "Ready"? Then Send test message; check the Sent alerts log for errors. |
| Evening job didn't run | The app must be open and the Mac awake at 6:30 PM (see "Keeping the Mac ready"); it catches up when opened later that evening. Check Alerts → Automatic jobs. |
| Port 8000 busy | `start.sh` frees 8000/3000. Otherwise `lsof -ti tcp:8000 \| xargs kill`. |
| Details of a failure | Logs tab (WARNING+), full log in `logs/bananapatterns.log` |
| Anything on the cloud server (blank page, can't reach the site, Kite redirect, backups, out of capacity) | [docs/CLOUD.md → Troubleshooting](docs/CLOUD.md#part-6-troubleshooting) |

---

## Roadmap

Each phase is explained first, built after approval, and shown before the next one starts.

### Phase 2: VCP engine *(in progress)*
- ✅ PR #3: RS rating, industry RS, Trend Template, base dials recalibrated against the site.
- ✅ PR #4: VCP screen (tightening, dry-up, U/D ratio, contractions, classic VCP rules, scoring, VCP check on charts).
- Next, PR #5: card view with mini charts, a cards/list toggle, contraction swings drawn on the chart, copy as CSV.
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

### Phase 4: daily EOD job and alerts *(core done: Telegram alerts, evening job, reminders, intraday heads-up, holiday calendar)*
Scheduler (APScheduler in-process, or launchd so it survives restarts) around 6:00 PM–7:00 PM IST on trading
days: sync → scan → persist results and diffs. NSE holiday calendar. Alert when the day's Kite login is
missing. Optional NSE CM-UDiFF bhavcopy cross-check later in the evening (unadjusted, so compare
only on days without corporate actions). Note: a sleeping Mac will not run jobs (`pmset` wake schedule or keep awake).

### Cloud deployment *(done)*
Runs on Oracle Cloud Always Free with Tailscale HTTPS, nightly backups and a runbook: see
[Cloud (Oracle + Tailscale)](#cloud-oracle--tailscale) and [docs/CLOUD.md](docs/CLOUD.md).

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
| 2026-10-23 | Token validity = issued after the last **6:00 AM IST** (fixes AlgoTrade's calendar-day check) |
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
| 2026-10-24 | VCP screen keeps the ≥ 1.5× breakout-volume rule (Gagan: "as you feel correct"); low-volume breakouts stay listed separately |
| 2026-10-24 | VCP = classic Minervini rules from the brief (shrinking pullbacks, last ≤ 12%), stricter than the site's list; relaxing dials documented |
| 2026-10-24 | ZigZag threshold max(5%, 2 × ATR%) and max 6 contractions: median 3 pullbacks per base on the Nifty 500 |
| 2026-10-24 | Telegram alerts + automatic evening job before the card view; individual alerts **and** a digest; chart images and an intraday heads-up included |
| 2026-10-24 | Bot token only in `.env`; alerts to Gagan's own chat (personal use) |
| 2026-10-24 | Intraday heads-up via the Kite quote API every 5 min (one call for all Forming names), clearly "unconfirmed" since breakouts are closing-basis |
| 2026-10-23 | `main` started with an initial `.gitignore` commit so Phase 1 can be reviewed as a PR into it |

## Development log

### 2026-10-23: Phase 1: Foundation
- Read AlgoTrade-v3 and pykiteconnect 5.2.2 source. Confirmed: `historical_data(token, from, to, interval, continuous, oi)`,
  the typed exceptions, `market_protection` on orders, `place_gtt`. Corrected two brief assumptions:
  8 years needs 2 historical calls per symbol (2,000-day limit), and the Kite instruments dump has no ISIN.
- Checked that both NSE Nifty 500 CSV URLs respond. The live file has 501 rows including 2 BE and 3 REIT (RR)
  rows, which led to the `include_series` dial and the `SYMBOL-BE` join.
- Built: config sections persisted to SQLite; WAL schema; logger (WARNING+ to DB); Kite client with token
  bucket, retries and error classification; the 6:00 AM IST token rule; universe snapshots and symbol join;
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

### 2026-10-24: Phase 2, step 4 (PR #4): VCP screen
- Volume rule for the VCP screen left to me: kept ≥ 1.5× (brief's definition, risk first), low-volume breakouts listed.
- Built `engine/vcp.py` (metrics, ZigZag contractions, rules, stop), `engine/scoring.py`, `classify_vcp`, a `vcp` config
  section, per-screen scan storage (`screen` column; old derived tables dropped and rebuilt), `/api/screens/vcp/*`,
  `/api/changes?screen=`, `/api/stock/{symbol}/vcp`, the VCP option (default), VCP columns, a VCP tag on Bases lists,
  and the VCP check panel on charts.
- Calibration (Nifty 500 + the site's six names, public prices, local only):
  - With the gates from the plan alone (depth, tightening, dry-up, RS, trend) 66 of ~313 forming bases qualified,
    far looser than the site (8 of ~5,000). Selectivity has to come from contraction structure.
  - Five of the site's six names fail classic VCP structure; only PREMEXPLN passes. So VCP = the brief's classic rules.
  - ZigZag at 3% / 1.5×ATR gave up to 28 "contractions" per base; 5% / 2×ATR gives a median of 3. With max 6 contractions:
    7–8 VCPs forming on the Nifty 500.
- Wording fix: failed-rule text shows one decimal ("last pullback 12% > 12%" was 12.x%).
- 65 offline tests passing.

### 2026-10-24: Phase 4 core (PR #4): Telegram alerts and automatic jobs
- PR #3 (RS + Trend Template + VCP screen) merged. Gagan asked for Telegram alerts like the "what changed" feed.
  Choices: smart defaults, individual alerts **and** a digest, chart images and an intraday heads-up, alerts with the
  evening job before the card view.
- Built `backend/alerts/` (Telegram client, chart PNG, messages, dispatch with dedup, scheduler), the NSE holiday
  calendar (official 2026 equity list fetched from NSE's holiday-master API), the Kite quote call, the `alerts`
  config section, alerts/jobs endpoints, the Alerts tab, `caffeinate` in `start.sh`, and a launchd template.
- QA: the dispatcher ran on 497 stocks of real data against a fake Telegram server: 12 stage alerts, 3 near-pivot,
  2 digests; breakout alerts carry charts. Chart labels were moved so the pivot/stop text never covers the latest candles.
  A real Telegram send can't be done from the sandbox: the first live test is Gagan's (Alerts → Send test message).
- A README update first failed on a mismatched anchor and landed in a follow-up commit.
- 80 offline tests passing.

### 2026-10-24: Sample alerts and bot check
- Gagan asked for a button to test the Telegram bot any time. Added **Check bot** (getMe, no message) and
  **Send sample** (one or all alert types, marked "🧪 TEST", from the latest scan or a built-in example, charts included).
  Samples skip the send-once rule and are logged as `test`, so they never block real alerts.
- Checked against the real Telegram API with a fake token: both buttons show "Unauthorized", and a sample run stops
  after the first failure. The success path is covered by fake-server tests.
- 85 offline tests passing.

### 2026-10-24: Dependency hardening
- On the Mac, `git pull` brought in matplotlib (chart images) but it wasn't installed, so the backend failed to import and
  the dashboard showed proxy errors. Fixes: `start.sh` reinstalls Python / frontend dependencies whenever
  `requirements.txt` / `package.json` change (checksum stamps inside `.venv/` and `node_modules/`), and matplotlib is
  optional: without it the app starts, alerts go out as text, and the Alerts tab says how to enable charts.
- 88 offline tests passing (incl. starting the backend with matplotlib blocked).

### 2026-10-24: Decision charts for every alert
- New `backend/alerts/decision.py`. It computes one decision context per alert from the same engine as the screens:
  base, VCP metrics, stop, buy zone, checks, verdict, watch lines, and an optional size. It renders an annotated chart
  with numbered markers and a decision panel. Breakout-family kinds are judged on the broken base at its breakout bar;
  forming kinds use the open base at the last bar. Callouts are placed so they clear the markers, the legend and the
  candles.
- Every stock alert kind now gets the image and `Verdict / Why / Caution / Levels / Watch` lines. Intraday heads-ups
  draw today's partial candle from the Kite quote (`LIVE`). Samples use the same path.
- Settings `chart_kinds`, `buy_zone_pct`, `capital`, `risk_pct`. Dashboard **Preview alert chart** button +
  `/api/stock/{symbol}/alert-chart.png`.
- 105 offline tests passing.

### 2026-10-24: Kite login link in Telegram
- One-time login links (`backend/kite_api/login_link.py`, table `login_links`): Kite's `redirect_params`
  carries a nonce; links work once for 15 minutes and a newer link supersedes older ones.
- Schedule: 6:01 AM and 8:45 AM links when Kite doesn't accept a session (missed slots catch up as one), an optional
  5:45 AM heads-up, a session check every 30 min, and links in the 3:30 PM reminder and the evening job.
- The token is handed to AlgoTrade-v3 (`ALGOTRADE_URL`, its existing `POST /api/kite/token`), since both share one
  Kite app. Telegram confirms the login and the handover.
- Bot commands `/login`, `/status`. `PUBLIC_BASE_URL` for phone / cloud use, `BP_DASHBOARD_PASSWORD` (HTTP Basic).
  Alerts → Kite login link panel.
- 126 offline tests passing.

### 2026-10-24: Catch breakouts the same day (act-before-close)
- Volume projection now follows a typical NSE intraday volume curve (heavy open and close), so morning
  projections are no longer inflated (`backend/alerts/intraday.py`).
- The intraday heads-up says strong / average / weak volume, or extended (don't chase).
- New failed-breakout alert (crossed, then 1% back under the pivot) and the 3:15 PM close check (holding / weak /
  extended, decision chart with the live candle, summary when more than 3 names). Day flags in `intraday_flags`,
  so restarts don't repeat alerts.
- 6:30 PM breakout alerts refer back to the 3:15 PM verdict; "didn't confirm" when a 3:15 PM "holding" name fails on
  the close.
- 145 offline tests passing.

### 2026-10-25: Grid view with mini charts
- Stage lists can be shown as a grid of cards, like the site: SVG mini chart (base box, pivot, 50-day line, breakout
  ▲, exit ✕) with the stage's key facts. Table view is still one click away.
- The scan now saves each base's start, end and low; new `/api/screens/{screen}/sparks` endpoint.
- 145 offline tests passing.

### 2026-10-25: Market health and A/B priority
- `backend/engine/market.py`: Nifty 500 trend, breadth, distribution days (total stock volume as the volume),
  breakout success rate, 52-week highs / lows. One status per scan in `market_health` (new `market` settings section).
- `backend/alerts/priority.py`: A/B tier, gate (B to the digest, mute in a correction, sell-side always sent),
  market line on every alert, Nifty 500 move on intraday alerts, market-aware size.
- Screens home market card, A/B badges, Alerts → Priority settings, `/api/market/health`.
- Photo captions over Telegram's limit are split at a line break, not cut.
- 177 offline tests passing.

### 2026-10-25: Daily plan
- `backend/alerts/plan.py`: ranking with the A bonus and "coiled" (NR7 / inside day on dried-up volume near the
  pivot), saved per scan in `daily_plan`. Jobs: Top N at 7:00 PM (after the evening scan), 8:50 AM morning plan,
  9:10 AM opening (gap) check. Screens home plan panel, `/api/plan`, Alerts settings.
- 190 offline tests passing.

### 2026-10-25: Position guard
- `backend/alerts/guard.py`: read-only Kite holdings + CNC positions, matched to the setup's pivot / stop
  (`guard_positions`), sell-rule alerts after the evening scan (stop, 50-day, profit zone, extended, failed) and an
  evening summary with gain % and R. Screens home positions panel with editable stops, `/api/positions`.
- 195 offline tests passing.

### 2026-10-25: Alert buttons and bot commands
- Watch / Mute / Chart / I bought buttons under stock alerts (`watchlist`, `muted` tables; watched names always
  alert, muted names get no buy-side alerts). "I bought" adds a manual guard position that survives holdings syncs.
- Bot commands `/top`, `/watch`, `/unwatch`, `/mute`, `/chart`. The chart preview moved to `backend/alerts/preview.py`
  (shared by the dashboard and the bot).
- 200 offline tests passing.

### 2026-10-25: Light and dark mode
- Header theme switch (Auto / Light / Dark); light palette, remaining hard-coded colours moved to CSS variables;
  the candle chart and base boxes follow the theme. WCAG AA contrast checked for every text colour.

### 2026-10-25: Mobile layout
- Bottom tab bar + More sheet, compact Kite bar and market card, full-screen chart with folded panels, position
  cards, pinned first column in tables, touch-size controls, phone keyboards for times and numbers, safe areas.
- Fixed tables without a scroll container that made the whole page scroll sideways on phones.

### 2026-10-25: Cup & handle screen
- `backend/engine/cups.py`: causal cup-with-handle detector returning bases (kind "cup") with the lips, bottom, handle
  and stop; `classify_cup` stages; a third scan screen `cup`; `cup` settings section. Breakout maths shared with box
  bases (`breakout_metrics`).
- Decision charts mark L / U / R and the handle; A/B tiers and the daily plan treat a cup like a VCP; grid cards draw
  the cup outline; Alerts → screens is now three checkboxes.
- Calibrated on the demo history: a "time in the bottom third" rule was added after spike bottoms showed up.
- 217 offline tests passing.

### 2026-10-25: Curved cup line and cup alerts
- Decision charts and grid cards draw the cup as a smooth U (`_cup_curve`, each side `bottom + depth·u^p` with p in
  1.7–4 fitted to the lows) and the handle as a curve (`_handle_curve`), replacing the dotted L-U-R line and handle
  box. Cup grid cards fetch enough history to show the left lip.
- Alerts: Cup & handle is an alert screen by default (one-time migration of saved settings); cup stop used in breakout
  lines, intraday "failed" alerts, the plan and the position guard; a "Cup … handle … stop" line on forming /
  near-pivot alerts; one alert per name and kind across screens (VCP > Cup > Bases).
- 222 offline tests passing.

### 2026-10-25: Results calendar
- `backend/ingest/events.py` + `corp_events`: NSE event calendar, refreshed in the evening job (failure-safe) and from
  the Data page; trading-session counts via the holiday calendar.
- Results warnings on stock alerts (added in `Notifier.send`), the decision chart panel, the plan lines, the guard
  (one alert per results date, plus the summary) and as 📅 chips on cards, tables, positions and the plan card.
- Tests never touch the network (conftest blocks the fetch). 232 offline tests passing.

### 2026-10-25: Exchange filings
- `backend/ingest/filings.py` + `filings` table: NSE corporate announcements sorted into kinds; a 15-minute `filings`
  job (7:00 AM-11:00 PM, every day).
- News lines on stock alerts (`news_note` in `Notifier.send`), filing alerts for held / watched / plan stocks
  (`Scheduler.alert_filings`), 📰 chips and a filings list in the chart window.
- 253 offline tests passing.

### 2026-10-25: Red flags and fund buying
- `backend/ingest/holders.py` + `deals` table: bulk / block deals and SAST Reg 29 disclosures, fetched with the
  filings job; `signals()` builds 🚩 red flags (promoter selling, legal, auditor exit, downgrade) and 🏦 fund moves.
- A red flag holds a stock at tier B (`market.tier`); alert lines (`flags_note`), held / watched / plan messages
  (`Scheduler.alert_deals`), plan rank +4 for fund buying, chips and a deals list in the stock window.
- 268 offline tests passing.

### 2026-10-25: News tab
- Moves / News / Results tabs in the What-changed box; `backend/ingest/news.py` + `GET /api/news` (board scope: all
  screens and stages + held / watched). 271 offline tests passing.

### 2026-10-25: Bull flag screen
- `backend/engine/flags.py`: causal bull-flag detector (pole, flag channel, tight / HTF tags) returning bases (kind
  "flag"); `classify_flag` (shared `_classify_shaped` with cups); a fourth scan screen `flag` with a post-results tag
  from the results calendar / filings; `flag` settings section.
- Decision chart (pole arrow, channel, volume shading, collision-free labels), grid cards, table columns, alerts
  (flag stop and line, "Bull flag" header), tiers, plan, guard (flag low), News scope, one SQL screen order
  (`SCREEN_ORDER_SQL`). 287 offline tests passing.

### 2026-10-25: Standalone (AlgoTrade link removed)
- Banana Patterns no longer hands Kite tokens to AlgoTrade-v3: `ALGOTRADE_URL`, `login_link.hand_over` and the
  handover status / Telegram line are gone. It keeps its own Kite login, Telegram login links and the paste-token
  box; the same API key can be used as long as no other program logs in with it at the same time.

### 2026-10-25: Docker
- `Dockerfile` (two stages), `docker-compose.yml`, `.dockerignore`, `docker.sh`; `ServerConfig.host` from
  `BP_HOST`; `start.sh` refuses while the Docker copy runs. Same port, database, settings and Kite redirect URL.

### 2026-10-26: Index ticker bar
- `backend/ingest/indices.py` (`IndexTicker`: quote cache, daily history top-up, 5-minute sparklines, market
  status), `KiteClient.historical_intraday`, `GET /api/indices`; `IndexTicker.jsx` under the header with
  click-to-chart. Stock chart hides an all-zero volume pane. 292 offline tests passing.

### 2026-10-26: Cloud (Oracle + Tailscale)
- Live on Oracle Cloud Always Free (Hyderabad, A1.Flex ARM, Ubuntu 24.04, Docker), private HTTPS via `tailscale
  serve`; Kite redirect and `PUBLIC_BASE_URL` on the tailnet address; reached from Mac, iPhone and Android.
- `docs/CLOUD.md` runbook; `scripts/server-setup.sh`, `scripts/backup.sh` (online SQLite backup, integrity check,
  14-day rotation, Telegram on failure, `--install-cron`), `scripts/pull-backups.sh`; `./docker.sh rebuild`.
- Fix (PR #28): `/api/positions` returned red-flag signals under the guard's `flags`, blanking the dashboard;
  they are now `signals`.

### 2026-10-27: Phone layout for the Screens page
- Phones (< 600 px): ticker chips first, screen pills, one-line market, compact stage cards with the top names of the
  chosen stage under them (`StagePeek`), then Moves / News / Results, plan and positions as one-line rows. Desktop
  unchanged. Checked with real renders at 390 px and 1280 px.

### 2026-10-27: Market hours: live guard, entry checklist, Today page
- `backend/alerts/live.py`: entry checklist and verdict (zone, VWAP, volume pace, vs Nifty 500, opening range), size,
  live guard rules, `LiveBoard` (cached quotes, opening ranges, sparklines). `KiteClient.historical_intraday` returns
  high / low / volume too.
- Scheduler: live position guard on the intraday tick (under stop, heavy selling, holdings re-read with "now
  following"), positions in the 9:10 AM opening check and the 3:15 PM check, checklist on heads-ups and the 3:15 PM check,
  one ⬆️ upgrade note, 🔕 Quiet today button.
- `GET /api/today`, `/api/today/sparks`, live fields on `/api/positions`; ⚡ Today page; live price, chip and stop bar
  on the positions card; new Alerts settings. 304 offline tests passing.

### 2026-10-27: 12-hour IST times
- Everything you read says 6:01 AM / 3:15 PM: Telegram alerts, chart images, the bot, the Kite callback page
  (`backend/timefmt.py`), and the dashboard (`frontend/src/timefmt.js`: ticker, Today page, Kite bar, Data, Alerts
  log, jobs, Logs; timestamps are read as IST whatever the device's time zone). Time settings stay 24-hour `HH:MM`
  inputs, each with its 12-hour value shown next to it. Alerts follow the configured check / scan / intraday times.

