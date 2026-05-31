#!/usr/bin/env python3
"""
Web-based stock momentum screener.
Usage: python server.py
Then open: http://localhost:5173
"""
import json
import logging
import math
import os
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.fetcher import fetch_all
from data.twse_fetcher import fetch_tw_all
from data.universe import get_combined_tickers, get_nasdaq100_tickers, get_sp500_tickers
from scoring.engine import compute_scores
from scoring.tw_engine import compute_tw_scores

logger = logging.getLogger(__name__)

PORT = 5177
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_state: dict = {
    "status": "idle",
    "progress": "",
    "scores": [],
    "ohlcv": {},
    "universe": "sp500",
    "last_updated": None,
    "error": None,
}
_lock = threading.Lock()
_live_refresh_lock = threading.Lock()

# 基本面快取：記憶體 + 磁碟（當日 TTL）。PE/PEG 盤中不變，重啟不必重抓。
_fund_cache: dict[str, dict] = {}
_FUND_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fund_cache.json")


def _load_fund_cache() -> None:
    """啟動時載入磁碟快取，僅當日資料有效。"""
    global _fund_cache
    try:
        with open(_FUND_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == datetime.now().strftime("%Y-%m-%d"):
            _fund_cache = data.get("fund", {})
            logger.info("從磁碟載入 %d 檔基本面快取（今日）", len(_fund_cache))
        else:
            logger.info("磁碟基本面快取已過期（非今日），忽略")
    except Exception:
        pass


def _save_fund_cache() -> None:
    """將記憶體快取寫入磁碟（含日期戳記）。"""
    try:
        with open(_FUND_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"date": datetime.now().strftime("%Y-%m-%d"), "fund": _fund_cache},
                f, ensure_ascii=False,
            )
    except Exception as e:
        logger.warning("基本面快取寫入失敗：%s", e)

_US_UNIVERSES = {
    "sp500":  get_sp500_tickers,
    "ndx100": get_nasdaq100_tickers,
    "all":    get_combined_tickers,
}

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript",
    ".css":  "text/css",
    ".json": "application/json; charset=utf-8",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
    ".svg":  "image/svg+xml",
}

SECTOR_ETF_MAP = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Communication Services": "XLC",
}

# 只保留實際在表格顯示的欄位（PE、PEG）。
# eps_beat / eps_consecutive_beats / sector_above_ema50 已從前端移除，
# 不再抓取以節省每檔額外的 earnings_dates 網路請求與族群 ETF 下載。
_FUNDAMENTAL_COLUMNS = [
    "pe",
    "peg_ratio",
]


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _safe_float(value):
    try:
        if value is None:
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    except Exception:
        return None


def _check_sector_ema(scores_df, all_ohlcv) -> dict:
    """Return ETF -> latest weekly close above EMA50 status."""
    del scores_df
    sector_ema: dict = {}
    for etf in set(SECTOR_ETF_MAP.values()):
        status = None
        try:
            df = all_ohlcv.get(etf) if all_ohlcv else None
            if df is not None and not df.empty and "Close" in df.columns:
                close = df["Close"].dropna()
                if len(close) >= 50:
                    ema50 = close.ewm(span=50, adjust=False).mean()
                    status = bool(float(close.iloc[-1]) > float(ema50.iloc[-1]))
        except Exception:
            status = None
        sector_ema[etf] = status
    return sector_ema


def _fetch_sector_ema(scores_df) -> dict:
    try:
        sector_ohlcv = fetch_all(
            list(SECTOR_ETF_MAP.values()),
            period="1y",
            interval="1wk",
        )
        return _check_sector_ema(scores_df, sector_ohlcv)
    except Exception:
        return {etf: None for etf in set(SECTOR_ETF_MAP.values())}


