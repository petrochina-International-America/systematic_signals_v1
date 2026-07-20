import { LAYOUT_BASE, COLORS } from './theme';

export function cotChart(cotData, priceData, commodity, maData) {
  const traces = [];

  const cotDates = cotData.dates;
  const mmNet = cotData.columns.mm_net;
  const barColors = mmNet.map(v => v >= 0 ? COLORS.green : COLORS.red);

  traces.push({
    type: 'bar', x: cotDates, y: mmNet, name: 'MM Net',
    marker: { color: barColors }, opacity: 0.6, yaxis: 'y',
  });

  if (maData) {
    traces.push({
      type: 'scatter', mode: 'lines', x: maData.dates, y: maData.columns.ma_fast,
      name: 'MA fast', line: { color: COLORS.blue, width: 1.4 }, yaxis: 'y',
    });
    traces.push({
      type: 'scatter', mode: 'lines', x: maData.dates, y: maData.columns.ma_slow,
      name: 'MA slow', line: { color: COLORS.muted, width: 1.4, dash: 'dash' }, yaxis: 'y',
    });
  }

  if (priceData && priceData.data && priceData.data.length) {
    traces.push({
      type: 'scatter', mode: 'lines',
      x: priceData.data.map(r => r.date), y: priceData.data.map(r => r.close),
      name: `${commodity} Price`, line: { color: COLORS.amber, width: 1.5 }, yaxis: 'y2',
    });
  }

  const layout = {
    ...LAYOUT_BASE,
    title: { text: `${commodity} — MM Positioning vs Price`, font: { color: COLORS.text, size: 14 } },
    yaxis: { ...LAYOUT_BASE.yaxis, title: 'MM Net Contracts' },
    yaxis2: { showgrid: false, tickfont: { color: '#6b7280' }, title: 'Price', overlaying: 'y', side: 'right' },
  };

  return { data: traces, layout };
}

export function sentimentChart(siData, thresholdPct, commodity) {
  const traces = [{
    type: 'scatter', mode: 'lines',
    x: siData.dates, y: siData.columns.sentiment_index,
    name: 'Sentiment Index', line: { color: COLORS.blue, width: 1.5 },
  }];

  const layout = {
    ...LAYOUT_BASE,
    title: { text: `${commodity} — Sentiment Index (Fade the Crowd)`, font: { color: COLORS.text, size: 14 } },
    yaxis: { ...LAYOUT_BASE.yaxis, title: 'SI (0–100)', range: [-5, 105] },
    showlegend: false,
    shapes: [
      { type: 'rect', y0: 0, y1: thresholdPct, x0: 0, x1: 1, xref: 'paper', fillcolor: 'rgba(99,153,34,0.10)', line: { width: 0 }, layer: 'below' },
      { type: 'rect', y0: 100 - thresholdPct, y1: 100, x0: 0, x1: 1, xref: 'paper', fillcolor: 'rgba(226,75,74,0.10)', line: { width: 0 }, layer: 'below' },
      { type: 'line', y0: thresholdPct, y1: thresholdPct, x0: 0, x1: 1, xref: 'paper', line: { color: COLORS.green, width: 1, dash: 'dot' } },
      { type: 'line', y0: 100 - thresholdPct, y1: 100 - thresholdPct, x0: 0, x1: 1, xref: 'paper', line: { color: COLORS.red, width: 1, dash: 'dot' } },
    ],
    annotations: [
      { y: thresholdPct, x: 1, xref: 'paper', text: `buy < ${thresholdPct}`, showarrow: false, font: { color: COLORS.green, size: 10 } },
      { y: 100 - thresholdPct, x: 1, xref: 'paper', text: `sell > ${100 - thresholdPct}`, showarrow: false, font: { color: COLORS.red, size: 10 } },
    ],
  };

  return { data: traces, layout };
}

export function percentileHistogram(cotData, commodity) {
  const ranks = cotData.columns.percentile_rank.filter(v => v != null);
  const latest = ranks.length ? ranks[ranks.length - 1] : null;

  const traces = [{
    type: 'histogram', x: ranks, nbinsx: 20,
    marker: { color: COLORS.blue }, opacity: 0.7, name: '52w percentile rank',
  }];

  const layout = {
    ...LAYOUT_BASE,
    title: { text: `${commodity} — Positioning Percentile Distribution`, font: { color: COLORS.text, size: 14 } },
    xaxis: { ...LAYOUT_BASE.xaxis, title: '52-week percentile rank' },
    yaxis: { ...LAYOUT_BASE.yaxis, title: 'Weeks' },
    showlegend: false, bargap: 0.05,
    shapes: latest != null ? [{
      type: 'line', x0: latest, x1: latest, y0: 0, y1: 1, yref: 'paper',
      line: { color: COLORS.amber, width: 2 },
    }] : [],
    annotations: latest != null ? [{
      x: latest, y: 1, yref: 'paper', text: `now: ${Math.round(latest)}th`,
      showarrow: false, font: { color: COLORS.amber, size: 10 },
    }] : [],
  };

  return { data: traces, layout };
}
