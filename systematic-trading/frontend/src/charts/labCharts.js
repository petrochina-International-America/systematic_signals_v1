import { LAYOUT_BASE, COLORS, UKRAINE_DATE, POSITION_COLORSCALE } from './theme';

function baseLayout() {
  const { xaxis, yaxis, ...rest } = LAYOUT_BASE;
  return rest;
}

function ukraineShapes(dates) {
  if (!dates || !dates.length) return [];
  if (dates[0] > UKRAINE_DATE || dates[dates.length - 1] < UKRAINE_DATE) return [];
  return [{ type: 'line', x0: UKRAINE_DATE, x1: UKRAINE_DATE, y0: 0, y1: 1, yref: 'paper', line: { color: COLORS.amber, width: 1, dash: 'dot' }, opacity: 0.6 }];
}

function positionTimeline(position) {
  return {
    type: 'heatmap', x: position.dates, y: [''], z: [position.values],
    zmin: -1, zmax: 1, colorscale: POSITION_COLORSCALE, showscale: false,
    hovertemplate: '%{x}: %{z:+.0f}<extra>position</extra>',
  };
}

export function priceSpaceFigure(result) {
  const held = result.held_price_native;
  const pos = result.position;
  const cumPnl = { dates: result.price_space.dates, values: result.price_space.columns.cum_pnl };

  return {
    data: [
      { type: 'scatter', mode: 'lines', x: held.dates, y: held.values, name: 'Held Price', line: { color: COLORS.blue, width: 1.5 }, xaxis: 'x', yaxis: 'y' },
      { ...positionTimeline(pos), xaxis: 'x2', yaxis: 'y2' },
      { type: 'scatter', mode: 'lines', x: cumPnl.dates, y: cumPnl.values, name: 'Cum PnL', line: { color: COLORS.amber, width: 1.5 }, fill: 'tozeroy', fillcolor: 'rgba(239,159,39,0.08)', xaxis: 'x3', yaxis: 'y3' },
    ],
    layout: {
      ...baseLayout(),
      title: { text: `${result.label} — Price Space`, font: { color: COLORS.text, size: 14 } },
      showlegend: false, height: 520,
      grid: { rows: 3, columns: 1, pattern: 'independent', roworder: 'top to bottom', ygap: 0.06 },
      xaxis: { ...LAYOUT_BASE.xaxis, anchor: 'y' },  yaxis: { ...LAYOUT_BASE.yaxis, title: 'Held Price' },
      xaxis2: { ...LAYOUT_BASE.xaxis, anchor: 'y2', matches: 'x' }, yaxis2: { ...LAYOUT_BASE.yaxis, showticklabels: false, domain: [0.42, 0.48] },
      xaxis3: { ...LAYOUT_BASE.xaxis, anchor: 'y3', matches: 'x' }, yaxis3: { ...LAYOUT_BASE.yaxis, title: 'Cum PnL ($/unit)' },
      shapes: ukraineShapes(held.dates),
    },
  };
}

export function spreadFigure(result) {
  const sp = result.spread;
  const pos = result.position;
  const cumPnl = { dates: result.price_space.dates, values: result.price_space.columns.cum_pnl };
  const entry = result.entry_threshold;

  return {
    data: [
      { type: 'scatter', mode: 'lines', x: sp.dates, y: sp.columns.upper_band, name: '+ε·σ', line: { color: COLORS.red, width: 0.8, dash: 'dot' } },
      { type: 'scatter', mode: 'lines', x: sp.dates, y: sp.columns.lower_band, name: '−ε·σ', line: { color: COLORS.green, width: 0.8, dash: 'dot' } },
      { type: 'scatter', mode: 'lines', x: sp.dates, y: sp.columns.spread_mean, name: 'Mean', line: { color: COLORS.muted, width: 1, dash: 'dash' } },
      { type: 'scatter', mode: 'lines', x: sp.dates, y: sp.columns.spread, name: 'Spread', line: { color: COLORS.blue, width: 1.5 } },
      { type: 'scatter', mode: 'lines', x: sp.dates, y: sp.columns.zscore, name: 'Z', line: { color: COLORS.amber, width: 1.2 }, xaxis: 'x2', yaxis: 'y2' },
      { ...positionTimeline(pos), xaxis: 'x3', yaxis: 'y3' },
      { type: 'scatter', mode: 'lines', x: cumPnl.dates, y: cumPnl.values, name: 'Cum PnL', line: { color: COLORS.amber, width: 1.5 }, fill: 'tozeroy', fillcolor: 'rgba(239,159,39,0.08)', xaxis: 'x4', yaxis: 'y4' },
    ],
    layout: {
      ...baseLayout(),
      title: { text: `${result.label} — Spread & Signal`, font: { color: COLORS.text, size: 14 } },
      showlegend: false, height: 620,
      grid: { rows: 4, columns: 1, pattern: 'independent', roworder: 'top to bottom', ygap: 0.05 },
      xaxis: { ...LAYOUT_BASE.xaxis }, yaxis: { ...LAYOUT_BASE.yaxis, title: 'Spread' },
      xaxis2: { ...LAYOUT_BASE.xaxis, matches: 'x' }, yaxis2: { ...LAYOUT_BASE.yaxis, title: 'Z-score' },
      xaxis3: { ...LAYOUT_BASE.xaxis, matches: 'x' }, yaxis3: { ...LAYOUT_BASE.yaxis, showticklabels: false },
      xaxis4: { ...LAYOUT_BASE.xaxis, matches: 'x' }, yaxis4: { ...LAYOUT_BASE.yaxis, title: 'Cum PnL' },
      shapes: [
        ...ukraineShapes(sp.dates),
        { type: 'line', y0: entry, y1: entry, x0: 0, x1: 1, xref: 'x2 domain', yref: 'y2', line: { color: COLORS.red, width: 1, dash: 'dot' } },
        { type: 'line', y0: -entry, y1: -entry, x0: 0, x1: 1, xref: 'x2 domain', yref: 'y2', line: { color: COLORS.green, width: 1, dash: 'dot' } },
      ],
    },
  };
}