def _enrich_fundamentals(scores_df, sector_ema_by_etf=None) -> object:
    """Fetch PE, EPS surprise, PEG, and sector trend in parallel for scored stocks."""
    import yfinance as yf

    tickers = scores_df["ticker"].tolist()
    if sector_ema_by_etf is None:
        sector_ema_by_etf = _fetch_sector_ema(scores_df)

    def _fetch_one(ticker: str) -> tuple:
        import time as _t
        pe = None
        eps_beat = None
        eps_consecutive_beats = None
        peg_ratio = None
        sector_above_ema50 = None
        try:
            t = yf.Ticker(ticker)
            # Retry once on rate limit
            for attempt in range(2):
                try:
                    info = t.info
                    break
                except Exception as e:
                    if "RateLimit" in str(type(e).__name__) or "429" in str(e):
                        _t.sleep(3 + attempt * 2)
                    else:
                        raise
            pe   = _safe_float(info.get("trailingPE") or info.get("forwardPE"))

            try:
                earnings_growth = _safe_float(info.get("earningsGrowth"))
                if pe is not None and earnings_growth is not None:
                    earnings_growth_pct = earnings_growth * 100
                    if earnings_growth_pct > 0:
                        peg_ratio = pe / earnings_growth_pct
            except Exception:
                peg_ratio = None

            try:
                sector = info.get("sector")
                etf = SECTOR_ETF_MAP.get(sector)
                if etf:
                    sector_above_ema50 = sector_ema_by_etf.get(etf)
            except Exception:
                sector_above_ema50 = None

            try:
                ed = t.earnings_dates
                if ed is not None and not ed.empty:
                    if "Surprise(%)" in ed.columns:
                        recent = ed.dropna(subset=["Surprise(%)"])
                    else:
                        recent = ed.iloc[0:0]
                    try:
                        recent = recent.sort_index(ascending=False)
                    except Exception:
                        pass
                    if not recent.empty:
                        eps_beat = float(recent["Surprise(%)"].iloc[0])
                    recent_two = recent.head(2)
                    if len(recent_two) >= 2:
                        eps_consecutive_beats = bool((recent_two["Surprise(%)"] >= 10).all())
            except Exception:
                pass

            return ticker, pe, eps_beat, eps_consecutive_beats, peg_ratio, sector_above_ema50
        except Exception:
            return ticker, pe, eps_beat, eps_consecutive_beats, peg_ratio, sector_above_ema50

    fund: dict = {}
    total = len(tickers)
    done  = 0
    import time as _time
    # 降低並發數避免 YFRateLimitError（2 workers，請求之間自然有間隔）
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_fetch_one, t): t for t in tickers}
        for future in as_completed(futures):
            ticker, pe, eps_beat, eps_consecutive_beats, peg_ratio, sector_above_ema50 = future.result()
            fund[ticker] = {
                "pe": pe,
                "eps_beat": eps_beat,
                "eps_consecutive_beats": eps_consecutive_beats,
                "peg_ratio": peg_ratio,
                "sector_above_ema50": sector_above_ema50,
            }
            done += 1
            with _lock:
                _state["progress"] = f"基本面 {done}/{total}"

    df = scores_df.copy()
    df["pe"]                    = df["ticker"].map(lambda t: fund.get(t, {}).get("pe"))
    df["eps_beat"]              = df["ticker"].map(lambda t: fund.get(t, {}).get("eps_beat"))
    df["eps_consecutive_beats"] = df["ticker"].map(lambda t: fund.get(t, {}).get("eps_consecutive_beats")).astype(object)
    df["peg_ratio"]             = df["ticker"].map(lambda t: fund.get(t, {}).get("peg_ratio"))
    df["sector_above_ema50"]    = df["ticker"].map(lambda t: fund.get(t, {}).get("sector_above_ema50")).astype(object)
    return df


