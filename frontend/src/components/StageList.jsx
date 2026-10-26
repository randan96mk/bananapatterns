import React, { useEffect, useState } from 'react';
import * as api from '../services/api';
import ResultsTag from './ResultsTag';
import StageGrid from './StageGrid';
import NewsTag from './NewsTag';
import FlagTags from './FlagTags';

const pct = (v) => (v == null ? '' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
const rs = (v) => (v == null ? '' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`);

// Columns per stage: [header, render(row)]
const COLS = {
  forming: [['Base', (r) => r.base_label], ['Pivot', (r) => rs(r.pivot)], ['Under pivot', (r) => `${r.dist_to_pivot_pct?.toFixed(1)}%`],
    ['Depth', (r) => `${r.depth_pct?.toFixed(1)}%`]],
  fresh: [['Broke out of', (r) => r.base_label], ['Breakout', (r) => r.breakout_date], ['Pivot', (r) => rs(r.pivot)],
    ['From pivot', (r) => pct(r.gain_from_pivot_pct)], ['Volume', (r) => `${r.volume_ratio?.toFixed(2)}x`]],
  climbing: [['Broke out of', (r) => r.base_label], ['Breakout', (r) => r.breakout_date], ['Sessions', (r) => r.sessions_since_breakout],
    ['From pivot', (r) => pct(r.gain_from_pivot_pct)], ['Peak', (r) => pct(r.peak_gain_pct)]],
  played_out: [['Run from', (r) => r.base_label], ['Breakout', (r) => r.breakout_date], ['Ended', (r) => r.ended_date],
    ['Why', (r) => r.ended_reason], ['At end', (r) => pct(r.gain_from_pivot_pct)], ['Peak', (r) => pct(r.peak_gain_pct)]],
};
COLS.fresh_low_volume = COLS.fresh;
const pullbacks = (r) => (r.vcp?.contractions || []).map((c) => `${Math.round(c.depth_pct)}`).join(' → ');
const x2 = (v) => (v == null ? '' : `${v.toFixed(2)}x`);
const VCP_COLS = [['Score', (r) => r.score?.toFixed(0)], ['Pullbacks %', pullbacks], ['Tightening', (r) => x2(r.vcp?.tightening)],
  ['Dry-up', (r) => x2(r.vcp?.dryup)], ['U/D', (r) => x2(r.vcp?.ud_ratio)], ['Stop', (r) => rs(r.vcp?.stop)]];

const pctv = (v) => (v == null ? '' : `${v.toFixed(1)}%`);
const CUP_COLS = [['Score', (r) => r.score?.toFixed(0)], ['Cup', (r) => r.cup && `${pctv(r.cup.cup.depth_pct)} · ${r.cup.cup.weeks}w`],
  ['Handle', (r) => r.cup && `${pctv(r.cup.handle.depth_pct)} · ${r.cup.handle.bars}d`],
  ['Handle vol', (r) => x2(r.cup?.handle.volume_x)], ['Prior rise', (r) => r.cup && pctv(r.cup.prior_advance_pct)],
  ['Stop', (r) => rs(r.cup?.stop)]];
const FLAG_COLS = [['Score', (r) => r.score?.toFixed(0)],
  ['Pole', (r) => r.flag && `+${r.flag.pole.gain_pct.toFixed(0)}% · ${r.flag.pole.bars}d`],
  ['Flag', (r) => r.flag && `${pctv(r.flag.flag.depth_pct)} · ${r.flag.flag.bars}d`],
  ['Flag vol', (r) => x2(r.flag?.flag.volume_x)], ['Tags', (r) => r.flag && [r.flag.htf && 'HTF',
    r.flag.tight && 'tight', r.flag.post_results && 'post-results'].filter(Boolean).join(' · ')],
  ['Stop', (r) => rs(r.flag?.stop)]];

const TITLES = { forming: 'Forming', fresh: 'Fresh breakouts', climbing: 'Climbing', played_out: 'Played out',
  fresh_low_volume: 'Low-volume breakouts (below the volume rule; not counted)' };

// Fetch + sort once; show as a table or as a grid of mini-chart cards.
function Results({ view, ...props }) {
  const { screen, stage, asOf, sort } = props;
  const [rows, setRows] = useState(null);
  useEffect(() => { setRows(null); api.screenResults(screen, stage).then((d) => setRows(d.rows)).catch(() => setRows([])); },
    [screen, stage, asOf]);
  if (!rows) return <p className="muted">Loading…</p>;
  if (!rows.length) return <p className="muted">None as of {asOf}.</p>;
  const shown = sort === 'rs' ? [...rows].sort((a, b) => (b.rs ?? -1) - (a.rs ?? -1)) : rows;
  return view === 'grid' ? <StageGrid {...props} rows={shown} /> : <Table {...props} shown={shown} />;
}

function Table({ screen, stage, selected, onSelect, shown }) {
  const isVcp = screen === 'vcp';
  const extra = { vcp: VCP_COLS, cup: CUP_COLS, flag: FLAG_COLS }[screen] || [];
  const cols = [...COLS[stage], ...extra];
  return (
    <div className="tablewrap">
      <table className="grid">
        <thead><tr><th>Symbol</th><th>Industry</th><th className="num">Close</th><th className="num">Chg %</th>
          <th className="num">RS</th><th className="num">Trend</th>
          {cols.map(([h]) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{shown.map((r) => (
          <tr key={r.symbol} className={r.symbol === selected ? 'sel' : ''} onClick={() => onSelect(r.symbol)}>
            <td>{r.tier && <span className={`tier t-${r.tier}`}>{r.tier}</span>}<b>{r.symbol}</b>
              {screen === 'bases' && r.vcp_tag && <span className="tag ok">VCP</span>}
              {screen === 'bases' && r.cup_tag && <span className="tag ok">C&amp;H</span>}
              {screen === 'bases' && r.flag_tag && <span className="tag ok">Flag</span>}
              <ResultsTag r={r.results} />
              <NewsTag n={r.news} />
              <FlagTags f={r.flags} />
              <div className="muted small">{r.company}</div></td><td className="muted">{r.industry}</td>
            <td className="num">{rs(r.close)}</td>
            <td className={`num ${r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}`}>{pct(r.change_pct)}</td>
            <td className="num">{r.rs ?? ''}</td>
            <td className={`num ${r.tt_all ? 'up' : ''}`}>{r.tt_of ? `${r.tt_passed}/${r.tt_of}` : ''}</td>
            {cols.map(([h, f]) => <td key={h}>{f(r)}</td>)}
          </tr>))}
        </tbody>
      </table>
    </div>
  );
}

// Phone: the selected stage's first names as one-line rows under the stage cards; "Show all" opens StageList.
const stopOf = (r) => r.vcp?.stop ?? r.cup?.stop ?? r.flag?.stop;
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
  const shaped = ['vcp', 'cup', 'flag'].includes(screen);
  return (
    <div className="panel peek">
      <div className="row"><b>{TITLES[stage]} · {count}</b>
        <span className="right muted small">{shaped ? 'best setups first' : 'stage order'}</span></div>
      {!rows ? <p className="muted">Loading…</p> : !rows.length ? <p className="muted">None as of {asOf}.</p> : (
        rows.slice(0, PEEK_N).map((r) => {
          const [main, sub] = (PEEK[stage] || PEEK.forming)(r);
          return (
            <button key={r.symbol} className="peekrow" onClick={() => onSelect(r.symbol)}>
              <span className="pk-left"><b>{r.symbol}</b>
                {r.tier && <span className={`tier t-${r.tier}`}>{r.tier}</span>}
                <ResultsTag r={r.results} /><NewsTag n={r.news} /><FlagTags f={r.flags} /></span>
              <span className="pk-right">{main}<span className="muted small">{sub}</span></span>
            </button>);
        }))}
      {(rows?.length || 0) > 0 && <button className="linkbtn" onClick={onAll}>
        {rows.length > PEEK_N ? `Show all ${rows.length}` : 'Open'} · grid with mini charts ›</button>}
    </div>
  );
}

const VIEW_KEY = 'bp.stageView';
const loadView = () => { try { return localStorage.getItem(VIEW_KEY) || 'grid'; } catch { return 'grid'; } };

export default function StageList(props) {
  const [sort, setSort] = useState('default');
  const [view, setView] = useState(loadView);
  const pick = (v) => { setView(v); try { localStorage.setItem(VIEW_KEY, v); } catch { /* private mode */ } };
  return (
    <div className="panel">
      <div className="row">
        <h3>{TITLES[props.stage]}</h3>
        <span className="seg">
          <button className={view === 'grid' ? 'active' : ''} onClick={() => pick('grid')}>▦ Grid</button>
          <button className={view === 'table' ? 'active' : ''} onClick={() => pick('table')}>☰ Table</button>
        </span>
        <span className="right muted">Sort</span>
        <select value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="default">{props.screen === 'vcp' ? 'Score' : 'Stage order'}</option>
          <option value="rs">RS rating</option>
        </select>
        {props.onLess && <button className="linkbtn" onClick={props.onLess}>Show less</button>}
      </div>
      <Results {...props} sort={sort} view={view} />
      {props.stage === 'fresh' && (
        <>
          <h4>{TITLES.fresh_low_volume}</h4>
          <Results {...props} sort={sort} view={view} stage="fresh_low_volume" />
        </>
      )}
    </div>
  );
}
