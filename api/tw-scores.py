from _helper import BaseHandler, read_json


class handler(BaseHandler):
    def do_GET(self):
        data = read_json("tw_scores.json")
        self._json(data if data else {"universe": "tw", "scores": [], "count": 0})
