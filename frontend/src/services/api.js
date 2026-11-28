// Thin fetch wrapper. Errors carry the backend's `detail` message.
async function req(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data && data.detail;
    const err = new Error(typeof detail === 'string' ? detail : `${res.status} ${res.statusText}`);
    err.status = res.status;                               // e.g. 409 while a job for the same key runs
    throw err;
  }
  return data;
}

export const kiteStatus = (verify = false) => req('GET', `/api/kite/status${verify ? '?verify=true' : ''}`);
export const setToken = (token) => req('POST', '/api/kite/token', { access_token: token });
export const refreshUniverse = () => req('POST', '/api/universe/refresh');
export const getUniverse = () => req('GET', '/api/universe');
export const startSync = (forceFull = false) => req('POST', '/api/ingest/sync', { force_full: forceFull });
export const cancelSync = () => req('POST', '/api/ingest/cancel');
export const ingestStatus = () => req('GET', '/api/ingest/status');
export const getCandles = (sym, limit = 750) =>
  req('GET', `/api/stock/${encodeURIComponent(sym)}/candles?limit=${limit}`);
export const getMetrics = (sym) => req('GET', `/api/stock/${encodeURIComponent(sym)}/metrics`);
export const getBases = (sym, since) =>
  req('GET', `/api/stock/${encodeURIComponent(sym)}/bases${since ? `?since=${since}` : ''}`);
export const screenSummary = (screen) => req('GET', `/api/screens/${screen}/summary`);
export const screenResults = (screen, stage) => req('GET', `/api/screens/${screen}/results?stage=${stage}`);
export const screenSparks = (screen, stage, bars = 120) =>
  req('GET', `/api/screens/${screen}/sparks?stage=${stage}&bars=${bars}`);
export const marketHealth = () => req('GET', '/api/market/health');
export const getPlan = () => req('GET', '/api/plan');
export const getPositions = () => req('GET', '/api/positions');
export const setStop = (sym, stop) => req('PUT', `/api/positions/${encodeURIComponent(sym)}`, { stop });
export const runScan = () => req('POST', '/api/screens/scan');
export const getChanges = (screen = 'bases') => req('GET', `/api/changes?screen=${screen}`);
export const alertChartUrl = (sym, kind = 'auto', stamp = 0) =>
  `/api/stock/${encodeURIComponent(sym)}/alert-chart.png?kind=${kind}&t=${stamp}`;
