import { COLORS, UKRAINE_DATE } from './theme';

const GRID = '#1e2235';

function base() {
  return {
    paper_bgcolor: COLORS.surface,
    plot_bgcolor: COLORS.surface,
    font: { family: 'Inter, system-ui, sans-serif', color: COLORS.muted, size: 12 },
    hovermode: 'x unified',
  };
}

function ax(title, extra = {}) {
  return { showgrid: true, gridcolor: GRID, zeroline: false, tickfont: { color: '#7a8194', size: 11 }, title: title ? { text: title, font: { color: COLORS.muted, size: 11 } } : undefined, ...extra };
}

const EVENT_MARKERS = [
  ['2026-03-01', 'Iran'],
  ['2020-03-11', 'COVID'],
  [UKRAINE_DATE, 'Ukraine'],
];

// Pannable time axis: full history is plotted, the initial view is the
// trailing window, and the user drags through time (dragmode 'pan' set on
// the layout) or jumps with the range buttons. Pair with uirevision on the
// layout so the panned position survives React re-renders.
function timeAxis(initRange) {
  return {
    showgrid: false,
    fixedrange: false,
    range: [...initRange],
    rangeselector: {
      buttons: [
        { count: 3, label: '3M', step: 'month', stepmode: 'backward' },
        { count: 1, label: '1Y', step: 'year', stepmode: 'backward' },
        { count: 3, label: '3Y', step: 'year', stepmode: 'backward' },
        { step: 'all', label: 'All' },
      ],
      x: 1, xanchor: 'right', y: 1.24, yanchor: 'top',
      bgcolor: 'rgba(0,0,0,0)', activecolor: '#2d3142',
      bordercolor: '#2d3142', borderwidth: 1,
      font: { color: COLORS.muted, size: 9 },
    },
  };
}

function markers(dates) {
  const shapes = [], annotations = [];
  for (const [date, label] of EVENT_MARKERS) {
    if (dates.length && dates[0] <= date && dates[dates.length - 1] >= date) {
      shapes.push({ type: 'line', x0: date, x1: date, y0: 0, y1: 1, yref: 'paper', line: { color: 'rgba(239,159,39,0.35)', width: 1, dash: 'dot' } });
      annotations.push({ x: date, y: 1.03, yref: 'paper', text: label, showarrow: false, font: { color: COLORS.amber, size: 9 } });
    }
  }
  return { shapes, annotations };
}

export function heroEquityChart(result, volTarget, capital = 1_000_000, sampleDays = 252) {
  const allDates = result.mtm.dates;
  const allEquity = result.mtm.columns.equity_index;
  const allPos = result.position ? result.position.values : null;
  const posDates = result.position ? result.position.dates : null;

  const window = sampleDays || allDates.length;
  const startIdx = Math.max(0, allDates.length - window);
  const dates = allDates.slice(startIdx);
  const rawEquity = allEquity.slice(startIdx);

  const baseVal = rawEquity.find(v => v != null && v > 0) || 1;
  const equity = rawEquity.map(v => v != null ? (v / baseVal) * capital : null);

  const peakLine = [];
  let peak = 0;
  for (let i = 0; i < equity.length; i++) {
    if (equity[i] == null) { peakLine.push(null); continue; }
    if (equity[i] > peak) peak = equity[i];
    peakLine.push(peak);
  }

  // Position timeline: map position values for the trailing window
  let posVals = null;
  if (allPos && posDates) {
    const posMap = new Map();
    for (let i = 0; i < posDates.length; i++) posMap.set(posDates[i], allPos[i]);
    posVals = dates.map(d => posMap.get(d) || 0);
  }

  const ev = markers(dates);

  const data = [
    { type: 'scatter', mode: 'lines', x: dates, y: peakLine, name: 'Peak', xaxis: 'x', yaxis: 'y',
      line: { color: 'rgba(0,0,0,0)', width: 0 }, showlegend: false, hoverinfo: 'skip' },
    { type: 'scatter', mode: 'lines', x: dates, y: equity, name: 'P/L', xaxis: 'x', yaxis: 'y',
      line: { color: '#4ade80', width: 2.5, shape: 'spline', smoothing: 0.3 },
      fill: 'tonexty', fillcolor: 'rgba(226,75,74,0.18)' },
  ];

  const layoutExtra = {};

  if (posVals) {
    const posLabels = posVals.map(v => v > 0 ? 'Long' : v < 0 ? 'Short' : 'Flat');
    data.push({
      type: 'heatmap', x: dates, y: ['pos'], z: [posVals],
      xaxis: 'x2', yaxis: 'y2',
      zmin: -1, zmax: 1, xgap: 0, ygap: 0,
      colorscale: [
        [0, 'rgba(226,75,74,0.45)'], [0.33, 'rgba(226,75,74,0.45)'],
        [0.34, 'rgba(45,49,66,0.35)'], [0.66, 'rgba(45,49,66,0.35)'],
        [0.67, 'rgba(99,153,34,0.45)'], [1, 'rgba(99,153,34,0.45)'],
      ],
      showscale: false,
      customdata: [posLabels],
      hovertemplate: '%{x|%b %d, %Y}: <b>%{customdata}</b><extra></extra>',
    });
    layoutExtra.xaxis2 = {
      showgrid: false, fixedrange: true, matches: 'x', anchor: 'y2',
      showline: false, tickfont: { color: '#7a8194', size: 10 },
      ticklabelmode: 'period',
    };
    layoutExtra.yaxis2 = {
      domain: [0, 0.02], showticklabels: false, fixedrange: true,
      showgrid: false, zeroline: false, anchor: 'x2',
    };
  }

  return {
    data,
    layout: {
      ...base(),
      title: { text: `Strategy P/L — ${sampleDays ? `Last ${sampleDays}d` : 'Full Sample'} (${Math.round(volTarget * 100)}% vol, $1M)`, font: { color: COLORS.text, size: 14, weight: 600 }, x: 0.02, xanchor: 'left' },
      xaxis: ax(null, { showgrid: false, fixedrange: true, anchor: 'y', showticklabels: false }),
      yaxis: ax(null, { fixedrange: true, tickprefix: '$', tickformat: ',.0f', domain: [0.08, 1] }),
      ...layoutExtra,
      showlegend: false, height: 360,
      margin: { l: 80, r: 20, t: 40, b: 32 },
      shapes: [...ev.shapes, { type: 'line', y0: capital, y1: capital, x0: 0, x1: 1, xref: 'paper', yref: 'y', line: { color: '#3d4260', width: 1, dash: 'dash' } }],
      annotations: ev.annotations,
    },
  };
}

