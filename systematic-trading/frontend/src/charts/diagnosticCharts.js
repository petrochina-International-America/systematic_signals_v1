import { LAYOUT_BASE, COLORS, UKRAINE_DATE } from './theme';

function baseLayout() {
  const { xaxis, yaxis, ...rest } = LAYOUT_BASE;
  return rest;
}

function eventShapes(dates) {
  const shapes = [];
  const annotations = [];
  const events = [
    { date: '2020-03-11', label: 'COVID' },
    { date: UKRAINE_DATE, label: 'Ukraine' },
  ];
  for (const ev of events) {
    if (dates[0] <= ev.date && dates[dates.length - 1] >= ev.date) {
      shapes.push({
        type: 'line', x0: ev.date, x1: ev.date, y0: 0, y1: 1, yref: 'paper',
        line: { color: COLORS.amber, width: 1, dash: 'dot' }, opacity: 0.6,
      });
      annotations.push({
        x: ev.date, y: 1.02, yref: 'paper', text: ev.label,
        showarrow: false, font: { color: COLORS.amber, size: 9 },
      });
    }
  }
  return { shapes, annotations };
}

export function indexedComparison(result) {
  const psDates = result.price_space.dates;
  const psCum = result.price_space.columns.cum_net_pnl || result.price_space.columns.cum_pnl;
  const mtmDates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index;

  const psFirst = psCum.find(v => v != null && v !== 0) || 1;
  const psIndexed = psCum.map(v => v != null ? v / Math.abs(psFirst) : null);

  const ev = eventShapes(psDates);

  return {
    data: [
      { type: 'scatter', mode: 'lines', x: psDates, y: psIndexed, name: 'Price Space', line: { color: COLORS.blue, width: 1.5 } },
      { type: 'scatter', mode: 'lines', x: mtmDates, y: equity, name: 'MTM', line: { color: COLORS.amber, width: 1.5 } },
    ],
    layout: {
      ...baseLayout(),
      title: { text: 'Price Space vs MTM (indexed)', font: { color: COLORS.text, size: 14 } },
      xaxis: LAYOUT_BASE.xaxis, yaxis: { ...LAYOUT_BASE.yaxis, title: 'Indexed' },
      showlegend: true, height: 360,
      shapes: [
        ...ev.shapes,
        { type: 'line', y0: 1, y1: 1, x0: 0, x1: 1, xref: 'paper', yref: 'y', line: { color: COLORS.flat, width: 1 } },
      ],
      annotations: ev.annotations,
    },
  };
}

export function returnScatter(result) {
  const psPnl = result.price_space.columns.daily_pnl || [];
  const mtmPnl = result.mtm.columns.daily_pnl || [];
  const len = Math.min(psPnl.length, mtmPnl.length);

  const xs = [], ys = [];
  for (let i = 0; i < len; i++) {
    if (psPnl[i] != null && mtmPnl[i] != null) {
      xs.push(psPnl[i]);
      ys.push(mtmPnl[i]);
    }
  }

  let corr = null;
  if (xs.length > 10) {
    const mx = xs.reduce((a, b) => a + b, 0) / xs.length;
    const my = ys.reduce((a, b) => a + b, 0) / ys.length;
    let num = 0, dx = 0, dy = 0;
    for (let i = 0; i < xs.length; i++) {
      num += (xs[i] - mx) * (ys[i] - my);
      dx += (xs[i] - mx) ** 2;
      dy += (ys[i] - my) ** 2;
    }
    corr = dx && dy ? (num / Math.sqrt(dx * dy)).toFixed(3) : null;
  }

  return {
    data: [{
      type: 'scatter', mode: 'markers', x: xs, y: ys,
      marker: { color: COLORS.blue, size: 3, opacity: 0.5 },
      name: 'Daily returns',
    }],
    layout: {
      ...baseLayout(),
      title: { text: `PS vs MTM Daily Returns${corr ? ` (r = ${corr})` : ''}`, font: { color: COLORS.text, size: 14 } },
      xaxis: { ...LAYOUT_BASE.xaxis, title: 'Price Space' },
      yaxis: { ...LAYOUT_BASE.yaxis, title: 'MTM' },
      showlegend: false, height: 360,
    },
  };
}

