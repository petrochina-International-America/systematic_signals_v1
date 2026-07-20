import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import { fetchSignalSnapshot, fetchSpreadSnapshot, fetchLabCommodities, fetchTopPerformers, fetchTodaysSize } from '../api/client';
import Loading, { ErrorNote } from '../components/Loading';

const fmtPair = name => name?.replace(/ \/ /g, ' − ') ?? name;

// Stat-arb spread strategies are hidden from the main view (and the top-performer
// rails) for now — methodology and backend stay intact. Flip to true to restore.
const SHOW_SPREADS = false;

// Lot sizing on the cards is hidden until the sizing numbers are reconciled.
// Flip to true to restore (re-enables the /todays-size calls too).
const SHOW_LOTS = false;

const DIRECTION_COLORS = {
  Long: 'var(--green)',
  Short: 'var(--red)',
  Flat: 'var(--muted)',
};

function SignalChip({ direction, subtext, onClick, clickable }) {
  const color = DIRECTION_COLORS[direction] || 'var(--muted)';
  const isActive = direction === 'Long' || direction === 'Short';

  return (
    <button
      className={`signal-chip${isActive ? ' signal-chip--active' : ''}${clickable ? ' signal-chip--clickable' : ''}`}
      style={{ '--chip-color': color }}
      onClick={clickable ? onClick : undefined}
      disabled={!clickable}
    >
      <span className="signal-chip__direction">{direction}</span>
      {subtext != null && (
        <span className="signal-chip__conviction">{subtext}</span>
      )}
    </button>
  );
}

function formatSubtext(strat, sig, unit) {
  if (unit === '$') {
    if (strat === 'Momentum' && sig.ma_value != null) return `$${sig.ma_value} 20d avg`;
    if (strat === 'Carry' && sig.spread != null) {
      const shape = sig.spread > 0 ? 'Backward' : 'Contango';
      return `$${Math.abs(sig.spread)} ${shape}`;
    }
  } else {
    if (strat === 'Momentum' && sig.pct_from_ma != null) return `${Math.abs(sig.pct_from_ma)}% from 20d avg`;
    if (strat === 'Carry' && sig.spread_pct != null) {
      const shape = sig.spread_pct > 0 ? 'Backward' : 'Contango';
      return `${Math.abs(sig.spread_pct)}% ${shape}`;
    }
  }
  return null;
}

/**
 * Fetch today's lot count for a non-flat signal, silently on mount.
 * Returns null if flat, loading, or failed — card degrades gracefully.
 */
