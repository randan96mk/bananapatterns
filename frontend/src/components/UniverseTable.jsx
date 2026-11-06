import React, { useMemo, useState } from 'react';
import { Head, Chip, Cards, Card, Bar, Panel, FixedTable, Seg, Tag } from './ui2';

// [key, header, width, number?, low priority on a phone?]
const COLS = [
  ['symbol', 'Symbol'], ['company', 'Company', 200, 0, 1], ['industry', 'Industry', 160, 0, 1], ['close', 'Close', 90, 1],
  ['change_pct', 'Chg %', 72, 1], ['rs', 'RS', 56, 1], ['tt_passed', 'Trend', 66, 1], ['bars', 'Bars', 64, 1], ['first_date', 'From', 100, 0, 1], ['last_date', 'To', 100, 0, 1],
];
const QUICK = [['all', 'All'], ['rs80', 'RS > 80'], ['err', 'With errors']];

// Sortable / filterable list of the universe with data coverage per symbol.
export default function UniverseTable({ data, selected, onSelect }) {
  const [q, setQ] = useState('');
  const [industry, setIndustry] = useState('');
  const [quick, setQuick] = useState('all');
  const [sort, setSort] = useState(['symbol', 1]);

  const rows = data?.rows || [];
  const industries = useMemo(
    () => [...new Set(rows.map((r) => r.industry).filter(Boolean))].sort(), [rows]);

  const view = useMemo(() => {
    const needle = q.trim().toUpperCase();
    const [key, dir] = sort;
    return rows
      .filter((r) => (!needle || r.symbol.includes(needle) || (r.company || '').toUpperCase().includes(needle))
        && (!industry || r.industry === industry)
        && (quick === 'all' || (quick === 'rs80' ? (r.rs ?? 0) > 80 : !!r.last_error)))
      .sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === 'index' ? -1 : 1;
        const x = a[key], y = b[key];
        if (x == null) return 1;
        if (y == null) return -1;
        return (x > y ? 1 : x < y ? -1 : 0) * dir;
      });
  }, [rows, q, industry, quick, sort]);

  if (!data) return <div className="rd"><div className="rbanner">Loading…</div></div>;
  if (!rows.length) {
    return <div className="rd"><Head title="Universe · Nifty 500" /><div className="rbanner">No universe yet. Log in, then use <b>Data → Refresh universe</b> and <b>Sync candles</b>.</div></div>;
  }
  const withData = rows.filter((r) => r.bars > 0).length;
  const lastDate = rows.reduce((m, r) => (r.last_date && r.last_date > m ? r.last_date : m), '');
  const failed = rows.filter((r) => r.last_error).length;
  const short = rows.filter((r) => r.bars > 0 && r.bars < 260).length;
  const indices = rows.filter((r) => r.kind === 'index').length;
  const head = (k, label) => <span style={{ cursor: 'pointer' }} onClick={() => setSort([k, sort[0] === k ? -sort[1] : 1])}>{label}{sort[0] === k ? (sort[1] > 0 ? ' ▲' : ' ▼') : ''}</span>;
  const cols = COLS.map(([k, label, w, n, lo]) => ({ key: k, h: head(k, label), w, n: !!n, lo: !!lo }));
  const list = view.map((r) => ({
    key: r.instrument_token, cls: r.symbol === selected ? 'sel' : '', onClick: () => onSelect(r.symbol), title: r.last_error || r.company || '',
    cells: [<><b>{r.symbol}</b>{r.kind === 'index' && <Tag>index</Tag>}
      {r.series && r.series !== 'EQ' && <Tag title={`Kite: ${r.tradingsymbol}`}>{r.series}</Tag>}
      {r.last_error && <Tag c="bad" title={r.last_error}>err</Tag>}</>,
    r.company, r.industry, r.close?.toFixed(2) ?? '',
    <span className={r.change_pct > 0 ? 'up' : r.change_pct < 0 ? 'down' : ''}>{r.change_pct != null ? r.change_pct.toFixed(2) : ''}</span>,
    r.rs ?? '', <span className={r.tt_passed === r.tt_of && r.tt_of ? 'up' : ''}>{r.tt_of ? `${r.tt_passed}/${r.tt_of}` : ''}</span>,
    <span className={r.bars && r.bars < 260 ? 'warn' : ''}>{r.bars}</span>, r.first_date, r.last_date],
  }));

  return (
    <div className="rd">
      <Head title="Universe · Nifty 500" right={<>
        <input placeholder="Search symbol / company" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 180 }} />
        <select value={industry} onChange={(e) => setIndustry(e.target.value)}>
          <option value="">All industries</option>
          {industries.map((i) => <option key={i}>{i}</option>)}
        </select>
        <Seg opts={QUICK} v={quick} set={setQuick} /></>}>
        <Chip><b className="mono">{withData}</b>/<span className="mono">{rows.length}</span> with data · snapshot <b>{data.snapshot_date || '—'}</b> · latest bar <b>{lastDate || '—'}</b></Chip>
        {failed > 0 && <Chip c="warn">{failed} with errors</Chip>}
        {data.unmatched.length > 0 && <Chip c="warn" title={data.unmatched.join(', ')}>unmatched: {data.unmatched.join(', ')}</Chip>}
        {data.excluded?.length > 0 && <Chip title={data.excluded.join(', ')}>excluded (series): {data.excluded.join(', ')}</Chip>}
      </Head>
      <Cards n={4}>
        <Card unit="n" label="Stocks" value={rows.length - indices} num={rows.length} sub={`${indices} ind${indices === 1 ? 'ex' : 'ices'} · snapshot ${data.snapshot_date || '—'}`} fact={<><b>{view.length}</b> shown with these filters</>} />
        <Card unit="n" label="With data" value={`${Math.round(100 * withData / rows.length)}%`} num={withData} k={withData === rows.length ? 'up' : 'warn'} sub={`${withData} of ${rows.length} synced`}
          fact={<><Bar pct={100 * withData / rows.length} ok={withData === rows.length} /> {rows.length - withData} waiting</>} />
        <Card label="Latest bar" value={lastDate || '—'} sub={short ? `${short} with under a year of bars` : 'a year of bars everywhere'} fact="Data → Sync candles" />
        <Card unit="n" label="Errors" value={failed} num={failed} k={failed ? 'down' : 'up'} sub={`${data.unmatched.length} unmatched · ${data.excluded?.length || 0} excluded`} fact={failed ? 'hover a row for the error' : 'last sync clean'} />
      </Cards>
      <Panel title="Stocks" meta={view.length} right={<span>click a header to sort · click a row for the chart</span>}>
        <FixedTable cols={cols} rows={list} empty="Nothing matches these filters." />
      </Panel>
    </div>
  );
}
