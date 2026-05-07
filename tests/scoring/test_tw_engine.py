import pandas as pd
import numpy as np
import pytest
from scoring.tw_engine import compute_tw_scores

_N = 130


def _make_ohlcv(n=_N, trend="up"):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = pd.Series(np.linspace(100, 150, n) if trend == "up" else np.linspace(150, 100, n), index=idx)
    return pd.DataFrame({
        "Open": close * 0.99, "High": close * 1.01,
        "Low": close * 0.98, "Close": close,
        "Volume": [1_000_000] * n,
    }, index=idx)


def _make_stock(fi_net=0.0, it_net=0.0, margin_chg=0,
                day_return=0.0, price=100.0, volume=1000,
                ohlcv=None, pe=None):
    return {
        "code": "1234", "name": "測試股",
        "price": price, "volume": volume,
        "day_return": day_return,
        "fi_net": fi_net, "it_net": it_net,
        "margin_chg": margin_chg, "pe": pe,
        "ohlcv": ohlcv if ohlcv is not None else _make_ohlcv(),
    }


def test_returns_dataframe():
    data = {f"T{i}.TW": _make_stock() for i in range(10)}
    result = compute_tw_scores(data)
    assert isinstance(result, pd.DataFrame)


def test_has_required_columns():
    data = {"A.TW": _make_stock(), "B.TW": _make_stock()}
    result = compute_tw_scores(data)
    for col in ["ticker", "name", "price", "volume", "day_return", "pe",
                "fi_net", "it_net", "margin_chg", "ret_20d",
                "amount_ratio", "rsi", "tw_score"]:
        assert col in result.columns, f"Missing column: {col}"


def test_top_20_limit():
    data = {f"T{i}.TW": _make_stock() for i in range(50)}
    result = compute_tw_scores(data)
    assert len(result) <= 20


def test_sorted_descending():
    data = {f"T{i}.TW": _make_stock(fi_net=float(i)) for i in range(30)}
    result = compute_tw_scores(data)
    scores = result["tw_score"].tolist()
    assert scores == sorted(scores, reverse=True)


def test_score_between_0_and_100():
    data = {f"T{i}.TW": _make_stock(fi_net=float(i * 100)) for i in range(20)}
    result = compute_tw_scores(data)
    assert result["tw_score"].between(0, 100).all()


def test_margin_chg_reverse_scoring():
    """高融資增減的股票評分應低於低融資增減的股票（反向計分）"""
    data = {
        "HIGH_MARGIN.TW": _make_stock(margin_chg=100_000, fi_net=1.0),
        "LOW_MARGIN.TW":  _make_stock(margin_chg=-100_000, fi_net=1.0),
    }
    result = compute_tw_scores(data)
    high_score = result[result["ticker"] == "HIGH_MARGIN.TW"]["tw_score"].iloc[0]
    low_score  = result[result["ticker"] == "LOW_MARGIN.TW"]["tw_score"].iloc[0]
    assert low_score > high_score


def test_none_ohlcv_handled():
    data = {
        "GOOD.TW": _make_stock(ohlcv=_make_ohlcv()),
        "BAD.TW":  _make_stock(ohlcv=None),
    }
    result = compute_tw_scores(data)
    assert len(result) > 0


def test_fewer_than_20_stocks():
    data = {f"T{i}.TW": _make_stock() for i in range(5)}
    result = compute_tw_scores(data)
    assert len(result) == 5
