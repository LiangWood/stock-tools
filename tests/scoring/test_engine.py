import pandas as pd
import numpy as np
import pytest
from scoring.engine import calculate_rsi, compute_scores


# ── test helpers ──────────────────────────────────────────────────────────────

def _make_df(n=260, seed=42, drift=0.001, vol_surge_days=5, vol_multiplier=2.0):
    """Random-walk uptrend with optional end-of-series volume surge."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(drift, 0.015, n)
    prices  = 100 * np.cumprod(1 + returns)
    volume  = np.ones(n) * 1_000_000
    if vol_surge_days > 0:
        volume[-vol_surge_days:] = 1_000_000 * vol_multiplier
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame({"Close": pd.Series(prices, index=idx),
                         "Volume": pd.Series(volume, index=idx)})


def _make_downtrend_df(n=260):
    """Strong downtrend: price well below EMA200."""
    rng = np.random.default_rng(7)
    returns = rng.normal(-0.003, 0.015, n)
    prices  = 200 * np.cumprod(1 + returns)
    volume  = np.ones(n) * 1_000_000
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame({"Close": pd.Series(prices, index=idx),
                         "Volume": pd.Series(volume, index=idx)})


def _make_flat_volume_df(n=260):
    """Uptrend but completely flat volume (ratio = 1.0 < 1.5)."""
    df = _make_df(n, vol_surge_days=0)
    return df


def _make_close_df(close_values):
    idx = pd.date_range("2024-01-01", periods=len(close_values), freq="B")
    return pd.DataFrame({
        "Close": pd.Series(close_values, index=idx),
        "Volume": pd.Series(np.ones(len(close_values)) * 1_000_000, index=idx),
    })


# ── RSI tests (unchanged behaviour) ──────────────────────────────────────────

def test_rsi_uptrend_above_50():
    df = _make_df(60, vol_surge_days=0)
    assert calculate_rsi(df["Close"]) > 50


def test_rsi_downtrend_below_50():
    # Use deterministic linear decline so RSI is unambiguously < 50
    idx   = pd.date_range("2024-01-01", periods=60, freq="B")
    close = pd.Series(np.linspace(150, 100, 60), index=idx)
    assert calculate_rsi(close) < 50


def test_rsi_range_0_to_100():
    rsi = calculate_rsi(_make_df(60)["Close"])
    assert 0 <= rsi <= 100


# ── New column contract ───────────────────────────────────────────────────────

def test_compute_scores_returns_dataframe():
    result = compute_scores({"T1": _make_df(), "T2": _make_df(seed=99)})
    assert isinstance(result, pd.DataFrame)


def test_compute_scores_has_required_columns():
    result = compute_scores({"T1": _make_df(), "T2": _make_df(seed=99)})
    for col in ["ticker", "price", "day_return", "ret_20d",
                "rs_vs_spy", "rs_5d_vs_spy", "vol_ratio", "rsi", "momentum_score"]:
        assert col in result.columns, f"missing column: {col}"


def test_compute_scores_sorted_descending():
    data = {f"T{i}": _make_df(seed=i) for i in range(10)}
    result = compute_scores(data)
    scores = result["momentum_score"].tolist()
    assert scores == sorted(scores, reverse=True)


def test_compute_scores_score_between_0_and_100():
    data = {f"T{i}": _make_df(seed=i) for i in range(5)}
    result = compute_scores(data)
    if not result.empty:
        assert result["momentum_score"].between(0, 100).all()


def test_compute_scores_top_n():
    """compute_scores should return at most TOP_N results."""
    from scoring.engine import TOP_N
    data = {f"T{i}": _make_df(seed=i) for i in range(150)}
    result = compute_scores(data)
    assert len(result) <= TOP_N


# ── Hard filter tests ─────────────────────────────────────────────────────────

def test_downtrend_stock_excluded():
    """Stock in sustained downtrend should be filtered out."""
    result = compute_scores({"BULL": _make_df(), "BEAR": _make_downtrend_df()})
    assert "BEAR" not in result["ticker"].tolist()


def test_none_ticker_excluded():
    """None DataFrame → excluded entirely (not scored as zero)."""
    result = compute_scores({"GOOD": _make_df(), "NULL": None})
    assert "NULL" not in result["ticker"].tolist()


def test_short_history_excluded():
    """< 105 days of data → excluded (not enough for EMA100)."""
    result = compute_scores({"SHORT": _make_df(n=50), "LONG": _make_df(n=260)})
    assert "SHORT" not in result["ticker"].tolist()


def test_flat_volume_scores_lower():
    """Flat volume scores lower than volume-surging stock (not excluded, but ranked below)."""
    flat  = _make_flat_volume_df()
    surge = _make_df(seed=42)  # has volume surge
    result = compute_scores({"FLAT": flat, "SURGE": surge})
    if len(result) == 2:
        flat_score  = result.loc[result["ticker"] == "FLAT",  "momentum_score"].iloc[0]
        surge_score = result.loc[result["ticker"] == "SURGE", "momentum_score"].iloc[0]
        assert surge_score >= flat_score


# ── SPY relative strength ─────────────────────────────────────────────────────

def test_spy_not_in_output():
    """SPY is used as benchmark and must not appear as a result row."""
    spy = _make_df(seed=0)
    result = compute_scores({"AAPL": _make_df(), "SPY": spy})
    assert "SPY" not in result["ticker"].tolist()


def test_rs_vs_spy_present_and_nonzero():
    """rs_vs_spy should reflect relative outperformance vs SPY."""
    spy = _make_df(seed=0, drift=0.0001)   # near-flat SPY
    bull = _make_df(seed=42, drift=0.002)  # outperforms SPY
    result = compute_scores({"BULL": bull, "SPY": spy})
    if not result.empty:
        assert result.iloc[0]["rs_vs_spy"] != 0.0


def test_rs_vs_spy_uses_21_day_window():
    """RS should track the recent 21-day launch, not stale 63-day weakness."""
    base = np.ones(130) * 100
    stock = base.copy()
    stock[-22] = 100
    stock[-1] = 110
    spy = base.copy()
    spy[-22] = 100
    spy[-1] = 102

    result = compute_scores({"LAUNCH": _make_close_df(stock), "SPY": _make_close_df(spy)})

    row = result[result["ticker"] == "LAUNCH"].iloc[0]
    assert row["rs_vs_spy"] == pytest.approx(0.08)


def test_rs_5d_vs_spy_captures_fresh_breakout():
    """Five-day RS is scored separately so very recent breakouts are not buried."""
    stock = np.ones(130) * 100
    stock[-6] = 100
    stock[-1] = 108
    spy = np.ones(130) * 100
    spy[-6] = 100
    spy[-1] = 101

    result = compute_scores({"FRESH": _make_close_df(stock), "SPY": _make_close_df(spy)})

    row = result[result["ticker"] == "FRESH"].iloc[0]
    assert row["rs_5d_vs_spy"] == pytest.approx(0.07)


# ── Empty / edge cases ────────────────────────────────────────────────────────

def test_empty_input_returns_empty_dataframe():
    result = compute_scores({})
    assert isinstance(result, pd.DataFrame)
    assert result.empty


def test_all_filtered_out_returns_empty_dataframe():
    result = compute_scores({"BEAR": _make_downtrend_df()})
    assert result.empty
