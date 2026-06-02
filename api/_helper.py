"""Vercel Python Serverless — 共用工具"""
import json, os, pathlib
from http.server import BaseHTTPRequestHandler

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "web" / "data"


def read_json(filename: str) -> dict:
    path = DATA_DIR / filename
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class BaseHandler(BaseHTTPRequestHandler):
    """所有 Serverless handler 的基底：自動加 CORS、Content-Type。"""

    def do_OPTIONS(self):
        self._cors(204)

    def _cors(self, status: int = 200):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
