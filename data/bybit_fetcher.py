"""
Bybit REST fetcher for supplement (non-Binance) crypto tickers.

Used to fill in coins that are not listed on Binance.
Add new coins by editing data/supplement_tickers.json — no code changes needed.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.bybit.com"
_TIMEOUT = 15
_SUPPLEMENT_PATH = Path(__file__).parent / "supplement_tickers.json"


# ── config ───────────────────────────────────────────────────────────────────

def load_supplement_tickers() -> list[dict]:
    """Load supplement_tickers.json.  Returns [] on any error."""
    try:
        with open(_SUPPLEMENT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("supplement_tickers.json: expected list, got %s", type(data).__name__)
            return []
        return data
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("supplement_tickers.json load error: %s", exc)
        return []


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{_BASE_URL}{path}", params=params, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise ValueError(f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}")
    return data


# ── kline parsing ─────────────────────────────────────────────────────────────

def _parse_klines(rows: list[list]) -> pd.DataFrame | None:
    """
    Parse Bybit klines into a chronological OHLCV DataFrame.

    Bybit returns rows newest-first; each row is:
    [startTime, open, high, low, close, volume, turnover]
    """
    if not rows:
        return None
    records = []
    for row in reversed(rows):  # newest-first → chronological
        try:
            records.append({
                "time": pd.to_datetime(int(row[0]), unit="ms", utc=True).tz_convert(None),
                "Open":   _to_float(row[1]),
                "High":   _to_float(row[2]),
                "Low":    _to_float(row[3]),
                "Close":  _to_float(row[4]),
                "Volume": _to_float(row[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    if not records:
        return None
    return pd.DataFrame(records).set_index("time")


# ── per-symbol fetch ──────────────────────────────────────────────────────────

def _fetch_one(ticker: dict, limit: int) -> tuple[str, dict | None]:
    """Fetch ticker snapshot + OHLCV for a single supplement ticker."""
    symbol = ticker["symbol"]
    try:
        # 24-hour ticker (price, day_return, volume)
        t_resp = _get("/v5/market/tickers", {"category": "spot", "symbol": symbol})
        t_list = t_resp.get("result", {}).get("list", [])
        if not t_list:
            raise ValueError(f"empty ticker list for {symbol}")
        t = t_list[0]
        price       = _to_float(t.get("lastPrice"))
        # price24hPcnt is already a decimal fraction (0.0698 = 6.98%)
        day_return  = _to_float(t.get("price24hPcnt"))
        volume      = _to_float(t.get("volume24h"))
        quote_volume = _to_float(t.get("turnover24h"))

        # Daily OHLCV (limit bars, newest-first from Bybit)
        k_resp = _get(
            "/v5/market/kline",
            {"category": "spot", "symbol": symbol, "interval": "D", "limit": limit},
        )
        k_list = k_resp.get("result", {}).get("list", [])
        ohlcv = _parse_klines(k_list)

        return symbol, {
            "name":         ticker.get("name", ticker.get("base", symbol)),
            "price":        price,
            "day_return":   day_return,
            "quote_volume": quote_volume,
            "volume":       volume,
            "ohlcv":        ohlcv,
        }
    except Exception as exc:
        logger.warning("Bybit fetch failed for %s: %s", symbol, exc)
        return symbol, None


# ── public API ────────────────────────────────────────────────────────────────

def fetch_supplement_ohlcv(
    tickers: list[dict] | None = None,
    limit: int = 260,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, dict]:
    """
    Fetch daily OHLCV + ticker data from Bybit for supplement tickers.

    Args:
        tickers: list of dicts from supplement_tickers.json; loads from file if None.
        limit:   number of daily bars to fetch (default 260 ≈ 1 trading year).
        progress_callback: optional (done, total) callable.

    Returns:
        dict keyed by symbol (e.g. "HYPEUSDT"), same schema as
        binance_fetcher.fetch_crypto_all(), ready to merge into ticker_data.
    """
    if tickers is None:
        tickers = load_supplement_tickers()
    if not tickers:
        return {}

    total = len(tickers)
    result: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {pool.submit(_fetch_one, t, limit): t for t in tickers}
        for done, future in enumerate(as_completed(future_map), start=1):
            symbol, data = future.result()
            if data is not None:
                result[symbol] = data
            if progress_callback:
                progress_callback(done, total)

    return result