export function computeYoyStats(result, capital = 1_000_000) {
  const dates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index;

  const yearMap = new Map();
  for (let i = 0; i < dates.length; i++) {
    if (equity[i] == null) continue;
    const y = dates[i].slice(0, 4);
    if (!yearMap.has(y)) yearMap.set(y, []);
    yearMap.get(y).push(equity[i]);
  }

  const rows = [];
  for (const [year, vals] of yearMap) {
    if (vals.length < 2) continue;
    const startVal = vals[0];
    const endVal = vals[vals.length - 1];
    const ret = endVal / startVal - 1;
    const pnl = ret * capital;

    const rets = [];
    for (let i = 1; i < vals.length; i++) rets.push(vals[i] / vals[i - 1] - 1);
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const std = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length);
    const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;

    let peak = vals[0], maxDD = 0;
    for (const v of vals) {
      if (v > peak) peak = v;
      const dd = v / peak - 1;
      if (dd < maxDD) maxDD = dd;
    }

    rows.push({
      Year: year,
      'Start Capital': `$${capital.toLocaleString()}`,
      'Total PnL': `${pnl >= 0 ? '' : '-'}$${Math.abs(Math.round(pnl)).toLocaleString()}`,
      Return: `${(ret * 100).toFixed(1)}%`,
      Sharpe: sharpe.toFixed(2),
      'Max DD': `${(maxDD * 100).toFixed(1)}%`,
      Days: String(vals.length),
    });
  }
  return rows;
}

