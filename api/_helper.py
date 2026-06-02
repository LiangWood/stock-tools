"""Vercel Python Serverless — 共用工具"""
import json, os, pathlib, sys
from http.server import BaseHTTPRequestHandler

# Vercel Lambda 的 /var/task/ 是專案根目錄
# __file__ = /var/task/api/_helper.py  → parent.parent = /var/task/
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "web" / "data"

# 確保 api/ 目錄在 sys.path，讓 api/*.py 可以 import _helper
_api_dir = str(pathlib.Path(__file__).resolve().parent)
if _api_dir not in sys.path:
    sys.path.insert(0, _api_dir)


def read_json(filename: str) -> dict:
    path = DATA_DIR / filename
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class BaseHandler(BaseHTTPRequestHandler):
    """所有 Serverless handler 的基底：自動加 CORS、Content-Type。"""

    def log_message(self, fmt, *args):  # 靜音預設 stderr log
        pass

    def do_OPTIONS(self):
        self.send_response(204)
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
