import numpy as np
import pandas as pd
from scoring.engine import calculate_rsi

_TW_WEIGHTS = {
    "fi_net":       0.30,
    "it_net":       0.20,
    "margin_chg":   0.15,  # 反向
    "day_return":   0.10,
    "ret_20d":      0.10,
    "amount_ratio": 0.10,
    "rsi":          0.05,
}


def _tech_metrics(ohlcv: pd.DataFrame | None) -> dict:
    defaults = {"ret_20d": 0.0, "amount_ratio": 1.0, "rsi": 50.0}
    if ohlcv is None or len(ohlcv) < 2:
        return defaults
    ohlcv_clean = ohlcv[["Close", "Volume"]].dropna()
    close  = ohlcv_clean["Close"]
    volume = ohlcv_clean["Volume"]
    n = len(close)
    if n < 2:
        return defaults

    ret_20d = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0

    amount = close * volume
    amt_avg = float(amount.iloc[-21:-1].mean()) if n >= 21 else float(amount.mean())
    amount_ratio = float(amount.iloc[-1] / amt_avg) if amt_avg > 0 else 1.0

    rsi = calculate_rsi(close)
    return {"ret_20d": ret_20d, "amount_ratio": amount_ratio, "rsi": rsi}


def compute_tw_scores(ticker_data: dict) -> pd.DataFrame:
    rows = []
    for ticker, d in ticker_data.items():
        if d is None:
            continue
        tech = _tech_metrics(d.get("ohlcv"))
        rows.append({
            "ticker":       ticker,
            "name":         d.get("name", ""),
            "price":        d.get("price", 0.0),
            "volume":       d.get("volume", 0),
            "day_return":   d.get("day_return", 0.0),
            "pe":           d.get("pe"),
            "fi_net":       d.get("fi_net", 0.0),
            "it_net":       d.get("it_net", 0.0),
            "margin_chg":   d.get("margin_chg", 0),
            "ret_20d":      tech["ret_20d"],
            "amount_ratio": tech["amount_ratio"],
            "rsi":          tech["rsi"],
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    weights = np.array(list(_TW_WEIGHTS.values()))
    metric_cols = list(_TW_WEIGHTS.keys())

    ranks = pd.DataFrame(index=df.index)
    for col in metric_cols:
        if col == "margin_chg":
            ranks[col] = (-df[col]).rank(pct=True) * 100
        else:
            ranks[col] = df[col].rank(pct=True) * 100

    df["tw_score"] = ranks[metric_cols].values @ weights

    return (
        df.sort_values("tw_score", ascending=False)
        .head(20)
        .reset_index(drop=True)
    )
