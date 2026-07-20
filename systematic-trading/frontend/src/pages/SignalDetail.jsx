import { useParams } from 'react-router-dom';
import MomentumDetail from './MomentumDetail';
import StatArbDetail from './StatArbDetail';
import CarryDetail from './CarryDetail';

export default function SignalDetail() {
  const { strategy } = useParams();
  if (strategy === 'Momentum') return <MomentumDetail />;
  if (strategy === 'Stat-Arb') return <StatArbDetail />;
  return <CarryDetail />;
}