export function drawdownChart(result) {
  const dates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index;
  const dd = [];
  let peak = 0;
  for (let i = 0; i < equity.length; i++) {
    if (equity[i] == null) { dd.push(null); continue; }
    if (equity[i] > peak) peak = equity[i];
    dd.push(peak > 0 ? equity[i] / peak - 1 : 0);
  }

  return {
    data: [{
      type: 'scatter', mode: 'lines', x: dates, y: dd,
      line: { color: COLORS.red, width: 1.5 },
      fill: 'tozeroy', fillcolor: 'rgba(226,75,74,0.20)',
    }],
    layout: {
      ...base(),
      title: { text: 'Drawdown', font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: ax(null, { showgrid: false, fixedrange: true }),
      yaxis: ax(null, { tickformat: '.0%', side: 'right', fixedrange: true }),
      showlegend: false, height: 250,
      margin: { l: 20, r: 50, t: 32, b: 28 },
    },
  };
}

export function rollingSharpeChart(result, window = 252) {
  const dates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index || [];
  if (equity.length < window + 10) return null;

  const rets = [];
  for (let i = 1; i < equity.length; i++) {
    rets.push(equity[i] != null && equity[i - 1] != null && equity[i - 1] !== 0
      ? equity[i] / equity[i - 1] - 1 : null);
  }

  const sd = [], sv = [];
  const sqrt252 = Math.sqrt(252);
  for (let i = window - 1; i < rets.length; i++) {
    const sl = rets.slice(i - window + 1, i + 1).filter(v => v != null);
    if (sl.length < window * 0.7) continue;
    const m = sl.reduce((a, b) => a + b, 0) / sl.length;
    const s = Math.sqrt(sl.reduce((a, b) => a + (b - m) ** 2, 0) / sl.length);
    sd.push(dates[i + 1]);
    sv.push(s > 0 ? parseFloat(((m / s) * sqrt252).toFixed(2)) : 0);
  }
  if (!sd.length) return null;

  const ev = markers(sd);

  return {
    data: [{
      type: 'bar', x: sd, y: sv,
      marker: { color: sv.map(v => v >= 0 ? 'rgba(74,222,128,0.7)' : 'rgba(226,75,74,0.7)'), line: { width: 0 } },
    }],
    layout: {
      ...base(),
      title: { text: '1-Year Rolling Sharpe', font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: ax(null, { showgrid: false, fixedrange: true }),
      yaxis: ax(null, { fixedrange: true }),
      showlegend: false, height: 250,
      margin: { l: 50, r: 16, t: 32, b: 28 },
      bargap: 0.3,
      shapes: [...ev.shapes, { type: 'line', y0: 0, y1: 0, x0: 0, x1: 1, xref: 'paper', line: { color: '#3d4260', width: 1 } }],
      annotations: ev.annotations,
    },
  };
}

export function returnDistChart(result) {
  const equity = result.mtm.columns.equity_index || [];
  const rets = [];
  for (let i = 1; i < equity.length; i++) {
    if (equity[i] != null && equity[i - 1] != null && equity[i - 1] !== 0)
      rets.push(equity[i] / equity[i - 1] - 1);
  }
  if (!rets.length) return { data: [], layout: { ...base(), height: 250 } };

  return {
    data: [{
      type: 'histogram', x: rets, nbinsx: 80,
      marker: { color: 'rgba(55,138,221,0.75)' },
    }],
    layout: {
      ...base(),
      title: { text: 'Daily Return Distribution', font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: ax('Daily Return', { tickformat: '.1%', showgrid: false, fixedrange: true }),
      yaxis: ax(null, { fixedrange: true }),
      showlegend: false, height: 250,
      margin: { l: 40, r: 10, t: 32, b: 38 },
    },
  };
}

export function computeDistStats(result, capital = 1_000_000) {
  const equity = result.mtm.columns.equity_index || [];
  const rets = [];
  for (let i = 1; i < equity.length; i++) {
    if (equity[i] != null && equity[i - 1] != null && equity[i - 1] !== 0)
      rets.push(equity[i] / equity[i - 1] - 1);
  }
  if (!rets.length) return null;

  const n = rets.length;
  const mean = rets.reduce((a, b) => a + b, 0) / n;
  const std = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / n);
  const skew = std > 0 ? rets.reduce((a, b) => a + ((b - mean) / std) ** 3, 0) / n : 0;
  const kurt = std > 0 ? rets.reduce((a, b) => a + ((b - mean) / std) ** 4, 0) / n - 3 : 0;

  const annDollar = mean * 252 * capital;
  const annDollarStr = `${annDollar >= 0 ? '' : '-'}$${Math.abs(Math.round(annDollar)).toLocaleString()}`;

  return {
    meanBp: (mean * 10000).toFixed(1),
    meanAnnDollar: annDollarStr,
    stdBp: (std * 10000).toFixed(0),
    stdAnnPct: `${(std * Math.sqrt(252) * 100).toFixed(1)}%`,
    skew: skew.toFixed(2),
    kurt: kurt.toFixed(1),
  };
}

export function computeRangeStats(result, from, to, capital = 1_000_000) {
  const dates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index;
  if (!dates.length) return null;
  const fromDate = from || dates[0];
  const toDate = to || dates[dates.length - 1];
  const vals = [];
  for (let i = 0; i < dates.length; i++) {
    if (dates[i] < fromDate || dates[i] > toDate || equity[i] == null) continue;
    vals.push(equity[i]);
  }
  if (vals.length < 2) return null;
  const ret = vals[vals.length - 1] / vals[0] - 1;
  const pnl = ret * capital;
  const rets = [];
  for (let i = 1; i < vals.length; i++) rets.push(vals[i] / vals[i - 1] - 1);
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const std = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length);
  const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;
  let peak = vals[0], maxDD = 0;
  for (const v of vals) { if (v > peak) peak = v; const dd = v / peak - 1; if (dd < maxDD) maxDD = dd; }
  return {
    days: String(vals.length),
    ret: `${(ret * 100).toFixed(1)}%`,
    pnl: `${pnl >= 0 ? '' : '-'}$${Math.abs(Math.round(pnl)).toLocaleString()}`,
    sharpe: sharpe.toFixed(2),
    maxDD: `${(maxDD * 100).toFixed(1)}%`,
  };
}

export function computeSubPeriodStats(result, splits, capital = 1_000_000) {
  const dates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index;
  if (!dates.length || !equity.length) return [];

  const periods = [];
  for (let i = 0; i < splits.length + 1; i++) {
    const from = i === 0 ? dates[0] : splits[i - 1];
    const to = i === splits.length ? dates[dates.length - 1] : splits[i];
    periods.push({ from, to });
  }

  return periods.map(({ from, to }) => {
    const vals = [], rets = [];
    for (let i = 0; i < dates.length; i++) {
      if (dates[i] < from || dates[i] > to || equity[i] == null) continue;
      vals.push(equity[i]);
    }
    if (vals.length < 2) return { label: `${from.slice(0,7)} → ${to.slice(0,7)}`, days: 0 };
    for (let i = 1; i < vals.length; i++) rets.push(vals[i] / vals[i - 1] - 1);
    const ret = vals[vals.length - 1] / vals[0] - 1;
    const pnl = ret * capital;
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const std = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length);
    const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;
    let peak = vals[0], maxDD = 0;
    for (const v of vals) { if (v > peak) peak = v; const dd = v / peak - 1; if (dd < maxDD) maxDD = dd; }

    return {
      Period: `${from.slice(0,7)} → ${to.slice(0,7)}`,
      Days: String(vals.length),
      Return: `${(ret * 100).toFixed(1)}%`,
      'Total PnL': `${pnl >= 0 ? '' : '-'}$${Math.abs(Math.round(pnl)).toLocaleString()}`,
      Sharpe: sharpe.toFixed(2),
      'Max DD': `${(maxDD * 100).toFixed(1)}%`,
    };
  });
}

export function computeSampleMetrics(result, sampleDays = null, capital = 1_000_000) {
  const dates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index;
  const window = sampleDays || dates.length;
  const startIdx = Math.max(0, dates.length - window);
  const vals = [];
  for (let i = startIdx; i < dates.length; i++) {
    if (equity[i] != null) vals.push(equity[i]);
  }
  if (vals.length < 2) return null;

  const ret = vals[vals.length - 1] / vals[0] - 1;
  const pnl = ret * capital;
  const years = vals.length / 252;
  const cagr = years > 0 ? Math.pow(vals[vals.length - 1] / vals[0], 1 / years) - 1 : 0;

  const rets = [];
  for (let i = 1; i < vals.length; i++) rets.push(vals[i] / vals[i - 1] - 1);
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const std = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length);
  const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;

  let peak = vals[0], maxDD = 0;
  for (const v of vals) { if (v > peak) peak = v; const dd = v / peak - 1; if (dd < maxDD) maxDD = dd; }

  return { Sharpe: sharpe, CAGR: cagr, 'Total PnL': pnl, Drawdown: maxDD };
}

