#!/usr/bin/env python3
"""
tw-stock-agent MCP server

Tools:
  screen_tw_stocks    — top-N TW stocks by tw_score (uses server cache if available)
  get_tw_stock_detail — quote + chips + technicals for a single TW ticker
  get_stock_history   — OHLCV history for any ticker via yfinance
  get_market_overview — TAIEX index and TWSE market overview

Cache strategy:
  If server.py is running on localhost:5177 with TW data ready, tools read from
  its cache (instant).  Otherwise they fall back to fetching directly.
"""
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tw-stock-agent")

_SERVER = "http://localhost:5177"
_TWSE_MARKET_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&type=IND"
_TWSE_MARKET_STATS_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&type=MS"
_TAIWAN_TZ = ZoneInfo("Asia/Taipei")


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


def _http_json(url: str, timeout: float = 12.0):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _num(value):
    if value is None:
        return None

    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "-"}:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def _int_num(value):
    parsed = _num(value)
    return int(parsed) if parsed is not None else None


def _stock_count(value):
    match = re.search(r"\d[\d,]*", str(value or ""))
    return int(match.group(0).replace(",", "")) if match else None


def _twse_date(date_text):
    text = str(date_text or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) == 7 and text.isdigit():
        return f"{int(text[:3]) + 1911}-{text[3:5]}-{text[5:7]}"
    return datetime.now(_TAIWAN_TZ).date().isoformat()


def _market_status():
    now = datetime.now(_TAIWAN_TZ)
    if now.weekday() >= 5:
        return "closed"
    return "open" if time(9, 0) <= now.time() <= time(13, 30) else "closed"


def _extract_taiex(payload):
    for table in payload.get("tables", []):
        fields = table.get("fields", [])
        if "指數" not in fields or "收盤指數" not in fields:
            continue

        for row in table.get("data", []):
            if not row or row[0] != "發行量加權股價指數":
                continue

            sign = -1 if len(row) > 2 and "-" in str(row[2]) else 1
            points = _num(row[3] if len(row) > 3 else None)
            pct = _num(row[4] if len(row) > 4 else None)
            return {
                "index_name": "TAIEX",
                "local_name": "發行量加權股價指數",
                "current_value": _num(row[1] if len(row) > 1 else None),
                "change_points": points * sign if points is not None else None,
                "change_percentage": pct * sign if pct is not None else None,
            }
    return None


def _extract_market_stats(payload):
    result = {}

    for table in payload.get("tables", []):
        title = table.get("title", "")

        if "大盤統計資訊" in title:
            for row in table.get("data", []):
                if row and str(row[0]).startswith("總計"):
                    result["turnover"] = _int_num(row[1] if len(row) > 1 else None)
                    result["volume"] = _int_num(row[2] if len(row) > 2 else None)
                    result["transactions"] = _int_num(row[3] if len(row) > 3 else None)
                    break

        if title == "漲跌證券數合計":
            fields = table.get("fields", [])
            stock_col = fields.index("股票") if "股票" in fields else 2
            for row in table.get("data", []):
                if len(row) <= stock_col:
                    continue

                label = str(row[0])
                count = _stock_count(row[stock_col])
                if label.startswith("上漲"):
                    result["advancing_stocks"] = count
                elif label.startswith("下跌"):
                    result["declining_stocks"] = count
                elif label.startswith("持平"):
                    result["unchanged_stocks"] = count
                elif label.startswith("未成交"):
                    result["not_traded_stocks"] = count
                elif label.startswith("無比價"):
                    result["no_comparison_stocks"] = count

    return result


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def screen_tw_stocks(top_n: int = 20) -> str:
    """
    Screen Taiwan listed stocks (TWSE + TPEX) by momentum and chips score.
    Returns the top-N ranked stocks as a JSON array sorted by tw_score desc.

    Each record: ticker, name, price, day_return, tw_score, rank,
    fi_net, it_net, margin_chg, ret_20d, amount_ratio, rsi,
    is_limit_up, is_limit_down, limit_up_price, limit_down_price, limit_basis.

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
def get_market_overview() -> str:
    """
    Taiwan market overview from TWSE.

    Returns JSON with TAIEX index, turnover, volume, transaction count,
    advancing/declining/unchanged stock counts, and current market status.
    """
    try:
        index_payload = _http_json(_TWSE_MARKET_INDEX_URL)
        stats_payload = _http_json(_TWSE_MARKET_STATS_URL)
    except Exception as exc:
        return json.dumps(
            {"error": f"failed to fetch TWSE market overview: {exc}"},
            ensure_ascii=False,
        )

    if index_payload.get("stat") != "OK" or stats_payload.get("stat") != "OK":
        return json.dumps(
            {
                "error": "TWSE market overview endpoints returned non-OK status",
                "index_status": index_payload.get("stat"),
                "stats_status": stats_payload.get("stat"),
            },
            ensure_ascii=False,
        )

    taiex = _extract_taiex(index_payload)
    if not taiex or taiex.get("current_value") is None:
        return json.dumps(
            {"error": "failed to parse TAIEX from TWSE market overview"},
            ensure_ascii=False,
        )

    stats = _extract_market_stats(stats_payload)
    date = _twse_date(index_payload.get("date") or stats_payload.get("date"))
    updated_at = datetime.now(_TAIWAN_TZ).isoformat()

    result = {
        "date": date,
        "taiex": taiex,
        "volume": stats.get("volume"),
        "turnover": stats.get("turnover"),
        "transactions": stats.get("transactions"),
        "advancing_stocks": stats.get("advancing_stocks"),
        "declining_stocks": stats.get("declining_stocks"),
        "unchanged_stocks": stats.get("unchanged_stocks"),
        "not_traded_stocks": stats.get("not_traded_stocks"),
        "no_comparison_stocks": stats.get("no_comparison_stocks"),
        "market_status": _market_status(),
        "updated_at": updated_at,
        "source": "TWSE",
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


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
        _fetch_twse_limits, _current_limit_flags,
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
            limit_data = _fetch_twse_limits()
        except Exception:
            limit_data = {}
        try:
            margin_data = _parse_twse_margin(_get(_TWSE_MARGIN))
        except Exception:
            pass
    else:
        limit_data = {}
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
    limits = limit_data.get(code, {})

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

    limit_up = _sf(limits.get("limit_up"))
    limit_down = _sf(limits.get("limit_down"))
    price = _sf(quote.get("price"))
    is_limit_up, is_limit_down, limit_basis = _current_limit_flags(quote)

    result = {
        "ticker": yf_ticker,
        "source": "server_cache" if cached_ohlcv else "live_fetch",
        "quote": {
            "name":       quote.get("name", ""),
            "price":      price,
            "day_return": _sf(quote.get("day_return")),
            "volume":     quote.get("volume"),
            "is_limit_up": is_limit_up,
            "is_limit_down": is_limit_down,
            "limit_up_price": limit_up,
            "limit_down_price": limit_down,
            "limit_basis": limit_basis,
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
