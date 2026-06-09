"""Tests for data.bybit_fetcher — Bybit supplement data source."""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from data.bybit_fetcher import (
    load_supplement_tickers,
    fetch_supplement_ohlcv,
    _parse_klines,
)


# ── load_supplement_tickers ─────────────────────────────────────────────────

def test_load_supplement_tickers_returns_list(tmp_path, monkeypatch):
    config = [{"symbol": "HYPEUSDT", "base": "HYPE", "name": "Hyperliquid"}]
    f = tmp_path / "supplement_tickers.json"
    f.write_text(json.dumps(config))
    monkeypatch.setattr("data.bybit_fetcher._SUPPLEMENT_PATH", f)

    result = load_supplement_tickers()

    assert result == config


def test_load_supplement_tickers_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "data.bybit_fetcher._SUPPLEMENT_PATH", tmp_path / "nonexistent.json"
    )

    result = load_supplement_tickers()

    assert result == []


def test_load_supplement_tickers_invalid_json_returns_empty(tmp_path, monkeypatch):
    f = tmp_path / "supplement_tickers.json"
    f.write_text("this is not json")
    monkeypatch.setattr("data.bybit_fetcher._SUPPLEMENT_PATH", f)

    result = load_supplement_tickers()

    assert result == []


def test_load_supplement_tickers_non_list_returns_empty(tmp_path, monkeypatch):
    f = tmp_path / "supplement_tickers.json"
    f.write_text(json.dumps({"symbol": "HYPEUSDT"}))
    monkeypatch.setattr("data.bybit_fetcher._SUPPLEMENT_PATH", f)

    result = load_supplement_tickers()

    assert result == []


# ── _parse_klines ───────────────────────────────────────────────────────────

def _bybit_kline_rows(n=10):
    """Simulate Bybit klines: newest-first, [startTime, o, h, l, c, vol, turnover]."""
    base_ts = 1_700_000_000_000
    rows = []
    for i in range(n - 1, -1, -1):  # newest first
        rows.append([
            str(base_ts + i * 86_400_000),
            str(100 + i * 0.5),   # open
            str(102 + i * 0.5),   # high
            str(99 + i * 0.5),    # low
            str(101 + i * 0.5),   # close
            "1000000",             # volume
            "101000000",           # turnover
        ])
    return rows


def test_parse_klines_returns_chronological_dataframe():
    rows = _bybit_kline_rows(5)
    df = _parse_klines(rows)

    assert df is not None
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    # chronological: index should be ascending
    assert df.index.is_monotonic_increasing


def test_parse_klines_close_values_are_ascending_for_rising_rows():
    rows = _bybit_kline_rows(10)
    df = _parse_klines(rows)

    assert df["Close"].iloc[-1] > df["Close"].iloc[0]


def test_parse_klines_returns_none_for_empty_input():
    assert _parse_klines([]) is None


# ── fetch_supplement_ohlcv ──────────────────────────────────────────────────

def _mock_ticker_response(symbol, price="26.52", pct="0.0698", vol="3413538", turnover="87680561"):
    return {
        "retCode": 0,
        "result": {
            "list": [{
                "symbol": symbol,
                "lastPrice": price,
                "price24hPcnt": pct,
                "volume24h": vol,
                "turnover24h": turnover,
            }]
        }
    }


def _mock_kline_response(symbol, n=100):
    return {
        "retCode": 0,
        "result": {
            "symbol": symbol,
            "list": _bybit_kline_rows(n),
        }
    }


def _make_get_side_effect(symbol):
    """Return a side_effect function that serves ticker then kline for symbol."""
    responses = [
        _mock_ticker_response(symbol),
        _mock_kline_response(symbol, n=100),
    ]
    calls = iter(responses)

    def side_effect(path, params=None):
        return next(calls)

    return side_effect


def test_fetch_supplement_ohlcv_returns_empty_for_no_tickers():
    result = fetch_supplement_ohlcv(tickers=[])
    assert result == {}


def test_fetch_supplement_ohlcv_returns_dict_keyed_by_symbol():
    tickers = [{"symbol": "HYPEUSDT", "base": "HYPE", "name": "Hyperliquid"}]

    with patch("data.bybit_fetcher._get", side_effect=_make_get_side_effect("HYPEUSDT")):
        result = fetch_supplement_ohlcv(tickers=tickers)

    assert "HYPEUSDT" in result


def test_fetch_supplement_ohlcv_dataframe_has_ohlcv_columns():
    tickers = [{"symbol": "HYPEUSDT", "base": "HYPE", "name": "Hyperliquid"}]

    with patch("data.bybit_fetcher._get", side_effect=_make_get_side_effect("HYPEUSDT")):
        result = fetch_supplement_ohlcv(tickers=tickers)

    df = result["HYPEUSDT"]["ohlcv"]
    assert isinstance(df, pd.DataFrame)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in df.columns


def test_fetch_supplement_ohlcv_price_and_day_return_populated():
    tickers = [{"symbol": "HYPEUSDT", "base": "HYPE", "name": "Hyperliquid"}]

    with patch("data.bybit_fetcher._get", side_effect=_make_get_side_effect("HYPEUSDT")):
        result = fetch_supplement_ohlcv(tickers=tickers)

    entry = result["HYPEUSDT"]
    assert entry["price"] == pytest.approx(26.52)
    assert entry["day_return"] == pytest.approx(0.0698)
    assert entry["name"] == "Hyperliquid"


def test_fetch_supplement_ohlcv_skips_failed_symbols():
    tickers = [
        {"symbol": "HYPEUSDT", "base": "HYPE", "name": "Hyperliquid"},
        {"symbol": "FAILUSDT", "base": "FAIL", "name": "FailCoin"},
    ]

    def get_side_effect(path, params=None):
        symbol = (params or {}).get("symbol", "")
        if symbol == "FAILUSDT" or "FAILUSDT" in str(params):
            raise ConnectionError("network error")
        if "/tickers" in path:
            return _mock_ticker_response("HYPEUSDT")
        return _mock_kline_response("HYPEUSDT", n=100)

    with patch("data.bybit_fetcher._get", side_effect=get_side_effect):
        result = fetch_supplement_ohlcv(tickers=tickers)

    assert "HYPEUSDT" in result
    assert "FAILUSDT" not in result


def test_fetch_supplement_ohlcv_calls_progress_callback():
    tickers = [{"symbol": "HYPEUSDT", "base": "HYPE", "name": "Hyperliquid"}]
    calls = []

    with patch("data.bybit_fetcher._get", side_effect=_make_get_side_effect("HYPEUSDT")):
        fetch_supplement_ohlcv(tickers=tickers, progress_callback=lambda d, t: calls.append((d, t)))

    assert len(calls) > 0
    assert calls[-1] == (1, 1)
