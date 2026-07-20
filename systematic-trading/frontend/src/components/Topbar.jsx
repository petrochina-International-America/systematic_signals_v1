import { useCommodity } from '../hooks/useCommodity';

const PAGE_TITLES = {
  '/': 'Signals',
  '/cot-flows': 'COT Flows',
  '/signals': 'Signals',
  '/levels': 'Proximity Scanner',
  '/strategy-lab': 'Strategy Lab',
};

export default function Topbar({ pathname }) {
  const { dataDate, pullTimestamp } = useCommodity();
  const title = PAGE_TITLES[pathname] || 'SystematicTrading';

  let label = '';
  const ctOpts = { timeZone: 'America/Chicago' };
  if (pullTimestamp) {
    const dt = new Date(pullTimestamp);
    label = `Prices as of ${dt.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', ...ctOpts })}, ${dt.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', ...ctOpts })} CT`;
  } else if (dataDate) {
    label = `Prices as of ${new Date(dataDate + 'T00:00:00').toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })}`;
  }

  return (
    <div className="topbar">
      <div className="topbar-left">
        <h1 className="topbar-title">{title}</h1>
      </div>
      <div className="topbar-right">
        <span className="topbar-timestamp">{label}</span>
      </div>
    </div>
  );
}