def _fetch_fund_dict(tickers: list) -> dict:
    """
    抓取各 ticker 的基本面（只取 PE、PEG），回傳 {ticker: {pe, peg_ratio}}。

    優化：
      - 每檔只發一次 t.info 請求（不再呼叫 t.earnings_dates / 族群 ETF）
      - 4 個 worker 並行（單一請求/檔，rate-limit 壓力比過去減半）
      - 即時回報 "基本面 X/Y" 進度
    """
    import yfinance as yf
    import time as _t

    total = len(tickers)

    def _one(ticker: str) -> tuple[str, dict]:
        pe = peg_ratio = None
        try:
            t = yf.Ticker(ticker)
            info = {}
            for attempt in range(2):
                try:
                    info = t.info
                    break
                except Exception as e:
                    if "RateLimit" in str(type(e).__name__) or "429" in str(e):
                        _t.sleep(3 + attempt * 2)
                    else:
                        break
            pe = _safe_float(info.get("trailingPE") or info.get("forwardPE"))
            eg = _safe_float(info.get("earningsGrowth"))
            if pe and eg and eg * 100 > 0:
                peg_ratio = pe / (eg * 100)
        except Exception:
            pass
        return ticker, {"pe": pe, "peg_ratio": peg_ratio}

    result: dict = {}
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_one, t): t for t in tickers}
        for future in as_completed(futures):
            tkr, data = future.result()
            result[tkr] = data
            done += 1
            with _lock:
                _state["progress"] = f"基本面 {done}/{total}"
    return result


def _apply_fund_dict(scores_df, fund: dict):
    """將基本面 dict 合併進 scores_df，只補缺失欄位。"""
    df = scores_df.copy()
    for col in _FUNDAMENTAL_COLUMNS:
        df[col] = df["ticker"].map(lambda t, c=col: fund.get(t, {}).get(c))
    for col in ("eps_consecutive_beats", "sector_above_ema50"):
        if col in df.columns:
            df[col] = df[col].astype(object)
    return df


def _merge_cached_fundamentals(scores_df, cached_scores: list[dict]) -> object:
    """Carry previously fetched fundamental fields onto freshly scored rows."""
    cached_by_ticker = {row.get("ticker"): row for row in cached_scores if row.get("ticker")}
    df = scores_df.copy()
    for col in _FUNDAMENTAL_COLUMNS:
        values = [cached_by_ticker.get(t, {}).get(col) for t in df["ticker"]]
        df[col] = pd.Series(values, index=df.index, dtype=object)
    return df


def _quote_from_ohlcv(df) -> dict | None:
    if df is None or df.empty or "Close" not in df.columns:
        return None
    close = df["Close"].dropna()
    if close.empty:
        return None
    price = _safe_float(close.iloc[-1])
    if price is None:
        return None
    prev = None
    if len(close) >= 2:
        prev = _safe_float(close.iloc[-2])
    if prev is None or prev == 0:
        day_return = 0.0
    else:
        day_return = float((price - prev) / prev)
    return {"price": price, "day_return": day_return}


def _refresh_live_scores(universe: str) -> tuple[list[dict], dict]:
    """
    快速即時更新：用 fast_info 並行抓最新報價，不重新下載 OHLCV。
    每支股票約 0.2–0.5s，20 個 worker 跑 100 檔約 1–3 秒。
    """
    del universe
    import yfinance as yf

    with _lock:
        cached_scores = [dict(row) for row in _state["scores"]]

    tickers = [row.get("ticker") for row in cached_scores if row.get("ticker")]
    if not tickers:
        raise ValueError("no cached scores for live refresh")

    def _get_quote(ticker: str) -> tuple[str, float | None, float | None]:
        try:
            fi = yf.Ticker(ticker).fast_info
            price = _safe_float(getattr(fi, "last_price", None))
            prev  = _safe_float(getattr(fi, "previous_close", None))
            return ticker, price, prev
        except Exception:
            return ticker, None, None

    quotes: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        for ticker, price, prev in pool.map(_get_quote, tickers):
            if price is not None:
                day_return = float((price - prev) / prev) if prev and prev > 0 else 0.0
                quotes[ticker] = {"price": price, "day_return": day_return}

    updated_scores = []
    for row in cached_scores:
        ticker = row.get("ticker")
        if ticker in quotes:
            row["price"]      = quotes[ticker]["price"]
            row["day_return"] = quotes[ticker]["day_return"]
        updated_scores.append(row)

    return updated_scores, {}   # 不更新 OHLCV，只更新報價


