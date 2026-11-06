import React, { useCallback, useEffect, useState } from 'react';
import * as api from '../services/api';
import StockChart from './StockChart';
import StageList, { StagePeek } from './StageList';
import MarketCard, { STATUS } from './MarketCard';
import PlanCard from './PlanCard';
import { useIsPhone } from '../useMedia';
import { NewsList, ResultsList } from './NewsFeed';
import { Head, Chip, Cards, Card, Bar, Panel, Seg, Act } from './ui2';

const fmtDate = (iso) => (iso ? iso.split('-').reverse().join('/') : '—');
const dayOf = (iso) => (iso ? new Date(`${iso}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : '—');
const MLABEL = { uptrend: '🟢 Uptrend', pressure: '🟡 Pressure', correction: '🔴 Correction' };
const KIND = { breakout: ['↑', 'up'], below_pivot: ['↓', 'down'], played_out: ['↓', 'down'], forming: ['~', 'warn'] };
const slot = { width: 118, textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', flex: 'none' };

// The stage board: the four stage cards, the market filter, what changed since the last close, and the names per stage.
export default function ScreensHome({ hasData }) {
  const [screen, setScreen] = useState('bases');
  const [summary, setSummary] = useState(null);
  const [feed, setFeed] = useState(null);
  const [stage, setStage] = useState('fresh');
  const [picked, setPicked] = useState(false);          // phone: the user tapped a stage (no more auto-pick)
  const [peekAll, setPeekAll] = useState(false);        // phone: full list / grid instead of the top 3
  const [selected, setSelected] = useState(null);
  const [fullFeed, setFullFeed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [mkt, setMkt] = useState(null);
  const phone = useIsPhone();
  const [plan, setPlan] = useState(null);
  const [news, setNews] = useState(null);
  const [tab, setTab] = useState('moves');

  const load = useCallback(async () => {
    try {
      const [s, f] = await Promise.all([api.screenSummary(screen), api.getChanges(screen)]);
      setSummary(s); setFeed(f); setErr('');
      api.marketHealth().then(setMkt).catch(() => setMkt(null));
      api.getPlan().then(setPlan).catch(() => setPlan(null));
      api.getNews().then(setNews).catch(() => setNews({ items: [], results: [], counts: {}, scope: 0, days: 3 }));
    } catch (e) { setErr(e.message); }
  }, [screen]);
  useEffect(() => { load(); }, [load]);

  const rescan = async () => {
    setBusy(true);
    try { await api.runScan(); await load(); } catch (e) { setErr(e.message); }
    setBusy(false);
  };

  const c = summary?.counts || {};
  const n = summary?.fresh_sessions || 5;
  // phone: open on the first stage that has names, until the user taps one
  useEffect(() => {
    if (!phone || picked || !summary?.counts) return;
    const first = ['fresh', 'forming', 'climbing', 'played_out'].find((id) => (summary.counts[id] || 0) > 0);
    if (first && (summary.counts[stage] || 0) === 0) setStage(first);
  }, [phone, picked, summary, stage]);
  const pickStage = (id) => { setStage(id); setSelected(null); setPicked(true); setPeekAll(false); };
  const pickScreen = (id) => { setScreen(id); setSelected(null); setPicked(false); setPeekAll(false); };
  // [id, title, sub, fact]
  const CARDS = [
    ['forming', 'Forming', 'tight range under its lid', 'waiting for the pivot'],
    ['fresh', 'Fresh breakouts', `cleared it in the last ${n} sessions`, c.fresh_low_volume ? `+${c.fresh_low_volume} on low volume` : 'on the volume rule'],
    ['climbing', 'Climbing', 'broke out earlier, holding', 'above pivot and 50-day'],
    ['played_out', 'Played out', 'below 50-day or 8% under pivot', 'back in the base, or done'],
  ];
  const SCREENS = [['vcp', 'VCP'], ['flag', 'Bull flag'], ['bases', 'Bases']];
  const rows = feed?.rows || [];
  const names = ['forming', 'fresh', 'climbing', 'played_out'].reduce((a, k) => a + (c[k] || 0), 0);
  const about = {
    vcp: 'Classic VCPs: shrinking pullbacks, drying volume, RS 70+, in an uptrend.',
    flag: 'Bull flags: a 20%+ pole in up to 6 weeks on volume, then a 1–5 week flag that gives back at most 38% on drying volume (HTF = +90% pole).',
  }[screen] || 'Every base the engine finds, at the stage it has reached.';
  const h = mkt?.health, br = h?.breadth || {}, bo = h?.breakouts || {};
  const mCls = h ? (STATUS[h.status] || [])[1] || '' : '', mLabel = h ? MLABEL[h.status] || h.status : '—';
  const feedN = phone ? 3 : 6, newsN = phone ? 4 : 8;

  const feedBox = (
    <Panel title="What changed since last close" meta={tab === 'moves' ? rows.length : tab === 'news' ? news?.items.length : news?.results.length}
      right={<Seg small opts={[['moves', `Moves ${rows.length}`], ['news', `News ${news?.items.length ?? '…'}`], ['results', `Results ${news?.results.length ?? '…'}`]]}
        v={tab} set={(id) => { setTab(id); setFullFeed(false); }} />}>
      {tab === 'news' && <>
        <div className="rnote">Exchange news, last {news?.days ?? 3} days · {news?.scope ?? 0} board stocks (VCP, Bull flag, Bases) + held / watched</div>
        <NewsList data={news} onSelect={setSelected} limit={fullFeed ? 0 : newsN} />
        {(news?.items.length || 0) > newsN && <div className="rnote"><button type="button" className="btn ghost small" onClick={() => setFullFeed(!fullFeed)}>
          {fullFeed ? 'Show less' : `Show the full feed (${news.items.length}) ›`}</button></div>}
      </>}
      {tab === 'results' && <>
        <div className="rnote">Board stocks reporting results soon: a results gap can jump the stop</div>
        <ResultsList data={news} onSelect={setSelected} />
      </>}
      {tab === 'moves' && <>
        <div className="rnote">{fmtDate(feed?.as_of)} · end of day · {rows.length} stage moves</div>
        {rows.length === 0 && <p className="muted small rempty">No stage moves today.</p>}
        {(fullFeed ? rows : rows.slice(0, feedN)).map((r, i) => {
          const [icon, cls] = KIND[r.kind] || ['·', ''];
          return <Act key={i} icon={icon} cls={cls} text={r.text} meta={`${r.kind.replace('_', ' ')} · ${screen}`} onClick={() => setSelected(r.symbol)}>
            <span className={`mono ${r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}`} style={slot}>
              ₹{r.close?.toLocaleString('en-IN')} {r.change_pct != null && `${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(1)}%`}</span>
          </Act>;
        })}
        {rows.length > feedN && <div className="rnote"><button type="button" className="btn ghost small" onClick={() => setFullFeed(!fullFeed)}>
          {fullFeed ? 'Show less' : `Show the full feed (${rows.length}) ›`}</button></div>}
      </>}
    </Panel>);
  const chart = selected && <StockChart symbol={selected} onClose={() => setSelected(null)} />;
  const listProps = { screen, stage, asOf: summary?.as_of, selected, onSelect: setSelected };
  const stageList = !summary?.as_of ? null : phone && !peekAll
    ? <StagePeek {...listProps} count={c[stage] ?? 0} onAll={() => setPeekAll(true)} />
    : <StageList {...listProps} onLess={phone ? () => setPeekAll(false) : undefined} />;

  return (
    <div className="rd">
      <Head title="Screens" right={<>
        <Seg opts={SCREENS} v={screen} set={pickScreen} />
        <Seg opts={CARDS.map(([id, t]) => [id, t.split(' ')[0]])} v={stage} set={pickStage} />
        <button type="button" className="btn ghost small" disabled={busy} onClick={rescan}>{busy ? 'Scanning…' : '↻ Rescan'}</button></>}>
        <Chip title={about}>scanned <b>{fmtDate(summary?.as_of)}</b> · {names} names · candles to {fmtDate(summary?.latest_bar)}</Chip>
      </Head>
      {summary?.stale && <div className="rbanner warn">Board is older than the data (latest bar {fmtDate(summary.latest_bar)}): rescan.</div>}
      {err && <div className="rbanner bad">{err}</div>}
      {summary && !summary.as_of && <div className="rbanner">No scan yet. {hasData ? <>Press <b>Rescan</b>.</> : <>Load data first: <b>Data → Refresh universe → Sync candles</b>.</>}</div>}
      {summary && <Cards n={6}>
        {CARDS.map(([id, title, sub, fact]) => (
          <Card unit="n" key={id} label={`${stage === id ? '▸ ' : ''}${title}`} value={c[id] ?? 0} num={c[id] ?? 0} k={stage === id ? 'up' : ''} sub={sub} fact={fact} onClick={() => pickStage(id)} />))}
        <Card label="Market filter" value={mLabel} k={mCls === 'ok' ? 'up' : mCls === 'bad' ? 'down' : mCls === 'w' ? 'warn' : ''}
          sub={h ? `breadth ${br.above_50_pct ?? '–'}% · dist ${h.dist_days ?? '–'} · working ${bo.working ?? 0}/${bo.total ?? 0}` : 'no market health yet'}
          fact={h ? <><Bar pct={br.above_50_pct} ok={br.above_50_pct >= (mkt.config?.breadth_up ?? 50)} bad={br.above_50_pct < (mkt.config?.breadth_correction ?? 35)} /> {br.above_50_pct ?? '–'}% above 50 d</> : 'Data → Sync candles'} />
        <Card label="Data" value={summary?.stale ? '⚠ stale' : summary?.as_of ? '✓ fresh' : '—'} k={summary?.stale ? 'warn' : summary?.as_of ? 'up' : ''}
          sub={`board ${dayOf(summary?.as_of)} · candles to ${dayOf(summary?.latest_bar)}`} fact={summary?.stale ? 'rescan to catch up' : 'updated EOD'} />
      </Cards>}
      {phone ? <>{stageList}{chart}{summary?.as_of && feedBox}<MarketCard data={mkt} /></>
        : <>{summary?.as_of && (mkt?.health ? <div className="two">{feedBox}<MarketCard data={mkt} /></div> : feedBox)}{stageList}{chart}</>}
      <PlanCard plan={plan} onSelect={setSelected} />
    </div>
  );
}