export function computeTradeStats(result, sampleDays = null) {
  const pos = result.position ? result.position.values : null;
  const dates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index || [];
  if (!pos || !dates.length) return null;

  const window = sampleDays || dates.length;
  const startIdx = Math.max(0, dates.length - window);

  // Count trades (position flips) and holding periods
  let trades = 0;
  let holdingDays = 0;
  let currentHold = 0;
  const holdLengths = [];
  let prevPos = 0;

  for (let i = startIdx; i < pos.length; i++) {
    const p = pos[i] || 0;
    if (p !== 0) holdingDays++;
    if (p !== prevPos) {
      if (prevPos !== 0 && currentHold > 0) holdLengths.push(currentHold);
      if (p !== 0) trades++;
      currentHold = 0;
    }
    if (p !== 0) currentHold++;
    prevPos = p;
  }
  if (prevPos !== 0 && currentHold > 0) holdLengths.push(currentHold);

  const totalDays = pos.length - startIdx;
  const years = totalDays / 252;

  // % positive days from equity returns
  let posDays = 0, totalRetDays = 0;
  for (let i = Math.max(1, startIdx); i < equity.length; i++) {
    if (equity[i] != null && equity[i - 1] != null && equity[i - 1] !== 0) {
      totalRetDays++;
      if (equity[i] >= equity[i - 1]) posDays++;
    }
  }

  const tradesPerYear = years > 0 ? trades / years : 0;
  const avgHold = holdLengths.length > 0 ? holdLengths.reduce((a, b) => a + b, 0) / holdLengths.length : 0;
  const pctPosDays = totalRetDays > 0 ? (posDays / totalRetDays) * 100 : 0;

  return {
    tradesPerYear: Math.round(tradesPerYear * 10) / 10,
    avgHoldDays: Math.round(avgHold),
    pctPositiveDays: Math.round(pctPosDays * 10) / 10,
  };
}

export function computeHitRate(result, sampleDays = null) {
  // Hit rate on CLOSED trades only (entry -> exit round trips), gross, on
  // the same MTM book the other metric cards read. A trade is a maximal run
  // of constant nonzero position; its P&L accrues on days s+1 .. e+1
  // (contracts held after day t's close earn day t+1's flow — the capital
  // loop's booking). A trade still open on the last day is NOT closed and
  // is excluded.
  const pos = result.position ? result.position.values : null;
  const dates = result.mtm.dates;
  const gross = result.mtm.columns.gross_pnl;
  if (!pos || !gross || !dates.length) return null;

  const window = sampleDays || dates.length;
  const startIdx = Math.max(0, dates.length - window);

  const trades = [];
  let start = null;
  for (let i = startIdx; i < pos.length; i++) {
    const p = pos[i] || 0;
    const prev = i > startIdx ? (pos[i - 1] || 0) : 0;
    if (p !== 0 && (prev === 0 || prev !== p)) {
      if (start !== null) trades.push([start, i - 1]);
      start = i;
    } else if (p === 0 && start !== null) {
      trades.push([start, i - 1]);
      start = null;
    }
  }
  // start !== null here => trade still open at data end: excluded (not closed)

  let wins = 0;
  for (const [s, e] of trades) {
    let pnl = 0;
    for (let i = s + 1; i <= Math.min(e + 1, gross.length - 1); i++) pnl += gross[i] || 0;
    if (pnl > 0) wins++;
  }
  if (!trades.length) return null;
  return { hitRate: wins / trades.length, nClosed: trades.length };
}