export function volScalarChart(result) {
  const dates = result.mtm.dates;
  const volScalar = result.mtm.columns.vol_scalar;
  if (!volScalar) return null;

  const ev = eventShapes(dates);

  return {
    data: [{
      type: 'scatter', mode: 'lines', x: dates, y: volScalar,
      name: 'Vol Scalar', line: { color: COLORS.blue, width: 1.2 },
    }],
    layout: {
      ...baseLayout(),
      title: { text: 'Vol Scalar', font: { color: COLORS.text, size: 14 } },
      xaxis: LAYOUT_BASE.xaxis, yaxis: { ...LAYOUT_BASE.yaxis, title: 'Scalar' },
      showlegend: false, height: 300,
      shapes: ev.shapes, annotations: ev.annotations,
    },
  };
}

export function returnDistribution(result) {
  const equity = result.mtm.columns.equity_index || [];
  const dailyRets = [];
  for (let i = 1; i < equity.length; i++) {
    if (equity[i] != null && equity[i - 1] != null && equity[i - 1] !== 0) {
      dailyRets.push(equity[i] / equity[i - 1] - 1);
    }
  }

  function stats(arr) {
    if (!arr.length) return {};
    const mean = arr.reduce((a, b) => a + b, 0) / arr.length;
    const std = Math.sqrt(arr.reduce((a, b) => a + (b - mean) ** 2, 0) / arr.length);
    const skew = arr.length > 2 && std > 0 ? arr.reduce((a, b) => a + ((b - mean) / std) ** 3, 0) / arr.length : 0;
    const kurt = arr.length > 3 && std > 0 ? arr.reduce((a, b) => a + ((b - mean) / std) ** 4, 0) / arr.length - 3 : 0;
    return { mean: (mean * 100).toFixed(3), std: (std * 100).toFixed(3), skew: skew.toFixed(2), kurt: kurt.toFixed(2) };
  }

  const s = stats(dailyRets);

  return {
    data: [{
      type: 'histogram', x: dailyRets, name: 'Daily Returns',
      marker: { color: COLORS.blue }, opacity: 0.7, nbinsx: 60,
    }],
    layout: {
      ...baseLayout(),
      title: { text: 'Daily Return Distribution', font: { color: COLORS.text, size: 14 } },
      xaxis: { ...LAYOUT_BASE.xaxis, title: 'Daily Return', tickformat: '.1%' },
      yaxis: { ...LAYOUT_BASE.yaxis, title: 'Frequency' },
      showlegend: false, height: 340,
      annotations: [{
        x: 0.02, y: 0.95, xref: 'paper', yref: 'paper', showarrow: false, align: 'left',
        font: { color: COLORS.muted, size: 10 },
        text: `μ=${s.mean}bp  σ=${s.std}bp  skew=${s.skew}  kurt=${s.kurt}`,
      }],
    },
  };
}

export function rollingSharpeChart(result, window = 252) {
  const dates = result.mtm.dates;
  const pnl = result.mtm.columns.daily_pnl || [];

  if (pnl.length < window) return null;

  const sharpeDates = [];
  const sharpeVals = [];
  const sqrt252 = Math.sqrt(252);

  for (let i = window - 1; i < pnl.length; i++) {
    const slice = pnl.slice(i - window + 1, i + 1).filter(v => v != null);
    if (slice.length < window * 0.8) continue;
    const mean = slice.reduce((a, b) => a + b, 0) / slice.length;
    const std = Math.sqrt(slice.reduce((a, b) => a + (b - mean) ** 2, 0) / slice.length);
    const sharpe = std > 0 ? (mean / std) * sqrt252 : 0;
    sharpeDates.push(dates[i]);
    sharpeVals.push(parseFloat(sharpe.toFixed(2)));
  }

  const ev = eventShapes(sharpeDates);

  return {
    data: [{
      type: 'scatter', mode: 'lines', x: sharpeDates, y: sharpeVals,
      name: '1Y Rolling Sharpe', line: { color: COLORS.blue, width: 1.5 },
    }],
    layout: {
      ...baseLayout(),
      title: { text: '1-Year Rolling Sharpe', font: { color: COLORS.text, size: 14 } },
      xaxis: LAYOUT_BASE.xaxis, yaxis: { ...LAYOUT_BASE.yaxis, title: 'Sharpe' },
      showlegend: false, height: 340,
      shapes: [
        ...ev.shapes,
        { type: 'line', y0: 0, y1: 0, x0: 0, x1: 1, xref: 'paper', line: { color: COLORS.flat, width: 1 } },
      ],
      annotations: ev.annotations,
    },
  };
}

