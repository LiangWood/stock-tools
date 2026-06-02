from _helper import BaseHandler, read_json


class handler(BaseHandler):
    def do_GET(self):
        data = read_json("tw_breakout.json")
        self._json(data if data else {"candidates": []})
