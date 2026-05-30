from unittest.mock import patch
import pytest
from data import universe
from data.universe import get_sp500_tickers


def test_returns_list_of_strings():
    tickers = get_sp500_tickers()
    assert isinstance(tickers, list)
    assert len(tickers) > 0
    assert all(isinstance(t, str) for t in tickers)


def test_fallback_when_wikipedia_fails():
    with patch("data.universe.pd.read_html", side_effect=Exception("network error")):
        tickers = get_sp500_tickers()
    assert len(tickers) >= 50
    assert "AAPL" in tickers


def test_no_duplicate_tickers():
    tickers = get_sp500_tickers()
    assert len(tickers) == len(set(tickers))


def test_combined_tickers_include_watchlist_when_live_lists_omit_them(monkeypatch):
    """High-interest watchlist names should not depend on Wikipedia fallback data."""
    monkeypatch.setattr(universe, "get_sp500_tickers", lambda: ["AAPL", "MSFT"])
    monkeypatch.setattr(universe, "get_nasdaq100_tickers", lambda: ["NVDA", "META"])

    tickers = universe.get_combined_tickers()

    assert "PLTR" in tickers
    assert "SOFI" in tickers
    assert "SNOW" in tickers