export function yoyEquityChart(result) {
  const dates = result.mtm.dates;
  const equity = result.mtm.columns.equity_index;

  const byYear = {};
  for (let i = 0; i < dates.length; i++) {
    if (equity[i] == null) continue;
    const yr = dates[i].slice(0, 4);
    if (!byYear[yr]) byYear[yr] = [];
    byYear[yr].push({ date: dates[i], val: equity[i] });
  }

  const years = Object.keys(byYear).sort();
  const palette = [COLORS.blue, COLORS.green, COLORS.amber, COLORS.red, '#8b5cf6', '#06b6d4', '#f97316', '#ec4899', '#84cc16', '#6366f1', '#14b8a6', '#f43f5e'];

  const traces = years.map((yr, idx) => {
    const pts = byYear[yr];
    const start = pts[0].val;
    const jan1 = new Date(parseInt(yr), 0, 1);
    return {
      type: 'scatter', mode: 'lines',
      x: pts.map(p => {
        const d = new Date(p.date);
        return Math.floor((d - jan1) / 86400000);
      }),
      y: pts.map(p => p.val / start),
      name: yr,
      line: { color: palette[idx % palette.length], width: 1.3 },
    };
  });

  return {
    data: traces,
    layout: {
      ...baseLayout(),
      title: { text: 'Year-over-Year Equity (indexed to 1.0 at Jan 1)', font: { color: COLORS.text, size: 14 } },
      xaxis: { ...LAYOUT_BASE.xaxis, title: 'Trading Day of Year', range: [0, 260] },
      yaxis: { ...LAYOUT_BASE.yaxis, title: 'Indexed Return' },
      showlegend: true, height: 420,
      legend: { ...LAYOUT_BASE.legend, font: { color: COLORS.muted, size: 10 } },
      shapes: [{ type: 'line', y0: 1, y1: 1, x0: 0, x1: 1, xref: 'paper', line: { color: COLORS.flat, width: 1 } }],
    },
  };
}

export function yoySharpeChart(splitData) {
  if (!splitData || !splitData.length) return null;

  const yearRows = splitData.filter(r => /^\d{4}$/.test(r.Sample));
  if (!yearRows.length) return null;

  const years = yearRows.map(r => r.Sample);
  const mtmKey = Object.keys(yearRows[0]).find(k => k.includes('MTM') && k.includes('Sharpe'));
  if (!mtmKey) return null;

  const sharpes = yearRows.map(r => {
    const v = parseFloat(r[mtmKey]);
    return isNaN(v) ? 0 : v;
  });

  const colors = sharpes.map(s => s >= 0 ? COLORS.green : COLORS.red);

  return {
    data: [{
      type: 'bar', x: years, y: sharpes,
      marker: { color: colors }, name: 'Sharpe',
    }],
    layout: {
      ...baseLayout(),
      title: { text: 'Sharpe by Year', font: { color: COLORS.text, size: 14 } },
      xaxis: { ...LAYOUT_BASE.xaxis, type: 'category' },
      yaxis: { ...LAYOUT_BASE.yaxis, title: 'Sharpe Ratio' },
      showlegend: false, height: 320,
      shapes: [{ type: 'line', y0: 0, y1: 0, x0: 0, x1: 1, xref: 'paper', line: { color: COLORS.flat, width: 1 } }],
    },
  };
}

export function performanceTable(splitData) {
  if (!splitData || !splitData.length) return [];

  const formatVal = (key, val) => {
    if (val == null || val === '' || val === 'nan') return '—';
    const n = parseFloat(val);
    if (isNaN(n)) return val;
    if (key.includes('Sharpe') || key.includes('Correlation')) return n.toFixed(2);
    if (key.includes('PnL') || key.includes('Capital')) return `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    return `${(n * 100).toFixed(1)}%`;
  };

  return splitData.map(row => {
    const out = {};
    for (const [k, v] of Object.entries(row)) {
      out[k] = k === 'Sample' ? v : formatVal(k, v);
    }
    return out;
  });
}
