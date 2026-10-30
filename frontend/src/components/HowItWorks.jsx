import React from 'react';
import { ampm } from '../timefmt';

// "How it works": the Trend Breakout rules in plain words, built from the live settings so it always matches what runs.
export default function HowItWorks({ r, paper = false }) {
  if (!r) return null;
  return (
    <details className="panel howitworks">
      <summary><b>ℹ️ How it works</b> <span className="muted small">· the rules in plain words</span></summary>
      <ol>
        <li><b>Every evening after the close</b> the daily candles are updated (only missing days), then the rules run on the day's close.
          {' '}Nothing is decided during the day.</li>
        <li><b>Market filter</b> (NIFTY 50's close): <b>A</b> buys only when NIFTY is above its 200-day average.
          {' '}<b>E</b> uses all {r.max_pos} slots above the 200-day, <b>{Math.floor(r.max_pos / 2)} slots</b> when NIFTY is above its 50-day but not its
          {' '}200-day, and makes no new buys below the 50-day. The filter only stops <i>new</i> buys; it never sells.</li>
        <li><b>Stocks</b>: each day the most traded Nifty 500 stocks (20-day average turnover, data up to that day), priced ₹{r.min_price} or more:
          {' '}<b>A</b> picks from the top {r.top}, <b>E</b> from the top {r.top_e}. A stock that later drops out is not sold for that.</li>
        <li><b>Buy signal</b>: an uptrend (close above the 50-, 150- and 200-day averages, in that order, the 200-day rising), within 25% of the
          {' '}52-week high and 30%+ above the 52-week low, a close above the previous {r.hi_window}-day high on at least {r.vol_mult}× the 50-day volume.</li>
        <li><b>Order</b>: the strongest 6-month return first, bought at the <b>next day's open</b>, each worth the account value ÷ {r.max_pos}
          {' '}(whole shares, never more than the cash left).</li>
        <li><b>Stop</b>: {r.stop_pct}% or {r.atr_mult} × ATR(14) under the signal close, whichever is closer, fixed and never moved; if a day's low
          {' '}touches it, out at the stop (or the open, if it opened below). <b>Exit</b>: a close under the {r.exit_sma}-day average sells at the next open.
          {' '}No profit target.</li>
        <li><b>Costs</b>: Zerodha delivery charges (STT, exchange, SEBI, GST, stamp, DP) and {r.slippage_pct}% slippage on every buy and sell
          {' '}({r.stress_slippage_pct}% in the stressed test). Before costs, costs and after costs are always shown apart.</li>
        {paper ? <li><b>Paper trading</b>: two virtual accounts (A and E) follow these rules from the start day, filled from the daily candles.
          {' '}<b>Simulated: no order is ever sent to Kite.</b> Stops are checked on the day's low after the close, as in the backtest.</li>
          : <li><b>Backtest</b>: {r.test_years} years after a {r.warmup_sessions}-session warm-up, re-run every evening; development / validation /
            {' '}out-of-sample are the first 50% / next 25% / last 25% of the sessions. Signals only: no orders.</li>}
      </ol>
      <p className="muted small">Times are IST; the evening job runs from {ampm('18:30')}.</p>
    </details>
  );
}
