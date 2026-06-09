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
from data.binance_fetcher import fetch_crypto_all, fetch_crypto_ticker_map
from data.bybit_fetcher import fetch_supplement_ohlcv, load_supplement_tickers
from data.twse_fetcher import fetch_tw_all
from data.tw_industry import refresh_industry_map_if_stale
from data.universe import get_combined_tickers, get_nasdaq100_tickers, get_sp500_tickers
from scoring.engine import apply_contextual_scoring, compute_scores, compute_breakout_candidates
from scoring.crypto_engine import compute_crypto_rs_scores
from scoring.tw_engine import (
    compute_tw_scores, compute_tw_rs_scores,
    compute_tw_observation_candidates,
    compute_tw_sector_rotation,
)

logger = logging.getLogger(__name__)

PORT = 5177
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_DATA_DIR = os.path.join(WEB_DIR, "data")

# Vercel serverless 環境偵測（VERCEL=1 由平台自動注入）
_IS_VERCEL = os.environ.get("VERCEL") == "1"


def _read_static(filename: str) -> dict:
    """從 web/data/ 讀取預產 JSON（供 Serverless / 首次載入 fallback）。"""
    import json as _json
    path = os.path.join(_DATA_DIR, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    return {}

_state: dict = {
    "status": "idle",
    "progress": "",
    "scores": [],
    "breakout_candidates": [],      # US 突破觀察清單
    "tw_rs_scores": [],             # 台股 RS Score 排名
    "tw_breakout_candidates": [],   # 台股突破觀察清單
    "tw_early_stage": [],           # 台股起漲觀察清單
    "tw_sector_rotation": [],       # 台股板塊資金輪動
    "crypto_rs_scores": [],         # 加密貨幣 RS 排名
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


# ─────────────────────────────────────────────────────────────
# TAIFEX MIS SockJS XHR 即時串流（後台常駐 Thread）
# 訂閱 TXF-S（現貨參考）、近月 -F 及 -M 合約，快取最新報價供
# /api/indices 直接讀取（免重複等候 REST 回應）。
# ─────────────────────────────────────────────────────────────
class _TaifexStream:
    """
    維持一條 SockJS XHR long-poll 連線到 TAIFEX MIS。
    斷線 / 封鎖 / 超時自動重連（指數退避，最長 60 秒）。

    公開介面：
      .get(symbol)  → dict | None   # 最新 values dict（field 125=last, 129=ref, …）
      .latest_txf() → dict | None   # 加工後的台指期報價 {price, change, change_pct, session}
    """

    # TAIFEX quote field mapping
    F_LAST  = "125"   # CLastPrice
    F_REF   = "129"   # CRefPrice (prev close)
    F_HIGH  = "130"   # CHighPrice
    F_LOW   = "131"   # CLowPrice
    F_DATE  = "144"   # CDate  YYYYMMDD
    F_TIME  = "143"   # CTime  HHMMSS
    F_VOL   = "140"   # CTotalVolume

    # 訂閱清單：現貨參考 + 所有近月期貨（含夜盤）
    _SUBSCRIBE = ["TXF-S", "TXF-P"]   # -S = 現貨快照；動態補 -F/-M 合約

    def __init__(self) -> None:
        self._lock    = threading.Lock()
        self._cache:  dict[str, dict]  = {}   # symbol → values dict
        self._thread: threading.Thread | None = None
        self._active  = False

    # ── public ────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._active = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="taifex-stream")
        self._thread.start()
        logger.info("TaifexStream started")

    def stop(self) -> None:
        self._active = False

    def get(self, symbol: str) -> dict | None:
        with self._lock:
            return dict(self._cache[symbol]) if symbol in self._cache else None

    def latest_txf(self) -> dict | None:
        """
        從快取組出台指期報價。
        策略：找成交量最大的 -F 合約；若夜盤（-M）有更新的成交時間則優先。
        """
        import math
        with self._lock:
            snapshot = dict(self._cache)

        def _f(vals: dict, key: str) -> float:
            try:
                return float(str(vals.get(key, "0")).replace(",", "") or 0)
            except Exception:
                return 0.0

        def _vol(vals: dict) -> int:
            try:
                return int(str(vals.get(self.F_VOL, "0")).replace(",", "") or 0)
            except Exception:
                return 0

        def _ts(sym: str, vals: dict) -> int:
            """把 CDate+CTime 轉成整數時間戳（夜盤跨夜 +1 day）。"""
            try:
                from datetime import datetime, timedelta
                d = vals.get(self.F_DATE, "19700101")
                t = vals.get(self.F_TIME, "000000").zfill(6)
                base = datetime.strptime(d + t, "%Y%m%d%H%M%S")
                if sym.endswith("-M") and int(t) < 90000:
                    base += timedelta(days=1)
                return int(base.timestamp())
            except Exception:
                return 0

        # 找所有 -F 合約
        day_contracts = {
            sym: vals for sym, vals in snapshot.items()
            if sym.endswith("-F") and _f(vals, self.F_LAST) > 0
        }
        if not day_contracts:
            return None

        # 近月 = 成交量最大的 -F
        best_f_sym = max(day_contracts, key=lambda s: _vol(day_contracts[s]))
        month_code = best_f_sym[:-2]          # e.g. "TXFF6"

        day_q  = snapshot.get(f"{month_code}-F")
        nite_q = snapshot.get(f"{month_code}-M")

        best, best_sym = day_q, f"{month_code}-F"
        if nite_q and _f(nite_q, self.F_LAST) > 0:
            if best is None or _ts(f"{month_code}-M", nite_q) > _ts(f"{month_code}-F", best):
                best, best_sym = nite_q, f"{month_code}-M"

        if not best:
            return None

        price = _f(best, self.F_LAST)
        ref   = _f(best, self.F_REF)
        if price <= 0:
            return None
        chg = price - ref
        chg_pct = chg / ref * 100 if ref else 0.0
        return {
            "price":      price,
            "change":     round(chg, 0),
            "change_pct": round(chg_pct, 2),
            "session":    "夜盤" if best_sym.endswith("-M") else "",
        }

    # ── internal ──────────────────────────────────────────────

    def _loop(self) -> None:
        """外層重連迴圈，指數退避。"""
        import time
        delay = 5
        while self._active:
            try:
                self._connect_and_stream()
                delay = 5            # 正常離開則重置退避
            except Exception as exc:
                logger.debug("TaifexStream session ended (%s), retry in %ds", exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)

    def _connect_and_stream(self) -> None:
        """建立一次 SockJS XHR 工作階段並持續 poll。"""
        import requests as _req
        import random, string, json as _json, time
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        sess = _req.Session()
        sess.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://mis.taifex.com.tw/futures/",
        })

        # 取 Cloudflare / BIG-IP cookie
        r0 = sess.get("https://mis.taifex.com.tw/futures/", timeout=10, verify=False)
        if r0.status_code >= 500:
            raise ConnectionError(f"homepage {r0.status_code}")

        server_id  = str(random.randint(100, 999))
        session_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        base = f"https://mis.taifex.com.tw/futures/rt/{server_id}/{session_id}"

        # ① open
        ro = sess.post(f"{base}/xhr", timeout=10, verify=False)
        if ro.status_code != 200 or ro.text.strip() != "o":
            raise ConnectionError(f"SockJS open failed: {ro.status_code} {ro.text[:30]!r}")

        # ② 先訂閱靜態清單；連線後再補 -F / -M 合約（等第一批 snapshot 回來）
        self._send_subscribe(sess, base, self._SUBSCRIBE)

        logger.info("TaifexStream connected (%s/%s)", server_id, session_id)
        dynamic_subscribed = False

        while self._active:
            try:
                rp = sess.post(f"{base}/xhr", timeout=30, verify=False)
            except _req.exceptions.ReadTimeout:
                # long-poll 超時是正常的（server 無新資料）
                continue

            if rp.status_code != 200:
                raise ConnectionError(f"poll {rp.status_code}")

            txt = rp.text
            if txt == "h\n":
                continue                        # heartbeat
            if txt.startswith("c"):
                raise ConnectionError("server closed")
            if not txt.startswith("a"):
                continue

            msgs = _json.loads(txt[1:])
            for raw in msgs:
                try:
                    msg = _json.loads(raw)
                except Exception:
                    continue

                if msg.get("type") != "quote":
                    continue

                q   = msg["quote"]
                sym = q.get("symbol", "")
                if not sym:
                    continue

                vals = {**q.get("trueValues", {}), **q.get("values", {})}
                with self._lock:
                    if sym in self._cache:
                        self._cache[sym].update(vals)
                    else:
                        self._cache[sym] = vals

                # 收到快照後，動態補訂 近月 -F 和 -M
                if not dynamic_subscribed and sym == "TXF-S":
                    dynamic_subscribed = True
                    threading.Thread(
                        target=self._subscribe_near_month,
                        args=(sess, base),
                        daemon=True,
                    ).start()

    def _subscribe_near_month(self, sess, base: str) -> None:
        """
        TXF-S 快照回來後，從 getQuoteList 找近月合約代碼並補訂閱。
        """
        import requests as _req, json as _json, time
        time.sleep(0.5)
        try:
            r = sess.post(
                "https://mis.taifex.com.tw/futures/api/getQuoteList",
                json={"CID": "TXF", "QuoteType": "0"},
                headers={"Content-Type": "application/json"},
                timeout=8, verify=False,
            )
            items = r.json().get("RtData", {}).get("QuoteList", []) or []
        except Exception as exc:
            logger.debug("TaifexStream getQuoteList failed: %s", exc)
            return

        def _vol(q):
            try: return int(str(q.get("CTotalVolume", "0")).replace(",", "") or 0)
            except: return 0

        day_contracts = [q for q in items if q.get("SymbolID", "").endswith("-F")]
        if not day_contracts:
            return
        near_month_id = max(day_contracts, key=_vol).get("SymbolID", "")   # e.g. TXFF6-F
        month_code    = near_month_id[:-2]                                  # e.g. TXFF6
        extra = [f"{month_code}-F", f"{month_code}-M"]
        logger.debug("TaifexStream subscribing near-month: %s", extra)
        self._send_subscribe(sess, base, extra)

    @staticmethod
    def _send_subscribe(sess, base: str, symbols: list[str]) -> None:
        import json as _json
        payload = _json.dumps({"type": "subscribe", "symbols": symbols})
        sess.post(
            f"{base}/xhr_send",
            data="[" + _json.dumps(payload) + "]",
            headers={"Content-Type": "text/plain"},
            timeout=8, verify=False,
        )


