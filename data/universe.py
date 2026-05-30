"""
US stock universe fetcher with JSON cache.

Priority:
  1. Wikipedia (dynamic, most up-to-date)
  2. JSON cache (last successful fetch, stored in data/tickers_cache.json)
  3. No hardcoded fallback — if both fail, raise and let the caller handle it.
"""
import io
import json
import logging
import os
from datetime import datetime

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_TIMEOUT  = 15

_SP500_URL  = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NDX100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

_CACHE_FILE = os.path.join(os.path.dirname(__file__), "tickers_cache.json")

# 自訂追蹤清單：強動能標的（不在主要指數但仍納入篩選）
_WATCHLIST = [
    "PLTR", "SOFI", "SNOW", "HOOD", "COIN", "MSTR", "RBLX", "RIVN", "LCID",
    "IONQ", "RXRX", "SOUN", "BBAI", "ARQQ", "QBTS", "RGTI",
]


# ── Cache helpers ────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(key: str, tickers: list[str]):
    cache = _load_cache()
    cache[key] = {"tickers": tickers, "updated_at": datetime.now().isoformat()}
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        logger.info("Saved %d tickers to cache[%s]", len(tickers), key)
    except Exception as e:
        logger.warning("Failed to save ticker cache: %s", e)


def _cached(key: str) -> list[str] | None:
    data = _load_cache().get(key)
    if data and isinstance(data.get("tickers"), list) and len(data["tickers"]) > 0:
        updated = data.get("updated_at", "unknown")
        logger.info("Using cached %s (%d tickers, updated %s)", key, len(data["tickers"]), updated)
        return data["tickers"]
    return None


# ── Fetch helpers ────────────────────────────────────────────────────────────

def _fetch_html(url: str) -> str:
    r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.text


# ── Public API ───────────────────────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    """Fetch S&P 500 constituent list from Wikipedia; fall back to cache."""
    try:
        html    = _fetch_html(_SP500_URL)
        tables  = pd.read_html(io.StringIO(html))
        tickers = [t.replace(".", "-") for t in tables[0]["Symbol"].tolist()]
        result  = list(dict.fromkeys(tickers))
        if len(result) < 400:
            raise ValueError(f"Wikipedia returned too few tickers: {len(result)}")
        _save_cache("sp500", result)
        logger.info("S&P 500: fetched %d tickers from Wikipedia", len(result))
        return result
    except Exception as e:
        logger.warning("S&P 500 Wikipedia fetch failed (%s), trying cache…", e)
        cached = _cached("sp500")
        if cached:
            return cached
        raise RuntimeError("S&P 500 tickers unavailable: Wikipedia failed and no cache found") from e


def get_nasdaq100_tickers() -> list[str]:
    """Fetch NASDAQ 100 constituent list from Wikipedia; fall back to cache."""
    try:
        html   = _fetch_html(_NDX100_URL)
        tables = pd.read_html(io.StringIO(html))
        for table in tables:
            for col in ("Ticker", "Symbol", "Ticker symbol"):
                if col in table.columns:
                    tickers = [
                        str(t).replace(".", "-")
                        for t in table[col].dropna().tolist()
                        if str(t).replace("-", "").isalpha()
                    ]
                    if len(tickers) >= 80:
                        result = list(dict.fromkeys(tickers))
                        _save_cache("ndx100", result)
                        logger.info("NASDAQ 100: fetched %d tickers from Wikipedia", len(result))
                        return result
        raise ValueError("ticker column not found in any table")
    except Exception as e:
        logger.warning("NASDAQ 100 Wikipedia fetch failed (%s), trying cache…", e)
        cached = _cached("ndx100")
        if cached:
            return cached
        raise RuntimeError("NASDAQ 100 tickers unavailable: Wikipedia failed and no cache found") from e


def get_combined_tickers() -> list[str]:
    sp500  = get_sp500_tickers()
    ndx100 = get_nasdaq100_tickers()
    return list(dict.fromkeys(sp500 + ndx100 + _WATCHLIST))
