from _helper import BaseHandler, read_json


class handler(BaseHandler):
    def do_GET(self):
        us = read_json("us_scores.json")
        meta = read_json("meta.json")
        self._json({
            "universe": "all",
            "scores": us.get("scores", []),
            "last_updated": meta.get("last_updated", "—"),
            "count": us.get("count", len(us.get("scores", []))),
        })
