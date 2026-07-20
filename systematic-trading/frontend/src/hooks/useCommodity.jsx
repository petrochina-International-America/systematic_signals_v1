import { createContext, useContext, useState, useEffect } from 'react';
import { fetchCommodities, fetchHealth } from '../api/client';

const CommodityContext = createContext(null);

export function CommodityProvider({ children }) {
  const [commodity, setCommodity] = useState(
    () => sessionStorage.getItem('commodity') || 'WTI'
  );
  const [commodities, setCommodities] = useState([]);
  const [dataDate, setDataDate] = useState(null);
  const [pullTimestamp, setPullTimestamp] = useState(null);

  useEffect(() => {
    fetchCommodities().then(r => setCommodities(r.commodities)).catch(() => {});
    fetchHealth().then(r => {
      setDataDate(r.latest_data_date);
      setPullTimestamp(r.pull_timestamp);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    sessionStorage.setItem('commodity', commodity);
  }, [commodity]);

  return (
    <CommodityContext.Provider value={{ commodity, setCommodity, commodities, dataDate, pullTimestamp }}>
      {children}
    </CommodityContext.Provider>
  );
}

export function useCommodity() {
  const ctx = useContext(CommodityContext);
  if (!ctx) throw new Error('useCommodity must be used within CommodityProvider');
  return ctx;
}
