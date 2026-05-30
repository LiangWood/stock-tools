from typing import Optional
import numpy as np
import pandas as pd

# ── 評分權重 ──────────────────────────────────────────────────────────────────
_WEIGHTS = {
    "_rs_rank":     0.18,   # RS vs SPY 21日超額報酬（近期動能）
    "_rs5_rank":    0.12,   # RS vs SPY 5日超額報酬（剛啟動動能）
    "_macd_score":  0.18,   # MACD 動能狀態
    "_vol_rank":    0.14,   # 量比 5d/20d
    "_ema_align":   0.10,   # EMA 排列程度（0=無/100=完整多頭）
    "_vol1d_rank":  0.08,   # 今日爆量
    "_rsi_rank":    0.08,   # RSI 接近最佳啟動區
    "_ret20_rank":  0.07,   # 20 日報酬
    "_ret10_rank":  0.05,   # 10 日報酬
}

TOP_N = 100   # 只顯示前 100 名


def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    delta = prices.diff().dropna()
    if len(delta) < period:
        return 50.0
    gain = delta.clip(lower=0).iloc[-period:]
    loss = (-delta).clip(lower=0).iloc[-period:]
    avg_gain = gain.mean()
    avg_loss = loss.mean()
    if avg_loss == 0:
        return 100.0
    return float(100 - (100 / (1 + avg_gain / avg_loss)))


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _macd_score(close: pd.Series) -> float:
    """MACD 動能評分：零軸以上金叉=100，零軸以下金叉=60，柱狀圖上升=40，其餘=20"""
    macd_line   = _ema(close, 12) - _ema(close, 26)
    signal_line = _ema(macd_line, 9)
    histogram   = macd_line - signal_line
    m      = float(macd_line.iloc[-1])
    s      = float(signal_line.iloc[-1])
    h      = float(histogram.iloc[-1])
    h_prev = float(histogram.iloc[-2]) if len(histogram) >= 2 else h
    if m > 0 and m > s:
        return 100.0
    if m > s:
        return 60.0
    if h > h_prev:
        return 40.0
    return 20.0


def _ema_alignment_score(close: pd.Series) -> float:
    """
    EMA 排列評分（0-100），以 EMA100 為基準（配合 6mo 資料週期）：
    - price > EMA20             → +33 分
    - EMA20 > EMA50             → +33 分（中期動能翻多，早期訊號）
    - EMA50 > EMA100            → +34 分（完整中期多頭排列）
    """
    price  = float(close.iloc[-1])
    ema20  = float(_ema(close, 20).iloc[-1])
    ema50  = float(_ema(close, 50).iloc[-1])
    ema100 = float(_ema(close, 100).iloc[-1])
    score  = 0.0
    if price > ema20:  score += 33.0
    if ema20  > ema50: score += 33.0
    if ema50  > ema100: score += 34.0
    return score


def _passes_hard_filter(close: pd.Series) -> bool:
    """
    硬過濾（任一滿足即進入候選）：
    1. price > EMA100       — 中期趨勢確認
    2. EMA20 > EMA50        — 短期金叉，捕捉底部啟動
    3. price > EMA50 × 0.98 — 站回 50MA 或在 2% 緩衝內（捕捉 SOFI/PLTR 類型）
    最小資料需求 105 天。
    """
    if len(close) < 105:
        return False
    price  = float(close.iloc[-1])
    ema20  = float(_ema(close, 20).iloc[-1])
    ema50  = float(_ema(close, 50).iloc[-1])
    ema100 = float(_ema(close, 100).iloc[-1])
    return (price > ema100) or (ema20 > ema50) or (price > ema50 * 0.98)


