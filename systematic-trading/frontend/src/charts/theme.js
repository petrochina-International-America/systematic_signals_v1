export const COLORS = {
  bg: '#0f1117',
  surface: '#12151f',
  surfaceAlt: '#1a1d27',
  border: '#2d3142',
  text: '#d4dae6',
  muted: '#9ba3b2',
  blue: '#378ADD',
  amber: '#EF9F27',
  green: '#639922',
  red: '#E24B4A',
  flat: '#2d3142',
};

export const LAYOUT_BASE = {
  paper_bgcolor: COLORS.surface,
  plot_bgcolor: COLORS.surface,
  font: { family: 'Inter, system-ui, sans-serif', color: COLORS.muted, size: 12 },
  margin: { l: 50, r: 30, t: 40, b: 40 },
  xaxis: { showgrid: true, gridcolor: '#1e2235', zeroline: false, tickfont: { color: '#6b7280' } },
  yaxis: { showgrid: true, gridcolor: '#1e2235', zeroline: false, tickfont: { color: '#6b7280' } },
  legend: { bgcolor: 'rgba(0,0,0,0)', bordercolor: 'rgba(0,0,0,0)', font: { color: COLORS.muted } },
  hovermode: 'x unified',
};

export const PLOTLY_CONFIG = { displayModeBar: false, responsive: true, scrollZoom: false };

export const UKRAINE_DATE = '2022-02-24';

export const POSITION_COLORSCALE = [
  [0.0, COLORS.red], [0.33, COLORS.red],
  [0.34, COLORS.flat], [0.66, COLORS.flat],
  [0.67, COLORS.green], [1.0, COLORS.green],
];
