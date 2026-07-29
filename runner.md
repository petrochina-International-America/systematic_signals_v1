This copy (SystematicTradingLive) runs on 3002/8002 so it can run
alongside the original stack on 3000/8000.

Terminal 1 — FastAPI backend (port 8002):

cd h:\SystematicTradingLive\systematic-trading
py -3.14 -m uvicorn api.main:app --reload --port 8002

Terminal 2 — React frontend (port 3002, proxies /api -> 8002):

cd h:\SystematicTradingLive\systematic-trading\frontend
npm run dev

Then open http://localhost:3002