def _setup_score(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
) -> float:
    """
    模式 A：起漲「前」的蓄勢度（0–100）。
    安靜、量縮、波幅收斂、靠近高點、RSI 中性 → 像壓縮的彈簧，隨時可能彈起。

      量能萎縮    30%  近5日均量 / 近20日均量越小越好
      波幅收縮    30%  近5日ATR  / 近20日ATR  越小越好
      靠近近期高點 25%  股價離20日高點越近
      RSI 中性區  15%  RSI 45–60 最佳（未超買）
    """
    v5  = float(volume.iloc[-5:].mean())
    v20 = float(volume.iloc[-20:].mean())
    vol_ratio = v5 / v20 if v20 > 0 else 1.0
    vol_score = float(np.clip(100 - vol_ratio * 60, 0, 100))

    atr5  = float((high.iloc[-5:]  - low.iloc[-5:]).mean())
    atr20 = float((high.iloc[-20:] - low.iloc[-20:]).mean())
    range_ratio = atr5 / atr20 if atr20 > 0 else 1.0
    range_score = float(np.clip(100 - range_ratio * 80, 0, 100))

    high20 = float(high.iloc[-20:].max())
    pct_from_high = max(0.0, (high20 - float(close.iloc[-1])) / high20)
    near_high_score = float(np.clip(100 - pct_from_high * 1000, 0, 100))

    rsi = calculate_rsi(close)
    rsi_score = float(np.clip(100 - abs(rsi - 52) * 3.5, 0, 100))

    return (
        vol_score       * 0.30 +
        range_score     * 0.30 +
        near_high_score * 0.25 +
        rsi_score       * 0.15
    )


def _breakout_confirm_score(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
) -> float:
    """
    模式 B：起漲「當下」的突破確認（0–100）。
    放量 + 突破近高 + 當日強漲 → 正在發動的突破（如剛起漲的 PLTR/DELL/MSFT）。

      今日量能爆發 40%  今日量 / 近20日均量，越大越確認
      突破近高    35%  收盤 vs 前20日高點（不含今日），站上 = 創新高突破
      當日強漲    25%  今日漲幅越大越強
    """
    # 今日量能爆發
    vol_20d = float(volume.iloc[-20:].mean())
    vol_today = float(volume.iloc[-1])
    vol_surge = vol_today / vol_20d if vol_20d > 0 else 1.0
    surge_score = float(np.clip((vol_surge - 1.0) * 67, 0, 100))  # 1x=0, 1.5x=33, 2x=67, 2.5x=100

    # 突破前20日高點（排除今日，偵測新高）
    prior_high = float(high.iloc[-21:-1].max())
    px_ratio = float(close.iloc[-1]) / prior_high if prior_high > 0 else 0.0
    breakout_score = float(np.clip((px_ratio - 0.95) * 1200, 0, 100))  # 前高=60, +3.3%=100

    # 當日強漲
    day_ret = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) if len(close) >= 2 else 0.0
    day_score = float(np.clip(day_ret * 100 * 18, 0, 100))  # +1%=18, +3%=54, +5.5%=100

    return (
        surge_score    * 0.40 +
        breakout_score * 0.35 +
        day_score      * 0.25
    )


def _breakout_score(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
) -> float:
    """
    混合突破評分（0–100）= max(蓄勢度 A, 突破確認 B)。

    - 安靜蓄勢的彈簧股 → A 高 → 提前埋伏
    - 正在放量突破的股 → B 高 → 突破確認（剛起漲）
    - 已漲完在高檔飄的 → A、B 皆低 → 正確排除

    取 max 讓「漲之前」與「漲當下」兩種狀態都能浮上來。
    """
    n = len(close)
    if n < 21 or len(high) < 21 or len(low) < 21 or len(volume) < 20:
        return 50.0

    setup   = _setup_score(close, high, low, volume)
    confirm = _breakout_confirm_score(close, high, low, volume)
    return float(max(setup, confirm))