function useSizing(sizingParams, direction) {
  const [lots, setLots] = useState(null);
  useEffect(() => {
    if (!sizingParams || direction === '—' || direction === 'Flat') return;
    let cancelled = false;
    fetchTodaysSize(sizingParams)
      .then(d => {
        if (cancelled) return;
        if (d.legs) {
          // pair: show each leg
          const legs = Object.entries(d.legs);
          setLots(legs.map(([n, l]) => `${l.lots?.toFixed(0)}L ${n}`).join(' / '));
        } else {
          setLots(d.lots != null ? `${d.lots.toFixed(0)} lots` : null);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [JSON.stringify(sizingParams), direction]); // eslint-disable-line react-hooks/exhaustive-deps
  return lots;
}

function StratChip({ strat, commodity, sig, inLab, unit, onDrill }) {
  const direction = sig.direction || '—';
  const clickable = inLab && direction !== '—';
  const sizingParams = SHOW_LOTS && inLab ? {
    strategy: strat,
    commodity,
    ...(strat === 'Momentum' ? { tier: 'Averaged' } : {}),
  } : null;
  const lots = useSizing(sizingParams, direction);
  return (
    <div className="signal-card__chip-group">
      <span className="signal-card__strat-label">{strat}</span>
      <SignalChip
        direction={direction}
        subtext={formatSubtext(strat, sig, unit)}
        clickable={clickable}
        onClick={clickable ? () => onDrill(commodity, strat) : undefined}
      />
      {lots && <span className="signal-card__lots">{lots}</span>}
    </div>
  );
}

const PERFORMERS_PER_PAGE = 5;

function TopPerformerRow({ title, items, sharpeKey, onDrill, style }) {
  const [page, setPage] = useState(0);
  const pages = Math.ceil(items.length / PERFORMERS_PER_PAGE);
  const start = page * PERFORMERS_PER_PAGE;
  const visible = items.slice(start, start + PERFORMERS_PER_PAGE);

  return (
    <>
      <div className="top-performers__label" style={style}>{title}</div>
      <div className="top-performers__cards">
        {pages > 1 && (
          <button
            className="top-performers__arrow"
            disabled={page === 0}
            onClick={() => setPage(p => p - 1)}
            aria-label="Previous performers"
          >‹</button>
        )}
        {visible.map((tp, i) => (
          <button key={tp.label} className="top-performer-card" onClick={() => onDrill(tp.commodity, tp.strategy)}>
            <span className="top-performer__rank">#{start + i + 1}</span>
            <div className="top-performer__info">
              <span className="top-performer__name">{fmtPair(tp.label)}</span>
              <span className="top-performer__stats">
                <span style={{ color: DIRECTION_COLORS[tp.direction] || 'var(--muted)', fontWeight: 700 }}>{tp.direction}</span>
                {' · '}Sharpe {tp[sharpeKey]}
              </span>
            </div>
          </button>
        ))}
        {Array.from({ length: PERFORMERS_PER_PAGE - visible.length }, (_, i) => (
          <div key={`ph-${i}`} className="top-performer-card top-performer-card--placeholder" aria-hidden="true" />
        ))}
        {pages > 1 && (
          <button
            className="top-performers__arrow"
            disabled={page === pages - 1}
            onClick={() => setPage(p => p + 1)}
            aria-label="Next performers"
          >›</button>
        )}
      </div>
    </>
  );
}

function CommodityCard({ commodity, strategies, signals, labSet, onDrill, unit }) {
  return (
    <div className="signal-card">
      <div className="signal-card__name">{commodity}</div>
      <div className="signal-card__chips">
        {strategies.map(strat => (
          <StratChip
            key={strat}
            strat={strat}
            commodity={commodity}
            sig={signals?.[commodity]?.[strat] || {}}
            inLab={labSet.has(commodity)}
            unit={unit}
            onDrill={onDrill}
          />
        ))}
      </div>
    </div>
  );
}

function spreadSubtext(spread, unit) {
  // Band price: entry band for Flat, exit band for in-trade (same level with exit-at-threshold rule).
  // dist_to_threshold = spread_value - band → band = spread_value - dist_to_threshold.
  const bandPrice = spread.spread_value != null && spread.dist_to_threshold != null
    ? `$${(spread.spread_value - spread.dist_to_threshold).toFixed(2)}`
    : null;

  if (spread.direction === 'Long' || spread.direction === 'Short') {
    // In-trade: exits when spread retraces back inside the entry band (exit-at-threshold).
    let distStr = null;
    if (unit === '$') {
      distStr = spread.dist_to_threshold != null
        ? `$${Math.abs(spread.dist_to_threshold).toFixed(2)} from exit`
        : null;
    } else {
      distStr = spread.pct_from_threshold != null
        ? `${Math.abs(spread.pct_from_threshold).toFixed(1)}% from exit`
        : null;
    }
    return distStr && bandPrice ? `${distStr}  ·  ${bandPrice}` : (distStr ?? bandPrice);
  }

  // Flat: distance to whichever entry band would fire next.
  const towardLabel = spread.deviation != null && spread.deviation < 0 ? 'Long' : 'Short';
  let distStr = null;
  if (unit === '$') {
    distStr = spread.dist_to_threshold != null
      ? `$${Math.abs(spread.dist_to_threshold).toFixed(2)} away from ${towardLabel}`
      : null;
  } else {
    distStr = spread.pct_from_threshold != null
      ? `${Math.abs(spread.pct_from_threshold).toFixed(1)}% away from ${towardLabel}`
      : null;
  }
  return distStr && bandPrice ? `${distStr}  ·  ${bandPrice}` : (distStr ?? bandPrice);
}

function SpreadCard({ spread, unit, onDrill }) {
  const direction = spread.direction || '—';
  const sizingParams = SHOW_LOTS && direction !== '—' && direction !== 'Flat'
    ? { strategy: 'Stat-Arb', pair: spread.pair }
    : null;
  const lots = useSizing(sizingParams, direction);

  return (
    <div className="signal-card">
      <div className="signal-card__name">{fmtPair(spread.pair)}</div>
      <div className="signal-card__chips">
        <div className="signal-card__chip-group">
          <span className="signal-card__strat-label">Mean Reversion</span>
          <SignalChip
            direction={direction}
            subtext={spreadSubtext(spread, unit)}
            clickable={direction !== '—'}
            onClick={() => onDrill(spread.pair, 'Stat-Arb')}
          />
          {lots && (
            <span className="signal-card__lots">{lots}</span>
          )}
        </div>
      </div>
    </div>
  );
}

export default function Signals() {
  const { data: snap, loading, error } = useApi(fetchSignalSnapshot);
  const { data: spreads } = useApi(SHOW_SPREADS ? fetchSpreadSnapshot : () => Promise.resolve(null));
  const { data: labCommodities } = useApi(fetchLabCommodities);
  const { data: topPerformers } = useApi(() => fetchTopPerformers(15));
  const navigate = useNavigate();
  const [unit, setUnit] = useState('$');

  const labSet = new Set(labCommodities?.commodities || []);

  function handleDrill(commodity, strategy) {
    navigate(`/signals/${encodeURIComponent(commodity)}/${encodeURIComponent(strategy)}`);
  }

  if (loading) return <div className="page-content"><Loading message="Loading signals..." /></div>;
  if (error) return <div className="page-content"><ErrorNote message={error} /></div>;
  if (!snap) return null;

  const { product_groups, strategies, signals } = snap;

  const filterPerformers = items =>
    (items || []).filter(tp => SHOW_SPREADS || tp.strategy !== 'Stat-Arb');
  const top1y = filterPerformers(topPerformers?.top_1y);
  const topAll = filterPerformers(topPerformers?.top_alltime);

  const outrightGroups = Object.entries(product_groups);
  const spreadGroups = SHOW_SPREADS && spreads ? Object.entries(spreads) : [];

  const rows = outrightGroups.map(([group, commodities], i) => ({
    outrightGroup: group,
    outrightCommodities: commodities,
    spreadGroup: spreadGroups[i]?.[0] || null,
    spreadPairs: spreadGroups[i]?.[1] || [],
  }));
  // Add any remaining spread groups that don't have an outright pair
  for (let i = outrightGroups.length; i < spreadGroups.length; i++) {
    rows.push({
      outrightGroup: null, outrightCommodities: [],
      spreadGroup: spreadGroups[i][0], spreadPairs: spreadGroups[i][1],
    });
  }

  return (
    <div className="page-content">
      {(top1y.length > 0 || topAll.length > 0) && (
        <div className="top-performers">
          {top1y.length > 0 && (
            <TopPerformerRow
              title="Top Performing (1Y Sharpe)"
              items={top1y}
              sharpeKey="sharpe_1y"
              onDrill={handleDrill}
            />
          )}
          {topAll.length > 0 && (
            <TopPerformerRow
              title="Top Performing (All Time Sharpe)"
              items={topAll}
              sharpeKey="sharpe_all"
              onDrill={handleDrill}
              style={{ marginTop: 8 }}
            />
          )}
        </div>
      )}

      <div className={`signals-columns${SHOW_SPREADS ? '' : ' signals-columns--single'}`}>
        <div className="signals-col__header">Outrights</div>
        {SHOW_SPREADS && <div className="signals-col__header">Spreads</div>}
      </div>

      {rows.map((row, i) => (
        <div key={i} className={`signals-columns signals-row${SHOW_SPREADS ? '' : ' signals-columns--single'}`}>
          <div className="signals-col">
            {row.outrightGroup && (
              <div className="signal-group">
                <div className="signal-group__heading">{row.outrightGroup}</div>
                <div className="signal-card-grid">
                  {row.outrightCommodities.map(c => (
                    <CommodityCard
                      key={c}
                      commodity={c}
                      strategies={strategies}
                      signals={signals}
                      labSet={labSet}
                      onDrill={handleDrill}
                      unit={unit}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
          {SHOW_SPREADS && (
            <div className="signals-col">
              {row.spreadGroup && (
                <div className="signal-group">
                  <div className="signal-group__heading">{row.spreadGroup}</div>
                  <div className="signal-card-grid">
                    {row.spreadPairs.map(s => (
                      <SpreadCard
                        key={s.pair}
                        spread={s}
                        unit={unit}
                        onDrill={handleDrill}
                      />
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      ))}

      <div className="signals-footer">
        <span className="signals-footer__label">Display:</span>
        <button
          className={`signals-toggle${unit === '$' ? ' signals-toggle--active' : ''}`}
          onClick={() => setUnit('$')}
        >$</button>
        <button
          className={`signals-toggle${unit === '%' ? ' signals-toggle--active' : ''}`}
          onClick={() => setUnit('%')}
        >%</button>
      </div>
    </div>
  );
}
