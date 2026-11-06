import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import StageGrid, { tagLine } from './StageGrid';
import { Panel, FixedTable, Seg, Tag, Act } from './ui2';

const pct = (v) => (v == null ? '' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
const rs = (v) => (v == null ? '' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);
const pcls = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '');

// Columns per stage: [header, render(row), width, number?]
const COLS = {
  forming: [['Base', (r) => r.base_label, 150], ['Pivot', (r) => rs(r.pivot), 90, 1], ['Under pivot', (r) => `${r.dist_to_pivot_pct?.toFixed(1)}%`, 90, 1],
    ['Depth', (r) => `${r.depth_pct?.toFixed(1)}%`, 70, 1]],
  fresh: [['Broke out of', (r) => r.base_label, 150], ['Breakout', (r) => r.breakout_date, 100], ['Pivot', (r) => rs(r.pivot), 90, 1],
    ['From pivot', (r) => pct(r.gain_from_pivot_pct), 90, 1], ['Volume', (r) => `${r.volume_ratio?.toFixed(2)}x`, 70, 1]],
  climbing: [['Broke out of', (r) => r.base_label, 150], ['Breakout', (r) => r.breakout_date, 100], ['Sessions', (r) => r.sessions_since_breakout, 70, 1],
    ['From pivot', (r) => pct(r.gain_from_pivot_pct), 90, 1], ['Peak', (r) => pct(r.peak_gain_pct), 70, 1]],
  played_out: [['Run from', (r) => r.base_label, 150], ['Breakout', (r) => r.breakout_date, 100], ['Ended', (r) => r.ended_date, 100],
    ['Why', (r) => r.ended_reason, 160], ['At end', (r) => pct(r.gain_from_pivot_pct), 80, 1], ['Peak', (r) => pct(r.peak_gain_pct), 70, 1]],
};
COLS.fresh_low_volume = COLS.fresh;
const pullbacks = (r) => (r.vcp?.contractions || []).map((c) => `${Math.round(c.depth_pct)}`).join(' → ');
const x2 = (v) => (v == null ? '' : `${v.toFixed(2)}x`);
const VCP_COLS = [['Score', (r) => r.score?.toFixed(0), 60, 1], ['Pullbacks %', pullbacks, 110], ['Tightening', (r) => x2(r.vcp?.tightening), 80, 1],
  ['Dry-up', (r) => x2(r.vcp?.dryup), 70, 1], ['U/D', (r) => x2(r.vcp?.ud_ratio), 60, 1], ['Stop', (r) => rs(r.vcp?.stop), 90, 1]];

const pctv = (v) => (v == null ? '' : `${v.toFixed(1)}%`);
const FLAG_COLS = [['Score', (r) => r.score?.toFixed(0), 60, 1],
  ['Pole', (r) => r.flag && `+${r.flag.pole.gain_pct.toFixed(0)}% · ${r.flag.pole.bars}d`, 90],
  ['Flag', (r) => r.flag && `${pctv(r.flag.flag.depth_pct)} · ${r.flag.flag.bars}d`, 90],
  ['Flag vol', (r) => x2(r.flag?.flag.volume_x), 70, 1], ['Tags', (r) => r.flag && [r.flag.htf && 'HTF',
    r.flag.tight && 'tight', r.flag.post_results && 'post-results'].filter(Boolean).join(' · '), 120],
  ['Stop', (r) => rs(r.flag?.stop), 90, 1]];

const TITLES = { forming: 'Forming', fresh: 'Fresh breakouts', climbing: 'Climbing', played_out: 'Played out',
  fresh_low_volume: 'Low-volume breakouts (below the volume rule; not counted)' };

// Fetch + sort once; show as a grid of stage cards or as a fixed table.
function Results({ view, onCount, ...props }) {
  const { screen, stage, asOf, sort } = props;
  const [rows, setRows] = useState(null);
  useEffect(() => { setRows(null); api.screenResults(screen, stage).then((d) => setRows(d.rows)).catch(() => setRows([])); },
    [screen, stage, asOf]);
  useEffect(() => { if (onCount && rows) onCount(rows.length); }, [rows, onCount]);
  if (!rows) return <p className="muted small rempty">Loading…</p>;
  if (!rows.length) return <p className="muted small rempty">None as of {asOf}.</p>;
  const shown = sort === 'rs' ? [...rows].sort((a, b) => (b.rs ?? -1) - (a.rs ?? -1)) : rows;
  return view === 'grid' ? <div className="rbody" style={{ padding: 10 }}><StageGrid {...props} rows={shown} /></div> : <Table {...props} shown={shown} />;
}

