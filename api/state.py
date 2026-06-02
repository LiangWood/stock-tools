import sys, pathlib as _pl
_d = str(_pl.Path(__file__).resolve().parent)
if _d not in sys.path: sys.path.insert(0, _d)
from _helper import BaseHandler, read_json


class handler(BaseHandler):
    def do_GET(self):
        meta = read_json("meta.json")
        us = read_json("us_scores.json")
        self._json({
            "status": "done",
            "progress": "靜態資料（每日收盤後更新）",
            "universe": "all",
            "last_updated": meta.get("last_updated", "—"),
            "error": None,
            "count": us.get("count", len(us.get("scores", []))),
        })