export const getVcp = (sym) => req('GET', `/api/stock/${encodeURIComponent(sym)}/vcp`);
export const alertsStatus = () => req('GET', '/api/alerts/status');
export const detectChat = () => req('POST', '/api/alerts/detect-chat');
export const sendLoginLink = () => req('POST', '/api/alerts/login-link');
export const testAlert = () => req('POST', '/api/alerts/test');
export const checkBot = () => req('GET', '/api/alerts/bot');
export const sampleAlert = (kind) => req('POST', '/api/alerts/sample', { kind });
export const alertsLog = (qs = 'limit=100') => req('GET', `/api/alerts/log?${qs}`);
export const stockEverywhere = (symbol) => req('GET', `/api/stock/${encodeURIComponent(symbol)}/everywhere`);
export const getJobs = () => req('GET', '/api/jobs?limit=30');
export const runJob = (job) => req('POST', `/api/jobs/${job}/run`);
export const getConfig = (section) => req('GET', `/api/config/${section}`);
export const putConfig = (section, values) => req('PUT', `/api/config/${section}`, values);
export const getLogs = (level) => req('GET', `/api/logs?limit=200${level ? `&level=${level}` : ''}`);
export const eventsStatus = () => req('GET', '/api/events');
export const refreshEvents = () => req('POST', '/api/events/refresh');
export const filingsStatus = () => req('GET', '/api/filings');
export const stockFilings = (symbol) => req('GET', `/api/filings?symbol=${encodeURIComponent(symbol)}`);
export const refreshFilings = () => req('POST', '/api/filings/refresh');
export const dealsStatus = () => req('GET', '/api/deals');
export const stockDeals = (symbol) => req('GET', `/api/deals?symbol=${encodeURIComponent(symbol)}`);
export const refreshDeals = () => req('POST', '/api/deals/refresh');
export const getNews = () => req('GET', '/api/news');
export const getIndices = () => req('GET', '/api/indices');
export const getToday = () => req('GET', '/api/today');
export const getTodaySparks = () => req('GET', '/api/today/sparks');
export const getBreakout = () => req('GET', '/api/breakout');
export const getBreakoutTrades = (variant, scenario, limit, offset) => req('GET', `/api/breakout/trades?variant=${variant}&scenario=${scenario}&limit=${limit}&offset=${offset}`);
export const getAttention = () => req('GET', '/api/attention');
export const getSchedule = () => req('GET', '/api/schedule');
export const follow = () => req('GET', '/api/follow');
export const health = () => req('GET', '/api/health');
export const configChanges = () => req('GET', '/api/config-changes');
export const backups = () => req('GET', '/api/backups');
export const backupsRun = () => req('POST', '/api/backups/run');
export const maintenance = () => req('GET', '/api/maintenance');                    // 🧹 archive and vacuum
export const maintenanceArchive = () => req('POST', '/api/maintenance/archive');
export const realReconcile = (days = 30) => req('GET', `/api/real/reconcile?days=${days}`);
export const realKronos = () => req('GET', '/api/real/kronos');                 // 📈 the candle model on the open positions
export const corpActions = () => req('GET', '/api/corp-actions');
export const followRebuild = () => req('POST', '/api/follow/rebuild');
export const followSettings = (per_position) => req('PUT', '/api/follow/settings', { per_position });
export const followDefaults = (defaults) => req('PUT', '/api/follow/settings', { defaults });
export const getPortfolio = (days = 365) => req('GET', `/api/portfolio?days=${days}`);
export const getOverview = () => req('GET', '/api/overview');
export const getReal = () => req('GET', '/api/real');
export const realSettings = (body) => req('POST', '/api/real/settings', body);
export const realRetry = (symbol, action) => req('POST', '/api/real/retry', { symbol, action });
export const realKill = (on) => req('POST', `/api/real/kill?on=${on}`);
export const realReset = () => req('POST', '/api/real/reset');
export const realCapital = (capital) => req('POST', '/api/real/capital', { capital });
export const realReplan = (maxPos) => req('POST', '/api/real/replan', maxPos ? { max_pos: maxPos } : {});
// corrections by hand (nothing is sent): dry = the effects only, nothing saved
export const realEditPosition = (symbol, body, dry = false) => req('POST', `/api/real/position/${encodeURIComponent(symbol)}${dry ? '?dry=1' : ''}`, body);
export const realRemovePosition = (symbol, note) => req('DELETE', `/api/real/position/${encodeURIComponent(symbol)}`, { note });
export const realAddPosition = (body) => req('POST', '/api/real/position', body);
// 💰 fill confirmation and the hand tools (nothing is ever sent)
export const realConfirmPosition = (symbol, note = '') => req('POST', `/api/real/position/${encodeURIComponent(symbol)}/confirm`, { note });
export const realCheckFills = () => req('POST', '/api/real/confirm');
export const realClosePosition = (symbol, body) => req('POST', `/api/real/position/${encodeURIComponent(symbol)}/close`, body);
export const realAddTrade = (body) => req('POST', '/api/real/trade', body);
export const realVoidTrade = (id, note = '') => req('POST', `/api/real/trade/${id}/void`, { note });
// ☀️ the morning dry run: today's plan with a per-order skip (for today only); send-now runs the send at once
export const realMorning = () => req('GET', '/api/real/morning');
export const realSkip = (symbol, on, note = '') => req('POST', '/api/real/skip', { symbol, on, note });
export const realHold = () => req('POST', '/api/real/hold');
export const realSendNow = () => req('POST', '/api/real/send-now');
export const realEditTrade = (id, body, dry = false) => req('POST', `/api/real/trade/${id}${dry ? '?dry=1' : ''}`, body);
export const runBreakout = () => req('POST', '/api/breakout/run');
export const getPaper = () => req('GET', '/api/paper');
export const startPaper = (capital) => req('POST', '/api/paper/start', { capital });
export const resetPaper = () => req('POST', '/api/paper/reset');
export const paperCapital = (capital) => req('POST', '/api/paper/capital', { capital });
export const paperReplan = (maxPos) => req('POST', '/api/paper/replan', maxPos ? { max_pos: maxPos } : {});