function Table({ screen, stage, selected, onSelect, shown }) {
  const extra = { vcp: VCP_COLS, flag: FLAG_COLS }[screen] || [];
  const stageCols = [...COLS[stage], ...extra];
  const cols = [{ h: 'Symbol' }, { h: 'Company', w: 160, lo: true }, { h: 'Industry', w: 130, lo: true }, { h: 'Tags', w: 150, lo: true }, { h: 'Close', w: 90, n: true }, { h: 'Chg %', w: 70, n: true },
    { h: 'RS', w: 50, n: true }, { h: 'Trend', w: 60, n: true, lo: true },
    ...stageCols.map(([h, , w, n], i) => ({ h, w, n: !!n, lo: i >= 2 }))];
  const rows = shown.map((r) => ({
    key: r.symbol, cls: r.symbol === selected ? 'sel' : '', onClick: () => onSelect(r.symbol), title: `${r.company || ''} · ${tagLine(r)}`,
    cells: [<>{r.tier && <span className={`tier t-${r.tier}`}>{r.tier}</span>} <b>{r.symbol}</b>
      {screen === 'bases' && r.vcp_tag && <Tag c="ok">VCP</Tag>}{screen === 'bases' && r.flag_tag && <Tag c="ok">Flag</Tag>}</>,
    r.company, r.industry, tagLine(r), rs(r.close), <span className={pcls(r.change_pct)}>{pct(r.change_pct)}</span>, r.rs ?? '',
    <span className={r.tt_all ? 'up' : ''}>{r.tt_of ? `${r.tt_passed}/${r.tt_of}` : ''}</span>,
    ...stageCols.map(([, f]) => f(r))],
  }));
  return <FixedTable cols={cols} rows={rows} />;
}

// Phone: the selected stage's first names as one-line rows under the stage cards; "Show all" opens StageList.
const stopOf = (r) => r.vcp?.stop ?? r.flag?.stop;
const PEEK = {   // [main figure, small line] per stage
  forming: (r) => [`${rs(r.pivot)} pivot`, [r.dist_to_pivot_pct != null && `${r.dist_to_pivot_pct.toFixed(1)}% away`,
    stopOf(r) != null && `stop ${rs(stopOf(r))}`].filter(Boolean).join(' · ')],
  fresh: (r) => [`${pct(r.gain_from_pivot_pct)} from pivot`, [`broke out ${r.breakout_date}`,
    r.volume_ratio != null && `vol ${r.volume_ratio.toFixed(1)}x`].filter(Boolean).join(' · ')],
  climbing: (r) => [`${pct(r.gain_from_pivot_pct)} from pivot`,
    `${r.sessions_since_breakout ?? '?'} sessions · peak ${pct(r.peak_gain_pct)}`],
  played_out: (r) => [`ended ${r.ended_date || ''}`, r.ended_reason || ''],
};
const PEEK_N = 3;

export function StagePeek({ screen, stage, asOf, onSelect, count, onAll }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { setRows(null); api.screenResults(screen, stage).then((d) => setRows(d.rows)).catch(() => setRows([])); },
    [screen, stage, asOf]);
  const shaped = ['vcp', 'flag'].includes(screen);
  const slot = { width: 120, textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', flex: 'none' };
  const list = (rows || []).slice(0, PEEK_N).map((r) => {      // one Act row a name: tier · name + detail · tags | the main figure
    const [main, sub] = (PEEK[stage] || PEEK.forming)(r);
    const tags = tagLine(r);
    return <Act key={r.symbol} icon={r.tier || '·'} cls={r.tier === 'A' ? 'up' : ''} onClick={() => onSelect(r.symbol)}
      text={<><b>{r.symbol}</b> <span className="muted small">{sub}</span></>} meta={tags || undefined}>
      <span className="mono small" style={slot} title={main}>{main}</span></Act>;
  });
  return (
    <Panel title={TITLES[stage]} meta={count} right={shaped ? 'best setups first' : 'stage order'}>
      {!rows ? <p className="muted small rempty">Loading…</p> : !rows.length ? <p className="muted small rempty">None as of {asOf}.</p>
        : list}
      {(rows?.length || 0) > 0 && <div className="rnote"><button type="button" className="btn ghost small" onClick={onAll}>
        {rows.length > PEEK_N ? `Show all ${rows.length}` : 'Open'} · grid with mini charts ›</button></div>}
    </Panel>
  );
}

const VIEW_KEY = 'bp.stageView';
const loadView = () => { try { return localStorage.getItem(VIEW_KEY) || 'grid'; } catch { return 'grid'; } };

export default function StageList(props) {
  const [sort, setSort] = useState('default');
  const [view, setView] = useState(loadView);
  const [count, setCount] = useState(null);
  const pick = (v) => { setView(v); try { localStorage.setItem(VIEW_KEY, v); } catch { /* private mode */ } };
  const right = <>
    <Seg small opts={[['grid', '▦ Grid'], ['table', '☰ Table']]} v={view} set={pick} />
    <Seg small opts={[['default', props.screen === 'vcp' ? 'Score' : 'Stage order'], ['rs', 'RS rating']]} v={sort} set={setSort} />
    {props.onLess && <button type="button" className="btn ghost small" onClick={props.onLess}>Show less</button>}
  </>;
  return <>
    <Panel title={TITLES[props.stage]} meta={count} right={right}>
      <Results {...props} sort={sort} view={view} onCount={setCount} />
    </Panel>
    {props.stage === 'fresh' && (
      <Panel title={TITLES.fresh_low_volume}>
        <Results {...props} sort={sort} view={view} stage="fresh_low_volume" />
      </Panel>)}
  </>;
}
