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
    throw new Error(typeof detail === 'string' ? detail : `${res.status} ${res.statusText}`);
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
export const alertsLog = () => req('GET', '/api/alerts/log?limit=100');
export const getJobs = () => req('GET', '/api/jobs?limit=30');
export const runJob = (job) => req('POST', `/api/jobs/${job}/run`);
export const getConfig = (section) => req('GET', `/api/config/${section}`);
export const putConfig = (section, values) => req('PUT', `/api/config/${section}`, values);
export const getLogs = (level) => req('GET', `/api/logs?limit=200${level ? `&level=${level}` : ''}`);
export const eventsStatus = () => req('GET', '/api/events');
export const stockEvents = (symbol) => req('GET', `/api/events?symbol=${encodeURIComponent(symbol)}`);
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
export const getBreakoutTrades = (scenario, limit, offset) => req('GET', `/api/breakout/trades?scenario=${scenario}&limit=${limit}&offset=${offset}`);
export const runBreakout = () => req('POST', '/api/breakout/run');
export const getPaper = () => req('GET', '/api/paper');
export const startPaper = (capital) => req('POST', '/api/paper/start', { capital });
export const resetPaper = () => req('POST', '/api/paper/reset');
