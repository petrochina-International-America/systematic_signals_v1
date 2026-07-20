function Tip({ text }) {
  return (
    <span className="tip-wrap">
      <span className="tip-icon">?</span>
      <span className="tip-popup">{text}</span>
    </span>
  );
}

export function TablePanel({ heading, action, tooltip, children }) {
  const headingEl = tooltip
    ? <div className="panel-heading"><span className="panel-heading-tip">{heading} <Tip text={tooltip} /></span></div>
    : <div className="panel-heading">{heading}</div>;

  return (
    <div className="table-panel">
      {heading && (
        action ? (
          <div className="panel-heading-row">
            {headingEl}
            {action}
          </div>
        ) : headingEl
      )}
      {children}
    </div>
  );
}

export function ChartPanel({ children, style, tooltip }) {
  return (
    <div className="chart-panel" style={style}>
      {tooltip && <div className="chart-panel__tip"><Tip text={tooltip} /></div>}
      {children}
    </div>
  );
}
