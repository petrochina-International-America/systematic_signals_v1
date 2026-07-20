import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { CommodityProvider } from './hooks/useCommodity';
import Sidebar from './components/Sidebar';
import Topbar from './components/Topbar';
import CotFlows from './pages/CotFlows';
import Signals from './pages/Signals';
import SignalDetail from './pages/SignalDetail';
import Levels from './pages/Levels';
import StrategyLab from './pages/StrategyLab';

function Shell() {
  const { pathname } = useLocation();
  return (
    <div id="app-shell">
      <Sidebar />
      <div className="main-area">
        <Topbar pathname={pathname} />
        <div className="page-container">
          <Routes>
            <Route path="/" element={<Navigate to="/signals" replace />} />
            <Route path="/cot-flows" element={<CotFlows />} />
            <Route path="/signals" element={<Signals />} />
            <Route path="/signals/:commodity/:strategy" element={<SignalDetail />} />
            <Route path="/levels" element={<Levels />} />
            <Route path="/strategy-lab" element={<StrategyLab />} />
          </Routes>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <CommodityProvider>
        <Shell />
      </CommodityProvider>
    </BrowserRouter>
  );
}
