import React, { useEffect, useRef, useState } from 'react';
import {
  createChart, createSeriesMarkers, CandlestickSeries, HistogramSeries, LineSeries, LineStyle,
} from 'lightweight-charts';
import * as api from '../services/api';
import { BasesPrimitive } from '../chart/basesPrimitive';
import BaseCard from './BaseCard';
import TrendPanel from './TrendPanel';

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

// Daily candles + volume + SMAs, with base boxes, pivot line and breakout markers.
export default function StockChart({ symbol, onClose }) {
  const box = useRef(null);
  const [info, setInfo] = useState(null);
  const [bases, setBases] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [showBases, setShowBases] = useState(true);
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
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.25 } });
    const vol = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    const lines = MAS.map(([, color]) => chart.addSeries(LineSeries, {
      color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    }));
    const boxes = new BasesPrimitive();
    candles.attachPrimitive(boxes);
    const markers = createSeriesMarkers(candles, []);
    let pivotLine = null;

    let alive = true;
    setErr('');
    setBases(null);
    setMetrics(null);
    api.getMetrics(symbol).then((m) => alive && setMetrics(m)).catch(() => {});
    api.getCandles(symbol, 750).then(async (d) => {
      if (!alive) return;
      setInfo(d);
      const cs = d.candles;
      candles.setData(cs.map((c) => ({ time: c.date, open: c.open, high: c.high, low: c.low, close: c.close })));
      vol.setData(cs.map((c) => ({
        time: c.date, value: c.volume, color: c.close >= c.open ? '#26a69a55' : '#ef535055',
      })));
      MAS.forEach(([n], i) => lines[i].setData(sma(cs, n)));
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, cs.length - 260), to: cs.length + 5 });
      if (!cs.length) return;

      const b = await api.getBases(symbol, cs[0].date);
      if (!alive) return;
      setBases(b);
      const latest = b.latest;
      if (latest) {
        pivotLine = candles.createPriceLine({
          price: latest.pivot, color: '#26a69a', lineWidth: 1, lineStyle: LineStyle.Dashed,
          axisLabelVisible: true, title: `pivot ₹${latest.pivot.toFixed(2)}`,
        });
      }
    }).catch((e) => alive && setErr(e.message));

    // handles for the overlay effect below
    box.current.__bp = { boxes, markers, candles, get pivotLine() { return pivotLine; } };
    return () => { alive = false; chart.remove(); };
  }, [symbol]);

  // draw (or hide) boxes, breakout markers and the pivot line; no refetch on toggle
  useEffect(() => {
    const h = box.current && box.current.__bp;
    if (!h || !bases) return;
    const first = info?.candles?.[0]?.date || '';
    const list = bases.bases;
    h.boxes.setBoxes(showBases ? list.map((x, i) => ({
      start: x.start_date < first ? first : x.start_date, end: x.end_date,
      top: x.pivot, bottom: x.low, label: x.label, active: i === list.length - 1,
    })) : []);
    h.markers.setMarkers(showBases ? list.filter((x) => x.breakout && x.breakout.date >= first).map((x) => ({
      time: x.breakout.date, position: 'aboveBar', shape: 'arrowUp',
      color: x.breakout.volume_ok ? '#26a69a' : '#f5a524',
    })) : []);
    if (h.pivotLine) h.pivotLine.applyOptions({ lineVisible: showBases, axisLabelVisible: showBases, title: showBases ? `pivot ₹${bases.latest.pivot.toFixed(2)}` : '' });
  }, [showBases, bases, info]);

  if (!symbol) return null;
  const last = info?.candles?.at(-1);
  return (
    <div className="panel chartpanel">
      <div className="row">
        <b>{symbol}</b>
        <span className="muted">{info?.company} {info?.industry && `· ${info.industry}`}
          {info?.series && info.series !== 'EQ' && ` · ${info.series === 'BE' ? 'trade-for-trade' : info.series}`}
          {last && ` · ${last.date} close ${last.close.toFixed(2)}`}</span>
        <span className="legend">{MAS.map(([n, c]) => <span key={n} style={{ color: c }}>SMA{n} </span>)}
          <span style={{ color: '#26a69a' }}>▲ breakout</span> <span style={{ color: '#f5a524' }}>▲ low-volume breakout</span></span>
        <label className="muted"><input type="checkbox" checked={showBases}
          onChange={(e) => setShowBases(e.target.checked)} /> bases</label>
        <button className="btn ghost right" onClick={onClose}>Close</button>
      </div>
      {err && <p className="warn">{err}</p>}
      <div ref={box} className="chart" />
      <TrendPanel m={metrics} />
      {bases && <BaseCard latest={bases.latest} baseline={bases.config.vol_baseline} />}
    </div>
  );
}
