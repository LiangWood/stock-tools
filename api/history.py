"""
/api/history?ticker=AAPL&period=6mo
唯一真正即時抓取的 endpoint — 單支 ticker yfinance OHLCV。
"""
import sys, pathlib as _pl
_d = str(_pl.Path(__file__).resolve().parent)
if _d not in sys.path: sys.path.insert(0, _d)

from _helper import BaseHandler
from urllib.parse import urlparse, parse_qs


VALID_PERIODS = {"6mo", "1y", "2y", "5y", "10y", "max"}


class handler(BaseHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        ticker = qs.get("ticker", [""])[0].upper().strip()
        period = qs.get("period", ["6mo"])[0]
        if period not in VALID_PERIODS:
            period = "6mo"

        if not ticker:
            self._json({"error": "ticker is required"}, 400)
            return

        try:
            import yfinance as yf
            import pandas as pd

            df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df = df.xs(ticker, axis=1, level=1)

            if df is None or df.empty:
                self._json({"error": "no data"}, 404)
                return

            bars = []
            for ts, row in df.iterrows():
                t = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts.value // 1e9)
                o, h, lo, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
                v = float(row.get("Volume", 0))
                if any(x != x for x in [o, h, lo, c]):
                    continue
                bars.append({"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": v})

            self._json({"ticker": ticker, "period": period, "bars": bars})

        except Exception as exc:
            self._json({"error": str(exc)}, 500)
