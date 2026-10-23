import React, { useState } from 'react';
import * as api from '../services/api';

const fmt = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) : '');

// Header strip: Kite login state, login button, manual token paste.
export default function KiteBar({ status, onChange }) {
  const [paste, setPaste] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  if (!status) return <div className="kitebar">Connecting to backend…</div>;

  const verify = async () => {
    setBusy(true); setErr('');
    try { onChange(await api.kiteStatus(true)); } catch (e) { setErr(e.message); }
    setBusy(false);
  };
  const saveToken = async () => {
    setErr('');
    try { await api.setToken(paste); setPaste(''); onChange(await api.kiteStatus(true)); } catch (e) { setErr(e.message); }
  };

  return (
    <div className="kitebar">
      <span className={`pill ${status.has_token ? 'ok' : 'bad'}`}>
        Kite: {status.has_token ? 'connected' : 'login needed'}
      </span>
      {status.user_id && <span className="muted">user {status.user_id}</span>}
      {status.expires_at && <span className="muted">expires {fmt(status.expires_at)} IST</span>}
      {status.token_source === 'env' && <span className="muted">token from .env (expiry unknown)</span>}
      {!status.api_key_configured && <span className="warn">Set KITE_API_KEY / KITE_API_SECRET in .env</span>}
      {status.login_url && (
        <a className="btn" href={status.login_url}>{status.has_token ? 'Re-login' : 'Login with Kite'}</a>
      )}
      {status.has_token && <button className="btn ghost" disabled={busy} onClick={verify}>Verify</button>}
      <input className="token" placeholder="or paste access token" value={paste}
             onChange={(e) => setPaste(e.target.value)} />
      {paste && <button className="btn ghost" onClick={saveToken}>Save</button>}
      {err && <span className="warn">{err}</span>}
    </div>
  );
}
