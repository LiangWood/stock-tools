from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
import pytest
from data.fetcher import fetch_all


def _make_ohlcv(n=60):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": np.random.rand(n) * 100 + 100,
        "High": np.random.rand(n) * 100 + 110,
        "Low": np.random.rand(n) * 100 + 90,
        "Close": np.random.rand(n) * 100 + 100,
        "Volume": np.random.randint(1_000_000, 10_000_000, n),
    }, index=idx)


def _make_multi_download(tickers):
    """Build MultiIndex DataFrame as yfinance returns for multiple tickers."""
    frames = {t: _make_ohlcv() for t in tickers}
    combined = pd.concat(frames, axis=1)
    combined.columns = pd.MultiIndex.from_tuples(
        [(col, ticker) for ticker in tickers for col in ["Open", "High", "Low", "Close", "Volume"]],
        names=["field", "ticker"],
    )
    return combined


def test_returns_dict_keyed_by_ticker():
    tickers = ["AAPL", "MSFT"]
    with patch("data.fetcher.yf.download", return_value=_make_multi_download(tickers)):
        result = fetch_all(tickers)
    assert set(result.keys()) == {"AAPL", "MSFT"}


def test_each_value_is_dataframe_with_ohlcv():
    tickers = ["AAPL"]
    with patch("data.fetcher.yf.download", return_value=_make_multi_download(tickers)):
        result = fetch_all(tickers)
    df = result["AAPL"]
    assert isinstance(df, pd.DataFrame)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in df.columns


def test_failed_ticker_returns_none():
    tickers = ["AAPL", "BADFOO"]
    good = _make_multi_download(["AAPL"])
    # BADFOO not present in download result
    with patch("data.fetcher.yf.download", return_value=good):
        result = fetch_all(tickers)
    assert result.get("BADFOO") is None


def test_progress_callback_is_called():
    tickers = [f"T{i}" for i in range(5)]
    calls = []
    with patch("data.fetcher.yf.download", return_value=_make_multi_download(tickers)):
        fetch_all(tickers, progress_callback=lambda done, total: calls.append((done, total)))
    assert len(calls) > 0
    assert calls[-1][0] == len(tickers)


def test_fetch_all_accepts_period_and_interval():
    tickers = ["XLK", "XLF"]
    history = MagicMock(return_value=_make_ohlcv())
    ticker = MagicMock(return_value=MagicMock(history=history))
    with patch("data.fetcher.yf.Ticker", ticker):
        fetch_all(tickers, period="1y", interval="1wk")

    assert ticker.call_count == len(tickers)
    history.assert_any_call(
        period="1y",
        interval="1wk",
        auto_adjust=True,
        raise_errors=False,
    )
