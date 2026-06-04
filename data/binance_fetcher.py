import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.binance.com"
_TIMEOUT = 15
_HEADERS = {"User-Agent": "stock-tool/1.0"}
_EXCLUDED_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "EUR", "TRY", "BRL", "AUD", "GBP",
}
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def _get(path: str, params: dict | None = None):
    r = requests.get(f"{_BASE_URL}{path}", params=params, timeout=_TIMEOUT, headers=_HEADERS)
    r.raise_for_status()
    return r.json()


def _is_spot_usdt_symbol(symbol: dict) -> bool:
    base = str(symbol.get("baseAsset", ""))
    name = str(symbol.get("symbol", ""))
    permissions = set(symbol.get("permissions", []) or [])
    if symbol.get("status") != "TRADING":
        return False
    if symbol.get("quoteAsset") != "USDT":
        return False
    if symbol.get("isSpotTradingAllowed") is False and "SPOT" not in permissions:
        return False
    if base in _EXCLUDED_BASES:
        return False
    if any(base.endswith(suffix) for suffix in _LEVERAGED_SUFFIXES):
        return False
    if any(name.endswith(f"{suffix}USDT") for suffix in _LEVERAGED_SUFFIXES):
        return False
    return True


def _parse_exchange_symbols(data: dict) -> list[dict]:
    return [s for s in data.get("symbols", []) if _is_spot_usdt_symbol(s)]


def _parse_24h_tickers(data: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in data:
        symbol = row.get("symbol")
        if not symbol:
            continue
        result[symbol] = {
            "price": _to_float(row.get("lastPrice")),
            "day_return": _to_float(row.get("priceChangePercent")) / 100,
            "quote_volume": _to_float(row.get("quoteVolume")),
            "volume": _to_float(row.get("volume")),
        }
    return result


def fetch_crypto_ticker_map() -> dict[str, dict]:
    """Fetch Binance 24h ticker data keyed by symbol."""
    return _parse_24h_tickers(_get("/api/v3/ticker/24hr"))


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_klines(rows: list[list]) -> Optional[pd.DataFrame]:
    if not rows:
        return None
    records = []
    for row in rows:
        try:
            records.append({
                "time": pd.to_datetime(int(row[0]), unit="ms", utc=True).tz_convert(None),
                "Open": _to_float(row[1]),
                "High": _to_float(row[2]),
                "Low": _to_float(row[3]),
                "Close": _to_float(row[4]),
                "Volume": _to_float(row[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    if not records:
        return None
    return pd.DataFrame(records).set_index("time")


def _fetch_klines(symbol: str, limit: int = 260) -> Optional[pd.DataFrame]:
    data = _get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": limit})
    return _parse_klines(data)


def fetch_crypto_all(
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_symbols: int = 150,
) -> dict[str, dict]:
    exchange = _get("/api/v3/exchangeInfo")
    tickers = _parse_24h_tickers(_get("/api/v3/ticker/24hr"))
    symbols = _parse_exchange_symbols(exchange)

    ranked = sorted(
        symbols,
        key=lambda s: tickers.get(s["symbol"], {}).get("quote_volume", 0),
        reverse=True,
    )[:max_symbols]

    total = len(ranked)
    result: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {
            pool.submit(_fetch_klines, s["symbol"]): s
            for s in ranked
        }
        for done, future in enumerate(as_completed(future_map), start=1):
            symbol = future_map[future]
            name = symbol["symbol"]
            quote = tickers.get(name, {})
            try:
                ohlcv = future.result()
            except Exception as exc:
                logger.warning("Binance klines failed %s: %s", name, exc)
                ohlcv = None
            result[name] = {
                "ticker": name,
                "name": symbol.get("baseAsset", name),
                "base_asset": symbol.get("baseAsset"),
                "price": quote.get("price", 0.0),
                "day_return": quote.get("day_return", 0.0),
                "quote_volume": quote.get("quote_volume", 0.0),
                "volume": quote.get("volume", 0.0),
                "ohlcv": ohlcv,
            }
            if progress_callback:
                progress_callback(done, total)
            time.sleep(0.02)

    return result
