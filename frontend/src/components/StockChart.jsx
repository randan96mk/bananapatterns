import React, { useEffect, useRef, useState } from 'react';
import {
  createChart, createSeriesMarkers, CandlestickSeries, HistogramSeries, LineSeries, LineStyle,
} from 'lightweight-charts';
import * as api from '../services/api';
import { BasesPrimitive } from '../chart/basesPrimitive';
import BaseCard from './BaseCard';
import TrendPanel from './TrendPanel';
import VcpPanel from './VcpPanel';
import { cssVar, useTheme } from '../theme';
import { useIsPhone } from '../useMedia';

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

const MAS = [[50, '--blue'], [150, '--purple'], [200, '--warn']];       // period, theme colour

// Chart colours from the page theme (lightweight-charts needs real colours, not CSS variables).
function palette() {
  const v = cssVar;
  const up = v('--ok'), muted = v('--muted');
  return {
    bg: v('--panel'), text: v('--text'), grid: v('--grid'), border: v('--border'), up, down: v('--bad'), warn: v('--warn'),
    mas: MAS.map(([, name]) => v(name)),
    bases: { active: { fill: `${up}1a`, stroke: `${up}e6`, text: up },
      past: { fill: `${muted}14`, stroke: `${muted}cc`, text: muted } },
  };
}

// Daily candles + volume + SMAs, with base boxes, pivot line and breakout markers.
const PREVIEW_KINDS = [['auto', 'Auto (current stage)'], ['breakout', 'Breakout'], ['near_pivot', 'Near pivot'],
  ['forming', 'Forming'], ['below_pivot', 'Below pivot'], ['played_out', 'Played out'], ['climbing', 'Climbing']];

export default function StockChart({ symbol, onClose }) {
  const box = useRef(null);
  const theme = useTheme();              // the chart is rebuilt in the new colours when it changes
  const phone = useIsPhone();            // phone: full-screen sheet, panels folded
  useEffect(() => {
    if (!phone || !symbol) return undefined;
    document.body.classList.add('modal-open');            // no page scroll behind the sheet
    return () => document.body.classList.remove('modal-open');
  }, [phone, symbol]);
  const [info, setInfo] = useState(null);
  const [bases, setBases] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [showBases, setShowBases] = useState(true);
  const [err, setErr] = useState('');
  const [preview, setPreview] = useState(null);       // {kind, stamp} while the alert-chart preview is open

  useEffect(() => {
    if (!symbol || !box.current) return undefined;
    const pal = palette();
    const chart = createChart(box.current, {
      autoSize: true,
      layout: { background: { color: pal.bg }, textColor: pal.text },
      grid: { vertLines: { color: pal.grid }, horzLines: { color: pal.grid } },
      timeScale: { borderColor: pal.border },
      rightPriceScale: { borderColor: pal.border },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: pal.up, downColor: pal.down, borderVisible: false,
      wickUpColor: pal.up, wickDownColor: pal.down,
    });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.25 } });
    const vol = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    const lines = MAS.map((_, i) => chart.addSeries(LineSeries, {
      color: pal.mas[i], lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    }));
    const boxes = new BasesPrimitive(pal.bases);
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
        time: c.date, value: c.volume, color: c.close >= c.open ? `${pal.up}55` : `${pal.down}55`,
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
          price: latest.pivot, color: pal.up, lineWidth: 1, lineStyle: LineStyle.Dashed,
          axisLabelVisible: true, title: `pivot ₹${latest.pivot.toFixed(2)}`,
        });
      }
    }).catch((e) => alive && setErr(e.message));

    // handles for the overlay effect below
    box.current.__bp = { boxes, markers, candles, pal, get pivotLine() { return pivotLine; } };
    return () => { alive = false; chart.remove(); };
  }, [symbol, theme]);

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
      color: x.breakout.volume_ok ? h.pal.up : h.pal.warn,
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
        <span className="legend">{MAS.map(([n, c]) => <span key={n} style={{ color: `var(${c})` }}>SMA{n} </span>)}
          <span className="up">▲ breakout</span> <span className="warn">▲ low-volume breakout</span></span>
        <label className="muted"><input type="checkbox" checked={showBases}
          onChange={(e) => setShowBases(e.target.checked)} /> bases</label>
        <button className="btn ghost right" onClick={() => setPreview(preview ? null : { kind: 'auto', stamp: Date.now() })}>
          {preview ? 'Hide alert chart' : 'Preview alert chart'}</button>
        <button className="btn ghost" onClick={onClose}>Close</button>
      </div>
      {err && <p className="warn">{err}</p>}
      {preview && (
        <div className="alertpreview">
          <div className="row">
            <span className="muted small">The image a Telegram alert would carry (screen rules, not advice)</span>
            <select value={preview.kind} onChange={(e) => setPreview({ kind: e.target.value, stamp: Date.now() })}>
              {PREVIEW_KINDS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
            </select>
          </div>
          <img alt={`${symbol} alert chart`} src={api.alertChartUrl(symbol, preview.kind, preview.stamp)}
               onError={() => setErr('No alert chart for this stock/kind (no base to annotate, or matplotlib missing)')} />
        </div>)}
      <div ref={box} className="chart" />
      <details className="fold" open={!phone}><summary>Trend Template &amp; RS</summary><TrendPanel m={metrics} /></details>
      <details className="fold" open={!phone}><summary>VCP check</summary><VcpPanel symbol={symbol} /></details>
      {bases && <details className="fold" open={!phone}><summary>Base</summary>
        <BaseCard latest={bases.latest} baseline={bases.config.vol_baseline} /></details>}
    </div>
  );
}