export function forwardCurveEvolution(priceData) {
  if (!priceData || !priceData.dates || !priceData.columns) return null;

  const dates = priceData.dates;
  const cols = Object.keys(priceData.columns)
    .filter(c => /^F\d+$/.test(c))
    .sort((a, b) => parseInt(a.slice(1)) - parseInt(b.slice(1)));
  if (cols.length < 3) return null;

  const tenorLabels = cols;
  const n = dates.length;
  const days = 22;
  const startIdx = Math.max(0, n - days);

  const traces = [];

  for (let di = startIdx; di < n; di++) {
    const t = (di - startIdx) / Math.max(1, n - 1 - startIdx);
    const vals = cols.map(c => priceData.columns[c][di]);
    const isLast = di === n - 1;
    const isFirst = di === startIdx;

    if (isFirst) {
      traces.push({
        type: 'scatter', mode: 'lines', x: tenorLabels, y: vals,
        name: dates[di], line: { color: 'rgba(55,138,221,0.12)', width: 0 },
        showlegend: false, hoverinfo: 'skip',
      });
    }

    traces.push({
      type: 'scatter', mode: 'lines', x: tenorLabels, y: vals,
      name: dates[di],
      line: { color: isLast ? 'rgba(55,138,221,1)' : `rgba(55,138,221,${0.06 + 0.25 * t})`, width: isLast ? 2.5 : 1 },
      fill: di > startIdx ? 'tonexty' : undefined,
      fillcolor: `rgba(55,138,221,${0.03 + 0.04 * t})`,
      hovertemplate: isLast ? `<b>Today</b> (${dates[di]})<br>%{x}: %{y:,.2f}<extra></extra>` : null,
      hoverinfo: isLast ? undefined : 'skip',
      showlegend: false,
    });
  }

  return {
    data: traces,
    layout: {
      ...base(),
      title: { text: `Forward Curve — Last ${days}d`, font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: ax('Tenor', { showgrid: false, fixedrange: true, type: 'category' }),
      yaxis: ax(null, { fixedrange: true }),
      showlegend: false, height: 250,
      margin: { l: 55, r: 16, t: 32, b: 38 },
    },
  };
}

const SPEED_COLORS = {
  'Very Fast': '#EF9F27',
  'Fast': '#a78bfa',
  'Medium': '#38bdf8',
  'Slow': '#f472b6',
  'Averaged': '#4ade80',
};
const SPEED_ORDER = ['Very Fast', 'Fast', 'Medium', 'Slow', 'Averaged'];

export function speedComparisonChart(speedData, selectedTier = 'Averaged', volTargetPct = 15, sampleDays = null, hoveredTier = null) {
  if (!speedData || !speedData.tiers) return null;

  const tiers = speedData.tiers;
  const data = [];
  const hasHover = hoveredTier != null;

  for (const name of SPEED_ORDER) {
    const tier = tiers[name];
    if (!tier) continue;

    const allDates = tier.dates;
    const allEq = tier.equity_index;
    const window = sampleDays || allDates.length;
    const startIdx = Math.max(0, allDates.length - window);
    const dates = allDates.slice(startIdx);
    const rawEq = allEq.slice(startIdx);

    const baseVal = rawEq.find(v => v != null && v > 0) || 1;
    const normEq = rawEq.map(v => v != null ? v / baseVal : null);

    const isSelected = name === selectedTier;
    const isHovered = name === hoveredTier;
    const isAveraged = name === 'Averaged';
    const color = SPEED_COLORS[name];
    const pairsStr = isAveraged ? 'Equal-weight avg of all speeds' : tier.ma_pairs.map(p => `(${p[0]},${p[1]})`).join('  ');

    let width, opacity;
    if (hasHover) {
      width = isHovered ? 3.5 : 1.5;
      opacity = isHovered ? 1 : 0.3;
    } else {
      width = isSelected ? 3.5 : 1.5;
      opacity = isSelected ? 1 : 0.3;
    }

    data.push({
      type: 'scatter',
      mode: 'lines',
      x: dates,
      y: normEq,
      name,
      line: { color, width, shape: 'spline', smoothing: 0.6 },
      opacity,
      hoverlabel: {
        bgcolor: 'rgba(15,17,23,0.92)',
        bordercolor: color,
        font: { family: 'Inter, system-ui, sans-serif', size: 12, color: '#e2e8f0' },
        namelength: 0,
      },
      hovertemplate:
        `<b style="color:${color}">${name}</b>  ·  SR ${tier.sharpe.toFixed(2)}  ·  ${tier.trades_yr}/yr<br>` +
        `<span style="color:#9ba3b2">${pairsStr}</span><br>` +
        `%{x|%b %d, %Y}  →  <b>%{y:.3f}</b>` +
        `<extra></extra>`,
      meta: { tier: name },
    });
  }

  const ev = markers(data[0]?.x || []);

  return {
    data,
    layout: {
      ...base(),
      title: {
        text: `Momentum · ${speedData.commodity} — MTM Equity (${volTargetPct}% vol) | Speed Comparison`,
        font: { color: COLORS.text, size: 14, weight: 600 },
        x: 0.02, xanchor: 'left',
      },
      xaxis: ax(null, { showgrid: false, fixedrange: true }),
      yaxis: ax(null, { fixedrange: true, tickformat: '.2f' }),
      showlegend: true,
      legend: {
        bgcolor: 'rgba(15,17,23,0.85)',
        bordercolor: 'rgba(45,49,66,0.4)',
        borderwidth: 1,
        font: { family: 'Inter, system-ui, sans-serif', color: COLORS.muted, size: 11 },
        x: 0.01, y: 0.99, xanchor: 'left', yanchor: 'top',
        tracegroupgap: 2,
        itemwidth: 25,
      },
      height: 420,
      margin: { l: 50, r: 20, t: 40, b: 32 },
      shapes: ev.shapes,
      annotations: ev.annotations,
      hovermode: 'closest',
      hoverdistance: 40,
    },
  };
}

export { SPEED_COLORS, SPEED_ORDER };

const TIER_MA_LEVELS = {
  'Very Fast': { fast: 2, slow: 10 },
  'Fast':      { fast: 5, slow: 20 },
  'Medium':    { fast: 20, slow: 60 },
  'Slow':      { fast: 40, slow: 180 },
  'Averaged':  { fast: 10, slow: 60 },
};

function rollingMean(values, window) {
  const out = new Array(values.length).fill(null);
  let sum = 0, count = 0;
  for (let i = 0; i < values.length; i++) {
    if (values[i] != null) { sum += values[i]; count++; }
    if (i >= window) {
      if (values[i - window] != null) { sum -= values[i - window]; count--; }
    }
    if (i >= window - 1 && count >= window * 0.7) out[i] = sum / count;
  }
  return out;
}

export function maLevelsChart(priceData, tier, customFast, customSlow, hoveredTier) {
  if (!priceData?.columns?.F1) return null;

  const allDates = priceData.dates;
  const f1 = priceData.columns.F1;

  const visibleDays = 66;
  const startIdx = Math.max(0, allDates.length - visibleDays);
  const dates = allDates.slice(startIdx);
  const price = f1.slice(startIdx);

  const data = [
    { type: 'scatter', mode: 'lines', x: dates, y: price,
      name: 'Price', line: { color: '#e2e8f0', width: 2.5 },
      hovertemplate: '<b>Price</b>  $%{y:.2f}<extra></extra>' },
  ];

  // Collect all levels: each tier's slow MA current value
  const tiers = tier === 'Custom'
    ? [{ name: `MA(${customFast},${customSlow})`, slow: customSlow, color: '#94a3b8' }]
    : SPEED_ORDER.map(name => ({
        name,
        slow: TIER_MA_LEVELS[name].slow,
        color: SPEED_COLORS[name],
      }));

  const levels = [];
  for (const t of tiers) {
    const fullMa = rollingMean(f1, t.slow);
    let lastVal = null;
    for (let i = fullMa.length - 1; i >= 0; i--) { if (fullMa[i] != null) { lastVal = fullMa[i]; break; } }
    if (lastVal != null) levels.push({ ...t, val: lastVal });
  }

  // Sort by value descending, then nudge display positions to avoid overlap
  levels.sort((a, b) => b.val - a.val);


  const activeTier = hoveredTier || tier;
  const annotations = [];

  // Nudge annotation y-positions to avoid overlap
  const validPrices = price.filter(v => v != null);
  const range = (Math.max(...validPrices) - Math.min(...validPrices)) || 1;
  const minGap = range * 0.055;
  const labelYs = levels.map(l => l.val);
  for (let i = 1; i < labelYs.length; i++) {
    if (labelYs[i - 1] - labelYs[i] < minGap) {
      labelYs[i] = labelYs[i - 1] - minGap;
    }
  }

  for (let i = 0; i < levels.length; i++) {
    const lev = levels[i];
    const isActive = lev.name === activeTier;
    data.push({
      type: 'scatter', mode: 'lines',
      x: dates, y: new Array(dates.length).fill(lev.val),
      name: `${lev.name} ${lev.slow}d`,
      line: { color: lev.color, width: isActive ? 1.5 : 1, dash: 'dot' },
      opacity: isActive ? 1 : 0.5,
      showlegend: false,
      hovertemplate: `<b>${lev.name}</b> ${lev.slow}d  $${lev.val.toFixed(2)}<extra></extra>`,
    });
    annotations.push({
      x: 1, xref: 'paper', xanchor: 'left',
      y: labelYs[i], yref: 'y', yanchor: 'middle',
      text: isActive ? `<b>${lev.name}</b> $${lev.val.toFixed(2)}` : `${lev.name} $${lev.val.toFixed(2)}`,
      font: { color: lev.color, size: isActive ? 10 : 8 },
      opacity: isActive ? 1 : 0.6,
      showarrow: false, bgcolor: 'rgba(15,17,23,0.85)', borderpad: 2,
    });
  }

  return {
    data,
    layout: {
      ...base(),
      title: { text: 'MA Speed Levels — Slow MA by Tier', font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: ax(null, { showgrid: false, fixedrange: true }),
      yaxis: ax(null, { fixedrange: true, tickprefix: '$', tickformat: ',.1f' }),
      showlegend: false,
      height: 280,
      margin: { l: 55, r: 105, t: 32, b: 32 },
      annotations,
      hovermode: 'x unified',
    },
  };
}

export function zscoreBandsChart(result, trailingDays = 252) {
  const spread = result.spread;
  if (!spread?.dates) return null;
  const allDates = spread.dates;
  const allZscore = spread.columns.zscore || spread.columns.deviation_pct;
  if (!allZscore) return null;

  // Full history plotted; initial view = trailing window (drag/pan for more)
  const startIdx = trailingDays ? Math.max(0, allDates.length - trailingDays) : 0;
  const initRange = [allDates[startIdx], allDates[allDates.length - 1]];
  const dates = allDates;
  const zscore = allZscore;

  // Fit y to the INITIAL window (full-history extremes would squash a short
  // default view); always keep the ±2σ band lines in frame.
  let zLo = -2, zHi = 2;
  for (let i = startIdx; i < zscore.length; i++) {
    const v = zscore[i];
    if (v == null) continue;
    if (v < zLo) zLo = v;
    if (v > zHi) zHi = v;
  }
  const yRange = [zLo - 0.4, zHi + 0.4];

  const ev = markers(dates);
  const bands = [
    { level: 1.0, color: '#eab308', dash: 'dot', label: '±1σ' },
    { level: 1.5, color: '#f97316', dash: 'dash', label: '±1.5σ' },
    { level: 2.0, color: '#ef4444', dash: 'solid', label: '±2σ' },
  ];

  const data = [{
    type: 'scatter', mode: 'lines', x: dates, y: zscore,
    name: 'Z-score', line: { color: COLORS.blue, width: 1 },
    hovertemplate: '<b>Z-score</b>  %{y:.2f}<extra></extra>',
  }];

  const shapes = [
    { type: 'line', y0: 0, y1: 0, x0: 0, x1: 1, xref: 'paper', line: { color: '#3d4260', width: 0.8 } },
    ...ev.shapes,
  ];

  for (const b of bands) {
    shapes.push(
      { type: 'line', y0: b.level, y1: b.level, x0: 0, x1: 1, xref: 'paper', line: { color: b.color, width: 1, dash: b.dash } },
      { type: 'line', y0: -b.level, y1: -b.level, x0: 0, x1: 1, xref: 'paper', line: { color: b.color, width: 1, dash: b.dash } },
    );
    data.push({
      type: 'scatter', mode: 'lines', x: [null], y: [null],
      name: b.label, line: { color: b.color, dash: b.dash, width: 1.5 }, showlegend: true,
    });
  }

  return {
    data,
    layout: {
      ...base(),
      title: { text: 'Z-Score Signal Bands', font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: ax(null, timeAxis(initRange)),
      yaxis: ax(null, { fixedrange: true, range: yRange }),
      dragmode: 'pan',
      // the window is part of the revision: a changed initial range must be
      // re-applied (uirevision otherwise pins the previous view)
      uirevision: (result.key || 'z-bands') + ':' + (trailingDays || 'full'),
      showlegend: true,
      legend: { bgcolor: 'rgba(0,0,0,0)', font: { color: COLORS.muted, size: 9 }, orientation: 'h', x: 0, y: -0.18, xanchor: 'left', yanchor: 'top' },
      height: 250,
      margin: { l: 50, r: 16, t: 32, b: 48 },
      shapes, annotations: ev.annotations,
      hovermode: 'x unified',
    },
  };
}

export function spreadLevelChart(result, trailingDays = 252) {
  const spread = result.spread;
  if (!spread?.dates) return null;
  const allDates = spread.dates;
  const allSp = spread.columns.spread;
  const allMean = spread.columns.spread_mean;
  const allUpper = spread.columns.upper_band;
  const allLower = spread.columns.lower_band;
  if (!allSp) return null;

  const allQuoted = spread.columns.quoted_spread;

  // Display re-anchoring (same convention as the Levels panel): the signal
  // series (flow cumsum) drifts from the quoted spread by the accumulated
  // roll carry (~-$55 over 2010-26 for WTI/Brent), so plotted raw it sits a
  // misleading distance below the quote. Shift signal/mean/bands by TODAY's
  // constant offset so the panel reads in screen-quote dollars. A constant
  // shift changes no z-score, no band distance, no entry/exit — presentation
  // only. Anchored on the FULL arrays so the shift is identical for every
  // trailing-window selection.
  let k = 0;
  if (allQuoted) {
    for (let i = allDates.length - 1; i >= 0; i--) {
      if (allSp[i] != null && allQuoted[i] != null) {
        k = allQuoted[i] - allSp[i];
        break;
      }
    }
  }
  const shift = (arr) => (arr ? arr.map((v) => (v == null ? null : v + k)) : null);

  // Full history plotted; initial view = trailing window (drag/pan for more)
  const startIdx = trailingDays ? Math.max(0, allDates.length - trailingDays) : 0;
  const initRange = [allDates[startIdx], allDates[allDates.length - 1]];
  const dates = allDates;
  const sp = shift(allSp);
  const mean = allMean ? shift(allMean) : null;
  const upper = allUpper ? shift(allUpper) : null;
  const lower = allLower ? shift(allLower) : null;
  const quoted = allQuoted;

  // The quoted spread is the ONE price line shown: after quote-anchoring it
  // sits within ~$1 of the signal series, so plotting both was duplication.
  // The signal series stays as the fallback line for pairs with no listed
  // quote, and markers snap to whichever line is drawn.
  const lineY = quoted || sp;
  const lineName = quoted ? 'Quoted Spread' : 'Signal Series';
  const markerY = (i) => (quoted && quoted[i] != null ? quoted[i] : sp[i]);

  // Fit y to the INITIAL window across every plotted series (full-history
  // extremes — e.g. COVID-era bands — would squash a short default view).
  let yLo = Infinity, yHi = -Infinity;
  for (const arr of [lineY, mean, upper, lower]) {
    if (!arr) continue;
    for (let i = startIdx; i < arr.length; i++) {
      const v = arr[i];
      if (v == null) continue;
      if (v < yLo) yLo = v;
      if (v > yHi) yHi = v;
    }
  }
  const yPad = (yHi - yLo) * 0.1 || 1;
  const yRange = isFinite(yLo) ? [yLo - yPad, yHi + yPad] : undefined;

  const ev = markers(dates);

  // ── Signal events: what makes this panel more than a re-projection of the
  // z-score panel. Position shading (long/short spans) + entry/exit markers
  // at the dollar level the state flipped, so the chart reads as a trade
  // blotter over the series the z actually fires on. `position` is the
  // decision series (state decided at that day's close from z through t-1).
  const posShapes = [];
  const entryLong = { x: [], y: [] };
  const entryShort = { x: [], y: [] };
  const exits = { x: [], y: [] };
  if (result.position?.dates) {
    const posByDate = {};
    result.position.dates.forEach((d, i) => { posByDate[d] = result.position.values[i]; });
    const pos = dates.map((d) => posByDate[d] ?? 0);
    let segStart = null;
    let segSide = 0;
    const closeSeg = (endDate) => {
      if (segStart != null && segSide !== 0) {
        posShapes.push({
          type: 'rect', xref: 'x', yref: 'paper',
          x0: segStart, x1: endDate, y0: 0, y1: 1,
          fillcolor: segSide > 0 ? 'rgba(34,197,94,0.10)' : 'rgba(239,68,68,0.10)',
          line: { width: 0 }, layer: 'below',
        });
      }
    };
    for (let i = 0; i < dates.length; i++) {
      const cur = pos[i] || 0;
      const prev = i > 0 ? (pos[i - 1] || 0) : cur; // window opens mid-position: shade, no marker
      if (cur !== segSide) {
        closeSeg(dates[i]);
        segStart = cur !== 0 ? dates[i] : null;
        segSide = cur;
      }
      if (i === 0) continue;
      if (cur !== prev) {
        if (prev !== 0) { exits.x.push(dates[i]); exits.y.push(markerY(i)); }
        if (cur > 0) { entryLong.x.push(dates[i]); entryLong.y.push(markerY(i)); }
        if (cur < 0) { entryShort.x.push(dates[i]); entryShort.y.push(markerY(i)); }
      }
    }
    closeSeg(dates[dates.length - 1]);
  }

  const data = [
    { type: 'scatter', mode: 'lines', x: dates, y: lineY,
      name: lineName, line: { color: COLORS.blue, width: 1.2 },
      hovertemplate: `<b>${quoted ? 'Quoted' : 'Signal'}</b>  $%{y:.2f}<extra></extra>` },
  ];

  if (mean) {
    data.push({
      type: 'scatter', mode: 'lines', x: dates, y: mean,
      name: 'Rolling Mean', line: { color: '#e2e8f0', width: 1, dash: 'dash' },
      hovertemplate: '<b>Mean</b>  $%{y:.2f}<extra></extra>',
    });
  }
  if (upper) {
    data.push({
      type: 'scatter', mode: 'lines', x: dates, y: upper,
      name: 'Upper Band', line: { color: '#f97316', width: 0.8, dash: 'dot' },
      hovertemplate: '<b>Upper</b>  $%{y:.2f}<extra></extra>',
    });
  }
  if (lower) {
    data.push({
      type: 'scatter', mode: 'lines', x: dates, y: lower,
      name: 'Lower Band', line: { color: '#f97316', width: 0.8, dash: 'dot' },
      hovertemplate: '<b>Lower</b>  $%{y:.2f}<extra></extra>',
    });
  }

  if (entryLong.x.length) {
    data.push({
      type: 'scatter', mode: 'markers', x: entryLong.x, y: entryLong.y,
      name: 'Long entry',
      marker: { symbol: 'triangle-up', size: 9, color: '#22c55e',
                line: { color: '#052e16', width: 1 } },
      hovertemplate: '<b>LONG entry</b>  $%{y:.2f}<extra></extra>',
    });
  }
  if (entryShort.x.length) {
    data.push({
      type: 'scatter', mode: 'markers', x: entryShort.x, y: entryShort.y,
      name: 'Short entry',
      marker: { symbol: 'triangle-down', size: 9, color: '#ef4444',
                line: { color: '#450a0a', width: 1 } },
      hovertemplate: '<b>SHORT entry</b>  $%{y:.2f}<extra></extra>',
    });
  }
  if (exits.x.length) {
    data.push({
      type: 'scatter', mode: 'markers', x: exits.x, y: exits.y,
      name: 'Exit',
      marker: { symbol: 'x-thin', size: 8, color: '#94a3b8',
                line: { color: '#94a3b8', width: 1.5 } },
      hovertemplate: '<b>Exit</b>  $%{y:.2f}<extra></extra>',
    });
  }

  return {
    data,
    layout: {
      ...base(),
      title: { text: 'Quoted Spread, Signal Bands & Trades', font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: ax(null, timeAxis(initRange)),
      yaxis: ax(null, { fixedrange: true, tickprefix: '$', range: yRange }),
      dragmode: 'pan',
      // the window is part of the revision: a changed initial range must be
      // re-applied (uirevision otherwise pins the previous view)
      uirevision: (result.key || 'spread-trades') + ':' + (trailingDays || 'full'),
      showlegend: true,
      legend: { bgcolor: 'rgba(0,0,0,0)', font: { color: COLORS.muted, size: 9 }, orientation: 'h', x: 0, y: -0.18, xanchor: 'left', yanchor: 'top' },
      height: 250,
      margin: { l: 55, r: 16, t: 32, b: 48 },
      shapes: [...posShapes, ...ev.shapes], annotations: ev.annotations,
      hovermode: 'x unified',
    },
  };
}

export function sweepHeatmapChart(sweepData) {
  if (!sweepData?.z) return null;

  const { x, y, z, x_title, y_title, cur_x, cur_y } = sweepData;

  const xs = x.map(String);
  const ys = [...y].reverse().map(String);
  const zRev = [...z].reverse().map(row => row.map(v => v ?? 0));

  const nRows = ys.length;
  const nCols = xs.length;

  const xIdx = Array.from({ length: nCols }, (_, i) => i);
  const yIdx = Array.from({ length: nRows }, (_, i) => i);

  const shapes = [];
  if (cur_x != null && cur_y != null) {
    const xi = xs.indexOf(String(cur_x));
    const yi = ys.indexOf(String(cur_y));
    if (xi >= 0 && yi >= 0) {
      shapes.push({
        type: 'rect', x0: xi - 0.5, x1: xi + 0.5, y0: yi - 0.5, y1: yi + 0.5,
        line: { color: '#fff', width: 2.5 }, fillcolor: 'rgba(0,0,0,0)',
      });
    }
  }

  const annotations = [];
  for (let ri = 0; ri < nRows; ri++) {
    for (let ci = 0; ci < nCols; ci++) {
      const val = zRev[ri]?.[ci];
      if (val != null) {
        annotations.push({
          x: ci, y: ri, text: val.toFixed(2),
          font: { color: '#000', size: 10 },
          showarrow: false,
        });
      }
    }
  }

  const cellPx = 36;
  const h = nRows * cellPx + 80;

  return {
    data: [{
      type: 'heatmap', x: xIdx, y: yIdx, z: zRev, xgap: 2, ygap: 2,
      colorscale: [[0, '#dc2626'], [0.25, '#ef4444'], [0.5, '#fbbf24'], [0.75, '#4ade80'], [1, '#15803d']],
      zmid: 0,
      showscale: true, colorbar: { tickfont: { color: COLORS.muted, size: 9 }, len: 0.6, thickness: 10, x: 1.02 },
      hovertemplate: 'Sharpe: %{z:.2f}<extra></extra>',
      customdata: zRev,
    }],
    layout: {
      ...base(),
      title: { text: 'Sharpe Grid Search', font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: {
        title: { text: x_title, font: { color: COLORS.muted, size: 10 } },
        tickfont: { color: '#7a8194', size: 10 },
        tickvals: xIdx, ticktext: xs,
        fixedrange: true,
      },
      yaxis: {
        title: { text: y_title, font: { color: COLORS.muted, size: 10 } },
        tickfont: { color: '#7a8194', size: 10 },
        tickvals: yIdx, ticktext: ys,
        fixedrange: true,
      },
      height: h,
      margin: { l: 55, r: 36, t: 32, b: 42 },
      shapes, annotations,
    },
    _labelX: xs, _labelY: ys,
  };
}

export function volScalarChart(result, sampleDays = null) {
  const dates = result.mtm.dates;
  const vs = result.mtm.columns.vol_scalar;
  if (!vs || !vs.some(v => v != null && v !== 0)) return null;

  // Initial view follows the page's sample-window selector; full history
  // stays reachable by dragging / range buttons (same UX as the z-score
  // and trade panels).
  const startIdx = sampleDays ? Math.max(0, dates.length - sampleDays) : 0;
  const initRange = [dates[startIdx], dates[dates.length - 1]];

  return {
    data: [{
      type: 'scatter', mode: 'lines', x: dates, y: vs,
      line: { color: COLORS.amber, width: 2 },
    }],
    layout: {
      ...base(),
      title: { text: 'Vol Scalar (position sizing)', font: { color: COLORS.text, size: 13 }, x: 0.02, xanchor: 'left' },
      xaxis: ax(null, timeAxis(initRange)),
      yaxis: ax(null, { fixedrange: true }),
      dragmode: 'pan',
      uirevision: (result.key || 'vol-scalar') + ':' + (sampleDays || 'full'),
      showlegend: false, height: 250,
      margin: { l: 50, r: 16, t: 32, b: 28 },
    },
  };
}
