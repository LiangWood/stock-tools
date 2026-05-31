#!/usr/bin/env python3
"""
tw-stock-agent MCP server

Tools:
  screen_tw_stocks    — top-N TW stocks by tw_score (uses server cache if available)
  get_tw_stock_detail — quote + chips + technicals for a single TW ticker
  get_stock_history   — OHLCV history for any ticker via yfinance

Cache strategy:
  If server.py is running on localhost:5177 with TW data ready, tools read from
  its cache (instant).  Otherwise they fall back to fetching directly.
"""
import json
import math
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tw-stock-agent")

_SERVER = "http://localhost:5177"


# ── Server helpers ────────────────────────────────────────────────────────────

def _server_get(path: str, timeout: float = 5.0):
    """GET from local server.py. Returns parsed JSON or None on any failure."""
    try:
        with urllib.request.urlopen(f"{_SERVER}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _server_has_tw() -> bool:
    """True iff server.py is running with fresh TW scores."""
    state = _server_get("/api/state")
    return (
        state is not None
        and state.get("universe") == "tw"
        and state.get("status") == "done"
        and state.get("count", 0) > 0
    )


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def screen_tw_stocks(top_n: int = 20) -> str:
    """
    Screen Taiwan listed stocks (TWSE + TPEX) by momentum and chips score.
    Returns the top-N ranked stocks as a JSON array sorted by tw_score desc.

    Each record: ticker, name, price, day_return, tw_score, rank,
    fi_net, it_net, margin_chg, ret_20d, amount_ratio, rsi.

    Fast path: reads from server.py cache if it is running on localhost:5177
    with TW universe data already fetched.
    Slow path: fetches all TW stocks directly (~2–5 min on first call).
    """
    # ── Fast path: server cache ──────────────────────────────────────────────
    if _server_has_tw():
        data = _server_get("/api/scores")
        if data and data.get("universe") == "tw":
            scores = data.get("scores", [])
            return json.dumps(scores[:top_n], ensure_ascii=False, indent=2)

    # ── Slow path: direct fetch ──────────────────────────────────────────────
    from data.twse_fetcher import fetch_tw_all
    from scoring.tw_engine import compute_tw_scores

    raw = fetch_tw_all()
    df = compute_tw_scores(raw)
    if df.empty:
        return json.dumps({"error": "no data from TWSE/TPEX"}, ensure_ascii=False)

    records = [{k: _clean(v) for k, v in row.items()}
               for row in df.head(top_n).to_dict(orient="records")]
    return json.dumps(records, ensure_ascii=False, indent=2)


@mcp.tool()
def get_tw_stock_detail(ticker: str) -> str:
    """
    Detailed data for a single Taiwan stock.

    ticker: "2330.TW" (TWSE), "6547.TWO" (TPEX), or bare code "2330".

    Returns JSON with:
      quote:      name, price, day_return, volume
      chips:      fi_net, it_net (TPEX only), margin_chg
      technicals: ret_20d, amount_ratio, rsi
      ohlcv:      last 60 trading days

    OHLCV is read from server.py cache when available; chips are always fetched
    live from TWSE/TPEX APIs (one HTTP request each, ~1 s total).
    """
    import pandas as pd
    import yfinance as yf
    from data.twse_fetcher import (
        _get,
        _parse_twse_quote, _parse_twse_margin,
        _parse_tpex_quote, _parse_tpex_chips, _parse_tpex_margin,
        _TWSE_QUOTE, _TWSE_MARGIN,
        _TPEX_QUOTE, _TPEX_CHIPS, _TPEX_MARGIN,
    )
    from scoring.tw_engine import _tech_metrics

    def _sf(v):
        try:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except Exception:
            return None

    # ── Normalise ticker ─────────────────────────────────────────────────────
    t = ticker.strip()
    if t.endswith(".TWO"):
        exchange, code, yf_ticker = "tpex", t[:-4], t
    elif t.endswith(".TW"):
        exchange, code, yf_ticker = "twse", t[:-3], t
    else:
        exchange, code, yf_ticker = "twse", t, f"{t}.TW"

    # ── Quote & chips from exchange APIs ─────────────────────────────────────
    quote_data: dict = {}
    chips_data: dict = {}
    margin_data: dict = {}

    if exchange == "twse":
        try:
            quote_data = _parse_twse_quote(_get(_TWSE_QUOTE))
        except Exception:
            pass
        try:
            margin_data = _parse_twse_margin(_get(_TWSE_MARGIN))
        except Exception:
            pass
    else:
        try:
            quote_data = _parse_tpex_quote(_get(_TPEX_QUOTE, verify=False))
        except Exception:
            pass
        try:
            chips_data = _parse_tpex_chips(_get(_TPEX_CHIPS, verify=False))
        except Exception:
            pass
        try:
            margin_data = _parse_tpex_margin(_get(_TPEX_MARGIN, verify=False))
        except Exception:
            pass

    quote  = quote_data.get(code, {})
    chips  = chips_data.get(code, {})
    margin = margin_data.get(code, {})

    if not quote:
        return json.dumps(
            {"error": f"{ticker!r} not found in exchange data"}, ensure_ascii=False
        )

    # ── OHLCV: server cache first, then yfinance ──────────────────────────
    ohlcv_df: pd.DataFrame | None = None

    cached_ohlcv = None
    if _server_has_tw():
        cached_ohlcv = _server_get(f"/api/ohlcv?ticker={yf_ticker}")

    if cached_ohlcv and "close" in cached_ohlcv:
        # Reconstruct a minimal DataFrame from server cache for _tech_metrics
        closes  = cached_ohlcv.get("close", [])
        volumes = cached_ohlcv.get("volume", [])
        dates   = cached_ohlcv.get("dates", [])
        if closes:
            import pandas as _pd
            idx = _pd.to_datetime(dates)
            ohlcv_df = _pd.DataFrame(
                {"Close": closes, "Volume": volumes}, index=idx
            )
    else:
        try:
            raw = yf.download(yf_ticker, period="3mo", progress=False, auto_adjust=True)
            if isinstance(raw.columns, pd.MultiIndex):
                raw = raw.xs(yf_ticker, axis=1, level=1)
            if raw is not None and not raw.empty:
                ohlcv_df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()
        except Exception:
            pass

    tech = _tech_metrics(ohlcv_df)

    # ── OHLCV payload (last 60 bars) ─────────────────────────────────────────
    ohlcv_payload: dict = {}
    if cached_ohlcv and "close" in cached_ohlcv:
        # Trim to last 60 from server cache
        n = min(60, len(cached_ohlcv.get("dates", [])))
        ohlcv_payload = {k: cached_ohlcv[k][-n:] for k in ("dates", "open", "high", "low", "close", "volume") if k in cached_ohlcv}
    elif ohlcv_df is not None and not ohlcv_df.empty:
        tail = ohlcv_df.tail(60)
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in tail.columns]
        ohlcv_payload = {
            "dates":  [str(d)[:10] for d in tail.index],
            **{c.lower(): [_sf(v) for v in tail[c].tolist()] for c in cols if c != "Volume"},
        }
        if "Volume" in tail.columns:
            ohlcv_payload["volume"] = [
                int(v) if v is not None else None for v in tail["Volume"].tolist()
            ]

    result = {
        "ticker": yf_ticker,
        "source": "server_cache" if cached_ohlcv else "live_fetch",
        "quote": {
            "name":       quote.get("name", ""),
            "price":      _sf(quote.get("price")),
            "day_return": _sf(quote.get("day_return")),
            "volume":     quote.get("volume"),
        },
        "chips": {
            "fi_net":     _sf(chips.get("fi_net", 0.0)),
            "it_net":     _sf(chips.get("it_net", 0.0)),
            "margin_chg": margin.get("margin_chg", 0),
        },
        "technicals": {
            "ret_20d":      _sf(tech.get("ret_20d")),
            "amount_ratio": _sf(tech.get("amount_ratio")),
            "rsi":          _sf(tech.get("rsi")),
        },
        "ohlcv": ohlcv_payload,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def get_stock_history(ticker: str, period: str = "1y") -> str:
    """
    OHLCV history for any ticker supported by yfinance.

    ticker: "2330.TW", "6547.TWO", "AAPL", etc.
    period: 1mo | 3mo | 6mo | 1y | 2y | 5y | 10y | max  (default: 1y)

    For TW tickers: reads from server.py cache (period ignored; returns full
    cached history) when the server is running with TW data.
    Otherwise downloads via yfinance.
    """
    import pandas as pd
    import yfinance as yf

    valid_periods = {"1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max"}
    if period not in valid_periods:
        period = "1y"

    def _sf(v):
        try:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except Exception:
            return None

    is_tw = ticker.endswith(".TW") or ticker.endswith(".TWO")

    # ── Fast path: server cache (TW tickers only) ────────────────────────────
    if is_tw and _server_has_tw():
        cached = _server_get(f"/api/ohlcv?ticker={ticker}")
        if cached and "close" in cached:
            cached["ticker"] = ticker
            cached["source"] = "server_cache"
            return json.dumps(cached, ensure_ascii=False)

    # ── Slow path: yfinance ──────────────────────────────────────────────────
    df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df = df.xs(ticker, axis=1, level=1)

    if df is None or df.empty:
        return json.dumps({"error": f"no data for {ticker}"})

    needed = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[needed].dropna()

    result = {
        "ticker": ticker,
        "period": period,
        "source": "yfinance",
        "dates":  [str(d)[:10] for d in df.index],
        "open":   [_sf(v) for v in df["Open"].tolist()]  if "Open"   in df else [],
        "high":   [_sf(v) for v in df["High"].tolist()]  if "High"   in df else [],
        "low":    [_sf(v) for v in df["Low"].tolist()]   if "Low"    in df else [],
        "close":  [_sf(v) for v in df["Close"].tolist()] if "Close"  in df else [],
        "volume": [int(v) if v is not None else None for v in df["Volume"].tolist()] if "Volume" in df else [],
    }
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