def _df_to_records(df) -> list[dict]:
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def _ohlcv_to_json(df) -> dict | None:
    if df is None or df.empty:
        return None
    needed = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[needed].dropna()
    if df.empty:
        return None
    dates = [str(d)[:10] for d in df.index]
    return {
        "dates":  dates,
        "open":   df["Open"].tolist()   if "Open"   in df else [],
        "high":   df["High"].tolist()   if "High"   in df else [],
        "low":    df["Low"].tolist()    if "Low"    in df else [],
        "close":  df["Close"].tolist()  if "Close"  in df else [],
        "volume": df["Volume"].tolist() if "Volume" in df else [],
    }


def _fetch_worker(universe: str):
    with _lock:
        _state["status"] = "fetching"
        _state["progress"] = "初始化…"
        _state["error"] = None

    def progress(done: int, total: int):
        with _lock:
            _state["progress"] = f"抓取中 {done}/{total}"

    try:
        if universe == "tw":
            raw = fetch_tw_all(progress_callback=progress)
            with _lock:
                _state["progress"] = "計算籌碼分數…"
            scores_df = compute_tw_scores(raw)
            with _lock:
                _state["progress"] = "整理 K 線資料…"
            ohlcv = {t: _ohlcv_to_json(d.get("ohlcv")) for t, d in raw.items() if d}
            with _lock:
                _state["status"]       = "done"
                _state["scores"]       = _df_to_records(scores_df)
                _state["ohlcv"]        = {k: v for k, v in ohlcv.items() if v}
                _state["universe"]     = universe
                _state["last_updated"] = datetime.now().strftime("%H:%M:%S")
                _state["progress"]     = "完成"
        else:
            tickers = _US_UNIVERSES.get(universe, get_sp500_tickers)()
            if "SPY" not in tickers:
                tickers = ["SPY"] + tickers

            # ── 計算哪些 ticker 還沒有基本面快取 ──
            raw = fetch_all(tickers, progress_callback=progress)
            with _lock:
                _state["progress"] = "計算動能分數…"
            scores_df = compute_scores(raw)
            with _lock:
                _state["progress"] = "整理 K 線資料…"
            ohlcv = {t: _ohlcv_to_json(df) for t, df in raw.items()}

            scored_tickers = set(scores_df["ticker"].tolist())
            missing = list(scored_tickers - set(_fund_cache.keys()))

            if missing:
                # 只抓「從未見過」的 ticker（首次啟動 = 全部；後續 = 新進入的）
                with _lock:
                    _state["progress"] = f"抓取基本面（{len(missing)} 檔新 ticker）…"
                logger.info("基本面快取缺少 %d 檔，開始抓取", len(missing))
                try:
                    new_fund = _fetch_fund_dict(missing)
                    _fund_cache.update(new_fund)
                    _save_fund_cache()   # 持久化到磁碟，重啟可重用
                except Exception as e:
                    logger.warning("Fundamentals fetch failed: %s", e)
            else:
                logger.info("基本面快取命中，跳過重新抓取")

            # 套用快取到本次評分結果
            scores_df = _apply_fund_dict(scores_df, _fund_cache)

            # US：評分 + 基本面全部完成後才設定 done
            with _lock:
                _state["status"]       = "done"
                _state["scores"]       = _df_to_records(scores_df)
                _state["ohlcv"]        = {k: v for k, v in ohlcv.items() if v}
                _state["universe"]     = universe
                _state["last_updated"] = datetime.now().strftime("%H:%M:%S")
                _state["progress"]     = "完成"

    except Exception as exc:
        logger.exception("Fetch worker error")
        with _lock:
            _state["status"] = "error"
            _state["error"] = str(exc)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _respond(self, status: int, ctype: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self._respond(status, "application/json; charset=utf-8", body)

    def _file(self, path: str):
        ext = os.path.splitext(path)[1]
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            self._respond(200, ctype, f.read())

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        try:
            if route in ("/", "/index.html"):
                self._file(os.path.join(WEB_DIR, "index.html"))

            elif route.startswith("/icons/") or route == "/manifest.json":
                path = os.path.join(WEB_DIR, route.lstrip("/"))
                if os.path.isfile(path):
                    self._file(path)
                else:
                    self._respond(404, "text/plain", b"Not Found")

            elif route == "/api/state":
                with _lock:
                    self._json({
                        "status":       _state["status"],
                        "progress":     _state["progress"],
                        "universe":     _state["universe"],
                        "last_updated": _state["last_updated"],
                        "error":        _state["error"],
                        "count":        len(_state["scores"]),
                    })

            elif route == "/api/scores":
                with _lock:
                    self._json({"universe": _state["universe"], "scores": _state["scores"]})

            elif route == "/api/ohlcv":
                ticker = params.get("ticker", [""])[0]
                with _lock:
                    data = _state["ohlcv"].get(ticker)
                self._json(data if data else {"error": "not found"}, 200 if data else 404)

            elif route == "/api/history":
                ticker = params.get("ticker", [""])[0]
                period = params.get("period", ["max"])[0]
                if period not in ("6mo", "1y", "2y", "5y", "10y", "max"):
                    period = "max"
                try:
                    import yfinance as yf
                    df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
                    if isinstance(df.columns, __import__("pandas").MultiIndex):
                        df = df.xs(ticker, axis=1, level=1)
                    data = _ohlcv_to_json(df)
                    self._json(data if data else {"error": "no data"}, 200 if data else 404)
                except Exception as exc:
                    logger.warning("history fetch failed %s: %s", ticker, exc)
                    self._json({"error": str(exc)}, 500)

            elif route == "/api/fetch":
                universe = params.get("universe", ["sp500"])[0]
                with _lock:
                    if _state["status"] == "fetching":
                        self._json({"error": "already fetching"}, 409)
                        return
                threading.Thread(target=_fetch_worker, args=(universe,), daemon=True).start()
                self._json({"status": "started"})

            elif route == "/api/live-refresh":
                universe = params.get("universe", [_state["universe"]])[0]
                if universe == "tw":
                    self._json({"error": "live refresh is only available for US stocks"}, 400)
                    return
                with _lock:
                    if _state["status"] != "done":
                        self._json({"error": "full refresh is not complete"}, 409)
                        return
                    if not _state["scores"]:
                        self._json({"error": "no cached scores for live refresh"}, 409)
                        return
                if not _live_refresh_lock.acquire(blocking=False):
                    self._json({"error": "already refreshing live prices"}, 409)
                    return
                try:
                    try:
                        scores, ohlcv = _refresh_live_scores(universe)
                    except ValueError as exc:
                        self._json({"error": str(exc)}, 409)
                        return
                    with _lock:
                        _state["scores"] = scores
                        _state["ohlcv"] = ohlcv
                        _state["universe"] = universe
                        _state["last_updated"] = datetime.now().strftime("%H:%M:%S")
                        _state["status"] = "done"
                        _state["progress"] = "即時行情完成"
                        _state["error"] = None
                        payload = {
                            "universe": _state["universe"],
                            "scores": _state["scores"],
                            "last_updated": _state["last_updated"],
                            "count": len(_state["scores"]),
                        }
                    self._json(payload)
                finally:
                    _live_refresh_lock.release()

            else:
                self._respond(404, "text/plain", b"Not Found")

        except Exception:
            logger.exception("Handler error: %s", route)
            self._respond(500, "text/plain", b"Internal Server Error")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _load_fund_cache()   # 啟動時載入今日基本面快取
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"  動能篩選器  →  {url}")
    print("  按 Ctrl+C 停止")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n伺服器已停止")


if __name__ == "__main__":
    main()
