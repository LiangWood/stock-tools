import sys, pathlib as _pl
_d = str(_pl.Path(__file__).resolve().parent)
if _d not in sys.path: sys.path.insert(0, _d)
from _helper import BaseHandler, read_json


class handler(BaseHandler):
    def do_GET(self):
        data = read_json("tw_scores.json")
        self._json(data if data else {"universe": "tw", "scores": [], "count": 0})
