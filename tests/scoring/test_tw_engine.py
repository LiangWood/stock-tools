import pandas as pd
import numpy as np
import pytest
from scoring.tw_engine import (
    compute_tw_breakout_candidates,
    compute_tw_rs_scores,
    compute_tw_scores,
    compute_tw_sector_rotation,
)

_N = 130


def _make_ohlcv(n=_N, trend="up"):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    if trend == "up":
        values = 100 + np.linspace(0, 8, n) + np.sin(np.arange(n) / 2) * 2
    else:
        values = 150 - np.linspace(0, 8, n) + np.sin(np.arange(n) / 2) * 2
    close = pd.Series(values, index=idx)
    return pd.DataFrame({
        "Open": close * 0.99, "High": close * 1.01,
        "Low": close * 0.98, "Close": close,
        "Volume": [1_000_000] * n,
    }, index=idx)


def _make_stock(fi_net=0.0, it_net=0.0, margin_chg=0,
                day_return=0.0, price=100.0, volume=1000,
                turnover_10k=50_000.0, ohlcv=None, pe=None, **extra):
    return {
        "code": "1234", "name": "測試股",
        "price": price, "volume": volume,
        "day_return": day_return,
        "fi_net": fi_net, "it_net": it_net,
        "margin_chg": margin_chg, "pe": pe,
        "turnover_10k": turnover_10k,
        "ohlcv": ohlcv if ohlcv is not None else _make_ohlcv(),
        **extra,
    }


def test_returns_dataframe():
    data = {f"T{i}.TW": _make_stock() for i in range(10)}
    result = compute_tw_scores(data)
    assert isinstance(result, pd.DataFrame)


def test_has_required_columns():
    data = {"A.TW": _make_stock(), "B.TW": _make_stock()}
    result = compute_tw_scores(data)
    for col in ["ticker", "name", "price", "volume", "day_return", "pe",
                "fi_net", "it_net", "margin_chg", "ret_10d", "ret_20d",
                "amount_ratio", "rsi", "tw_score",
                "is_limit_up", "is_limit_down",
                "limit_up_price", "limit_down_price", "limit_basis"]:
        assert col in result.columns, f"Missing column: {col}"


def test_ret_10d_is_computed_from_ohlcv():
    data = {"A.TW": _make_stock()}
    result = compute_tw_scores(data)
    close = data["A.TW"]["ohlcv"]["Close"]
    expected = float((close.iloc[-1] - close.iloc[-11]) / close.iloc[-11])

    assert result.iloc[0]["ret_10d"] == pytest.approx(expected)


def test_tw_rs_day_return_uses_exchange_quote_not_ohlcv():
    ohlcv = _make_ohlcv()
    ohlcv.loc[ohlcv.index[-2], "Close"] = 100.0
    ohlcv.loc[ohlcv.index[-1], "Close"] = 106.0
    data = {"A.TW": _make_stock(day_return=0.1, ohlcv=ohlcv)}

    result = compute_tw_rs_scores(data)

    assert result.iloc[0]["day_return"] == pytest.approx(0.1)


def test_tw_breakout_day_return_uses_exchange_quote_not_ohlcv(monkeypatch):
    monkeypatch.setattr("scoring.tw_engine._linear_slope", lambda _series, window: 0.01 if window == 20 else -0.01)
    monkeypatch.setattr("scoring.tw_engine._breakout_score", lambda *_args, **_kwargs: 80.0)
    ohlcv = _make_ohlcv(n=90, trend="up")
    ohlcv.loc[ohlcv.index[-2], "Close"] = 100.0
    ohlcv.loc[ohlcv.index[-1], "Close"] = 106.0
    data = {"A.TW": _make_stock(day_return=0.1, ohlcv=ohlcv)}

    result = compute_tw_breakout_candidates(data)

    assert result.iloc[0]["day_return"] == pytest.approx(0.1)


def test_limit_fields_preserved_for_ui_and_mcp():
    data = {
        "A.TW": _make_stock(
            is_limit_up=True,
            is_limit_down=False,
            limit_up_price=110.0,
            limit_down_price=90.0,
            limit_basis="tw-stock-agent:TWSE_TWT84U",
        )
    }

    result = compute_tw_scores(data)
    row = result.iloc[0]

    assert row["is_limit_up"] == True
    assert row["is_limit_down"] == False
    assert row["limit_up_price"] == 110.0
    assert row["limit_down_price"] == 90.0
    assert row["limit_basis"] == "tw-stock-agent:TWSE_TWT84U"


