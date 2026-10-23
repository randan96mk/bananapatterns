import React, { useEffect, useRef, useState } from 'react';
import { createChart, CandlestickSeries, HistogramSeries, LineSeries } from 'lightweight-charts';
import * as api from '../services/api';

function sma(candles, n) {
  const out = [];
  let sum = 0;
  candles.forEach((c, i) => {
    sum += c.close;
    if (i >= n) sum -= candles[i - n].close;
    if (i >= n - 1) out.push({ time: c.date, value: sum / n });
  });
  return out;
}

const MAS = [[50, '#4ea1ff'], [150, '#c58af9'], [200, '#f5a524']];

// Daily candles + volume + 50/150/200 SMA. Phase 3 adds contractions, pivot and breakout markers.
export default function StockChart({ symbol, onClose }) {
  const box = useRef(null);
  const [info, setInfo] = useState(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!symbol || !box.current) return undefined;
    const chart = createChart(box.current, {
      autoSize: true,
      layout: { background: { color: '#12161c' }, textColor: '#c9d1d9' },
      grid: { vertLines: { color: '#1e242c' }, horzLines: { color: '#1e242c' } },
      timeScale: { borderColor: '#2a313b' },
      rightPriceScale: { borderColor: '#2a313b' },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    });
    const vol = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.25 } });
    const lines = MAS.map(([, color]) => chart.addSeries(LineSeries, {
      color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    }));

    let alive = true;
    setErr('');
    api.getCandles(symbol, 750).then((d) => {
      if (!alive) return;
      setInfo(d);
      const cs = d.candles;
      candles.setData(cs.map((c) => ({ time: c.date, open: c.open, high: c.high, low: c.low, close: c.close })));
      vol.setData(cs.map((c) => ({
        time: c.date, value: c.volume, color: c.close >= c.open ? '#26a69a55' : '#ef535055',
      })));
      MAS.forEach(([n], i) => lines[i].setData(sma(cs, n)));
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, cs.length - 260), to: cs.length + 5 });
    }).catch((e) => alive && setErr(e.message));

    return () => { alive = false; chart.remove(); };
  }, [symbol]);

  if (!symbol) return null;
  const last = info?.candles?.at(-1);
  return (
    <div className="panel chartpanel">
      <div className="row">
        <b>{symbol}</b>
        <span className="muted">{info?.company} {info?.industry && `· ${info.industry}`}
          {last && ` · ${last.date} close ${last.close.toFixed(2)} · ${info.candles.length} bars shown`}</span>
        <span className="legend">{MAS.map(([n, c]) => <span key={n} style={{ color: c }}>SMA{n} </span>)}</span>
        <button className="btn ghost right" onClick={onClose}>Close</button>
      </div>
      {err && <p className="warn">{err}</p>}
      <div ref={box} className="chart" />
    </div>
  );
}
