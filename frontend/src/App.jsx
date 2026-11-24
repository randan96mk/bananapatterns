import React, { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import KiteBar from './components/KiteBar';
import IndexTicker from './components/IndexTicker';
import { AttentionDot } from './components/AttentionStrip';
import StatusBar from './components/StatusBar';
import CommandPalette from './components/CommandPalette';
import * as api from './services/api';
import { useLive } from './services/live';
import { useIsPhone } from './useMedia';
import Loading from './components/Loading';

// Pages load on first visit (code splitting): the top bar, nav, status bar and ⌘K stay in the main bundle.
// After a deploy the chunk file names change; a tab still holding the old index would ask for files that no longer
// exist and sit on "Loading…" for ever. `lazyPage` retries the import once, then reloads the page one time (a flag in
// sessionStorage stops a loop); if the reload does not help it renders a short message instead of a blank page.
const RELOAD_KEY = 'bp.chunk.reloaded';
function lazyPage(load) {
  return lazy(() => load().catch(() => new Promise((r) => setTimeout(r, 800)).then(load)).catch((err) => {
    let done = false;
    try { done = sessionStorage.getItem(RELOAD_KEY) === String(window.location.hash); } catch { /* no storage */ }
    if (!done) {
      try { sessionStorage.setItem(RELOAD_KEY, String(window.location.hash)); } catch { /* no storage */ }
      window.location.reload();
      return new Promise(() => {});                                   // the reload takes over
    }
    console.error('page chunk failed to load', err);
    return { default: () => <div className="rd"><div className="rbanner bad">This page did not load (a new version may have just been deployed). Refresh the page; if it keeps failing, check the server log.</div></div> };
  }).then((m) => { try { sessionStorage.removeItem(RELOAD_KEY); } catch { /* no storage */ } return m; }));
}
const UniverseTable = lazyPage(() => import('./components/UniverseTable'));
const ScreensHome = lazyPage(() => import('./components/ScreensHome'));
const FollowPage = lazyPage(() => import('./components/FollowPage'));
const StockChart = lazyPage(() => import('./components/StockChart'));
const DataPanel = lazyPage(() => import('./components/DataPanel'));
const SettingsPanel = lazyPage(() => import('./components/SettingsPanel'));
const LogsPanel = lazyPage(() => import('./components/LogsPanel'));
const SentAlerts = lazyPage(() => import('./components/SentAlerts'));
const AlertsPanel = lazyPage(() => import('./components/AlertsPanel'));
const BreakoutPage = lazyPage(() => import('./components/BreakoutPage'));
const PaperPage = lazyPage(() => import('./components/PaperPage'));
const RealPage = lazyPage(() => import('./components/RealPage'));
const OptionsPage = lazyPage(() => import('./components/OptionsPage'));
const AiPage = lazyPage(() => import('./components/AiPage'));
const TodayPage = lazyPage(() => import('./components/TodayPage'));
const HomePage = lazyPage(() => import('./components/HomePage'));
const PortfolioPage = lazyPage(() => import('./components/PortfolioPage'));
const WeekPage = lazyPage(() => import('./components/WeekPage'));
const StrategiesPage = lazyPage(() => import('./components/StrategiesPage'));
const SystemHome = lazyPage(() => import('./components/SystemHome'));
const TipsPage = lazyPage(() => import('./components/TipsPage'));
const CryptoPage = lazyPage(() => import('./components/CryptoPage'));

// v2 layout: five sections (sidebar on a computer, bottom bar on a phone), each with its pages. The page is kept in the
// address (#/section/page), so links and the back button work.
const ICONS = {
  home: 'M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z',
  strategies: 'M4 19V9m6 10V5m6 14v-7m4 7H2',
  portfolio: 'M3 7h18v13H3zM8 7V4h8v3',
  research: 'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zm10 17-5-5',
  system: 'M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8zM3 12h2m14 0h2M12 3v2m0 14v2M5.6 5.6 7 7m10 10 1.4 1.4M5.6 18.4 7 17m10-10 1.4-1.4',
};
export const SECTIONS = [
  ['home', 'Home', [['home', 'Overview']]],
  ['strategies', 'Strategies', [['strategies', 'All strategies'], ['breakout', 'Trend Breakout'], ['options', '🧩 Options'], ['crypto', '₿ BTC patterns']]],
  ['portfolio', 'Portfolio', [['portfolio', 'All accounts'], ['real', 'Real'], ['paper', 'Paper · swing'], ['week', '📖 Week']]],
  ['research', 'Research', [['screens', 'Screens'], ['follow', '📈 Follow'], ['plan', "Today's plan"], ['universe', 'Universe'], ['tips', '💡 Tips']]],
  ['system', 'System', [['system', 'Connections'], ['alerts', 'Alerts & schedule'], ['sent', 'Sent alerts'], ['data', 'Data'], ['settings', 'Settings'], ['logs', 'Logs'], ['ai', '🤖 AI']]],
];
const SECTION_OF = Object.fromEntries(SECTIONS.flatMap(([s, , pages]) => pages.map(([p]) => [p, s])));
const DOTS = ['breakout', 'paper', 'real', 'options', 'crypto'];
const Icon = ({ id }) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"
  strokeLinejoin="round" aria-hidden="true"><path d={ICONS[id]} /></svg>;

const fromHash = () => {
  const p = window.location.hash.replace(/^#\/?/, '').split('/')[1];
  return SECTION_OF[p] ? p : 'home';
};

export default function App() {
  const [tab, setTab] = useState(fromHash);
  const [kite, setKite] = useState(null);
  const [universe, setUniverse] = useState(null);
  const [selected, setSelected] = useState(null);
  const [stock, setStock] = useState(null);                // a stock opened from ⌘K or the index ticker
  const [backendErr, setBackendErr] = useState('');
  const [showKite, setShowKite] = useState(false);
  const [cmdk, setCmdk] = useState(false);
  const [help, setHelp] = useState(false);
  const [more, setMore] = useState(false);
  const phone = useIsPhone();
  const sched = useLive('schedule');                       // 📡 the live connection (also feeds the status bar)
  const realOn = !!(sched?.real?.enabled && !sched?.real?.killed);
  const liveKite = useLive('kite');
  useEffect(() => { if (liveKite && !liveKite.error) setKite(liveKite); }, [liveKite]);
  const section = SECTION_OF[tab] || 'home';
  const go = useCallback((id) => {
    const p = SECTION_OF[id] ? id : (SECTIONS.find(([s]) => s === id)?.[2][0][0] || 'home');
    setTab(p); setMore(false);
    if (window.location.hash !== `#/${SECTION_OF[p]}/${p}`) window.history.pushState(null, '', `#/${SECTION_OF[p]}/${p}`);
    window.scrollTo(0, 0);
  }, []);
  useEffect(() => {
    const on = () => setTab(fromHash());
    window.addEventListener('popstate', on);
    let g = 0;                                            // "g" then a letter: jump to a section (like GitHub / Gmail)
    const typing = (e) => /input|textarea|select/i.test(e.target.tagName) || e.target.isContentEditable;
    const key = (e) => {
      if ((e.key === 'k' && (e.metaKey || e.ctrlKey)) || (e.key === '/' && !typing(e))) {
        e.preventDefault(); setCmdk(true); return;
      }
      if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === '?') { setHelp((h) => !h); return; }
      if (e.key === 'Escape') { setHelp(false); return; }
      if (e.key === 'g') { g = Date.now(); return; }
      if (Date.now() - g < 1500) {
        const to = { h: 'home', s: 'strategies', p: 'portfolio', r: 'research', y: 'system', b: 'breakout', o: 'options', w: 'week', l: 'follow', c: 'screens', a: 'alerts', d: 'data', t: 'tips' }[e.key];
        if (to) { go(to); g = 0; }
      }
    };
    window.addEventListener('keydown', key);
    return () => { window.removeEventListener('popstate', on); window.removeEventListener('keydown', key); };
  }, []);

  const loadKite = useCallback(async (verify = false) => {
    try { setKite(await api.kiteStatus(verify)); setBackendErr(''); } catch (e) { setBackendErr(e.message); }
  }, []);
  const loadUniverse = useCallback(() => api.getUniverse().then(setUniverse).catch(() => {}), []);
  useEffect(() => {
    const justLoggedIn = new URLSearchParams(window.location.search).get('kite') === 'connected';
    if (justLoggedIn) window.history.replaceState(null, '', window.location.pathname + window.location.hash);
    loadKite(justLoggedIn);
    loadUniverse();                                        // the 06:00 IST expiry arrives on the live connection
  }, [loadKite, loadUniverse]);

  const symbols = useMemo(() => (universe?.rows || []).map((r) => r.symbol), [universe]);
  const [, title, pages] = SECTIONS.find(([s]) => s === section);
  const pageTitle = pages.find(([p]) => p === tab)?.[1];

  return (
    <div className="shell">
      {!phone && <aside className="sidebar" aria-label="Sections">
        <div className="brand" title="BananaPatterns">🍌<span className="brandtxt"> Banana<span>Patterns</span></span></div>
        {SECTIONS.map(([s, label, sub], i) => (
          <React.Fragment key={s}>
            {s === 'system' && <div className="navsep" />}
            <button className={`navsec ${section === s ? 'active' : ''}`} aria-current={section === s ? 'page' : undefined}
              onClick={() => go(s === section ? tab : s)}><Icon id={s} />{label}
              {sub.some(([p]) => DOTS.includes(p)) && section !== s && <AttentionDot tab={sub.find(([p]) => DOTS.includes(p))[0]} />}</button>
            {section === s && sub.length > 1 && <div className="navsub">{sub.map(([p, pl]) => (
              <button key={p} className={tab === p ? 'active' : ''} onClick={() => go(p)}>{pl}
                {DOTS.includes(p) && <AttentionDot tab={p} active={tab === p} />}</button>))}</div>}
          </React.Fragment>))}
        <div className="sidefoot">{realOn ? '💰 Real trading ON' : 'Screen-only (no orders)'}<br />Personal use · data © NSE via Kite</div>
      </aside>}
      <div className="mainpane">
        <div className="topbar">
          {!backendErr && <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingRight: 12 }}>
            <div style={{ flex: 1, minWidth: 0 }}><StatusBar kite={kite} onKite={() => setShowKite(!showKite)} onGo={go} /></div>
            <button className="cmdk-btn" onClick={() => setCmdk(true)} aria-label="Search">⌕ {phone ? '' : 'Search stock, page…'}<kbd>⌘K</kbd></button>
          </div>}
          {!backendErr && !phone && <IndexTicker onSelect={(s) => setStock(stock === s ? null : s)} />}
        </div>
        {backendErr ? <div className="kitebar warn">Backend unreachable: {backendErr}. Is the app running?</div>
          : (!kite?.has_token || showKite) && <KiteBar status={kite} onChange={setKite} />}
        <div className="pagehead"><h2>{title}</h2>{pages.length > 1 && <span className="crumb">› {pageTitle}</span>}</div>
        {pages.length > 1 && <nav className="subnav" aria-label={title}>{pages.map(([p, pl]) => (
          <button key={p} className={tab === p ? 'active' : ''} onClick={() => go(p)}>{pl}{DOTS.includes(p) && <AttentionDot tab={p} active={tab === p} />}</button>))}</nav>}
        {stock && <><div className="drawer-back" onClick={() => setStock(null)} />
          <aside className="drawer" aria-label={stock}><Suspense fallback={<Loading />}><StockChart symbol={stock} onClose={() => setStock(null)} /></Suspense></aside></>}
        <main><Suspense fallback={<Loading />}>
          {tab === 'home' && <HomePage onGo={go} onStock={setStock} />}
          {tab === 'strategies' && <StrategiesPage onGo={go} />}
          {tab === 'portfolio' && <PortfolioPage onStock={setStock} />}
          {tab === 'system' && <SystemHome onGo={go} onKite={() => { setShowKite(true); window.scrollTo(0, 0); }} />}
          {tab === 'plan' && <TodayPage />}
          {tab === 'breakout' && <BreakoutPage />}
          {tab === 'real' && <RealPage />}
          {tab === 'options' && <OptionsPage />}
          {tab === 'crypto' && <CryptoPage />}
          {tab === 'paper' && <PaperPage />}
          {tab === 'week' && <WeekPage onGo={go} />}
          {tab === 'follow' && <FollowPage />}
          {tab === 'tips' && <TipsPage />}
          {tab === 'screens' && <ScreensHome hasData={(universe?.rows || []).some((r) => r.bars > 0)} />}
          {tab === 'universe' && <>
            {selected && <StockChart symbol={selected} onClose={() => setSelected(null)} />}
            <UniverseTable data={universe} selected={selected} onSelect={setSelected} /></>}
          {tab === 'alerts' && <AlertsPanel />}
          {tab === 'data' && <DataPanel hasToken={!!kite?.has_token} onDataChanged={loadUniverse} />}
          {tab === 'settings' && <SettingsPanel />}
          {tab === 'logs' && <LogsPanel />}
          {tab === 'ai' && <AiPage />}
          {tab === 'sent' && <SentAlerts onStock={setStock} />}
        </Suspense></main>
      </div>
      {help && <div className="drawer" style={{ maxWidth: 360 }} aria-label="Keyboard shortcuts" onClick={() => setHelp(false)}>
        <b style={{ fontSize: 16 }}>⌨️ Keyboard</b>
        <table className="grid small" style={{ marginTop: 8 }}><tbody>
          {[['⌘K or /', 'search a stock or a page'], ['g then h', 'Home'], ['g then s', 'Strategies'], ['g then p', 'Portfolio'], ['g then r', 'Research'], ['g then y', 'System'],
            ['g then b / o', 'Trend Breakout / Options'], ['g then l / c / t', 'Follow / Screens / Tips'], ['g then a / d', 'Alerts / Data'], ['?', 'this list'], ['Esc', 'close']]
            .map(([k, l]) => <tr key={k}><td><kbd>{k}</kbd></td><td className="muted">{l}</td></tr>)}</tbody></table></div>}
      {cmdk && <CommandPalette sections={SECTIONS} symbols={symbols} onClose={() => setCmdk(false)}
        onPage={(p) => { go(p); setCmdk(false); }} onStock={(s) => { setStock(s); setCmdk(false); }} />}
      {phone && <>
        {more && <div className="sheet" role="dialog" aria-label="System">
          {SECTIONS.find(([s]) => s === 'system')[2].map(([p, pl]) => (
            <button key={p} className={`sheetitem ${tab === p ? 'active' : ''}`} onClick={() => go(p)}>{pl}</button>))}</div>}
        <nav className="bottomnav">
          {SECTIONS.filter(([s]) => s !== 'system').map(([s, label]) => (
            <button key={s} className={section === s && !more ? 'active' : ''} onClick={() => go(s === section ? tab : s)}>
              <Icon id={s} />{label}</button>))}
          <button className={more || section === 'system' ? 'active' : ''} onClick={() => setMore(!more)}><Icon id="system" />System</button>
        </nav></>}
    </div>
  );
}
