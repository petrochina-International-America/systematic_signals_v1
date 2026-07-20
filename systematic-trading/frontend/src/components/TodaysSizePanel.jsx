/**
 * TodaysSizePanel — today's model position size, shown on the detail/backtest pages.
 *
 * Fires POST /api/sizing/today with the same params as the running backtest.
 * The vol scalar is read directly from the backtest result's last row, so it
 * is numerically identical to the vol scalar shown on the charts.
 *
 * The reference price for notional / VaR comes from the backtest's own last
 * held price (the actual rolled settlement price), NOT a fixed calibration
 * constant.  capital_base is the shared $1M risk-sizing denominator (same
 * for every strategy and pair — see energy.sizing.daily_size.CAPITAL_BASE).
 */
import { useState, useEffect, useCallback } from 'react';
import { fetchTodaysSize } from '../api/client';
import './TodaysSizePanel.css';

const TOOLTIP_CONTENT = (
  <>
    <b>How the lot count is calculated:</b><br /><br />
    <b>Vol scalar</b> = (target vol / √252) ÷ realized_daily_vol<br />
    &nbsp;&nbsp;Realized vol = trailing {'{'}vol_window{'}'}-day std of daily returns.<br />
    &nbsp;&nbsp;Below 1 = high vol environment, leverage reduced.<br /><br />
    <b>Lots</b> = leg_fraction × vol_scalar × capital_base ÷ (price × multiplier)<br />
    &nbsp;&nbsp;Single-leg: leg_fraction = 1. Each leg of a pair: 0.5 (dollar-neutral).<br />
    &nbsp;&nbsp;Lot counts for each pair leg differ when prices differ — that is correct.<br /><br />
    <b>1d 95% VaR</b> = notional × realized_vol_daily × 1.645<br />
    &nbsp;&nbsp;Gaussian approximation. Use empirical_var mode for fat-tail estimate.<br /><br />
    <b>Price</b> = last held settlement price from the backtest (actual rolled price).<br />
    <b>Capital base</b> = $1M for every strategy and pair — the same research book<br />
    &nbsp;&nbsp;the backtest runs at, so lot counts are comparable across pairs and match<br />
    &nbsp;&nbsp;the backtested position scale. Scale lots linearly for a larger allocation.
  </>
);

function Tip() {
  return (
    <span className="tip-wrap">
      <span className="tip-icon">?</span>
      <span className="tip-popup tip-popup--sizing">{TOOLTIP_CONTENT}</span>
    </span>
  );
}

function DirectionBadge({ direction }) {
  const cls =
    direction === 'Long' || direction === 'Long spread'
      ? 'sizing-badge sizing-badge--long'
      : direction === 'Short' || direction === 'Short spread'
      ? 'sizing-badge sizing-badge--short'
      : 'sizing-badge sizing-badge--flat';
  return <span className={cls}>{direction}</span>;
}

function fmt$(n) {
  if (n == null || isNaN(n)) return '—';
  if (Math.abs(n) >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (Math.abs(n) >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

function fmtPct(n) {
  if (n == null || isNaN(n)) return '—';
  return `${n.toFixed(1)}%`;
}

function StatItem({ label, value, risk }) {
  return (
    <div className="sizing-stat">
      <span className="sizing-stat__label">{label}</span>
      <span className={`sizing-stat__value${risk ? ' sizing-stat__value--risk' : ''}`}>{value}</span>
    </div>
  );
}

function SingleLegView({ data }) {
  return (
    <div className="sizing-body">
      <div className="sizing-row sizing-row--hero">
        <DirectionBadge direction={data.direction} />
        <span className="sizing-lots">
          {data.direction === 'Flat' ? '0' : data.lots?.toFixed(0)} lots
        </span>
      </div>
      <div className="sizing-stats">
        <StatItem label="Notional"     value={fmt$(data.notional_usd)} />
        <StatItem label="1d 95% VaR"   value={fmt$(data.var_95_usd)} risk />
        <StatItem label="Vol scalar"   value={data.scalar != null ? data.scalar.toFixed(2) : '—'} />
        <StatItem label="Realized vol" value={fmtPct(data.realized_vol_ann_pct)} />
      </div>
      <div className="sizing-footer">
        as of {data.as_of_date}
        {data.ref_price != null && (
          <> · settlement ${data.ref_price.toFixed(2)}</>
        )}
      </div>
    </div>
  );
}

function PairView({ data }) {
  const legs = data.legs ? Object.entries(data.legs) : [];
  return (
    <div className="sizing-body">
      <div className="sizing-row sizing-row--hero">
        <DirectionBadge direction={data.direction} />
      </div>
      <div className="sizing-legs">
        {legs.map(([name, leg]) => (
          <div key={name} className="sizing-leg">
            <span className="sizing-leg__name">{name}</span>
            <DirectionBadge direction={leg.direction} />
            <span className="sizing-lots sizing-lots--sm">{leg.lots?.toFixed(0)} lots</span>
            <span className="sizing-leg__notional">{fmt$(leg.notional_usd)}</span>
            <span className="sizing-leg__var sizing-stat__value--risk">{fmt$(leg.var_95_usd)} VaR</span>
            {leg.ref_price != null && (
              <span className="sizing-leg__price">${leg.ref_price.toFixed(2)}</span>
            )}
          </div>
        ))}
      </div>
      <div className="sizing-stats">
        <StatItem label="Total notional"  value={fmt$(data.total_notional_usd)} />
        <StatItem label="Total 1d 95% VaR" value={fmt$(data.total_var_95_usd)} risk />
        <StatItem label="Vol scalar"       value={data.scalar != null ? data.scalar.toFixed(2) : '—'} />
        <StatItem label="Realized vol"     value={fmtPct(data.realized_vol_ann_pct)} />
      </div>
      <div className="sizing-footer">
        as of {data.as_of_date}
      </div>
    </div>
  );
}

export default function TodaysSizePanel({ sizingParams }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(true);

  // stringify so the effect re-fires when backtest params change (e.g. vol target slider)
  const paramsKey = JSON.stringify(sizingParams); // eslint-disable-line react-hooks/exhaustive-deps

  const load = useCallback(() => {
    if (!sizingParams) return;
    setLoading(true);
    setError(null);
    fetchTodaysSize(sizingParams)
      .then(d => { setData(d); setLoading(false); })
      .catch(e => { setError(e.message); setLoading(false); });
  }, [paramsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load(); }, [load]);

  const isPair = data && 'legs' in data;

  return (
    <div className="sizing-panel">
      <div className="sizing-header" onClick={() => setOpen(o => !o)}>
        <span className="sizing-header__title">
          Today&apos;s Trade
          <span onClick={e => e.stopPropagation()}>
            <Tip />
          </span>
        </span>
        {data && !loading && (
          <span className="sizing-header__peek">
            {isPair
              ? <DirectionBadge direction={data.direction} />
              : <><DirectionBadge direction={data.direction} />{' '}{data.lots?.toFixed(0)} lots</>
            }
          </span>
        )}
        <span className="sizing-header__calib-warn">
          capital base set at calibration — verify before trading
        </span>
        <button className="sizing-header__toggle">{open ? '▲' : '▼'}</button>
      </div>

      {open && (
        <div className="sizing-content">
          {loading && <div className="sizing-loading">Computing…</div>}
          {error && <div className="sizing-error">{error}</div>}
          {!loading && !error && data && (
            isPair ? <PairView data={data} /> : <SingleLegView data={data} />
          )}
          {!loading && (
            <button className="sizing-refresh" onClick={load} title="Refresh sizing">↻ Refresh</button>
          )}
        </div>
      )}
    </div>
  );
}
