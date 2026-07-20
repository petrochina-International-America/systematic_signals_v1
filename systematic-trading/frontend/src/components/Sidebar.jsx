import { NavLink } from 'react-router-dom';

const NAV_ITEMS = [
  // COT Flows hidden for now (not in use yet) — page and route still exist,
  // uncomment to restore the tab.
  // { label: 'COT Flows', href: '/cot-flows', icon: '◈' },
  { label: 'Signals', href: '/signals', icon: '◉' },
  { label: 'Levels', href: '/levels', icon: '◧' },
];

export default function Sidebar() {
  return (
    <div className="sidebar">
      <div className="sidebar-brand">
        <span className="brand-mark">ST</span>
        <span className="brand-text">SystematicTrading</span>
      </div>
      <div className="sidebar-divider" />
      <nav className="sidebar-nav">
        {NAV_ITEMS.map(item => (
          <NavLink
            key={item.href}
            to={item.href}
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
          >
            <span className="nav-icon">{item.icon}</span>
            <span className="nav-label">{item.label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
