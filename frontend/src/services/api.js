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
export const getFlow = () => req('GET', '/api/flow');
export const flowStrategy = () => req('GET', '/api/flow/strategy');
export const follow = () => req('GET', '/api/follow');
export const health = () => req('GET', '/api/health');
export const configChanges = () => req('GET', '/api/config-changes');
export const backups = () => req('GET', '/api/backups');
export const backupsRun = () => req('POST', '/api/backups/run');
export const realReconcile = (days = 30) => req('GET', `/api/real/reconcile?days=${days}`);
export const corpActions = () => req('GET', '/api/corp-actions');
export const followRebuild = () => req('POST', '/api/follow/rebuild');
export const followSettings = (per_position) => req('PUT', '/api/follow/settings', { per_position });
export const followDefaults = (defaults) => req('PUT', '/api/follow/settings', { defaults });
export const flowNode = (id, values) => req('PUT', `/api/flow/node/${id}`, values);
export const flowMode = (mode) => req('POST', '/api/flow/mode', { mode });
export const flowStop = () => req('POST', '/api/flow/stop');
export const flowStart = () => req('POST', '/api/flow/start');
export const flowBacktest = (qs = '') => req('GET', `/api/flow/backtest${qs ? `?${qs}` : ''}`);
export const flowBacktestStop = () => req('POST', '/api/flow/backtest/stop');
export const flowBacktestRun = (setups, period) => req('POST', '/api/flow/backtest', { setups, period });
export const flowSetupSave = (name) => req('POST', '/api/flow/setups', { name });
export const flowSetupLoad = (name) => req('POST', '/api/flow/setups/load', { name });
export const flowSetupDelete = (name) => req('POST', '/api/flow/setups/delete', { name });
export const flowResetNodes = () => req('POST', '/api/flow/reset');
export const flowLive = () => req('GET', '/api/flow/live');
export const getPortfolio = (days = 365) => req('GET', `/api/portfolio?days=${days}`);
export const getOverview = () => req('GET', '/api/overview');
export const flowCheck = (symbol) => req('GET', `/api/flow/journey/check?symbol=${encodeURIComponent(symbol)}`);
export const flowNodeStocks = (nid) => req('GET', `/api/flow/journey/node/${nid}`);
export const flowRerun = (nid) => req('POST', `/api/flow/journey/rerun/${nid}`);
export const flowRun = () => req('POST', '/api/flow/run');
export const flowLayout = (pos) => req('PUT', '/api/flow/layout', pos);
export const flowJournal = (days) => req('GET', `/api/flow/journal?days=${days}`);
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

// 📐 the futures book and Backtest F
export const getFutures = () => req('GET', '/api/futures');
export const futuresSettings = (body) => req('POST', '/api/futures/settings', body);
export const futuresKill = (on) => req('POST', `/api/futures/kill?on=${on}`);
export const futuresCapital = (capital) => req('POST', '/api/futures/capital', { capital });
export const futuresReplan = () => req('POST', '/api/futures/replan');
export const futuresMorning = () => req('GET', '/api/futures/morning');
export const futuresSkip = (symbol, on, note = '') => req('POST', '/api/futures/skip', { symbol, on, note });
export const futuresHold = () => req('POST', '/api/futures/hold');
export const futuresEditPosition = (symbol, body, dry = false) => req('POST', `/api/futures/position/${encodeURIComponent(symbol)}${dry ? '?dry=1' : ''}`, body);
export const futuresRemovePosition = (symbol, note, account) => req('DELETE', `/api/futures/position/${encodeURIComponent(symbol)}`, { note, account });
export const futuresAddPosition = (body) => req('POST', '/api/futures/position', body);
export const futuresOrders = (limit = 200) => req('GET', `/api/futures/orders?limit=${limit}`);
export const futuresTrades = (account) => req('GET', `/api/futures/trades${account ? `?account=${account}` : ''}`);
export const futuresRollNow = () => req('POST', '/api/futures/roll');
export const getFuturesBacktest = () => req('GET', '/api/futures/backtest');
export const runFuturesBacktest = () => req('POST', '/api/futures/backtest/run');
export const getFuturesBacktestTrades = (side = 'long', limit = 200, offset = 0) => req('GET', `/api/futures/backtest/trades?side=${side}&limit=${limit}&offset=${offset}`);

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
