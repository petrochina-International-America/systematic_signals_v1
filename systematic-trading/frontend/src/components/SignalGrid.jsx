const DIRECTION_COLORS = {
  Long: 'var(--green)',
  Short: 'var(--red)',
  Flat: 'var(--muted)',
};

function SignalCell({ direction }) {
  const color = DIRECTION_COLORS[direction] || 'var(--muted)';
  return (
    <div className="signal-cell">
      <span className="signal-cell-direction" style={{ color }}>{direction}</span>
    </div>
  );
}

export default function SignalGrid({ rowLabels, colLabels, signals, onCellClick }) {
  return (
    <div className="signal-grid">
      <div className="signal-grid-row signal-grid-header">
        <div className="signal-grid-row-label" style={{ width: 160 }} />
        {colLabels.map(col => (
          <div key={col} className="signal-grid-col-label">{col}</div>
        ))}
      </div>
      {rowLabels.map(row => (
        <div key={row} className="signal-grid-row">
          <div className="signal-grid-row-label" style={{ width: 160 }}>{row}</div>
          {colLabels.map(col => {
            const direction = signals?.[row]?.[col] || '—';
            const clickable = onCellClick && direction !== '—';
            return (
              <div
                key={col}
                className={`signal-grid-cell${clickable ? ' signal-grid-cell-clickable' : ''}`}
                onClick={clickable ? () => onCellClick(row, col) : undefined}
              >
                <SignalCell direction={direction} />
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