# 全域 stream 實例（server 啟動時呼叫 .start()）
_taifex_stream = _TaifexStream()


# ─────────────────────────────────────────────────────────────
# Binance WebSocket 即時串流（後台常駐 Thread）
# 訂閱 !miniTicker@arr → 取得所有 USDT 交易對的即時價格 / 24h 統計。
# 快取供 /api/stream/crypto SSE 端點每 10 秒重算 RS scores。
# ─────────────────────────────────────────────────────────────
class _BinanceStream:
    """
    維持一條 Binance WebSocket 到 !miniTicker@arr。
    每次收到全量推播（約 1 秒一次）就更新快取。
    斷線自動重連（指數退避，最長 60 秒）。

    公開介面：
      .get_ticker(symbol)  → dict | None   # {price, day_return, quote_volume, volume}
      .get_tickers()       → dict          # 全量快照（symbol → dict）
      .ready               → bool          # 是否已收到至少一次資料
    """

    _WS_URI = "wss://stream.binance.com:9443/ws/!miniTicker@arr"

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._ready  = False
        self._active = False
        self._thread: threading.Thread | None = None

    # ── public ────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._active = True
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="binance-stream")
        self._thread.start()
        logger.info("BinanceStream started")

    def stop(self) -> None:
        self._active = False

    @property
    def ready(self) -> bool:
        return self._ready

    def get_ticker(self, symbol: str) -> dict | None:
        with self._lock:
            return dict(self._cache[symbol]) if symbol in self._cache else None

    def get_tickers(self) -> dict:
        with self._lock:
            return dict(self._cache)

    # ── internal ──────────────────────────────────────────────

    def _run_thread(self) -> None:
        import asyncio
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        import asyncio
        delay = 5
        while self._active:
            try:
                await self._connect_and_stream()
                delay = 5
            except Exception as exc:
                logger.debug("BinanceStream session ended (%s), retry in %ds", exc, delay)
                import asyncio as _a
                await _a.sleep(delay)
                delay = min(delay * 2, 60)

    async def _connect_and_stream(self) -> None:
        import websockets, json as _json

        async with websockets.connect(
            self._WS_URI,
            ping_interval=20,
            ping_timeout=30,
            close_timeout=10,
        ) as ws:
            logger.info("BinanceStream connected to !miniTicker@arr")
            async for raw in ws:
                if not self._active:
                    break
                try:
                    items = _json.loads(raw)
                    batch: dict[str, dict] = {}
                    for item in items:
                        sym = item.get("s", "")
                        if not sym.endswith("USDT"):
                            continue
                        c = float(item.get("c") or 0)
                        o = float(item.get("o") or 0)
                        q = float(item.get("q") or 0)   # quote volume (USDT)
                        v = float(item.get("v") or 0)   # base volume
                        batch[sym] = {
                            "price":        c,
                            "day_return":   (c - o) / o if o > 0 else 0.0,
                            "quote_volume": q,
                            "volume":       v,
                        }
                    with self._lock:
                        self._cache.update(batch)
                    if not self._ready:
                        self._ready = True
                        logger.info("BinanceStream ready (%d symbols cached)", len(self._cache))
                except Exception as exc:
                    logger.debug("BinanceStream parse error: %s", exc)


# 全域 Binance stream 實例
_binance_stream = _BinanceStream()


