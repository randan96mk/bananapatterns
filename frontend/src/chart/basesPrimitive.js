// lightweight-charts v5 series primitive that draws base boxes ("B6 · 12w") under the candles.
// Each box spans start..end date horizontally and low..pivot vertically. The active (latest)
// base is green-shaded; earlier bases are grey. Dates outside the loaded data are clamped by the caller.

// Colours come from the page theme (see makeStyle in StockChart): {active, past} x {fill, stroke, text}.
const DEFAULT_STYLE = {
  active: { fill: 'rgba(38,166,154,0.10)', stroke: 'rgba(38,166,154,0.9)', text: '#26a69a' },
  past: { fill: 'rgba(201,209,217,0.05)', stroke: 'rgba(125,133,144,0.8)', text: '#9aa4af' },
};

export class BasesPrimitive {
  constructor(style = DEFAULT_STYLE) {
    this._style = style;
    this._boxes = [];
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
    const self = this;
    this._view = {
      zOrder: () => 'bottom',
      renderer: () => ({ draw: (target) => self._draw(target) }),
    };
  }

  attached({ chart, series, requestUpdate }) {
    this._chart = chart;
    this._series = series;
    this._requestUpdate = requestUpdate;
  }

  detached() {
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
  }

  setBoxes(boxes) {
    this._boxes = boxes || [];
    if (this._requestUpdate) this._requestUpdate();
  }

  updateAllViews() {}

  paneViews() {
    return [this._view];
  }

  _draw(target) {
    if (!this._chart || !this._series || !this._boxes.length) return;
    const ts = this._chart.timeScale();
    const half = (ts.options().barSpacing || 6) / 2;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      for (const b of this._boxes) {
        const x1 = ts.timeToCoordinate(b.start);
        const x2 = ts.timeToCoordinate(b.end);
        const yTop = this._series.priceToCoordinate(b.top);
        const yBot = this._series.priceToCoordinate(b.bottom);
        if (x1 === null || x2 === null || yTop === null || yBot === null) continue;
        const st = b.active ? this._style.active : this._style.past;
        const left = x1 - half;
        const width = x2 - x1 + 2 * half;
        ctx.fillStyle = st.fill;
        ctx.fillRect(left, yTop, width, yBot - yTop);
        ctx.setLineDash([2, 3]);
        ctx.lineWidth = 1;
        ctx.strokeStyle = st.stroke;
        ctx.strokeRect(Math.round(left) + 0.5, Math.round(yTop) + 0.5, Math.round(width), Math.round(yBot - yTop));
        ctx.setLineDash([]);
        ctx.fillStyle = st.text;
        ctx.font = '600 11px -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.fillText(b.label, left + 2, yTop - 4);
      }
    });
  }
}
