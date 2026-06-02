"""
Vercel 無常駐 worker，無法在請求中跑完整 500+ 支 OHLCV 抓取。
直接回 "done"，前端會接著打 /api/state → /api/scores 取得每日預產資料。
"""
from _helper import BaseHandler


class handler(BaseHandler):
    def do_GET(self):
        self._json({"status": "started"})