class _BybitStream:
    """
    Bybit WebSocket 即時 ticker 串流，補充 Binance 未上架的幣種。

    訂閱 supplement_tickers.json 裡設定的幣種（tickers.{SYMBOL}）。
    斷線自動重連（指數退避，最長 60 秒）；斷線期間保留上次快取。

    公開介面與 _BinanceStream 一致：
      .get_ticker(symbol) → dict | None   # {price, day_return, quote_volume, volume}
      .get_tickers()      → dict          # 全量快照
      .ready              → bool
    """

    _WS_URI = "wss://stream.bybit.com/v5/public/spot"

    def __init__(self) -> None:
        self._lock    = threading.Lock()
        self._cache:  dict[str, dict] = {}
        self._ready   = False
        self._active  = False
        self._symbols: list[str] = []
        self._thread: threading.Thread | None = None

    # ── public ────────────────────────────────────────────────

    def start(self) -> None:
        tickers = load_supplement_tickers()
        self._symbols = [t["symbol"] for t in tickers]
        if not self._symbols:
            logger.info("BybitStream: no supplement tickers configured, skipping")
            self._ready = True
            return
        if self._thread and self._thread.is_alive():
            return
        self._active = True
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="bybit-stream")
        self._thread.start()
        logger.info("BybitStream started (%d symbols)", len(self._symbols))

    def stop(self) -> None:
        self._active = False

    @property
    def ready(self) -> bool:
        return self._ready

    def get_ticker(self, symbol: str) -> dict | None:
        with self._lock:
            return dict(self._cache[symbol]) if symbol in self._cache else None

    def get_tickers(self) -> dict:
        with self._lock:
            return dict(self._cache)

    # ── internal ──────────────────────────────────────────────

    def _run_thread(self) -> None:
        import asyncio
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        import asyncio
        delay = 5
        while self._active:
            try:
                await self._connect_and_stream()
                delay = 5
            except Exception as exc:
                logger.debug("BybitStream session ended (%s), retry in %ds", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _connect_and_stream(self) -> None:
        import asyncio, json as _json
        import websockets

        async with websockets.connect(
            self._WS_URI,
            ping_interval=None,   # Bybit 用 op:ping/pong，不用 ws 層 ping
            ping_timeout=30,
            close_timeout=10,
        ) as ws:
            # 訂閱所有補充幣種的 ticker 頻道
            await ws.send(_json.dumps({
                "op": "subscribe",
                "args": [f"tickers.{s}" for s in self._symbols],
            }))
            logger.info("BybitStream subscribed: %s", self._symbols)

            last_ping = asyncio.get_event_loop().time()

            async for raw in ws:
                if not self._active:
                    break

                # 每 20 秒送一次 heartbeat（Bybit 規格）
                now = asyncio.get_event_loop().time()
                if now - last_ping > 20:
                    await ws.send(_json.dumps({"op": "ping"}))
                    last_ping = now

                try:
                    msg = _json.loads(raw)
                except Exception:
                    continue

                # 略過非 ticker 訊息（subscribe ack、pong 等）
                if not str(msg.get("topic", "")).startswith("tickers."):
                    continue

                data = msg.get("data", {})
                symbol = data.get("symbol")
                if not symbol:
                    continue

                # price24hPcnt 是小數比例（0.0698 = 6.98%），不需 /100
                with self._lock:
                    self._cache[symbol] = {
                        "price":        _safe_float(data.get("lastPrice")),
                        "day_return":   _safe_float(data.get("price24hPcnt")),
                        "quote_volume": _safe_float(data.get("turnover24h")),
                        "volume":       _safe_float(data.get("volume24h")),
                    }
                    if not self._ready and len(self._cache) >= len(self._symbols):
                        self._ready = True
                        logger.info("BybitStream ready (%d symbols cached)", len(self._cache))


# 全域 Bybit stream 實例（補充非 Binance 幣種）
_bybit_stream = _BybitStream()

# Fear & Greed 日線快取（每日更新一次，避免 SSE 每 10 秒重打）
_fng_cache: dict = {}   # {value, updated_date}

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
    ".ttf":  "font/ttf",
    ".woff": "font/woff",
    ".woff2":"font/woff2",
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

# 板塊英文 → 繁體中文翻譯
SECTOR_ZH: dict[str, str] = {
    "Technology":             "科技",
    "Financial Services":     "金融服務",
    "Healthcare":             "醫療保健",
    "Consumer Cyclical":      "非必需消費",
    "Consumer Defensive":     "必需消費",
    "Energy":                 "能源",
    "Industrials":            "工業",
    "Basic Materials":        "基本材料",
    "Real Estate":            "房地產",
    "Utilities":              "公用事業",
    "Communication Services": "通訊服務",
}

_FUNDAMENTAL_COLUMNS = [
    "pe",
    "peg_ratio",
    "eps_beat",
    "eps_consecutive_beats",
    "sector_above_ema50",
    "sector",
    "sector_zh",   # 板塊（繁體中文翻譯）
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


def _fetch_fund_dict(tickers: list, sector_ema_by_etf=None) -> dict:
    """
    抓取各 ticker 的基本面與 sector context。

    回傳 {ticker: {pe, peg_ratio, eps_beat, eps_consecutive_beats,
    sector_above_ema50}}，供 final momentum_score 重新合成。
    """
    import yfinance as yf
    import time as _t

    total = len(tickers)
    if sector_ema_by_etf is None:
        sector_ema_by_etf = {etf: None for etf in set(SECTOR_ETF_MAP.values())}

    def _one(ticker: str) -> tuple[str, dict]:
        pe = peg_ratio = eps_beat = eps_consecutive_beats = sector_above_ema50 = None
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

            sector = info.get("sector")
            etf = SECTOR_ETF_MAP.get(sector)
            if etf:
                sector_above_ema50 = sector_ema_by_etf.get(etf)

            try:
                ed = t.earnings_dates
                if ed is not None and not ed.empty and "Surprise(%)" in ed.columns:
                    recent = ed.dropna(subset=["Surprise(%)"])
                    try:
                        recent = recent.sort_index(ascending=False)
                    except Exception:
                        pass
                    if not recent.empty:
                        eps_beat = _safe_float(recent["Surprise(%)"].iloc[0])
                    recent_two = recent.head(2)
                    if len(recent_two) >= 2:
                        eps_consecutive_beats = bool((recent_two["Surprise(%)"] >= 10).all())
            except Exception:
                pass
        except Exception:
            pass
        sector_name = sector or ""
        return ticker, {
            "pe": pe,
            "peg_ratio": peg_ratio,
            "eps_beat": eps_beat,
            "eps_consecutive_beats": eps_consecutive_beats,
            "sector_above_ema50": sector_above_ema50,
            "sector": sector_name,
            "sector_zh": SECTOR_ZH.get(sector_name, sector_name),  # 繁體中文板塊名稱
        }

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


def _compute_us_sentiment(today_chg: dict | None = None) -> dict:
    """
    美股市場氣氛值（0-100）：
      廣度訊號 45%  — _state["scores"] 中 p_60ma > 1.0 的比例
      VIX 訊號  45%  — ^VIX 當前值 + 與 20MA 的關係
      動能訊號  10%  — ^DJI / ^NDX 相對 MA20/50/200 的位置

    today_chg: {"^DJI": -1.35, "^NDX": -4.77, "^GSPC": -2.64}  (單位 %)，
               若傳入則優先使用，避免 yfinance 日K不含盤中資料的問題。

    Returns:
      { "dow":    { "score": int, "label": "偏多"|"中性"|"偏空" },
        "nasdaq": { "score": int, "label": ... } }
    """
    import yfinance as yf
    import numpy as np

    # ── 廣度 (0-45) ─────────────────────────────────────────────────────────────
    with _lock:
        scores = list(_state.get("scores", []))

    if scores:
        above = sum(1 for r in scores if (r.get("p_60ma") or 0) > 1.0)
        breadth_pct = above / len(scores)          # 0.0 ~ 1.0
        # 非線性映射：< 30% → 低分  50% → 20  > 70% → 40
        if breadth_pct >= 0.70:
            breadth_score = 40.0
        elif breadth_pct >= 0.30:
            breadth_score = (breadth_pct - 0.30) / 0.40 * 40.0
        else:
            breadth_score = 0.0
    else:
        breadth_score = 20.0  # 無資料時中性

    # ── VIX (0-45) ──────────────────────────────────────────────────────────────
    vix_score = 12.5  # default 中性
    try:
        vix_df = yf.Ticker("^VIX").history(period="2mo", auto_adjust=True, raise_errors=False)
        if vix_df is not None and len(vix_df) >= 2:
            vix_now = float(vix_df["Close"].iloc[-1])
            vix_ma20 = float(vix_df["Close"].tail(20).mean())

            # 絕對值分數（0-22）
            if vix_now < 15:
                base = 22.0
            elif vix_now < 18:
                base = 17.0
            elif vix_now < 20:
                base = 12.0
            elif vix_now < 25:
                base = 7.0
            elif vix_now < 30:
                base = 3.0
            else:
                base = 0.0

            # 趨勢加減分（±3）
            trend = 3.0 if vix_now < vix_ma20 else (-3.0 if vix_now > vix_ma20 * 1.2 else 0.0)
            vix_score = float(np.clip(base + trend, 0, 25))
    except Exception as exc:
        logger.debug("VIX fetch failed: %s", exc)

    # ── 動能 (0-10) 各指數獨立 ──────────────────────────────────────────────────
    def _momentum(symbol: str) -> float:
        try:
            df = yf.Ticker(symbol).history(period="1y", auto_adjust=True, raise_errors=False)
            if df is None or len(df) < 20:
                return 17.5
            close = df["Close"]
            price  = float(close.iloc[-1])
            ma10   = float(close.tail(10).mean())
            ma20   = float(close.tail(20).mean())
            ma50   = float(close.tail(50).mean())  if len(close) >= 50  else ma20
            ma100  = float(close.tail(100).mean()) if len(close) >= 100 else ma50
            score  = 0.0
            # 近期均線權重較高（反映短期靈敏度）
            if price > ma10:  score += 11.0
            if price > ma20:  score += 10.0
            if price > ma50:  score +=  8.0
            if price > ma100: score +=  6.0
            # 單日跌幅懲罰：優先用傳入的即時漲跌幅，避免 yfinance 日K不含盤中資料
            if today_chg and symbol in today_chg:
                chg1d = today_chg[symbol] / 100.0
            elif len(close) >= 2:
                prev  = float(close.iloc[-2])
                chg1d = (price - prev) / prev if prev else 0.0
            else:
                chg1d = 0.0
            if chg1d < -0.04:    score -= 20.0   # 跌逾 4%：重懲
            elif chg1d < -0.03:  score -= 15.0   # 跌逾 3%
            elif chg1d < -0.015: score -=  8.0   # 跌逾 1.5%
            elif chg1d > 0.03:   score +=  5.0   # 漲逾 3%：小加分
            return max(0.0, score)   # 0 ~ 35
        except Exception:
            return 17.5

    dow_mom    = _momentum("^DJI")
    nasdaq_mom = _momentum("^NDX")
    sp500_mom  = _momentum("^GSPC")

    # ── 合成 ────────────────────────────────────────────────────────────────────
    def _make(mom: float) -> dict:
        score = int(round(breadth_score + vix_score + mom))
        score = max(0, min(100, score))
        if score >= 80:
            label = "狂熱"
        elif score >= 60:
            label = "偏多"
        elif score >= 40:
            label = "中性"
        elif score >= 20:
            label = "偏空"
        else:
            label = "恐慌"
        return {"score": score, "label": label, "bullish": score >= 60}

    return {"dow": _make(dow_mom), "nasdaq": _make(nasdaq_mom), "sp500": _make(sp500_mom)}


def _fetch_vixtwn() -> dict | None:
    """
    從台灣期交所官網取得 VIXTWN（臺指選擇權波動率指數）。
    資料來源：https://www.taifex.com.tw/cht/7/getVixData?filesname=YYYYMMDD
    每15秒更新一次，檔案含當日所有分鐘數據。
    回傳 {"price": float, "change": float, "change_pct": float} 或 None。
    """
    import requests
    from datetime import datetime, timedelta

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer":    "https://www.taifex.com.tw/cht/7/vixMinNew",
    }

    def _get_trading_days(n: int) -> list[str]:
        """取最近 n 個交易日（簡化：排除週六日）。"""
        days = []
        d = datetime.now()
        while len(days) < n:
            d -= timedelta(days=1)
            if d.weekday() < 5:   # 0=Mon … 4=Fri
                days.append(d.strftime("%Y%m%d"))
        return days

    def _parse_vixtwn_file(date_str: str) -> float | None:
        """下載並解析指定日期的 VIXTWN 資料，回傳最後一筆收盤值。"""
        url = f"https://www.taifex.com.tw/cht/7/getVixData?filesname={date_str}"
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200 or len(r.content) < 100:
                return None
            text = r.content.decode("utf-8", errors="replace")
            last_val = None
            for line in text.split("\r\n"):
                parts = [p.strip() for p in line.split("\t") if p.strip()]
                if len(parts) >= 2:
                    try:
                        val = float(parts[-1])
                        last_val = val
                    except ValueError:
                        pass
            return last_val
        except Exception as exc:
            logger.debug("VIXTWN file fetch %s failed: %s", date_str, exc)
            return None

    try:
        # 嘗試取今日資料（盤中）
        today_str = datetime.now().strftime("%Y%m%d")
        today_val = _parse_vixtwn_file(today_str)

        # 若今日沒有資料（休市/週末），改取最近一個交易日
        recent_days = _get_trading_days(3)
        if today_val is None:
            for d in recent_days:
                today_val = _parse_vixtwn_file(d)
                if today_val is not None:
                    today_str = d
                    break

        if today_val is None:
            return None

        # 取前一交易日作為 ref（計算漲跌）
        # 如果 today_str 已是最近交易日，往前再找一天
        prev_val = None
        found_today = False
        for d in [today_str] + recent_days:
            if d == today_str:
                found_today = True
                continue
            if found_today:
                prev_val = _parse_vixtwn_file(d)
                if prev_val is not None:
                    break

        chg = today_val - prev_val if prev_val is not None else 0.0
        chg_pct = chg / prev_val * 100 if prev_val else 0.0
        return {
            "price":      round(today_val, 2),
            "change":     round(chg, 2),
            "change_pct": round(chg_pct, 2),
        }
    except Exception as exc:
        logger.debug("VIXTWN fetch failed: %s", exc)
        return None


def _fetch_tw_futures() -> dict | None:
    """
    取台指期近月報價。
    優先使用 _taifex_stream 快取（SockJS 即時流）；
    若串流尚未就緒，fallback 到 TAIFEX MIS REST getQuoteList。
    回傳 {"price": float, "change": float, "change_pct": float, "session": str} 或 None。
    """
    # ── 優先讀 SockJS 串流快取 ────────────────────────────────
    streamed = _taifex_stream.latest_txf()
    if streamed is not None:
        return streamed
    # ── Fallback：REST API ───────────────────────────────────
    import requests
    from datetime import datetime, timedelta
    try:
        r = requests.post(
            "https://mis.taifex.com.tw/futures/api/getQuoteList",
            json={"CID": "TXF", "QuoteType": "0"},
            headers={
                "User-Agent":   "Mozilla/5.0",
                "Referer":      "https://mis.taifex.com.tw/",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        data  = r.json()
        items = data.get("RtData", {}).get("QuoteList") or []

        def _f(q, key):
            try: return float(str(q.get(key, "0")).replace(",", "") or 0)
            except: return 0.0

        def _vol(q):
            try: return int(str(q.get("CTotalVolume", "0")).replace(",", "") or 0)
            except: return 0

        def _effective_dt(q) -> datetime:
            """把 CDate+CTime 轉成實際時間；夜盤 CTime<090000 表示跨夜，日期+1。"""
            try:
                d = q.get("CDate", "19700101")
                t = q.get("CTime", "000000").zfill(6)
                base = datetime.strptime(d + t, "%Y%m%d%H%M%S")
                # 夜盤合約（-M）且時間在凌晨 → 實為次日
                if q.get("SymbolID", "").endswith("-M") and int(t) < 90000:
                    base += timedelta(days=1)
                return base
            except Exception:
                return datetime.min

        # 只取 -F 和 -M（排除現貨列 TXF-P / TXF-S）
        candidates = [
            q for q in items
            if (q.get("SymbolID", "").endswith("-F") or q.get("SymbolID", "").endswith("-M"))
            and q.get("CLastPrice", "")
        ]
        if not candidates:
            return None

        # 先找近月（成交量最大的 -F）確定月份代碼
        day_contracts = [q for q in candidates if q.get("SymbolID", "").endswith("-F")]
        if not day_contracts:
            return None
        near_month_id = max(day_contracts, key=_vol).get("SymbolID", "")  # e.g. "TXFF6-F"
        month_code    = near_month_id[:-2]  # e.g. "TXFF6"

        # 找對應的日盤與夜盤
        day_q  = next((q for q in candidates if q.get("SymbolID") == f"{month_code}-F"), None)
        nite_q = next((q for q in candidates if q.get("SymbolID") == f"{month_code}-M"), None)

        # 取兩者中較新的那個
        best = day_q
        if nite_q and _f(nite_q, "CLastPrice") > 0:
            if best is None or _effective_dt(nite_q) > _effective_dt(best):
                best = nite_q

        if not best:
            return None

        price = _f(best, "CLastPrice")
        ref   = _f(best, "CRefPrice")
        chg   = _f(best, "CDiff")
        if price <= 0:
            return None
        chg_pct = chg / ref * 100 if ref else 0.0
        session = "夜盤" if best.get("SymbolID", "").endswith("-M") else ""
        return {
            "price":      price,
            "change":     chg,
            "change_pct": round(chg_pct, 2),
            "session":    session,   # 前端可選用，標示夜盤
        }
    except Exception as exc:
        logger.debug("台指期 fetch failed: %s", exc)
        return None


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def _linear_score(value: float | None, low: float, high: float) -> float:
    if value is None:
        return 50.0
    if high == low:
        return 50.0
    return _clamp((float(value) - low) / (high - low) * 100.0)


def _sentiment_label(score: int) -> str:
    if score >= 80:
        return "狂熱"
    if score >= 65:
        return "偏多"
    if score >= 50:
        return "中性"
    if score >= 35:
        return "偏空"
    return "恐慌"


def _close_values_from_ohlcv(data: dict | None) -> list[float]:
    if not data:
        return []
    closes = data.get("close") or data.get("Close") or []
    result = []
    for v in closes:
        f = _safe_float(v)
        if f is not None and f > 0:
            result.append(f)
    return result


def _ratio_to_market_score(ratio: float | None) -> float:
    """Map market participation ratios into a neutral 50 around 50% breadth."""
    return _linear_score(ratio, 0.25, 0.75)


def _compute_breadth_component(rows: list[dict], ohlcv: dict) -> float:
    """
    Breadth score (0-100): combines 20D positive-return breadth and MA participation.
    20D return > 0 carries 40%, price > MA20 30%, price > MA60 30%.
    """
    if not rows:
        return 50.0

    ret_rows = [r for r in rows if r.get("ret_20d") is not None]
    ret_ratio = (
        sum(1 for r in ret_rows if (r.get("ret_20d") or 0) > 0) / len(ret_rows)
        if ret_rows else None
    )

    ma20_hit = ma20_total = 0
    ma60_hit = ma60_total = 0
    for r in rows:
        closes = _close_values_from_ohlcv(ohlcv.get(r.get("ticker")))
        if len(closes) >= 20:
            ma20_total += 1
            if closes[-1] > sum(closes[-20:]) / 20:
                ma20_hit += 1
        if len(closes) >= 60:
            ma60_total += 1
            if closes[-1] > sum(closes[-60:]) / 60:
                ma60_hit += 1

    ma20_ratio = ma20_hit / ma20_total if ma20_total else None
    ma60_ratio = ma60_hit / ma60_total if ma60_total else None
    return (
        _ratio_to_market_score(ret_ratio) * 0.40 +
        _ratio_to_market_score(ma20_ratio) * 0.30 +
        _ratio_to_market_score(ma60_ratio) * 0.30
    )


def _compute_flow_component(rows: list[dict]) -> float:
    """
    Turnover / capital diffusion score (0-100).
    Uses up-turnover share and turnover-weighted amount_ratio expansion.
    """
    if not rows:
        return 50.0

    up_value = 0.0
    down_value = 0.0
    weighted_amount_ratio = 0.0
    weight_total = 0.0
    for r in rows:
        turnover = _safe_float(r.get("turnover_10k")) or 0.0
        if turnover <= 0:
            turnover = (_safe_float(r.get("price")) or 0.0) * (_safe_float(r.get("volume")) or 0.0) / 10.0
        ret = _safe_float(r.get("day_return")) or 0.0
        if ret > 0:
            up_value += turnover
        elif ret < 0:
            down_value += turnover
        amount_ratio = _safe_float(r.get("amount_ratio"))
        if turnover > 0 and amount_ratio is not None:
            weighted_amount_ratio += turnover * amount_ratio
            weight_total += turnover

    total_directional = up_value + down_value
    up_share = up_value / total_directional if total_directional > 0 else None
    expansion = weighted_amount_ratio / weight_total if weight_total > 0 else None

    return _ratio_to_market_score(up_share) * 0.60 + _linear_score(expansion, 0.80, 1.50) * 0.40


def _compute_index_momentum_component(closes: list[float], today_chg_pct: float | None = None) -> float:
    """
    Index momentum score (0-100): trend location, MA slopes, and 5D/20D returns.
    Intraday/1D change is only a small adjustment.
    """
    closes = [float(c) for c in closes if c and c > 0]
    if len(closes) < 20:
        return 50.0

    price = closes[-1]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else ma20

    location = (50.0 if price > ma20 else 0.0) + (50.0 if price > ma60 else 0.0)

    slope_parts = []
    if len(closes) >= 40:
        prev_ma20 = sum(closes[-40:-20]) / 20
        slope_parts.append(_linear_score((ma20 - prev_ma20) / prev_ma20 if prev_ma20 else None, -0.02, 0.02))
    if len(closes) >= 120:
        prev_ma60 = sum(closes[-120:-60]) / 60
        slope_parts.append(_linear_score((ma60 - prev_ma60) / prev_ma60 if prev_ma60 else None, -0.03, 0.03))
    slope = sum(slope_parts) / len(slope_parts) if slope_parts else 50.0

    ret_parts = []
    if len(closes) >= 6 and closes[-6] > 0:
        ret_parts.append(_linear_score((price - closes[-6]) / closes[-6], -0.03, 0.03))
    if len(closes) >= 21 and closes[-21] > 0:
        ret_parts.append(_linear_score((price - closes[-21]) / closes[-21], -0.08, 0.08))
    ret_score = sum(ret_parts) / len(ret_parts) if ret_parts else 50.0

    score = location * 0.40 + slope * 0.35 + ret_score * 0.25
    if today_chg_pct is not None:
        if today_chg_pct <= -4.0:
            score -= 15.0
        elif today_chg_pct <= -2.0:
            score -= 8.0
        elif today_chg_pct >= 3.0:
            score += 5.0
        elif today_chg_pct >= 1.5:
            score += 3.0
    return _clamp(score)


def _compute_volatility_component(vix_now: float | None, vix_chg_pct: float | None = None) -> float:
    """
    Volatility risk score (0-100): lower volatility is better.
    Uses calibrated VIXTWN bands plus a single-day spike penalty.
    """
    if vix_now is None:
        return 50.0
    # VIXTWN roughly in the low teens is calm; above 30 is panic.
    score = 100.0 - _linear_score(vix_now, 12.0, 32.0)
    if vix_chg_pct is not None:
        if vix_chg_pct >= 30:
            score -= 35.0
        elif vix_chg_pct >= 15:
            score -= 22.0
        elif vix_chg_pct >= 8:
            score -= 10.0
        elif vix_chg_pct <= -10:
            score += 8.0
    return _clamp(score)


def _compute_tw_sentiment(vix_now: float | None = None, vix_chg_pct: float | None = None) -> dict:
    """
    台股市場氣氛值（0-100）：
      廣度訊號 45%  — 20D 報酬、price > MA20、price > MA60 的市場參與度
      動能訊號 30%  — 指數位置、MA 斜率、5D/20D 報酬；當日漲跌只做小幅修正
      波動訊號 20%  — VIXTWN 絕對值 + 單日急升懲罰（無資料時 fallback ^VIX）
      資金訊號  5%  — 上漲成交值占比 + 成交值擴張

    vix_now / vix_chg_pct：由 indices handler 傳入即時值（VIXTWN），避免重複抓歷史日K。

    Returns:
      { "market": { "score": int, "label": ... },   # 大盤（TWSE .TW）
        "otc":    { "score": int, "label": ... } }   # 櫃買（TPEX .TWO）
    """
    import yfinance as yf

    with _lock:
        scores = list(_state.get("scores", []))
        ohlcv = dict(_state.get("ohlcv", {}))

    def _rows(suffix: str) -> list[dict]:
        return [
            r for r in scores
            if r.get("ticker", "").endswith(suffix)
            and r.get("stock_type") == "stock"
        ]

    twse_rows = _rows(".TW")
    tpex_rows = _rows(".TWO")

    # ── 波動訊號 (0-100) 共用 VIXTWN ─────────────────────────────────────────
    # 優先使用 indices handler 傳入的即時值，避免歷史日K不含今日資料的問題。
    try:
        _vix = vix_now
        _vix_chg = vix_chg_pct
        if _vix is None:
            vix_df = yf.Ticker("^VIX").history(period="2d", auto_adjust=True, raise_errors=False)
            if vix_df is not None and len(vix_df) >= 2:
                _vix = float(vix_df["Close"].iloc[-1])
                prev = float(vix_df["Close"].iloc[-2])
                _vix_chg = (_vix - prev) / prev * 100 if prev else 0.0
        vix_score = _compute_volatility_component(_vix, _vix_chg)
    except Exception as exc:
        logger.debug("VIX fetch for TW sentiment failed: %s", exc)
        vix_score = 50.0

    # ── 指數動能 (0-100) 各指數獨立 ─────────────────────────────────────────
    twse_mom = 50.0
    try:
        df = yf.Ticker("^TWII").history(period="1y", auto_adjust=True, raise_errors=False)
        if df is not None and len(df) >= 20:
            twse_mom = _compute_index_momentum_component([float(v) for v in df["Close"].dropna()])
    except Exception as exc:
        logger.debug("TWSE momentum failed: %s", exc)

    # 櫃買指數動能：yfinance 無此 symbol，改用 TPEX open API 歷史資料
    tpex_mom = 50.0
    try:
        import requests as _rq, warnings as _wn
        with _wn.catch_warnings():
            _wn.simplefilter("ignore")
            tpex_hist = _rq.get(
                "https://www.tpex.org.tw/openapi/v1/tpex_index",
                headers={"User-Agent": "Mozilla/5.0"},
                verify=False, timeout=10,
            ).json()
        if len(tpex_hist) >= 20:
            closes = [float(r.get("Close", 0) or 0) for r in tpex_hist[-120:]]
            tpex_mom = _compute_index_momentum_component(closes)
    except Exception as exc:
        logger.debug("TPEX momentum failed: %s", exc)

    # ── 合成 ────────────────────────────────────────────────────────────────
    def _make(rows: list[dict], mom: float) -> dict:
        breadth = _compute_breadth_component(rows, ohlcv)
        flow = _compute_flow_component(rows)
        score = int(round(
            breadth * 0.45 +
            mom * 0.30 +
            vix_score * 0.20 +
            flow * 0.05
        ))
        score = max(0, min(100, score))
        label = _sentiment_label(score)
        return {
            "score": score,
            "label": label,
            "bullish": score >= 65,
            "components": {
                "breadth": round(breadth, 1),
                "momentum": round(mom, 1),
                "volatility": round(vix_score, 1),
                "flow": round(flow, 1),
            },
        }

    return {
        "market": _make(twse_rows, twse_mom),
        "otc":    _make(tpex_rows, tpex_mom),
    }


def _compute_sector_metrics(scores_df) -> object:
    """
    板塊雙層結構：
      sector_rs         — 板塊內所有個股 rs_rating 的平均值
      sector_rank       — 個股在板塊內的排名，格式 'N/M'
      sector_multiplier — 強板塊 ×1.10 / 中性 ×1.00 / 弱板塊 ×0.90
    """
    import pandas as pd

    df = scores_df.copy()
    if "sector" not in df.columns or "rs_rating" not in df.columns:
        df["sector_rs"]         = 0.0
        df["sector_rank"]       = ""
        df["sector_multiplier"] = 1.0
        return df

    # 每個板塊的平均 rs_rating
    sector_avg = df.groupby("sector")["rs_rating"].mean().rename("sector_rs")
    df = df.merge(sector_avg, on="sector", how="left")
    df["sector_rs"] = df["sector_rs"].fillna(50.0)

    # 板塊內個股排名（rs_rating 降序）
    df["sector_rank"] = df.groupby("sector")["rs_rating"].rank(
        ascending=False, method="min"
    ).astype(int)
    sector_count = df.groupby("sector")["ticker"].transform("count")
    df["sector_rank"] = df["sector_rank"].astype(str)   # 只顯示排名，不附 /N

    # 板塊乘數：全部板塊的 sector_rs 做三分位
    all_sector_rs = sector_avg.values
    q30 = float(pd.Series(all_sector_rs).quantile(0.70))  # 前 30%
    q70 = float(pd.Series(all_sector_rs).quantile(0.30))  # 後 30%
    def _mult(sr: float) -> float:
        if sr >= q30: return 1.10
        if sr <= q70: return 0.90
        return 1.00
    df["sector_multiplier"] = df["sector_rs"].apply(_mult)

    return df


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
    if universe == "crypto":
        return _refresh_crypto_live_scores()

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


def _ohlcv_json_to_df(data: dict | None):
    if not data:
        return None
    dates = data.get("dates") or []
    closes = data.get("close") or []
    if not dates or not closes or len(dates) != len(closes):
        return None
    df = pd.DataFrame({
        "Open": data.get("open") or closes,
        "High": data.get("high") or closes,
        "Low": data.get("low") or closes,
        "Close": closes,
        "Volume": data.get("volume") or [0] * len(closes),
    }, index=pd.to_datetime(dates))
    return df


def _refresh_crypto_live_scores(
    tickers: dict | None = None,
) -> tuple[list[dict], dict]:
    """
    從最新價格重算加密貨幣 RS scores。

    tickers: symbol → {price, day_return, quote_volume, volume}
      - 傳入 None 時從 Binance REST API 拉取（舊行為）
      - 傳入 _binance_stream.get_tickers() 時使用 WebSocket 快取（SSE 模式）
    """
    with _lock:
        cached_ohlcv = {ticker: dict(data) for ticker, data in _state["ohlcv"].items() if data}

    if not cached_ohlcv:
        raise ValueError("no cached crypto OHLCV for live refresh")

    if tickers is None:
        tickers = fetch_crypto_ticker_map()

    raw: dict[str, dict] = {}
    updated_ohlcv: dict[str, dict] = {}

    for ticker, data in cached_ohlcv.items():
        quote = tickers.get(ticker, {})
        df = _ohlcv_json_to_df(data)
        if df is None or df.empty:
            continue
        price = _safe_float(quote.get("price"))
        volume = _safe_float(quote.get("volume"))
        if price is not None and price > 0:
            last_idx = df.index[-1]
            df.loc[last_idx, "Close"] = price
            df.loc[last_idx, "High"] = max(float(df.loc[last_idx, "High"]), price)
            df.loc[last_idx, "Low"] = min(float(df.loc[last_idx, "Low"]), price)
            if volume is not None and volume > 0:
                df.loc[last_idx, "Volume"] = volume
        raw[ticker] = {
            "ticker": ticker,
            "name": ticker.removesuffix("USDT"),
            "price": quote.get("price", price or 0.0),
            "day_return": quote.get("day_return", 0.0),
            "quote_volume": quote.get("quote_volume", 0.0),
            "volume": quote.get("volume", 0.0),
            "ohlcv": df,
        }
        updated = _ohlcv_to_json(df)
        if updated:
            updated_ohlcv[ticker] = updated

    scores_df = compute_crypto_rs_scores(raw)
    return _df_to_records(scores_df), updated_ohlcv


def _get_fear_greed() -> dict | None:
    """
    取得 Fear & Greed Index（每日快取，不每次重打 API）。
    回傳 {"value": int, "label": str} 或 None。
    """
    global _fng_cache
    today = datetime.now().strftime("%Y-%m-%d")
    if _fng_cache.get("date") == today:
        return _fng_cache.get("data")
    try:
        import requests as _req
        payload = _req.get(
            "https://api.alternative.me/fng/",
            params={"limit": 1, "format": "json"},
            timeout=10,
        ).json()
        item = (payload.get("data") or [{}])[0]
        if item.get("value") is not None:
            data = {
                "value":       int(item["value"]),
                "label":       item.get("value_classification", ""),
                "price":       float(item["value"]),
                "change_pct":  None,
            }
            _fng_cache = {"date": today, "data": data}
            return data
    except Exception as exc:
        logger.debug("Fear & Greed fetch failed: %s", exc)
    return None


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
            try:
                refresh_industry_map_if_stale()   # 官方產業別快取（補充板塊「未分類」）
            except Exception as exc:
                logger.warning("產業別快取更新失敗：%s", exc)
            with _lock:
                _state["progress"] = "計算 RS Score…"
            tw_rs_df = compute_tw_rs_scores(raw)
            with _lock:
                _state["progress"] = "計算籌碼分數…"
            scores_df = compute_tw_scores(raw)
            sector_rotation = sorted(
                compute_tw_sector_rotation(raw).values(),
                key=lambda r: r.get("sector_flow_score", 0),
                reverse=True,
            )
            for i, row in enumerate(sector_rotation, start=1):
                row["rank"] = i
            with _lock:
                _state["progress"] = "掃描突破 / 起漲候選…"
            tw_bk_df, tw_early_df = compute_tw_observation_candidates(raw)
            with _lock:
                _state["progress"] = "整理 K 線資料…"
            ohlcv = {t: _ohlcv_to_json(d.get("ohlcv")) for t, d in raw.items() if d}
            with _lock:
                _state["status"]                = "done"
                _state["scores"]                = _df_to_records(scores_df)
                _state["tw_rs_scores"]          = _df_to_records(tw_rs_df)
                _state["tw_sector_rotation"]    = sector_rotation
                _state["tw_breakout_candidates"] = _df_to_records(tw_bk_df)
                _state["tw_early_stage"]        = _df_to_records(tw_early_df)
                _state["ohlcv"]                 = {k: v for k, v in ohlcv.items() if v}
                _state["universe"]              = universe
                _state["last_updated"]          = datetime.now().strftime("%H:%M:%S")
                _state["progress"]              = "完成"
        elif universe == "crypto":
            raw = fetch_crypto_all(progress_callback=progress)
            # 合併 Bybit 補充幣種（Binance 未上架，如 HYPE）
            supplement = fetch_supplement_ohlcv()
            if supplement:
                raw = {**raw, **supplement}   # Binance 優先；supplement 填補空缺
                logger.info("Supplement tickers merged: %s", list(supplement.keys()))
            with _lock:
                _state["progress"] = "計算加密貨幣 RS Rating…"
            crypto_rs_df = compute_crypto_rs_scores(raw)
            with _lock:
                _state["progress"] = "整理 K 線資料…"
            ohlcv = {t: _ohlcv_to_json(d.get("ohlcv")) for t, d in raw.items() if d}
            with _lock:
                _state["status"]           = "done"
                _state["scores"]           = _df_to_records(crypto_rs_df)
                _state["crypto_rs_scores"] = _df_to_records(crypto_rs_df)
                _state["ohlcv"]            = {k: v for k, v in ohlcv.items() if v}
                _state["universe"]         = universe
                _state["last_updated"]     = datetime.now().strftime("%H:%M:%S")
                _state["progress"]         = "完成"
        else:
            tickers = _US_UNIVERSES.get(universe, get_sp500_tickers)()
            if "SPY" not in tickers:
                tickers = ["SPY"] + tickers

            # ── 計算哪些 ticker 還沒有基本面快取 ──
            raw = fetch_all(tickers, progress_callback=progress)
            with _lock:
                _state["progress"] = "計算動能分數…"
            scores_df = compute_scores(raw)
            # 突破觀察：全 universe 原始篩選，不受 TOP_N 限制
            with _lock:
                _state["progress"] = "掃描突破候選…"
            breakout_df = compute_breakout_candidates(raw)
            with _lock:
                _state["progress"] = "整理 K 線資料…"
            ohlcv = {t: _ohlcv_to_json(df) for t, df in raw.items()}

            scored_tickers = set(scores_df["ticker"].tolist())
            missing = [
                ticker for ticker in scored_tickers
                if ticker not in _fund_cache
                or any(col not in _fund_cache.get(ticker, {}) for col in _FUNDAMENTAL_COLUMNS)
            ]

            if missing:
                # 只抓「從未見過」的 ticker（首次啟動 = 全部；後續 = 新進入的）
                with _lock:
                    _state["progress"] = f"抓取基本面（{len(missing)} 檔新 ticker）…"
                logger.info("基本面快取缺少 %d 檔，開始抓取", len(missing))
                try:
                    with _lock:
                        _state["progress"] = "抓取板塊趨勢…"
                    sector_ema = _fetch_sector_ema(scores_df)
                    new_fund = _fetch_fund_dict(missing, sector_ema_by_etf=sector_ema)
                    _fund_cache.update(new_fund)
                    _save_fund_cache()   # 持久化到磁碟，重啟可重用
                except Exception as e:
                    logger.warning("Fundamentals fetch failed: %s", e)
            else:
                logger.info("基本面快取命中，跳過重新抓取")

            # 套用快取到本次評分結果
            scores_df = _apply_fund_dict(scores_df, _fund_cache)
            # 板塊雙層結構：sector_rs / sector_rank / sector_multiplier
            scores_df = _compute_sector_metrics(scores_df)
            # 加入基本面加成 + adjusted_score（板塊乘數後的最終分數）
            scores_df = apply_contextual_scoring(scores_df)

            # US：評分 + 基本面全部完成後才設定 done
            with _lock:
                _state["status"]             = "done"
                _state["scores"]             = _df_to_records(scores_df)
                _state["breakout_candidates"] = _df_to_records(breakout_df) if not breakout_df.empty else []
                _state["ohlcv"]              = {k: v for k, v in ohlcv.items() if v}
                _state["universe"]           = universe
                _state["last_updated"]       = datetime.now().strftime("%H:%M:%S")
                _state["progress"]           = "完成"

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

            elif route.startswith("/fonts/") or route.startswith("/icons/") or route == "/manifest.json":
                path = os.path.join(WEB_DIR, route.lstrip("/"))
                if os.path.isfile(path):
                    self._file(path)
                else:
                    self._respond(404, "text/plain", b"Not Found")

            elif route == "/api/stream/tw-indices":
                # ── Server-Sent Events：台股即時指數推播 ─────────────────
                # 瀏覽器用 EventSource('/api/stream/tw-indices') 訂閱；
                # 每 5 秒送一次 JSON（資料來自 _taifex_stream 快取 + VIXTWN）。
                self.send_response(200)
                self.send_header("Content-Type",  "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection",    "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    import time as _time, yfinance as yf
                    while True:
                        try:
                            indices_data = []
                            # 加權指數（yfinance ^TWII）
                            try:
                                twii = yf.Ticker("^TWII").history(
                                    period="2d", auto_adjust=True, raise_errors=False
                                )
                                if twii is not None and not twii.empty:
                                    last = float(twii["Close"].iloc[-1])
                                    prev = float(twii["Close"].iloc[-2]) if len(twii) >= 2 else last
                                    indices_data.append({
                                        "name": "加權指數",
                                        "price": last,
                                        "change": last - prev,
                                        "change_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
                                    })
                            except Exception:
                                pass

                            # 台指期（SockJS 串流快取，fallback REST）
                            fut = _fetch_tw_futures()
                            if fut:
                                indices_data.append({"name": "台指期", **fut})

                            # 恐慌指數（VIXTWN）
                            vixtwn = _fetch_vixtwn()
                            if vixtwn:
                                indices_data.append({"name": "恐慌指數", **vixtwn})

                            if indices_data:
                                payload = json.dumps({"indices": indices_data})
                                self.wfile.write(
                                    f"data: {payload}\n\n".encode("utf-8")
                                )
                                self.wfile.flush()
                        except Exception as _exc:
                            logger.debug("SSE tw-indices build error: %s", _exc)

                        _time.sleep(5)   # 每 5 秒推一次
                except (BrokenPipeError, ConnectionResetError):
                    pass   # 瀏覽器關閉連線，正常結束

            elif route == "/api/stream/crypto":
                # ── Server-Sent Events：加密貨幣即時串流 ─────────────────────
                # 瀏覽器用 EventSource('/api/stream/crypto') 訂閱；
                # 每 10 秒推一次 {indices, scores}（價格來自 Binance WebSocket 快取）。
                self.send_response(200)
                self.send_header("Content-Type",  "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection",    "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    import time as _time
                    while True:
                        try:
                            # ① 即時指數（BTC / ETH / 貪婪指數）
                            indices: list[dict] = []
                            for name, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
                                tk = _binance_stream.get_ticker(sym)
                                if tk:
                                    indices.append({
                                        "name":       name,
                                        "price":      tk["price"],
                                        "change_pct": round(tk["day_return"] * 100, 2),
                                    })
                            fng = _get_fear_greed()
                            if fng:
                                indices.append({"name": "貪婪指數", **fng})

                            # ② RS 排名（只在 WebSocket 有資料且快取 OHLCV 存在時計算）
                            scores: list[dict] = []
                            if _binance_stream.ready:
                                try:
                                    # Binance 優先；Bybit 補充幣種填補空缺
                                    tickers = {**_bybit_stream.get_tickers(), **_binance_stream.get_tickers()}
                                    new_scores, updated_ohlcv = _refresh_crypto_live_scores(tickers)
                                    scores = new_scores
                                    if updated_ohlcv:
                                        with _lock:
                                            _state["ohlcv"].update(updated_ohlcv)
                                            _state["crypto_rs_scores"] = scores
                                except Exception as _se:
                                    logger.debug("crypto SSE score refresh: %s", _se)

                            payload = json.dumps({
                                "indices": indices,
                                "scores":  scores,
                            })
                            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except Exception as _exc:
                            logger.debug("SSE crypto build error: %s", _exc)

                        _time.sleep(5)   # 每 5 秒推一次
                except (BrokenPipeError, ConnectionResetError):
                    pass   # 瀏覽器關閉連線，正常結束

            elif route == "/api/state":
                with _lock:
                    status   = _state["status"]
                    progress = _state["progress"]
                    universe = _state["universe"]
                    updated  = _state["last_updated"]
                    error    = _state["error"]
                    count    = len(_state["scores"])
                # Serverless fallback：Vercel 上無背景 worker，直接用預產 JSON
                if _IS_VERCEL or (status == "idle" and count == 0):
                    meta = _read_static("meta.json")
                    us   = _read_static("us_scores.json")
                    if meta or us:
                        status   = "done"
                        progress = "靜態資料（每日收盤後更新）"
                        universe = "all"
                        updated  = meta.get("last_updated", updated)
                        count    = us.get("count", len(us.get("scores", [])))
                self._json({
                    "status":       status,
                    "progress":     progress,
                    "universe":     universe,
                    "last_updated": updated,
                    "error":        error,
                    "count":        count,
                })

            elif route == "/api/scores":
                with _lock:
                    scores  = _state["scores"]
                    universe = _state["universe"]
                if not scores:
                    data = _read_static("us_scores.json")
                    if data:
                        self._json(data)
                        return
                self._json({"universe": universe, "scores": scores})

            elif route == "/api/breakout-candidates":
                with _lock:
                    candidates = _state.get("breakout_candidates", [])
                if not candidates:
                    data = _read_static("us_breakout.json")
                    if data:
                        self._json(data)
                        return
                self._json({"candidates": candidates})

            elif route == "/api/tw-scores":
                with _lock:
                    tw_chips = _state.get("scores", []) if _state.get("universe") == "tw" else []
                if not tw_chips:
                    data = _read_static("tw_scores.json")
                    if data:
                        self._json(data)
                        return
                self._json({"universe": "tw", "scores": tw_chips})

            elif route == "/api/tw-rs-scores":
                with _lock:
                    tw_rs = _state.get("tw_rs_scores", [])
                if not tw_rs:
                    data = _read_static("tw_rs_scores.json")
                    if data:
                        self._json(data)
                        return
                self._json({"scores": tw_rs})

            elif route == "/api/tw-sector-rotation":
                with _lock:
                    sector_rotation = _state.get("tw_sector_rotation", []) if _state.get("universe") == "tw" else []
                if not sector_rotation:
                    with _lock:
                        tw_scores = _state.get("scores", []) if _state.get("universe") == "tw" else []
                    seen = {}
                    for row in tw_scores:
                        theme = row.get("sector_theme")
                        if not theme or theme == "未分類" or theme in seen:
                            continue
                        seen[theme] = {
                            "sector_theme": theme,
                            "sector_flow_status": row.get("sector_flow_status"),
                            "sector_flow_score": row.get("sector_flow_score"),
                            "sector_net_1d_yi": row.get("sector_net_1d_yi"),
                            "sector_net_5d_yi": row.get("sector_net_5d_yi"),
                            "sector_net_20d_yi": row.get("sector_net_20d_yi"),
                            "sector_accel_yi": row.get("sector_accel_yi"),
                            "sector_ret_5d": row.get("sector_ret_5d"),
                            "stocks": row.get("stocks") or [],
                        }
                    sector_rotation = sorted(
                        seen.values(),
                        key=lambda r: r.get("sector_flow_score") or 0,
                        reverse=True,
                    )
                    for i, row in enumerate(sector_rotation, start=1):
                        row["rank"] = i
                self._json({"sectors": sector_rotation})

            elif route == "/api/tw-breakout-candidates":
                with _lock:
                    tw_bk = _state.get("tw_breakout_candidates", [])
                if not tw_bk:
                    data = _read_static("tw_breakout.json")
                    if data:
                        self._json(data)
                        return
                self._json({"candidates": tw_bk})

            elif route == "/api/tw-early-stage":
                with _lock:
                    tw_early = _state.get("tw_early_stage", [])
                if not tw_early:
                    data = _read_static("tw_early_stage.json")
                    if data:
                        self._json(data)
                        return
                self._json({"candidates": tw_early})

            elif route == "/api/crypto-rs-scores":
                with _lock:
                    crypto_rs = _state.get("crypto_rs_scores", []) if _state.get("universe") == "crypto" else []
                self._json({"scores": crypto_rs})

            elif route == "/api/ohlcv":
                ticker = params.get("ticker", [""])[0]
                with _lock:
                    data = _state["ohlcv"].get(ticker)
                self._json(data if data else {"error": "not found"}, 200 if data else 404)

            elif route == "/api/indices":
                universe = params.get("universe", ["us"])[0]
                try:
                    if universe == "crypto":
                        import requests

                        result = []
                        for name, symbol in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
                            payload = requests.get(
                                "https://api.binance.com/api/v3/ticker/24hr",
                                params={"symbol": symbol},
                                timeout=10,
                            ).json()
                            result.append({
                                "name": name,
                                "price": float(payload.get("lastPrice") or 0),
                                "change_pct": float(payload.get("priceChangePercent") or 0),
                            })
                        try:
                            payload = requests.get(
                                "https://api.alternative.me/fng/",
                                params={"limit": 1, "format": "json"},
                                timeout=10,
                            ).json()
                            item = (payload.get("data") or [{}])[0]
                            if item.get("value") is not None:
                                result.append({
                                    "name": "貪婪指數",
                                    "price": float(item["value"]),
                                    "change_pct": None,
                                })
                        except Exception as exc:
                            logger.warning("fear greed index fetch failed: %s", exc)
                        self._json({"indices": result})
                        return

                    import yfinance as yf
                    import requests as _req
                    if universe == "tw":
                        symbols = {"加權指數": "^TWII"}
                    else:
                        symbols = {"DOW": "^DJI", "NAQ100": "^NDX", "SP500": "^GSPC"}
                    result = []
                    for name, symbol in symbols.items():
                        df = yf.Ticker(symbol).history(period="2d", auto_adjust=True, raise_errors=False)
                        if df is not None and not df.empty:
                            last = float(df["Close"].iloc[-1])
                            prev = float(df["Close"].iloc[-2]) if len(df) >= 2 else last
                            chg     = last - prev
                            chg_pct = chg / prev * 100 if prev else 0.0
                            result.append({"name": name, "price": last,
                                           "change": chg, "change_pct": chg_pct})
                    # 台股：附加台指期 + 恐慌指數（櫃買指數僅用於氣氛計算，不顯示在 header）
                    if universe == "tw":
                        # 台指期
                        fut = _fetch_tw_futures()
                        if fut:
                            result.append({"name": "台指期", **fut})
                        # 恐慌指數（VIXTWN — 臺指選擇權波動率指數，來源：台灣期交所官網）
                        vixtwn = _fetch_vixtwn()
                        if vixtwn:
                            result.append({"name": "恐慌指數", **vixtwn})
                        else:
                            # fallback：用美股 ^VIX
                            try:
                                vix_df = yf.Ticker("^VIX").history(period="2d", auto_adjust=True, raise_errors=False)
                                if vix_df is not None and not vix_df.empty:
                                    vix_now  = float(vix_df["Close"].iloc[-1])
                                    vix_prev = float(vix_df["Close"].iloc[-2]) if len(vix_df) >= 2 else vix_now
                                    vix_chg  = vix_now - vix_prev
                                    vix_chg_pct = vix_chg / vix_prev * 100 if vix_prev else 0.0
                                    result.append({"name": "恐慌指數", "price": round(vix_now, 2),
                                                   "change": round(vix_chg, 2), "change_pct": round(vix_chg_pct, 2)})
                            except Exception as exc:
                                logger.debug("VIX fallback fetch failed: %s", exc)

                    payload = {"indices": result}
                    # 美股額外計算市場氣氛值
                    if universe == "us":
                        try:
                            # 把已抓到的即時漲跌幅傳入，確保單日懲罰用的是 header 顯示的同一個數字
                            sym_map = {"DOW": "^DJI", "NAQ100": "^NDX", "SP500": "^GSPC"}
                            today_chg = {
                                sym_map[r["name"]]: r["change_pct"]
                                for r in result if r["name"] in sym_map and r.get("change_pct") is not None
                            }
                            payload["sentiment"] = _compute_us_sentiment(today_chg or None)
                        except Exception as exc:
                            logger.debug("sentiment compute failed: %s", exc)
                    # 台股額外計算大盤 / 櫃買氣氛（傳入即時 VIX，確保恐慌指數當日衝擊有被感知）
                    if universe == "tw":
                        try:
                            vix_idx = next((r for r in result if r.get("name") == "恐慌指數"), None)
                            payload["sentiment"] = _compute_tw_sentiment(
                                vix_now     = vix_idx["price"]      if vix_idx else None,
                                vix_chg_pct = vix_idx["change_pct"] if vix_idx else None,
                            )
                        except Exception as exc:
                            logger.debug("TW sentiment compute failed: %s", exc)
                    self._json(payload)
                except Exception as exc:
                    logger.warning("indices fetch failed: %s", exc)
                    self._json({"indices": []})

            elif route == "/api/history":
                ticker = params.get("ticker", [""])[0].strip()
                period = params.get("period", ["6mo"])[0]
                if period not in ("6mo", "1y", "2y", "5y", "10y", "max"):
                    period = "6mo"
                with _lock:
                    cached = _state["ohlcv"].get(ticker) if _state.get("universe") == "crypto" else None
                if cached:
                    self._json(cached)
                    return
                try:
                    import yfinance as yf
                    # 用 Ticker.history() 避免多線程下的 AttributeError bug
                    df = yf.Ticker(ticker).history(
                        period=period, auto_adjust=True, raise_errors=False
                    )
                    if df is None or df.empty:
                        self._json({"error": "no data"}, 404)
                        return
                    data = _ohlcv_to_json(df)
                    if data:
                        self._json(data)
                    else:
                        self._json({"error": "no data"}, 404)
                except Exception as exc:
                    logger.warning("history fetch failed %s: %s", ticker, exc)
                    self._json({"error": str(exc)}, 500)

            elif route == "/api/fetch":
                universe = params.get("universe", ["sp500"])[0]
                # Vercel serverless：無法跑背景 worker，直接回 started；
                # 下一個 /api/state 請求會立即回傳預產 JSON (done)
                if _IS_VERCEL:
                    self._json({"status": "started"})
                    return
                with _lock:
                    if _state["status"] == "fetching":
                        self._json({"error": "already fetching"}, 409)
                        return
                threading.Thread(target=_fetch_worker, args=(universe,), daemon=True).start()
                self._json({"status": "started"})

            elif route == "/api/live-refresh":
                universe = params.get("universe", [_state["universe"]])[0]
                if universe == "tw":
                    self._json({"error": "live refresh is not available for Taiwan stocks"}, 400)
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
                        if universe == "crypto":
                            _state["crypto_rs_scores"] = scores
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

    def do_POST(self):
        """POST /api/patch-scores — merge MCP data into existing scores."""
        parsed = urlparse(self.path)
        if parsed.path != "/api/patch-scores":
            self._respond(404, "text/plain", b"Not Found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            patch  = json.loads(body)           # {ticker: {field: val, ...}}
            count  = 0
            with _lock:
                for row in _state["scores"]:
                    ticker = row.get("ticker", "")
                    if ticker in patch:
                        row.update(patch[ticker])
                        count += 1
            self._json({"updated": count})
        except Exception as exc:
            self._json({"error": str(exc)}, 400)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _load_fund_cache()       # 啟動時載入今日基本面快取
    _taifex_stream.start()  # 啟動 TAIFEX MIS SockJS 即時串流
    _binance_stream.start() # 啟動 Binance WebSocket 即時串流
    _bybit_stream.start()   # 啟動 Bybit WebSocket（補充非 Binance 幣種）
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