def _metrics_for(
    ticker: str,
    df: Optional[pd.DataFrame],
    spy_ret_21d: float,
    spy_ret_5d: float,
) -> Optional[dict]:
    if df is None or df.empty:
        return None
    close  = df["Close"].dropna()
    volume = df["Volume"].dropna()
    high   = df["High"].dropna()  if "High"  in df.columns else close
    low    = df["Low"].dropna()   if "Low"   in df.columns else close
    if not _passes_hard_filter(close):
        return None

    n     = len(close)
    price = float(close.iloc[-1])

    day_return = float((close.iloc[-1] - close.iloc[-2])  / close.iloc[-2])  if n >= 2  else 0.0
    ret_10d    = float((close.iloc[-1] - close.iloc[-11]) / close.iloc[-11]) if n >= 11 else 0.0
    ret_20d    = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0

    # RS vs SPY 21 日（比 63 日更快捕捉近期動能爆發）
    rs_vs_spy = 0.0
    if n >= 22:
        ret_21d   = float((close.iloc[-1] - close.iloc[-22]) / close.iloc[-22])
        rs_vs_spy = ret_21d - spy_ret_21d

    # RS vs SPY 5 日：補足剛啟動標的，避免被 1 個月內早段弱勢壓住
    rs_5d_vs_spy = 0.0
    if n >= 6:
        ret_5d        = float((close.iloc[-1] - close.iloc[-6]) / close.iloc[-6])
        rs_5d_vs_spy  = ret_5d - spy_ret_5d

    # 量比 5d/20d
    vol_ratio = 1.0
    if len(volume) >= 20:
        v5  = float(volume.iloc[-5:].mean())
        v20 = float(volume.iloc[-20:].mean())
        if v20 > 0:
            vol_ratio = v5 / v20

    # 今日量比
    vol_1d20d = 1.0
    if len(volume) >= 21:
        denom = float(volume.iloc[-21:-1].mean())
        if denom > 0:
            vol_1d20d = float(volume.iloc[-1]) / denom

    # % 高於 EMA100（配合 6mo 資料，EMA100 更可靠）
    ema100_val   = float(_ema(close, 100).iloc[-1])
    ema_above200 = float((close.iloc[-1] - ema100_val) / ema100_val) if ema100_val > 0 else 0.0

    return {
        "ticker":       ticker,
        "price":        price,
        "ema_above200": ema_above200,
        "day_return":   day_return,
        "vol_1d20d":    vol_1d20d,
        "ret_10d":      ret_10d,
        "ret_20d":      ret_20d,
        "rs_vs_spy":    rs_vs_spy,
        "rs_5d_vs_spy": rs_5d_vs_spy,
        "vol_ratio":    vol_ratio,
        "rsi":             calculate_rsi(close),
        "breakout_score":  _breakout_score(close, high, low, volume),
        "_macd_score":     _macd_score(close),
        "_ema_align":      _ema_alignment_score(close),
    }


def compute_scores(ticker_data: dict) -> pd.DataFrame:
    _cols  = ["ticker", "price", "ema_above200", "day_return", "vol_1d20d",
              "ret_10d", "ret_20d", "rs_vs_spy", "rs_5d_vs_spy",
              "vol_ratio", "rsi", "breakout_score", "momentum_score"]
    _empty = pd.DataFrame(columns=_cols)

    if not ticker_data:
        return _empty

    spy_ret_21d = 0.0
    spy_ret_5d = 0.0
    spy_df = ticker_data.get("SPY")
    if spy_df is not None and not spy_df.empty:
        spy_close = spy_df["Close"].dropna()
        if len(spy_close) >= 22:
            spy_ret_21d = float(
                (spy_close.iloc[-1] - spy_close.iloc[-22]) / spy_close.iloc[-22]
            )
        if len(spy_close) >= 6:
            spy_ret_5d = float(
                (spy_close.iloc[-1] - spy_close.iloc[-6]) / spy_close.iloc[-6]
            )

    rows = [
        _metrics_for(t, df, spy_ret_21d, spy_ret_5d)
        for t, df in ticker_data.items()
        if t != "SPY"
    ]
    rows = [r for r in rows if r is not None]

    if not rows:
        return _empty

    df = pd.DataFrame(rows)

    df["_rs_rank"]    = df["rs_vs_spy"].rank(pct=True) * 100
    df["_rs5_rank"]   = df["rs_5d_vs_spy"].rank(pct=True) * 100
    df["_vol_rank"]   = df["vol_ratio"].rank(pct=True) * 100
    df["_vol1d_rank"] = df["vol_1d20d"].rank(pct=True) * 100
    df["_ret20_rank"] = df["ret_20d"].rank(pct=True) * 100
    df["_ret10_rank"] = df["ret_10d"].rank(pct=True) * 100
    # RSI 分數以 60 為峰值（不作硬過濾）
    df["_rsi_rank"]   = (100 - (df["rsi"] - 60).abs() * 3).clip(0, 100)
    # EMA 排列直接使用 0-100 分（不做百分位，保留絕對意義）

    weights    = np.array(list(_WEIGHTS.values()))
    score_cols = list(_WEIGHTS.keys())
    df["momentum_score"] = (df[score_cols].values @ weights).clip(0, 100)

    result = (
        df.drop(columns=[c for c in df.columns if c.startswith("_")])
        .sort_values("momentum_score", ascending=False)
        .head(TOP_N)
        .reset_index(drop=True)
    )
    result["rank"] = range(1, len(result) + 1)   # 真實動能排名，排序改變時不變
    return result
