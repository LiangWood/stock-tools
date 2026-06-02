from _helper import BaseHandler, read_json


class handler(BaseHandler):
    def do_GET(self):
        data = read_json("us_scores.json")
        self._json(data if data else {"universe": "all", "scores": [], "count": 0})
