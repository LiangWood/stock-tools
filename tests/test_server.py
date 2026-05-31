import pandas as pd
import pytest

import server


def _ohlcv_from_close(values):
    idx = pd.date_range("2025-01-03", periods=len(values), freq="W")
    return pd.DataFrame({"Close": values}, index=idx)


def test_check_sector_ema_returns_weekly_close_above_ema50():
    rising = _ohlcv_from_close(range(1, 61))
    falling = _ohlcv_from_close(range(60, 0, -1))

    result = server._check_sector_ema(
        pd.DataFrame({"ticker": ["AAPL"]}),
        {"XLK": rising, "XLF": falling},
    )

    assert result["XLK"] is True
    assert result["XLF"] is False
    assert result["XLV"] is None


def test_enrich_fundamentals_adds_eps_beats_peg_and_sector(monkeypatch):
    earnings_dates = pd.DataFrame(
        {"Surprise(%)": [12.5, 10.0]},
        index=pd.to_datetime(["2026-04-30", "2026-01-30"]),
    )

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker
            self.info = {
                "trailingPE": 30.0,
                "forwardPE": 25.0,
                "earningsGrowth": 0.2,
                "sector": "Technology",
            }
            self.earnings_dates = earnings_dates

    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", FakeTicker)

    df = server._enrich_fundamentals(
        pd.DataFrame({"ticker": ["AAPL"]}),
        sector_ema_by_etf={"XLK": True},
    )
    row = df.iloc[0]

    assert row["pe"] == 30.0
    assert row["eps_beat"] == 12.5
    assert row["eps_consecutive_beats"] is True
    assert row["peg_ratio"] == pytest.approx(1.5)
    assert row["sector_above_ema50"] is True


def test_enrich_fundamentals_uses_none_for_missing_data(monkeypatch):
    class FakeTicker:
        def __init__(self, ticker):
            self.info = {}
            self.earnings_dates = pd.DataFrame()

    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", FakeTicker)

    df = server._enrich_fundamentals(
        pd.DataFrame({"ticker": ["MISSING"]}),
        sector_ema_by_etf={},
    )
    row = df.iloc[0]

    assert row["pe"] is None
    assert row["eps_beat"] is None
    assert row["eps_consecutive_beats"] is None
    assert row["peg_ratio"] is None
    assert row["sector_above_ema50"] is None


def test_merge_cached_fundamentals_preserves_existing_values():
    fresh = pd.DataFrame({
        "ticker": ["AAPL", "MSFT"],
        "price": [200.0, 400.0],
        "momentum_score": [90.0, 80.0],
    })
    cached = [
        {"ticker": "AAPL", "pe": 30.0, "peg_ratio": 1.5},
        {"ticker": "OLD",  "pe": 10.0, "peg_ratio": 2.0},
    ]

    result = server._merge_cached_fundamentals(fresh, cached)

    aapl = result[result["ticker"] == "AAPL"].iloc[0]
    msft = result[result["ticker"] == "MSFT"].iloc[0]
    assert aapl["price"] == 200.0
    assert aapl["pe"] == 30.0
    assert aapl["peg_ratio"] == 1.5
    assert msft["pe"] is None
    assert msft["peg_ratio"] is None
    assert msft["eps_beat"] is None
    assert msft["eps_consecutive_beats"] is None
    assert msft["sector_above_ema50"] is None


def test_fetch_fund_dict_includes_context_fields(monkeypatch):
    earnings_dates = pd.DataFrame(
        {"Surprise(%)": [16.0, 11.0]},
        index=pd.to_datetime(["2026-04-30", "2026-01-30"]),
    )

    class FakeTicker:
        def __init__(self, ticker):
            self.info = {
                "trailingPE": 20.0,
                "earningsGrowth": 0.25,
                "sector": "Technology",
            }
            self.earnings_dates = earnings_dates

    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", FakeTicker)

    result = server._fetch_fund_dict(["AAPL"], sector_ema_by_etf={"XLK": True})

    assert result["AAPL"]["pe"] == 20.0
    assert result["AAPL"]["peg_ratio"] == pytest.approx(0.8)
    assert result["AAPL"]["eps_beat"] == 16.0
    assert result["AAPL"]["eps_consecutive_beats"] is True
    assert result["AAPL"]["sector_above_ema50"] is True


def test_refresh_live_scores_preserves_structure(monkeypatch):
    """_refresh_live_scores returns updated_scores list and empty ohlcv dict."""
    import yfinance as yf

    class FakeFastInfo:
        last_price = 110.0
        previous_close = 100.0

    monkeypatch.setattr(yf, "Ticker", lambda t: type("T", (), {"fast_info": FakeFastInfo()})())

    with server._lock:
        server._state["scores"] = [
            {"ticker": "AAPL", "price": 100.0, "day_return": 0.0, "pe": 30.0},
            {"ticker": "MSFT", "price": 200.0, "day_return": 0.0, "pe": 35.0},
        ]

    scores, ohlcv = server._refresh_live_scores("all")

    assert isinstance(scores, list)
    assert len(scores) == 2
    assert all("ticker" in r for r in scores)
    assert ohlcv == {}   # fast_info path returns no OHLCV


def test_refresh_live_scores_rejects_empty_cache():
    with server._lock:
        server._state["scores"] = []

    with pytest.raises(ValueError, match="no cached scores"):
        server._refresh_live_scores("all")
