import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import Plot from 'react-plotly.js';
import { useApi } from '../hooks/useApi';
import { fetchLevelsProximity } from '../api/client';
import { COLORS, PLOTLY_CONFIG } from '../charts/theme';
import Loading, { ErrorNote } from '../components/Loading';

const DIR_COLORS = { Long: COLORS.green, Short: COLORS.red, Flat: COLORS.muted };
const STRAT_COLORS = { 'Trend Following': COLORS.blue, Carry: COLORS.amber, 'Mean Reversion': '#a78bfa' };

/**
 * Plotly draws its SVG at a fixed pixel width measured at mount/last-resize
 * time. When the surrounding CSS grid changes (e.g. a column is hidden) with
 * no browser window resize event, the chart is left stuck at its old,
 * narrower width. Every <Plot> already has `useResizeHandler`, which resizes
 * on a genuine window resize event — so watch the wrapping element and
 * dispatch one whenever its box actually changes, instead of importing
 * plotly.js again just to call Plotly.Plots.resize ourselves.
 */
function useChartAutoResize() {
  const wrapRef = useRef(null);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      window.dispatchEvent(new Event('resize'));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return wrapRef;
}

// Spread (mean-reversion) cards are hidden from the scanner for now — the
// Spreads panel and any spread entries in the Near-Trigger / Recent-Signals
// banners. Methodology and backend stay intact; flip to true to restore.
const SHOW_SPREADS = false;

const OUTRIGHT_ORDER = [
  'WTI', 'Brent', 'Dubai',
  'HTT', 'WTI Midland', 'YV', 'DAB',
  'TDL', 'WDF',
  'RBOB', 'ULSD', 'Gasoil',
  'Propane', 'Ethane', 'Butane', 'Natgas',
];
const SPREAD_ORDER = [
  'WTI − Brent', 'Brent − Dubai',
  'Brent − RBOB', 'Brent − ULSD', 'ULSD − WTI',
  'Propane − Ethane', 'Propane − Butane', 'RBOB − Butane',
  'Ethane − Natgas',
];

// Popups render into a portal at document.body instead of as a CSS-hover
// child, so they're never clipped by an ancestor's `overflow: hidden` (every
// tip here lives inside a .scanner-card, which clips for the rounded chart
// corners). Position is computed from the icon's own bounding box, opening
// to the left where the wide chart area gives it room to render whole.
function Tip({ text, up }) {
  const [pos, setPos] = useState(null);
  const wrapRef = useRef(null);
  const POPUP_W = 260;
  const GAP = 8;

  function handleEnter() {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    let left = rect.left - POPUP_W - GAP;
    if (left < GAP) left = Math.min(rect.right + GAP, window.innerWidth - POPUP_W - GAP);
    setPos(up
      ? { left, bottom: window.innerHeight - rect.top + GAP }
      : { left, top: rect.bottom + GAP });
  }

  return (
    <span className="tip-wrap" ref={wrapRef} onMouseEnter={handleEnter} onMouseLeave={() => setPos(null)}>
      <span className="tip-icon">?</span>
      {pos && createPortal(
        <div className="tip-popup--portal" style={{ position: 'fixed', width: POPUP_W, ...pos }}>
          {text}
        </div>,
        document.body
      )}
    </span>
  );
}

/* ── Drill confirmation ── */

