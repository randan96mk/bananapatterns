import React, { useCallback, useEffect, useState } from 'react';
import KiteBar from './components/KiteBar';
import UniverseTable from './components/UniverseTable';
import ScreensHome from './components/ScreensHome';
import StockChart from './components/StockChart';
import IndexTicker from './components/IndexTicker';
import DataPanel from './components/DataPanel';
import SettingsPanel from './components/SettingsPanel';
import LogsPanel from './components/LogsPanel';
import AlertsPanel from './components/AlertsPanel';
import BreakoutPage from './components/BreakoutPage';
import TodayPage from './components/TodayPage';
import * as api from './services/api';
import { applyMode, getMode } from './theme';
import { useIsPhone } from './useMedia';

// Phone: these sit in the bottom bar; the rest go under "More".
const PHONE_TABS = ['screens', 'today', 'alerts'];
const TABS = [
  ['screens', 'Screens'],
  ['today', '⚡ Today'],
  ['breakout', '📈 Trend Breakout'],
  ['universe', 'Universe'],
  ['data', 'Data'],
  ['alerts', 'Alerts'],
  ['settings', 'Settings'],
  ['logs', 'Logs'],
];

export default function App() {
  const [tab, setTab] = useState('screens');
  const [kite, setKite] = useState(null);
  const [universe, setUniverse] = useState(null);
  const [selected, setSelected] = useState(null);
  const [indexChart, setIndexChart] = useState(null);
  const [backendErr, setBackendErr] = useState('');
  const [mode, setMode] = useState(getMode);
  const pickMode = (m) => { setMode(m); applyMode(m); };
  const phone = useIsPhone();
  const [more, setMore] = useState(false);
  const go = (id) => { setTab(id); setMore(false); window.scrollTo(0, 0); };
  const themeSwitch = (
    <span className="seg themeseg" role="group" aria-label="Theme">
      {[['auto', '◐ Auto'], ['light', '☀ Light'], ['dark', '☾ Dark']].map(([m, label]) => (
        <button key={m} className={mode === m ? 'active' : ''} aria-pressed={mode === m}
                onClick={() => pickMode(m)}>{label}</button>))}
    </span>);

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
        {!phone && <nav>{TABS.map(([id, label]) => (
          <button key={id} className={tab === id ? 'active' : ''} onClick={() => go(id)}>{label}</button>))}
        </nav>}
        {!phone && themeSwitch}
      </header>
      {!backendErr && <IndexTicker onSelect={(s) => setIndexChart(indexChart === s ? null : s)} />}
      {indexChart && <StockChart symbol={indexChart} onClose={() => setIndexChart(null)} />}
      {backendErr ? <div className="kitebar warn">Backend unreachable: {backendErr}. Is `python run.py` running?</div>
        : <KiteBar status={kite} onChange={setKite} />}
      <main>
        {tab === 'screens' && <ScreensHome hasData={(universe?.rows || []).some((r) => r.bars > 0)} />}
        {tab === 'breakout' && <BreakoutPage />}
        {tab === 'today' && <TodayPage />}
        {tab === 'universe' && (
          <>
            {selected && <StockChart symbol={selected} onClose={() => setSelected(null)} />}
            <UniverseTable data={universe} selected={selected} onSelect={setSelected} />
          </>
        )}
        {tab === 'data' && <DataPanel hasToken={!!kite?.has_token} onDataChanged={loadUniverse} />}
        {tab === 'settings' && <SettingsPanel />}
        {tab === 'alerts' && <AlertsPanel />}
        {tab === 'logs' && <LogsPanel />}
      </main>
      <footer className="muted">Personal use only · screen-only mode (no orders) · data © NSE via Zerodha Kite</footer>
      {phone && (
        <>
          {more && (
            <div className="sheet" role="dialog" aria-label="More">
              {TABS.filter(([id]) => !PHONE_TABS.includes(id)).map(([id, label]) => (
                <button key={id} className={`sheetitem ${tab === id ? 'active' : ''}`} onClick={() => go(id)}>{label}</button>))}
              <div className="sheetrow"><span className="muted">Theme</span>{themeSwitch}</div>
            </div>)}
          <nav className="bottomnav">
            {[['screens', '📊', 'Screens'], ['today', '⚡', 'Today'], ['alerts', '🔔', 'Alerts']].map(([id, icon, label]) => (
              <button key={id} className={tab === id && !more ? 'active' : ''} onClick={() => go(id)}>
                <span aria-hidden="true">{icon}</span>{label}</button>))}
            <button className={more || !PHONE_TABS.includes(tab) ? 'active' : ''} onClick={() => setMore(!more)}>
              <span aria-hidden="true">☰</span>More</button>
          </nav>
        </>)}
    </div>
  );
}
