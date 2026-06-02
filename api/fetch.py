"""
Vercel 無常駐 worker，無法在請求中跑完整 500+ 支 OHLCV 抓取。
直接回 started，前端會接著打 /api/state → /api/scores 取得每日預產資料。
"""
import sys, pathlib as _pl
_d = str(_pl.Path(__file__).resolve().parent)
if _d not in sys.path: sys.path.insert(0, _d)

from _helper import BaseHandler


class handler(BaseHandler):
    def do_GET(self):
        self._json({"status": "started"})