// Cards are huge click targets and drilling in kicks off the backtest
// computation, so an accidental click is expensive — catch it with a tiny
// confirm first. Enter/click Yes proceeds; Esc/click-away cancels.
function ConfirmDrill({ target, onYes, onNo }) {
  useEffect(() => {
    const onKey = e => {
      if (e.key === 'Escape') onNo();
      if (e.key === 'Enter') onYes();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onYes, onNo]);

  return createPortal(
    <div className="confirm-overlay" onClick={onNo}>
      <div className="confirm-box" onClick={e => e.stopPropagation()}>
        <div className="confirm-box__text">
          Examine <b>{target}</b> backtest?
        </div>
        <div className="confirm-box__actions">
          <button type="button" className="confirm-box__btn confirm-box__btn--yes" onClick={onYes} autoFocus>
            Yes
          </button>
          <button type="button" className="confirm-box__btn" onClick={onNo}>
            No
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

/* ── Banners ── */

function fmtDate(d) {
  if (!d) return '';
  const dt = new Date(d + 'T00:00:00');
  return dt.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
}

function HotBanner({ signals, onDrill }) {
  if (!signals?.length) return null;
  return (
    <div className="scanner-banner scanner-banner--hot">
      <div className="scanner-banner__label">Near Trigger</div>
      <div className="scanner-banner__cards">
        {signals.map((s, i) => (
          <button key={i} className="scanner-pill" onClick={() => onDrill(s.commodity, s.strategy)}>
            <span className="scanner-pill__name">{s.commodity}</span>
            <span className="scanner-pill__dir" style={{ color: DIR_COLORS[s.direction] }}>{s.direction}</span>
            <span className="scanner-pill__detail">{s.detail}{s.current != null ? ` · now $${s.current}` : ''}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function TradesBanner({ trades, onDrill }) {
  if (!trades?.length) return null;
  return (
    <div className="scanner-banner scanner-banner--trades">
      <div className="scanner-banner__label">Recent Signals</div>
      <div className="scanner-banner__cards">
        {trades.map((t, i) => (
          <button key={i} className="scanner-pill" onClick={() => onDrill(t.commodity, t.strategy)}>
            <span className="scanner-pill__name">{t.commodity}</span>
            <span className="scanner-pill__flip">
              <span style={{ color: DIR_COLORS[t.from] }}>{t.from}</span>
              {' → '}
              <span style={{ color: DIR_COLORS[t.to] }}>{t.to}</span>
            </span>
            <span className="scanner-pill__detail">
              {t.tier && <>{t.tier} · </>}
              {t.price != null && <>@ ${t.price}</>}
              {t.date && <> · {fmtDate(t.date)}</>}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ── Info panel ── */

function signedColor(v) {
  return v > 0 ? COLORS.green : v < 0 ? COLORS.red : COLORS.muted;
}

function CommodityInfo({ cta, cot, priceInput, onPriceChange, onPriceApply, tenorLabel, liveCta }) {
  const liveColor = '#06b6d4';
  const dir = cta?.direction || 'Flat';
  const dirColor = DIR_COLORS[dir];
  const posPct = cta?.position_pct ?? 0;
  const posPctPrev = cta?.position_pct_prev ?? null;
  const posColor = signedColor(posPct);
  const maxMag = cta?.vol_scalar != null ? Math.round(cta.vol_scalar * 100) : null;
  const maxSigned = maxMag != null ? (posPct < 0 ? -maxMag : maxMag) : null;
  const maxLabel = posPct < 0 ? 'Max Short' : posPct > 0 ? 'Max Long' : 'Max ±';
  // How much of the vol-target cap the current position is using, e.g.
  // +4.7% position / 24% max -> 20%. Signed variant keeps direction (needed
  // to size Daily Chg correctly across a Long<->Short flip); the unsigned
  // utilPct is what's displayed as the headline — direction is read off
  // Composite / the color, not this number.
  const utilSigned = maxMag ? Math.round((posPct / maxMag) * 100) : null;
  const utilPct = utilSigned != null ? Math.abs(utilSigned) : null;
  const utilSignedPrev = maxMag != null && posPctPrev != null
    ? Math.round((posPctPrev / maxMag) * 100)
    : null;
  const [breakdownOpen, setBreakdownOpen] = useState(false);
  const [chgOpen, setChgOpen] = useState(false);

  const cotPct = cot?.percentile;
  const cotPending = cot?.pending;
  let cotLabel, cotColor;
  if (cotPct != null && !cotPending) {
    if (cotPct >= 75) { cotLabel = `Crowded (${cotPct}th)`; cotColor = COLORS.red; }
    else if (cotPct <= 25) { cotLabel = `Washed (${cotPct}th)`; cotColor = COLORS.green; }
    else { cotLabel = `Neutral (${cotPct}th)`; cotColor = COLORS.muted; }
  }

  return (
    <div className="scanner-info">
      <div className="scanner-info__row" onClick={e => e.stopPropagation()}>
        <span className="scanner-info__label">Live{tenorLabel ? ` ${tenorLabel}` : ' Price'}</span>
        <input
          type="text"
          value={priceInput}
          onChange={e => onPriceChange(e.target.value)}
          onBlur={onPriceApply}
          onKeyDown={e => { if (e.key === 'Enter') { onPriceApply(); e.target.blur(); } }}
          style={{
            width: 56, background: 'var(--surface-alt)', border: '1px solid var(--border)',
            color: liveCta != null ? liveColor : 'var(--text)', borderRadius: 4,
            padding: '2px 6px', fontSize: 12, textAlign: 'right', outline: 'none',
          }}
        />
      </div>
      {liveCta != null && (() => {
        // Identical arithmetic to the Position readout: sized position over
        // the (rounded) cap — so at the actual current price this shows the
        // same number as Position.
        const liveUtil = maxMag
          ? Math.round((liveCta.posPct / maxMag) * 100)
          : Math.round(liveCta.net * 100);
        const liveMaxLabel = liveCta.posPct < 0 ? 'Max Short' : liveCta.posPct > 0 ? 'Max Long' : 'Max ±';
        const liveMaxSigned = maxMag != null ? (liveCta.posPct < 0 ? -maxMag : maxMag) : null;
        return <>
          <div className="scanner-info__row">
            <span className="scanner-info__label">
              Live Pos
              <Tip text="What-if at your entered price: strategies re-signed against today's levels, sized and divided by the same Max cap as Position — the two percentages compare directly. Sign shows direction." />
            </span>
            <span className="scanner-info__value" style={{ color: liveColor }}>
              {liveUtil > 0 ? '+' : ''}{liveUtil}%
            </span>
          </div>
          <div className="scanner-info__breakdown">
            <div className="scanner-info__breakdown-row">
              <span className="scanner-info__breakdown-label">Position</span>
              <span className="scanner-info__breakdown-value" style={{ color: liveColor }}>
                {liveCta.posPct > 0 ? '+' : ''}{liveCta.posPct}%
              </span>
            </div>
            {liveMaxSigned != null && (
              <div className="scanner-info__breakdown-row">
                <span className="scanner-info__breakdown-label">{liveMaxLabel}</span>
                <span className="scanner-info__breakdown-value" style={{ color: liveColor }}>
                  {liveMaxSigned > 0 ? '+' : ''}{liveMaxSigned}%
                </span>
              </div>
            )}
          </div>
        </>;
      })()}
      <div className="scanner-info__row">
        <span className="scanner-info__label">
          Composite
          <Tip text={<>Raw signal direction: inverse-vol risk parity across 4 trend speeds + carry, before any position sizing. Slower strategies get more weight.</>} />
        </span>
        <span className="scanner-info__value" style={{ color: dirColor }}>{dir}</span>
      </div>
      <div className="scanner-info__row">
        <span className="scanner-info__label">
          Position
          <Tip text="Share of the vol-target cap in use — 100% would mean every strategy agrees at full conviction. Click the number for the breakdown." />
        </span>
        {utilPct != null ? (
          <button
            type="button"
            className="scanner-info__value scanner-info__value--btn"
            style={{ color: posColor }}
            onClick={e => { e.stopPropagation(); setBreakdownOpen(o => !o); }}
          >
            {utilPct}%
            <span className="scanner-info__chevron">{breakdownOpen ? '▾' : '▸'}</span>
          </button>
        ) : (
          <span className="scanner-info__value" style={{ color: posColor }}>
            {posPct > 0 ? '+' : ''}{posPct}%
          </span>
        )}
      </div>
      {breakdownOpen && utilPct != null && (
        <div className="scanner-info__breakdown">
          <div className="scanner-info__breakdown-row">
            <span className="scanner-info__breakdown-label">
              Position
              <Tip text="Actual sized position: Composite scaled down by the vol-target overlay. What we'd actually trade, not just the raw signal direction." />
            </span>
            <span className="scanner-info__breakdown-value" style={{ color: posColor }}>
              {posPct > 0 ? '+' : ''}{posPct}%
            </span>
          </div>
          <div className="scanner-info__breakdown-row">
            <span className="scanner-info__breakdown-label">
              {maxLabel}
              <Tip text="Vol-target cap: 15% risk target ÷ this commodity's realized vol. Lower-vol names get a bigger cap; Position can't exceed this." />
            </span>
            <span className="scanner-info__breakdown-value" style={{ color: signedColor(maxSigned) }}>
              {maxSigned > 0 ? '+' : ''}{maxSigned}%
            </span>
          </div>
        </div>
      )}
      {posPctPrev != null && (() => {
        // Same util-of-cap basis as Position above, so the two numbers read
        // consistently; falls back to raw position % if no cap data.
        const chgToday = utilSigned != null ? utilSigned : posPct;
        const chgYesterday = utilSignedPrev != null ? utilSignedPrev : posPctPrev;
        const chg = chgToday - chgYesterday;
        const chgColor = chg === 0 ? COLORS.muted : signedColor(chg);
        return <>
          <div className="scanner-info__row">
            <span className="scanner-info__label">
              Daily Chg
              <Tip text="Change vs yesterday, on the same cap-utilization basis as Position. Click the number for the breakdown." />
            </span>
            <button
              type="button"
              className="scanner-info__value scanner-info__value--btn"
              style={{ color: chgColor }}
              onClick={e => { e.stopPropagation(); setChgOpen(o => !o); }}
            >
              {chg > 0 ? '+' : ''}{chg}%
              <span className="scanner-info__chevron">{chgOpen ? '▾' : '▸'}</span>
            </button>
          </div>
          {chgOpen && (
            <div className="scanner-info__breakdown">
              <div className="scanner-info__breakdown-row">
                <span className="scanner-info__breakdown-label">Today</span>
                <span className="scanner-info__breakdown-value" style={{ color: signedColor(chgToday) }}>
                  {chgToday > 0 ? '+' : ''}{chgToday}%
                </span>
              </div>
              <div className="scanner-info__breakdown-row">
                <span className="scanner-info__breakdown-label">Yesterday</span>
                <span className="scanner-info__breakdown-value" style={{ color: signedColor(chgYesterday) }}>
                  {chgYesterday > 0 ? '+' : ''}{chgYesterday}%
                </span>
              </div>
            </div>
          )}
        </>;
      })()}
      {cotPct != null && !cotPending && (
        <div className="scanner-info__row">
          <span className="scanner-info__label">
            COT
            <Tip up text="Managed Money positioning percentile. Crowded (>75th) = reversal risk. Washed (<25th) = room to run." />
          </span>
          <span className="scanner-info__value" style={{ color: cotColor }}>
            {cotLabel}
          </span>
        </div>
      )}
    </div>
  );
}

function SpreadInfo({ data, priceInput, onPriceChange, onPriceApply, overrideZ }) {
  const { zscore, direction, in_trade, lookback, threshold, signal_prev } = data;
  const dirColor = DIR_COLORS[direction] || COLORS.muted;
  const lbLabel = lookback != null ? `${lookback}-day` : 'rolling';
  const thLabel = threshold != null ? `±${threshold}σ` : 'the entry band';
  const sigPct = direction === 'Long' ? 100 : direction === 'Short' ? -100 : 0;
  const prevPct = signal_prev != null ? Math.round(signal_prev * 100) : null;
  const fmtPct = v => `${v > 0 ? '+' : ''}${v}%`;
  const liveColor = '#06b6d4';
  return (
    <div className="scanner-info">
      <div className="scanner-info__row" onClick={e => e.stopPropagation()}>
        <span className="scanner-info__label">Live Price</span>
        <input
          type="text"
          value={priceInput}
          onChange={e => onPriceChange(e.target.value)}
          onBlur={onPriceApply}
          onKeyDown={e => { if (e.key === 'Enter') { onPriceApply(); e.target.blur(); } }}
          style={{
            width: 56, background: 'var(--surface-alt)', border: '1px solid var(--border)',
            color: overrideZ != null ? liveColor : 'var(--text)', borderRadius: 4,
            padding: '2px 6px', fontSize: 12, textAlign: 'right', outline: 'none',
          }}
        />
      </div>
      {overrideZ != null && (
        <div className="scanner-info__row">
          <span className="scanner-info__label">Live Z</span>
          <span className="scanner-info__value" style={{ color: liveColor }}>
            {overrideZ >= 0 ? '+' : ''}{overrideZ.toFixed(2)}σ
          </span>
        </div>
      )}
      <div className="scanner-info__row">
        <span className="scanner-info__label">
          Mean Reversion
          <Tip text={`Mean reversion on the ${lbLabel} rolling spread. Entry at ${thLabel}, exit when spread reverts back inside ${thLabel}.`} />
        </span>
        <span className="scanner-info__value" style={{ color: dirColor }}>{direction}</span>
      </div>
      <div className="scanner-info__row">
        <span className="scanner-info__label">
          Z-Score
          <Tip text="σ from rolling mean. Positive = rich (short), negative = cheap (long)." />
        </span>
        <span className="scanner-info__value">{zscore != null ? `${zscore}σ` : '—'}</span>
      </div>
      {prevPct != null && (
        <div className="scanner-info__row">
          <span className="scanner-info__label">
            Daily Chg
            <Tip text="Position today vs yesterday: +100% = full long, -100% = full short, 0% = flat." up />
          </span>
          <span className="scanner-info__chgline" style={{ color: signedColor(sigPct) }}>
            {fmtPct(sigPct)} (Tdy)
          </span>
          <span className="scanner-info__chgarrow" style={{ color: sigPct === prevPct ? COLORS.muted : signedColor(sigPct) }}>
            {sigPct > prevPct ? '↑' : sigPct < prevPct ? '↓' : '→'}
          </span>
          <span className="scanner-info__chgline" style={{ color: signedColor(prevPct) }}>
            {fmtPct(prevPct)} (Yest)
          </span>
        </div>
      )}
      {in_trade && (
        <div className="scanner-info__row">
          <span className="scanner-info__tag" style={{ backgroundColor: dirColor + '33', color: dirColor, borderColor: dirColor + '66' }}>In Trade</span>
        </div>
      )}
    </div>
  );
}

function SpreadExposureChart({ dates, history, zscores }) {
  const hasData = history && history.some(v => v != null && v !== 0);
  if (!hasData) return null;

  const posVals = history.map(v => (v == null ? null : Math.max(v, 0)));
  const negVals = history.map(v => (v == null ? null : Math.min(v, 0)));
  const hoverLabels = history.map((v, i) => {
    if (v == null) return '';
    const dir = v > 0 ? 'Long' : v < 0 ? 'Short' : 'Flat';
    const pct = Math.round(v * 100);
    const pctStr = `${pct > 0 ? '+' : ''}${pct}%`;
    const z = zscores?.[i];
    return z != null ? `${dir} ${pctStr}  z=${z}σ` : `${dir} ${pctStr}`;
  });

  let latest = null;
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i] != null) { latest = history[i]; break; }
  }
  const latestLabel = latest == null ? '' : latest > 0 ? 'Long' : latest < 0 ? 'Short' : 'Flat';
  const latestColor = DIR_COLORS[latestLabel] || COLORS.muted;

  const traces = [
    {
      type: 'scatter', mode: 'lines', x: dates, y: posVals,
      line: { color: COLORS.green, width: 1 }, fill: 'tozeroy',
      fillcolor: 'rgba(34,197,94,0.55)', connectgaps: false,
      hoverinfo: 'skip', showlegend: false,
    },
    {
      type: 'scatter', mode: 'lines', x: dates, y: negVals,
      line: { color: COLORS.red, width: 1 }, fill: 'tozeroy',
      fillcolor: 'rgba(239,68,68,0.55)', connectgaps: false,
      hoverinfo: 'skip', showlegend: false,
    },
    {
      type: 'scatter', mode: 'lines', x: dates, y: history,
      line: { color: 'rgba(0,0,0,0)', width: 0 }, connectgaps: false,
      customdata: hoverLabels, showlegend: false,
      hovertemplate: '%{customdata}<extra></extra>',
    },
  ];

  const layout = {
    paper_bgcolor: COLORS.surface, plot_bgcolor: COLORS.surface,
    font: { family: 'Inter, system-ui, sans-serif', color: COLORS.muted, size: 9 },
    margin: { l: 24, r: 76, t: 4, b: 4 },
    hovermode: 'closest', hoverdistance: 20, showlegend: false,
    hoverlabel: { bgcolor: COLORS.surfaceAlt, bordercolor: COLORS.border, font: { color: COLORS.text, size: 11 } },
    xaxis: { showgrid: false, zeroline: false, showticklabels: false },
    yaxis: {
      showgrid: true, gridcolor: '#1e2235', zeroline: true, zerolinecolor: '#3a4058',
      tickfont: { color: '#5a6278', size: 8 },
      range: [-1.3, 1.3], tickvals: [-1, 0, 1], ticktext: ['Short', '0', 'Long'],
    },
    annotations: latest == null || latest === 0 ? [] : [{
      x: 1, xref: 'paper', xanchor: 'left', y: latest, yref: 'y', yanchor: 'middle',
      text: `${latestLabel} ${latest > 0 ? '+' : ''}${Math.round(latest * 100)}%`,
      font: { color: latestColor, size: 10, weight: 700 },
      showarrow: false, bgcolor: 'rgba(15,17,23,0.85)', borderpad: 2,
    }],
    height: 70,
  };

  return (
    <Plot data={traces} layout={layout} config={PLOTLY_CONFIG}
      style={{ width: '100%', height: 70 }} useResizeHandler />
  );
}

/* ── Charts ── */

function ExposureChart({ dates, history }) {
  const hasData = history && history.some(v => v != null);
  if (!hasData) return null;

  const posVals = history.map(v => (v == null ? null : Math.max(v, 0)));
  const negVals = history.map(v => (v == null ? null : Math.min(v, 0)));

  let latest = null;
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i] != null) { latest = history[i]; break; }
  }
  const latestLabel = latest == null ? '' : latest > 0 ? 'Long' : latest < 0 ? 'Short' : 'Flat';
  const latestColor = latest == null ? COLORS.muted : DIR_COLORS[latestLabel];

  const allVals = history.filter(v => v != null);
  const maxAbs = allVals.length ? Math.max(50, ...allVals.map(v => Math.abs(v))) : 50;
  const dirLabels = history.map(v => (v == null ? '' : v > 0 ? 'Long' : v < 0 ? 'Short' : 'Flat'));

  const traces = [
    {
      type: 'scatter', mode: 'lines', x: dates, y: posVals,
      line: { color: COLORS.green, width: 1 }, fill: 'tozeroy',
      fillcolor: 'rgba(34,197,94,0.55)', connectgaps: false,
      hoverinfo: 'skip', showlegend: false,
    },
    {
      type: 'scatter', mode: 'lines', x: dates, y: negVals,
      line: { color: COLORS.red, width: 1 }, fill: 'tozeroy',
      fillcolor: 'rgba(239,68,68,0.55)', connectgaps: false,
      hoverinfo: 'skip', showlegend: false,
    },
    {
      type: 'scatter', mode: 'lines', x: dates, y: history,
      line: { color: 'rgba(0,0,0,0)', width: 0 }, connectgaps: false,
      customdata: dirLabels, showlegend: false,
      hovertemplate: '%{customdata} %{y:+.1f}%<extra></extra>',
    },
  ];

  const layout = {
    paper_bgcolor: COLORS.surface, plot_bgcolor: COLORS.surface,
    font: { family: 'Inter, system-ui, sans-serif', color: COLORS.muted, size: 9 },
    margin: { l: 32, r: 76, t: 4, b: 4 },
    hovermode: 'closest', hoverdistance: 20, showlegend: false,
    hoverlabel: { bgcolor: COLORS.surfaceAlt, bordercolor: COLORS.border, font: { color: COLORS.text, size: 11 } },
    xaxis: { showgrid: false, zeroline: false, showticklabels: false },
    yaxis: {
      showgrid: true, gridcolor: '#1e2235', zeroline: true, zerolinecolor: '#3a4058',
      tickfont: { color: '#5a6278', size: 8 }, ticksuffix: '%',
      range: [-maxAbs * 1.15, maxAbs * 1.15],
    },
    annotations: latest == null ? [] : [{
      x: 1, xref: 'paper', xanchor: 'left', y: latest, yref: 'y', yanchor: 'middle',
      text: `${latestLabel} ${latest > 0 ? '+' : ''}${latest}%`,
      font: { color: latestColor, size: 10, weight: 700 },
      showarrow: false, bgcolor: 'rgba(15,17,23,0.85)', borderpad: 2,
    }],
    height: 90,
  };

  return (
    <Plot data={traces} layout={layout} config={PLOTLY_CONFIG}
      style={{ width: '100%', height: 90 }} useResizeHandler />
  );
}

function CommodityChart({ card, tenor, tenorPending, onTenorChange, baseRank = 1, onClick }) {
  const { commodity, dates, prices, ma_levels, carry, cta, cot } = card;
  const isHot = (card.closest_dist ?? 999) < 2;
  const [hoveredLevel, setHoveredLevel] = useState(null);
  const chartWrapRef = useChartAutoResize();

  const [priceOverride, setPriceOverride] = useState(null);
  const [priceInput, setPriceInput] = useState(() => {
    for (let i = prices.length - 1; i >= 0; i--) {
      if (prices[i] != null) return prices[i].toFixed(2);
    }
    return '';
  });

  function applyOverride() {
    const val = parseFloat(priceInput);
    setPriceOverride(isNaN(val) ? null : val);
  }

  // What-if position at the override price: re-sign each strategy against
  // today's levels using the server's inverse-vol weights. Returns the sized
  // position (net × vol scalar, like cta.position_pct) plus the raw net —
  // CommodityInfo divides by the same Max Long the Position readout uses, so
  // the two percentages are directly comparable.
  const liveCta = useMemo(() => {
    if (priceOverride == null || !cta?.weights) return null;
    let net = 0;
    let total = 0;
    for (const l of ma_levels) {
      const w = cta.weights[l.label];
      if (w == null) continue;
      net += w * (priceOverride > l.value ? 1 : -1);
      total += w;
    }
    if (cta.weights.Carry != null && carry?.level?.value != null) {
      net += cta.weights.Carry * (priceOverride > carry.level.value ? 1 : -1);
      total += cta.weights.Carry;
    }
    if (!total) return null;
    return {
      net,
      posPct: Math.round(net * (cta.vol_scalar ?? 1) * 1000) / 10,
    };
  }, [priceOverride, ma_levels, carry, cta]);

  const allLevels = useMemo(() => {
    const levels = ma_levels.map(l => ({
      val: l.value, label: l.label || l.tier, color: l.color, history: l.history || null,
    }));
    if (carry?.level) levels.push({
      val: carry.level.value, label: 'Carry', color: COLORS.amber, history: carry.level.history || null,
    });
    levels.sort((a, b) => b.val - a.val);
    return levels;
  }, [ma_levels, carry]);

  const validPrices = prices.filter(v => v != null);
  const priceRange = validPrices.length ? Math.max(...validPrices) - Math.min(...validPrices) : 1;
  const minGap = priceRange * 0.06;
  const labelYs = allLevels.map(l => l.val);
  for (let i = 1; i < labelYs.length; i++) {
    if (labelYs[i - 1] - labelYs[i] < minGap) labelYs[i] = labelYs[i - 1] - minGap;
  }

  const traces = [{
    type: 'scatter', mode: 'lines', x: dates, y: prices,
    name: commodity, line: { color: '#f1f5f9', width: 2.5 },
    hovertemplate: '$%{y:.2f}<extra></extra>',
  }];

  const annotations = [];

  for (let i = 0; i < allLevels.length; i++) {
    const lev = allLevels[i];
    const isHovered = hoveredLevel === lev.label;
    // Plot the level's actual historical trajectory when available, instead
    // of projecting today's value flat across the whole window. Falls back
    // to a flat 2-point line if history is missing/misaligned.
    const hasHistory = Array.isArray(lev.history) && lev.history.length === dates.length;
    const xVals = hasHistory ? dates : [dates[0], dates[dates.length - 1]];
    const yVals = hasHistory ? lev.history : [lev.val, lev.val];
    traces.push({
      type: 'scatter', mode: 'lines',
      x: xVals, y: yVals,
      name: lev.label,
      meta: { levelLabel: lev.label },
      line: { color: lev.color, width: isHovered ? 2.5 : 1, dash: isHovered ? 'solid' : 'dot' },
      opacity: isHovered ? 1 : 0.7,
      connectgaps: true,
      showlegend: false,
      hovertemplate: `<b>${lev.label}</b>  $%{y:.2f}<extra></extra>`,
    });
    // Tick marking today's level, so it stays obvious even as the line moves.
    traces.push({
      type: 'scatter', mode: 'markers',
      x: [dates[dates.length - 1]], y: [lev.val],
      marker: {
        color: lev.color, size: isHovered ? 9 : 7, symbol: 'circle',
        line: { color: COLORS.surface, width: 1.5 },
      },
      meta: { levelLabel: lev.label },
      showlegend: false,
      hovertemplate: `<b>${lev.label}</b> (today)  $${lev.val.toFixed(2)}<extra></extra>`,
    });
    annotations.push({
      x: 1, xref: 'paper', xanchor: 'left', y: labelYs[i], yref: 'y', yanchor: 'middle',
      text: `${lev.label} $${lev.val.toFixed(2)}`,
      font: { color: lev.color, size: isHovered ? 11 : 9, weight: isHovered ? 700 : 400 },
      opacity: isHovered ? 1 : 0.8,
      showarrow: false, bgcolor: 'rgba(15,17,23,0.85)', borderpad: 2,
    });
  }

  if (priceOverride != null) {
    const liveColor = '#06b6d4';
    traces.push({
      type: 'scatter', mode: 'lines',
      x: [dates[0], dates[dates.length - 1]],
      y: [priceOverride, priceOverride],
      line: { color: liveColor, width: 1.5, dash: 'dot' },
      showlegend: false,
      hovertemplate: `<b>Live</b>  $${priceOverride.toFixed(2)}<extra></extra>`,
    });
    annotations.push({
      x: 1, xref: 'paper', xanchor: 'left', y: priceOverride, yref: 'y', yanchor: 'middle',
      text: `Live $${priceOverride.toFixed(2)}`,
      font: { color: liveColor, size: 10, weight: 700 },
      showarrow: false, bgcolor: 'rgba(15,17,23,0.85)', borderpad: 2,
    });
  }

  const layout = {
    paper_bgcolor: COLORS.surface, plot_bgcolor: COLORS.surface,
    font: { family: 'Inter, system-ui, sans-serif', color: COLORS.muted, size: 11 },
    margin: { l: 50, r: 100, t: 6, b: 24 },
    hovermode: 'closest', hoverdistance: 30, showlegend: false,
    xaxis: { showgrid: false, zeroline: false, tickfont: { color: '#5a6278', size: 9 }, tickformat: '%b %d' },
    yaxis: { showgrid: true, gridcolor: '#1e2235', zeroline: false, tickfont: { color: '#5a6278', size: 9 }, tickprefix: '$' },
    annotations, height: 280,
  };

  function handleHover(data) {
    const pt = data?.points?.[0];
    const label = pt?.data?.meta?.levelLabel;
    if (label) setHoveredLevel(label);
  }

  function handleUnhover() {
    setHoveredLevel(null);
  }

  return (
    <div className={`scanner-card${isHot ? ' scanner-card--hot' : ''}`} onClick={onClick}>
      <div className="scanner-card__title">
        {commodity}
        {card.tenor_label && (
          <span
            className="scanner-card__tenor"
            title="Contract rank shown. Bal-month products (NGLs, Dubai) start at M2 — their front generic is balance-of-month."
          >
            {card.tenor_label}
          </span>
        )}
        {onTenorChange && (
          <div
            className={`tenor-toggle tenor-toggle--card${tenorPending ? ' tenor-toggle--pending' : ''}`}
            onClick={e => e.stopPropagation()}
          >
            {TENORS.map(n => (
              <button
                key={n}
                type="button"
                className={`tenor-toggle__btn${tenor === n ? ' tenor-toggle__btn--active' : ''}`}
                onClick={() => onTenorChange(n)}
              >
                M{baseRank + n - 1}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="scanner-card__body">
        <div className="scanner-card__chart" ref={chartWrapRef}>
          <Plot data={traces} layout={layout} config={PLOTLY_CONFIG}
            onHover={handleHover} onUnhover={handleUnhover}
            style={{ width: '100%', height: 280 }} useResizeHandler />
          <ExposureChart dates={dates} history={cta?.position_util_history} />
        </div>
        <CommodityInfo
          cta={cta}
          cot={cot}
          priceInput={priceInput}
          onPriceChange={setPriceInput}
          onPriceApply={applyOverride}
          tenorLabel={card.tenor_label}
          liveCta={liveCta}
        />
      </div>
    </div>
  );
}

function SpreadChart({ pair, data, onClick }) {
  const { dates, spread, mean, spread_std, threshold } = data;
  const [hoveredLevel, setHoveredLevel] = useState(null);
  const chartWrapRef = useChartAutoResize();
  const th = threshold != null ? threshold : 1.5;
  const std = spread_std;

  const [priceOverride, setPriceOverride] = useState(null);
  const [priceInput, setPriceInput] = useState(() => {
    for (let i = spread.length - 1; i >= 0; i--) {
      if (spread[i] != null) return spread[i].toFixed(2);
    }
    return '';
  });

  const overrideZ = priceOverride != null && std > 0 && mean != null
    ? (priceOverride - mean) / std
    : null;

  function applyOverride() {
    const val = parseFloat(priceInput);
    setPriceOverride(isNaN(val) ? null : val);
  }

  // Graduated sigma bands: ±1σ (watch), ±entry σ (trigger), ±2σ (extended)
  const levelLines = useMemo(() => {
    if (mean == null || std == null) return [];
    const red1   = 'rgba(226,75,74,0.7)';
    const red15  = COLORS.red;
    const red2   = 'rgba(226,75,74,0.9)';
    const grn1   = 'rgba(99,153,34,0.7)';
    const grn15  = COLORS.green;
    const grn2   = 'rgba(99,153,34,0.9)';

    return [
      { val: mean + 2 * std,   label: '+2σ',          color: red2,  width: 1.2, dash: 'dot',   entry: false },
      { val: mean + th * std,  label: `+${th}σ entry`, color: red15, width: 1.5, dash: 'dash',  entry: true,  entryDir: 'Short' },
      { val: mean + 1 * std,   label: '+1σ',          color: red1,  width: 1.2, dash: 'dot',   entry: false },
      { val: mean,             label: '0σ mean',      color: COLORS.muted, width: 1, dash: 'dot', entry: false },
      { val: mean - 1 * std,   label: '-1σ',          color: grn1,  width: 1.2, dash: 'dot',   entry: false },
      { val: mean - th * std,  label: `-${th}σ entry`, color: grn15, width: 1.5, dash: 'dash',  entry: true,  entryDir: 'Long'  },
      { val: mean - 2 * std,   label: '-2σ',          color: grn2,  width: 1.2, dash: 'dot',   entry: false },
    ];
  }, [mean, std, th]);

  // Anti-overlap annotation positions (sort top-to-bottom, enforce min gap)
  // Show last 45 trading days to zoom in and reduce clutter
  const TRAILING = 45;
  const trailStart = Math.max(0, dates.length - TRAILING);
  const visDates = dates.slice(trailStart);
  const visSpread = spread.slice(trailStart);
  const visSignal = data.signal_history ? data.signal_history.slice(trailStart) : null;
  const visZscores = data.zscore_history ? data.zscore_history.slice(trailStart) : null;

  const validPrices = visSpread.filter(v => v != null);
  const priceRange = validPrices.length ? Math.max(...validPrices) - Math.min(...validPrices) : 1;
  const minGap = priceRange * 0.06;
  const sortedLevels = [...levelLines].sort((a, b) => b.val - a.val);
  const labelYs = sortedLevels.map(l => l.val);
  for (let i = 1; i < labelYs.length; i++) {
    if (labelYs[i - 1] - labelYs[i] < minGap) labelYs[i] = labelYs[i - 1] - minGap;
  }

  const traces = [{
    type: 'scatter', mode: 'lines', x: visDates, y: visSpread,
    name: 'Spread', line: { color: '#f1f5f9', width: 2.5 },
    hovertemplate: '%{y:.4f}<extra></extra>',
  }];

  const annotations = [];

  for (let i = 0; i < sortedLevels.length; i++) {
    const lev = sortedLevels[i];
    const isHovered = hoveredLevel === lev.label;
    traces.push({
      type: 'scatter', mode: 'lines',
      x: [visDates[0], visDates[visDates.length - 1]],
      y: [lev.val, lev.val],
      name: lev.label,
      meta: { levelLabel: lev.label },
      line: { color: lev.color, width: isHovered ? lev.width + 1 : lev.width, dash: isHovered ? 'solid' : lev.dash },
      opacity: isHovered ? 1 : (lev.entry ? 0.95 : 0.85),
      showlegend: false,
      hovertemplate: `<b>${lev.label}</b>  $${lev.val.toFixed(4)}<extra></extra>`,
    });
    const annText = lev.entry
      ? `${lev.label}  ·  ${lev.entryDir} $${lev.val.toFixed(2)}`
      : lev.label;
    annotations.push({
      x: 1, xref: 'paper', xanchor: 'left', y: labelYs[i], yref: 'y', yanchor: 'middle',
      text: annText,
      font: { color: lev.color, size: isHovered ? 11 : (lev.entry ? 10 : 9), weight: isHovered || lev.entry ? 700 : 400 },
      opacity: isHovered ? 1 : (lev.entry ? 1 : 0.85),
      showarrow: false, bgcolor: 'rgba(15,17,23,0.85)', borderpad: 2,
    });
  }

  if (priceOverride != null) {
    const liveColor = '#06b6d4';
    traces.push({
      type: 'scatter', mode: 'lines',
      x: [visDates[0], visDates[visDates.length - 1]],
      y: [priceOverride, priceOverride],
      line: { color: liveColor, width: 1.5, dash: 'dot' },
      showlegend: false,
      hovertemplate: `<b>Live</b>  $${priceOverride.toFixed(2)}<extra></extra>`,
    });
    annotations.push({
      x: 1, xref: 'paper', xanchor: 'left', y: priceOverride, yref: 'y', yanchor: 'middle',
      text: `Live $${priceOverride.toFixed(2)}`,
      font: { color: liveColor, size: 10, weight: 700 },
      showarrow: false, bgcolor: 'rgba(15,17,23,0.85)', borderpad: 2,
    });
  }

  const layout = {
    paper_bgcolor: COLORS.surface, plot_bgcolor: COLORS.surface,
    font: { family: 'Inter, system-ui, sans-serif', color: COLORS.muted, size: 11 },
    margin: { l: 50, r: 155, t: 6, b: 24 },
    hovermode: 'closest', hoverdistance: 30, showlegend: false,
    xaxis: { showgrid: false, zeroline: false, tickfont: { color: '#5a6278', size: 9 }, tickformat: '%b %d' },
    yaxis: { showgrid: true, gridcolor: '#1e2235', zeroline: false, tickfont: { color: '#5a6278', size: 9 } },
    annotations, height: 285,
  };

  function handleHover(data) {
    const pt = data?.points?.[0];
    const label = pt?.data?.meta?.levelLabel;
    if (label) setHoveredLevel(label);
  }

  function handleUnhover() {
    setHoveredLevel(null);
  }

  return (
    <div className="scanner-card" onClick={onClick}>
      <div className="scanner-card__title">{pair}</div>
      <div className="scanner-card__body">
        <div className="scanner-card__chart" ref={chartWrapRef}>
          <Plot data={traces} layout={layout} config={PLOTLY_CONFIG}
            onHover={handleHover} onUnhover={handleUnhover}
            style={{ width: '100%', height: 285 }} useResizeHandler />
          <SpreadExposureChart dates={visDates} history={visSignal} zscores={visZscores} />
        </div>
        <SpreadInfo
          data={data}
          priceInput={priceInput}
          onPriceChange={setPriceInput}
          onPriceApply={applyOverride}
          overrideZ={overrideZ}
        />
      </div>
    </div>
  );
}

/* ── Page ── */

const TENORS = [1, 2, 3, 4];

export default function Levels() {
  const { data, loading, error } = useApi(fetchLevelsProximity);
  const navigate = useNavigate();

  // Per-card tenor selection. The API returns every commodity at one tenor,
  // so tenor payloads are fetched lazily on first use (server caches them)
  // and cached here as tenor → {commodity: card}.
  const [cardTenors, setCardTenors] = useState({});
  const [tenorCards, setTenorCards] = useState({});
  const tenorFetching = useRef(new Set());

  useEffect(() => {
    if (data?.groups) {
      const all = Object.values(data.groups).flat();
      setTenorCards(prev => ({ ...prev, 1: Object.fromEntries(all.map(c => [c.commodity, c])) }));
    }
  }, [data]);

  function selectTenor(commodity, t) {
    setCardTenors(prev => ({ ...prev, [commodity]: t }));
    if (!tenorCards[t] && !tenorFetching.current.has(t)) {
      tenorFetching.current.add(t);
      fetchLevelsProximity(t)
        .then(d => {
          const all = Object.values(d.groups || {}).flat();
          setTenorCards(prev => ({ ...prev, [t]: Object.fromEntries(all.map(c => [c.commodity, c])) }));
        })
        .catch(() => {})
        .finally(() => tenorFetching.current.delete(t));
    }
  }

  // The published-snapshot source only serves tenor 1 (it advertises
  // tenors_available: [1]); the live API serves all of TENORS and sends no
  // such field. With a single tenor the M1–M4 toggle is not rendered.
  const multiTenor = (data?.tenors_available ?? TENORS).length > 1;

  const outrights = useMemo(() => {
    if (!data?.groups) return [];
    const all = Object.values(data.groups).flat();
    return OUTRIGHT_ORDER
      .map(name => all.find(c => c.commodity === name))
      .filter(Boolean);
  }, [data]);

  const spreads = useMemo(() => {
    if (!SHOW_SPREADS || !data?.spreads) return [];
    return SPREAD_ORDER
      .filter(p => data.spreads[p])
      .map(p => ({ pair: p, ...data.spreads[p] }));
  }, [data]);

  // Drop spread (mean-reversion) rows from the banners when spreads are hidden.
  const bannerFilter = arr =>
    SHOW_SPREADS ? arr : (arr || []).filter(x => x.strategy !== 'Mean Reversion');
  const hotSignals = bannerFilter(data?.hot);
  const recentTrades = bannerFilter(data?.recent_trades);

  // All drills go through a confirm first — see ConfirmDrill.
  const [confirmNav, setConfirmNav] = useState(null); // {label, path}

  function drillCommodity(name) {
    setConfirmNav({ label: name, path: `/signals/${encodeURIComponent(name)}/Momentum` });
  }
  function drillSpread(pair) {
    const routePair = pair.replace(' − ', ' / ');
    setConfirmNav({ label: pair, path: `/signals/${encodeURIComponent(routePair)}/Stat-Arb` });
  }
  function drillBanner(commodity, strategy) {
    if (strategy === 'Mean Reversion') {
      const routePair = commodity.replace(' − ', ' / ');
      setConfirmNav({ label: commodity, path: `/signals/${encodeURIComponent(routePair)}/Stat-Arb` });
    } else {
      setConfirmNav({ label: commodity, path: `/signals/${encodeURIComponent(commodity)}/Momentum` });
    }
  }

  // Only show the full-page loader on first load; on a tenor switch keep the
  // stale charts up until the new payload lands (useApi retains `data`).
  if (loading && !data) return <div className="page-content"><Loading message="Computing proximity levels..." /></div>;
  if (error) return <div className="page-content"><ErrorNote message={error} /></div>;

  return (
    <div className="page-content">
      <div className="scanner-banners">
        <HotBanner signals={hotSignals} onDrill={drillBanner} />
        <TradesBanner trades={recentTrades} onDrill={drillBanner} />
      </div>

      <div className={`scanner-split${SHOW_SPREADS ? '' : ' scanner-split--single'}`}>
        <div className="scanner-panel">
          <div className="level-section-label">Outrights</div>
          <div className="scanner-panel__scroll">
            {outrights.map(c => {
              const t = cardTenors[c.commodity] || 1;
              // Fall back to the M1 card while a deeper tenor is in flight.
              const card = tenorCards[t]?.[c.commodity] || c;
              const pending = t !== 1 && !tenorCards[t];
              // Starting rank from the base-tenor card (M2 for bal-month
              // names like Dubai/NGLs) so the toggle reads M2…M5 for them.
              const baseRank = parseInt(c.tenor_label?.slice(1), 10) || 1;
              return (
                // Key includes the shown tenor so the card remounts on a
                // switch — priceInput/priceOverride state is per-tenor.
                <CommodityChart
                  key={`${c.commodity}-${card.tenor_label}`}
                  card={card}
                  tenor={t}
                  tenorPending={pending}
                  onTenorChange={multiTenor ? (n => selectTenor(c.commodity, n)) : undefined}
                  baseRank={baseRank}
                  onClick={() => drillCommodity(c.commodity)}
                />
              );
            })}
          </div>
        </div>
        {SHOW_SPREADS && (
          <div className="scanner-panel">
            <div className="level-section-label">Spreads</div>
            <div className="scanner-panel__scroll">
              {spreads.map(s => (
                <SpreadChart key={s.pair} pair={s.pair} data={s} onClick={() => drillSpread(s.pair)} />
              ))}
            </div>
          </div>
        )}
      </div>

      {confirmNav && (
        <ConfirmDrill
          target={confirmNav.label}
          onYes={() => {
            const path = confirmNav.path;
            setConfirmNav(null);
            navigate(path);
          }}
          onNo={() => setConfirmNav(null)}
        />
      )}
    </div>
  );
}