// 🧩 the NIFTY options book (paper only; the Real switch is refused with 400 "not wired")
export const getOptions = () => req('GET', '/api/options');
export const putOptionsSettings = (body) => req('PUT', '/api/options/settings', body);
export const optionsKill = (on) => req('POST', `/api/options/kill?on=${on}`);
export const optionsClose = () => req('POST', '/api/options/close');
export const optionsHold = () => req('POST', '/api/options/hold');
export const optionsCapital = (capital) => req('POST', '/api/options/capital', { capital });
export const getOptionsDays = (limit = 30) => req('GET', `/api/options/days?limit=${limit}`);
export const getOptionsTrades = (limit = 100) => req('GET', `/api/options/trades?limit=${limit}`);
export const getOptionsChain = () => req('GET', '/api/options/chain');
export const getOptionsAsif = (days = 60) => req('GET', `/api/options/asif?days=${days}`);   // Report as if · exit rules (replayed from the chain snapshots)
// 🤖 the local AI (System › AI): switches, the full stop, the model downloads, a test prompt, the job log
export const getAi = () => req('GET', '/api/ai');
export const putAiSettings = (body) => req('PUT', '/api/ai/settings', body);
export const aiStop = () => req('POST', '/api/ai/stop');
export const aiStart = () => req('POST', '/api/ai/start');
export const aiPull = () => req('POST', '/api/ai/pull');
export const aiTest = () => req('POST', '/api/ai/test');
export const getAiJobs = (limit = 50) => req('GET', `/api/ai/jobs?limit=${limit}`);
// 🤖 the AI's filing / deal notes and the quality test set
export const getAiNotes = (params = {}) => { const q = Object.entries(params).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&'); return req('GET', `/api/ai/notes${q ? `?${q}` : ''}`); };
export const getAiNote = (id) => req('GET', `/api/ai/notes/${id}`);
export const aiNotesRun = () => req('POST', '/api/ai/notes/run');
export const aiRerun = () => req('POST', '/api/ai/rerun');
export const aiTestsetRun = () => req('POST', '/api/ai/testset/run');
// 🤖 PR 3: suggestions (Home panel + scorecard), the trade "why" lines, ask the app
const qs = (params = {}) => { const q = Object.entries(params).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&'); return q ? `?${q}` : ''; };
export const getAiSuggestions = (params = {}) => req('GET', `/api/ai/suggestions${qs(params)}`);
export const getAiSuggestion = (id) => req('GET', `/api/ai/suggestions/${id}`);
export const aiSuggestionAccept = (id) => req('POST', `/api/ai/suggestions/${id}/accept`);
export const aiSuggestionDismiss = (id, reason = 'none') => req('POST', `/api/ai/suggestions/${id}/dismiss`, { reason });
export const aiSuggestionLater = (id) => req('POST', `/api/ai/suggestions/${id}/later`);
export const aiSuggestionsRun = () => req('POST', '/api/ai/suggestions/run');
export const aiSuggestionMute = (kind, muted) => req('POST', '/api/ai/suggestions/mute', { kind, muted });
export const getAiScorecard = (month) => req('GET', `/api/ai/suggestions/scorecard${qs({ month })}`);
export const aiWhyRun = () => req('POST', '/api/ai/why/run');
export const aiAsk = (question) => req('POST', '/api/ai/ask', { question });
export const getAiAsks = (limit = 10) => req('GET', `/api/ai/asks?limit=${limit}`);
// 🤖 the AI view: a rating beside every system-suggested stock (reads only; it never acts)
export const getAiView = (symbols) => req('GET', `/api/ai/view?symbols=${encodeURIComponent((symbols || []).join(','))}`);
export const getAiViewRecord = (weeks) => req('GET', `/api/ai/view/record${qs({ weeks })}`);
export const aiViewRun = () => req('POST', '/api/ai/view/run');
// ✨ the Today card (Home), the overnight rating changes, the live heat on the plan, the Week story page
export const getTodayCard = () => req('GET', '/api/today-card');
export const getOvernight = (day) => req('GET', `/api/ai/overnight${qs({ day })}`);
export const getPlanHeat = () => req('GET', '/api/plan/heat');
export const getWeek = (week) => req('GET', `/api/week${qs({ week })}`);
export const writeWeek = (week) => req('POST', `/api/week/write${qs({ week })}`);
// 💡 Tips: a channel's calls caught, decoded and scored; a separate paper book (never linked to any strategy).
export const getTips = () => req('GET', '/api/tips');
export const getHomeTips = () => req('GET', '/api/home/tips');              // 🏠 Home · 💡 Tips · Kite (no quote / Kite call)
export const getTipsHints = (params = {}) => req('GET', `/api/tips/hints${qs(params)}`);
export const getTipsHint = (id) => req('GET', `/api/tips/hints/${encodeURIComponent(id)}`);
export const tipsTake = (id, body = {}) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/take`, body);
export const tipsSkip = (id) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/skip`, {});
export const tipsExit = (id) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/exit`, {});
export const tipsTrail = (id, to) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/trail`, { to });
export const tipsSetStop = (id, value, rev) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/stop`, rev == null ? { value } : { value, rev });
export const tipsSymbol = (id, symbol) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/symbol`, { symbol });
// 📷 a screenshot strike mismatch: {strike, opt_type?, symbol?} = use the screenshot's · {keep: true} = keep the text's
export const tipsStrike = (id, body) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/strike`, body);
export const tipsPause = (id) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/pause`, {});
export const tipsResume = (id) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/resume`, {});
export const getTipsFeed = (params = {}) => req('GET', `/api/tips/feed${qs(params)}`);
export const getTipsRecord = (params = {}) => req('GET', `/api/tips/record${qs(params)}`);
export const putTipsSettings = (values) => req('PUT', '/api/tips/settings', values);
// ⚡ Tips → Kite orders (real money; every action is the owner's click through a confirm)
export const getTipsKite = () => req('GET', '/api/tips/kite');
export const tipsKite = (id, action, body = {}) => req('POST', `/api/tips/hints/${encodeURIComponent(id)}/kite/${encodeURIComponent(action)}`, body);
export const tipsKiteKill = (on) => req('POST', '/api/tips/kite/kill', { on: !!on });
export const tipsKiteResync = () => req('POST', '/api/tips/kite/resync', {});
export const tipsKiteTest = () => req('POST', '/api/tips/kite/test', {});            // the connection test (real Kite, ~6 s)
export const tipsKiteDryClear = () => req('POST', '/api/tips/kite/dry/clear', {});   // void today's dry run
export const tipsKiteRehearse = () => req('POST', '/api/tips/kite/dry/rehearse', {}); // a made-up hint to rehearse on (dry run)
export const tipsKiteDayCheck = () => req('POST', '/api/tips/kite/daycheck', {});     // the Kite day check now (3:35 PM by itself)
export const tipsTestPost = (text) => req('POST', '/api/tips/test-post', { text });
// 🤖 AI on Tips: the weekly channel profile (Research › Tips › Track); Write now is one per hour (429 before that)
// 📈 the market read: the "Market now" panel (Research › Tips) and the candle model's scorecard (Settings › market / ai)
export const getMarketNow = (index) => req('GET', `/api/market/now?index=${encodeURIComponent(index || 'NIFTY')}`);
export const getMarketScorecard = () => req('GET', '/api/market/scorecard');
// 📈 System › AI · Candle model: the worker's state, today's reads, the mode and the gate; "Read now" = one NIFTY read (one a minute)
export const getMarketStatus = () => req('GET', '/api/market/status');
export const marketReadNow = (symbol = 'NIFTY') => req('POST', '/api/market/read', { symbol });
export const getTipsWeekly = (week) => req('GET', `/api/tips/ai/weekly${week ? `?week=${encodeURIComponent(week)}` : ''}`);
export const tipsWeeklyWrite = (week) => req('POST', '/api/tips/ai/weekly/write', week ? { week } : {});
export const tipsLogin = (phone) => req('POST', '/api/tips/login', { phone });
export const tipsLoginCode = (code, password) => req('POST', '/api/tips/login/code', password ? { code, password } : { code });
// The Telegram HTML export (one or more .html files) as multipart; onProgress(0..1) follows the upload.
export const tipsImport = (files, onProgress) => new Promise((resolve, reject) => {
  const fd = new FormData();
  [...(files || [])].forEach((f) => fd.append('files', f, f.name));
  const x = new window.XMLHttpRequest();
  x.open('POST', '/api/tips/import');
  if (x.upload && onProgress) x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
  x.onload = () => {
    let data = null;
    try { data = x.responseText ? JSON.parse(x.responseText) : null; } catch { data = null; }
    if (x.status >= 200 && x.status < 300) resolve(data);
    else { const err = new Error(typeof data?.detail === 'string' ? data.detail : `${x.status} ${x.statusText}`); err.status = x.status; reject(err); }
  };
  x.onerror = () => reject(new Error('upload failed (network)'));
  x.send(fd);
});

// ₿ BTC pivot patterns (alerts only)
export const getCrypto = () => req('GET', '/api/crypto');
export const cryptoCandles = (tf, range, win) => req('GET', `/api/crypto/candles?tf=${encodeURIComponent(tf)}&range=${encodeURIComponent(range)}${win ? `&from=${win.from}&to=${win.to}` : ''}`);
export const cryptoQuiet = (hours) => req('POST', '/api/crypto/quiet', { hours });
export const cryptoRefresh = () => req('POST', '/api/crypto/refresh');
export const cryptoScan = () => req('POST', '/api/crypto/scan');
export const cryptoForecastNow = (horizon) => req('GET', `/api/crypto/forecast_now?horizon=${encodeURIComponent(horizon)}`);   // the cached per-candle read (never calls the model)
export const cryptoScores = (days, counted = true) => req('GET', `/api/crypto/scores?days=${encodeURIComponent(days)}&counted=${counted ? 1 : 0}`);   // the tiles' 1 d · 7 d · 30 d toggle