export function mtmFigure(result, volTarget) {
  const mtm = result.mtm;
  const equity = mtm.columns.equity_index;
  const dates = mtm.dates;
  const drawdown = equity.map((v, i) => {
    const peak = Math.max(...equity.slice(0, i + 1).filter(x => x != null));
    return v != null && peak ? v / peak - 1 : null;
  });
  const volScalar = mtm.columns.vol_scalar;
  const vtText = volTarget != null ? ` — ${Math.round(volTarget * 100)}% vol target` : '';

  return {
    data: [
      { type: 'scatter', mode: 'lines', x: dates, y: equity, name: 'Equity', line: { color: COLORS.green, width: 1.5 } },
      { type: 'scatter', mode: 'lines', x: dates, y: drawdown, name: 'Drawdown', line: { color: COLORS.red, width: 1.2 }, fill: 'tozeroy', fillcolor: 'rgba(226,75,74,0.12)', xaxis: 'x2', yaxis: 'y2' },
      ...(volScalar ? [{ type: 'scatter', mode: 'lines', x: dates, y: volScalar, name: 'Vol Scalar', line: { color: COLORS.blue, width: 1.2 }, xaxis: 'x3', yaxis: 'y3' }] : []),
    ],
    layout: {
      ...baseLayout(),
      title: { text: `${result.label} — MTM Space${vtText}`, font: { color: COLORS.text, size: 14 } },
      showlegend: false, height: 520,
      grid: { rows: 3, columns: 1, pattern: 'independent', roworder: 'top to bottom', ygap: 0.07 },
      xaxis: { ...LAYOUT_BASE.xaxis }, yaxis: { ...LAYOUT_BASE.yaxis, title: 'Equity Index' },
      xaxis2: { ...LAYOUT_BASE.xaxis, matches: 'x' }, yaxis2: { ...LAYOUT_BASE.yaxis, title: 'Drawdown', tickformat: '.0%' },
      xaxis3: { ...LAYOUT_BASE.xaxis, matches: 'x' }, yaxis3: { ...LAYOUT_BASE.yaxis, title: 'Vol Scalar' },
      shapes: [
        ...ukraineShapes(dates),
        { type: 'line', y0: 1, y1: 1, x0: 0, x1: 1, xref: 'paper', yref: 'y', line: { color: COLORS.flat, width: 1 } },
      ],
    },
  };
}

export function sweepHeatmap(sweep) {
  const absMax = Math.max(
    ...sweep.z.flat().filter(v => v != null).map(Math.abs),
    0.1
  );

  return {
    data: [{
      type: 'heatmap',
      x: sweep.x.map(String), y: sweep.y.map(String), z: sweep.z,
      zmin: -absMax, zmax: absMax,
      colorscale: [[0, COLORS.red], [0.5, '#1e2235'], [1, COLORS.green]],
      colorbar: { title: { text: 'Sharpe', font: { color: COLORS.muted, size: 10 } }, tickfont: { color: COLORS.muted, size: 9 }, thickness: 10 },
      hovertemplate: `${sweep.y_title}: %{y}<br>${sweep.x_title}: %{x}<br>Sharpe: %{z:.2f}<extra></extra>`,
    }],
    layout: {
      ...baseLayout(),
      title: { text: sweep.title, font: { color: COLORS.text, size: 14 } },
      xaxis: { title: { text: sweep.x_title, font: { color: COLORS.muted, size: 11 } }, type: 'category', tickfont: { color: '#6b7280' }, showgrid: false },
      yaxis: { title: { text: sweep.y_title, font: { color: COLORS.muted, size: 11 } }, type: 'category', tickfont: { color: '#6b7280' }, showgrid: false },
      height: 420,
      shapes: [
        ...(sweep.cur_x != null ? [{ type: 'line', x0: String(sweep.cur_x), x1: String(sweep.cur_x), y0: 0, y1: 1, yref: 'paper', line: { color: COLORS.amber, width: 1, dash: 'dash' }, opacity: 0.9 }] : []),
        ...(sweep.cur_y != null ? [{ type: 'line', y0: String(sweep.cur_y), y1: String(sweep.cur_y), x0: 0, x1: 1, xref: 'paper', line: { color: COLORS.amber, width: 1, dash: 'dash' }, opacity: 0.9 }] : []),
      ],
    },
  };
}
