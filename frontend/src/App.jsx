import React, { useCallback, useEffect, useState } from 'react';
import KiteBar from './components/KiteBar';
import UniverseTable from './components/UniverseTable';
import StockChart from './components/StockChart';
import DataPanel from './components/DataPanel';
import SettingsPanel from './components/SettingsPanel';
import LogsPanel from './components/LogsPanel';
import * as api from './services/api';

const TABS = [
  ['universe', 'Universe'],
  ['data', 'Data'],
  ['settings', 'Settings'],
  ['logs', 'Logs'],
];

export default function App() {
  const [tab, setTab] = useState('universe');
  const [kite, setKite] = useState(null);
  const [universe, setUniverse] = useState(null);
  const [selected, setSelected] = useState(null);
  const [backendErr, setBackendErr] = useState('');

  const loadKite = useCallback(async (verify = false) => {
    try { setKite(await api.kiteStatus(verify)); setBackendErr(''); } catch (e) { setBackendErr(e.message); }
  }, []);
  const loadUniverse = useCallback(() => api.getUniverse().then(setUniverse).catch(() => {}), []);

  useEffect(() => {
    const justLoggedIn = new URLSearchParams(window.location.search).get('kite') === 'connected';
    if (justLoggedIn) window.history.replaceState(null, '', window.location.pathname);
    loadKite(justLoggedIn);
    loadUniverse();
    const t = setInterval(() => loadKite(false), 60000); // catches the 06:00 IST expiry
    return () => clearInterval(t);
  }, [loadKite, loadUniverse]);

  return (
    <div className="app">
      <header>
        <h1>🍌 Banana Patterns <span className="muted">personal VCP screener · NSE</span></h1>
        <nav>{TABS.map(([id, label]) => (
          <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>))}
        </nav>
      </header>
      {backendErr ? <div className="kitebar warn">Backend unreachable: {backendErr}. Is `python run.py` running?</div>
        : <KiteBar status={kite} onChange={setKite} />}
      <main>
        {tab === 'universe' && (
          <>
            {selected && <StockChart symbol={selected} onClose={() => setSelected(null)} />}
            <UniverseTable data={universe} selected={selected} onSelect={setSelected} />
          </>
        )}
        {tab === 'data' && <DataPanel hasToken={!!kite?.has_token} onDataChanged={loadUniverse} />}
        {tab === 'settings' && <SettingsPanel />}
        {tab === 'logs' && <LogsPanel />}
      </main>
      <footer className="muted">Personal use only · screen-only mode (no orders) · data © NSE via Zerodha Kite</footer>
    </div>
  );
}
