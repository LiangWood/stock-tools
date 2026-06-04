import pandas as pd


def compute_crypto_rs_scores(ticker_data: dict) -> pd.DataFrame:
    records = []
    for ticker, d in ticker_data.items():
        if not d:
            continue
        ohlcv = d.get("ohlcv")
        if ohlcv is None or ohlcv.empty or "Close" not in ohlcv:
            continue
        close = ohlcv["Close"].dropna()
        n = len(close)
        if n < 64 or float(close.iloc[-1]) <= 0:
            continue

        q1 = float((close.iloc[-1] - close.iloc[-64]) / close.iloc[-64]) if n >= 64 else 0.0
        q2 = float((close.iloc[-64] - close.iloc[-127]) / close.iloc[-127]) if n >= 127 else 0.0
        q3 = float((close.iloc[-127] - close.iloc[-190]) / close.iloc[-190]) if n >= 190 else 0.0
        q4 = float((close.iloc[-190] - close.iloc[-253]) / close.iloc[-253]) if n >= 253 else 0.0

        records.append({
            "ticker": ticker,
            "name": d.get("name", ticker),
            "price": d.get("price", float(close.iloc[-1])),
            "day_return": d.get("day_return", 0.0),
            "quote_volume": d.get("quote_volume", 0.0),
            "volume": d.get("volume", 0.0),
            "q1_pct": q1,
            "q2_pct": q2,
            "q3_pct": q3,
            "q4_pct": q4,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    for col in ("q1_pct", "q2_pct", "q3_pct", "q4_pct"):
        df[f"_{col}_rank"] = df[col].rank(pct=True, na_option="bottom") * 99

    df["rs_rating"] = (
        df["_q1_pct_rank"] * 0.50 +
        df["_q2_pct_rank"] * 0.25 +
        df["_q3_pct_rank"] * 0.15 +
        df["_q4_pct_rank"] * 0.10
    ).clip(0, 99).round(1)

    result = (
        df.drop(columns=[c for c in df.columns if c.startswith("_")])
        .sort_values("rs_rating", ascending=False)
        .head(100)
        .reset_index(drop=True)
    )
    result.insert(0, "rank", range(1, len(result) + 1))
    return result