def test_top_n_limit():
    data = {f"T{i}.TW": _make_stock() for i in range(50)}
    result = compute_tw_scores(data)
    assert len(result) <= 200


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


def test_tw_chips_score_does_not_require_bullish_technicals_or_rsi_range():
    weak_ohlcv = _make_ohlcv(trend="down")
    data = {
        "CHIPS.TW": _make_stock(
            fi_net=5_000_000,
            it_net=3_000_000,
            margin_chg=-500,
            day_return=-0.02,
            ohlcv=weak_ohlcv,
        ),
        "BASE.TW": _make_stock(
            fi_net=100,
            it_net=100,
            margin_chg=500,
            day_return=0.01,
            ohlcv=_make_ohlcv(),
        ),
    }

    result = compute_tw_scores(data)

    assert "CHIPS.TW" in set(result["ticker"])


def test_tw_chips_score_prefers_institutional_flows_over_breakout_only():
    chip_stock = _make_stock(
        fi_net=5_000_000,
        it_net=3_000_000,
        inst_net=8_000_000,
        it_consec_days=5,
        fi_consec_days=4,
        margin_chg=-500,
    )
    breakout_only = _make_stock(
        fi_net=0,
        it_net=0,
        inst_net=0,
        it_consec_days=0,
        fi_consec_days=0,
        margin_chg=500,
        day_return=0.1,
    )
    data = {"CHIPS.TW": chip_stock, "BREAKOUT.TW": breakout_only}

    result = compute_tw_scores(data)
    ranked = result.set_index("ticker")

    assert ranked.loc["CHIPS.TW", "tw_score"] > ranked.loc["BREAKOUT.TW", "tw_score"]


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


def test_tw_sector_rotation_scores_main_sector_above_exiting_sector():
    data = {
        "2317.TW": _make_stock(price=100, fi_net=1_000_000, it_net=1_000_000),
        "6669.TW": _make_stock(price=100, fi_net=1_000_000, it_net=1_000_000),
        "2603.TW": _make_stock(price=100, fi_net=-1_000_000, it_net=-1_000_000),
        "2609.TW": _make_stock(price=100, fi_net=-1_000_000, it_net=-1_000_000),
    }
    history = {
        "2317": [{"date": f"2026-05-{i:02d}", "fi_net": i * 100_000, "it_net": i * 100_000} for i in range(1, 11)],
        "6669": [{"date": f"2026-05-{i:02d}", "fi_net": i * 80_000, "it_net": i * 80_000} for i in range(1, 11)],
        "2603": [{"date": f"2026-05-{i:02d}", "fi_net": -i * 100_000, "it_net": -i * 100_000} for i in range(1, 11)],
        "2609": [{"date": f"2026-05-{i:02d}", "fi_net": -i * 80_000, "it_net": -i * 80_000} for i in range(1, 11)],
    }

    result = compute_tw_sector_rotation(data, history=history)

    assert result["AI伺服器"]["sector_flow_status"] == "主力"
    assert result["航運"]["sector_flow_status"] == "退潮"
    assert result["AI伺服器"]["sector_flow_score"] > result["航運"]["sector_flow_score"]
    assert result["AI伺服器"]["sector_net_1d_yi"] > 0
    assert result["AI伺服器"]["stocks"][0]["stock_id"] in {"2317", "6669"}
    assert "net_1d_yi" in result["AI伺服器"]["stocks"][0]


def test_tw_scores_include_sector_flow_columns():
    data = {
        "2317.TW": _make_stock(fi_net=5_000_000, it_net=3_000_000, margin_chg=-500),
        "BASE.TW": _make_stock(fi_net=100, it_net=100, margin_chg=500),
    }

    result = compute_tw_scores(data)

    for col in ["sector_theme", "sector_flow_status", "sector_flow_score", "sector_net_1d_yi", "sector_ret_5d"]:
        assert col in result.columns
    assert result[result["ticker"] == "2317.TW"]["sector_theme"].iloc[0] == "AI伺服器"
