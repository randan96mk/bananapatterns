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
import PaperPage from './components/PaperPage';
import RealPage from './components/RealPage';
import { AttentionDot } from './components/AttentionStrip';
import TodayPage from './components/TodayPage';
import StatusBar from './components/StatusBar';
import FlowPage from './components/FlowPage';
import * as api from './services/api';
import { useIsPhone } from './useMedia';

// Navigation in groups: Trade · Research, and a ⚙️ System menu. Phone: Trade (Today), Screens, Alerts in the bottom
// bar, the rest under "More".
const GROUPS = [
  ['Trade', [['today', '⚡ Today'], ['flow', '🔀 Intraday Flow'], ['breakout', '📈 Trend Breakout'], ['paper', '📝 Paper'], ['real', '💰 Real']]],
  ['Research', [['screens', 'Screens'], ['universe', 'Universe']]],
];
const SYSTEM = [['alerts', '🔔 Alerts'], ['data', '🗄 Data'], ['settings', '⚙️ Settings'], ['logs', '📜 Logs']];
const TABS = [...GROUPS.flatMap(([, t]) => t), ...SYSTEM];
const PHONE_TABS = ['today', 'screens', 'alerts'];
const DOTS = ['breakout', 'paper', 'real'];

export default function App() {
  const [tab, setTab] = useState('screens');
  const [kite, setKite] = useState(null);
  const [universe, setUniverse] = useState(null);
  const [selected, setSelected] = useState(null);
  const [indexChart, setIndexChart] = useState(null);
  const [backendErr, setBackendErr] = useState('');
  const [showKite, setShowKite] = useState(false);
  const [sysOpen, setSysOpen] = useState(false);
  const phone = useIsPhone();
  const [realOn, setRealOn] = useState(false);
  useEffect(() => { api.getReal().then((r) => setRealOn(r.enabled && !r.killed)).catch(() => {}); }, [tab]);
  const [more, setMore] = useState(false);
  const go = (id) => { setTab(id); setMore(false); setSysOpen(false); window.scrollTo(0, 0); };

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

  const tabBtn = ([id, label]) => (
    <button key={id} className={tab === id ? 'active' : ''} aria-current={tab === id ? 'page' : undefined} onClick={() => go(id)}>{label}
      {DOTS.includes(id) && <AttentionDot tab={id} active={tab === id} />}</button>);
  return (
    <div className="app">
      <header>
        <h1>🍌 Banana Patterns</h1>
        {!phone && <nav className="groupnav">
          {GROUPS.map(([g, tabs]) => <div key={g} className="navgroup"><span className="navlabel">{g}</span>{tabs.map(tabBtn)}</div>)}
          <div className="navgroup sysmenu">
            <button className={SYSTEM.some(([id]) => id === tab) ? 'active' : ''} aria-expanded={sysOpen} onClick={() => setSysOpen(!sysOpen)}>
              ⚙️ System ▾</button>
            {sysOpen && <div className="sysdrop" role="menu">{SYSTEM.map(([id, label]) => (
              <button key={id} role="menuitem" className={tab === id ? 'active' : ''} onClick={() => go(id)}>{label}</button>))}</div>}
          </div>
        </nav>}
      </header>
      {!backendErr && <StatusBar kite={kite} onKite={() => setShowKite(!showKite)} onGo={go} />}
      {!backendErr && <IndexTicker onSelect={(s) => setIndexChart(indexChart === s ? null : s)} />}
      {indexChart && <StockChart symbol={indexChart} onClose={() => setIndexChart(null)} />}
      {backendErr ? <div className="kitebar warn">Backend unreachable: {backendErr}. Is `python run.py` running?</div>
        : (!kite?.has_token || showKite) && <KiteBar status={kite} onChange={setKite} />}
      <main>
        {tab === 'screens' && <ScreensHome hasData={(universe?.rows || []).some((r) => r.bars > 0)} />}
        {tab === 'breakout' && <BreakoutPage />}
        {tab === 'paper' && <PaperPage />}
        {tab === 'real' && <RealPage />}
        {tab === 'today' && <TodayPage />}
        {tab === 'flow' && <FlowPage />}
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
      <footer className="muted">Personal use only · {realOn ? '💰 real trading ON: orders go to Tradetron by webhook' : 'screen-only mode (no orders)'} · data © NSE via Zerodha Kite</footer>
      {phone && (
        <>
          {more && (
            <div className="sheet" role="dialog" aria-label="More">
              {TABS.filter(([id]) => !PHONE_TABS.includes(id)).map(([id, label]) => (
                <button key={id} className={`sheetitem ${tab === id ? 'active' : ''}`} onClick={() => go(id)}>{label}
                  {DOTS.includes(id) && <AttentionDot tab={id} active={tab === id} />}</button>))}
            </div>)}
          <nav className="bottomnav">
            {[['today', '📈', 'Trade'], ['screens', '📊', 'Screens'], ['alerts', '🔔', 'Alerts']].map(([id, icon, label]) => (
              <button key={id} className={tab === id && !more ? 'active' : ''} onClick={() => go(id)}>
                <span aria-hidden="true">{icon}</span>{label}</button>))}
            <button className={more || !PHONE_TABS.includes(tab) ? 'active' : ''} onClick={() => setMore(!more)}>
              <span aria-hidden="true">☰</span>More</button>
          </nav>
        </>)}
    </div>
  );
}
