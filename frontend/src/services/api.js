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
export const getBases = (sym, since) =>
  req('GET', `/api/stock/${encodeURIComponent(sym)}/bases${since ? `?since=${since}` : ''}`);
export const getConfig = (section) => req('GET', `/api/config/${section}`);
export const putConfig = (section, values) => req('PUT', `/api/config/${section}`, values);
export const getLogs = (level) => req('GET', `/api/logs?limit=200${level ? `&level=${level}` : ''}`);
