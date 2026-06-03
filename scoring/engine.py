from __future__ import annotations

from typing import Optional
import numpy as np
import pandas as pd

# ── FinTasticRS v3.1 評分權重 ─────────────────────────────────────────────────
# RS Rating：純斜率趨勢強度（不含 breakout）
# breakout_score：獨立管道，與 RS Rating 分開貢獻
_WEIGHTS = {
    "_rs_rating_rank":     0.18,  # FinTasticRS 純斜率動能（趨勢持續性）
    "_breakout_rank":      0.12,  # 量價突破確認（獨立管道，捕捉急拉型）
    "_ema_align":          0.15,  # EMA 21/50 多頭排列
    "_vol1d_rank":         0.13,  # 今日量比
    "_rs_trend_score":     0.10,  # RS 趨勢方向（momentum_shift）
    "_rs_breakout_score":  0.08,  # RS 翻強旗標（早期輪動）
    "_vol_rank":           0.09,  # 5日量比
    "_rsi_rank":           0.08,  # RSI 時機確認
    "_eps_beat_rank":      0.07,  # EPS 超預期（基本面支撐）
    # 合計 = 1.00
}

_CONTEXT_WEIGHTS = {
    "technical_score":    0.72,
    "_sector_score":      0.12,
    "_valuation_score":   0.10,
    "_earnings_score":    0.06,
}

TOP_N = 100          # Layer 1：依 FinTasticRS RS Rank 取前 100 檔


# ── 基本面評分函數 ────────────────────────────────────────────────────────────

def _score_pe(pe) -> float:
    """P/E 排雷：負值或 >200 直接歸零。"""
    try:
        if pe is None or pd.isna(pe):
            return 50.0
        pe = float(pe)
    except Exception:
        return 50.0
    if pe <= 0 or pe > 200:
        return 0.0
    if pe <= 25:   return 100.0
    if pe <= 50:   return float(100 - (pe - 25) / 25 * 25)
    if pe <= 100:  return float(75  - (pe - 50)  / 50 * 35)
    return float(40 - (pe - 100) / 100 * 30)


def _score_peg(peg) -> float:
    """PEG < 1 最佳，作為同分標的優先排序。"""
    try:
        if peg is None or pd.isna(peg):
            return 50.0
        peg = float(peg)
    except Exception:
        return 50.0
    if peg <= 0:  return 20.0
    if peg <= 1:  return 100.0
    if peg <= 2:  return float(100 - (peg - 1) * 30)
    if peg <= 4:  return float(70  - (peg - 2) * 17.5)
    return 10.0


def _score_eps_beat(eps_beat) -> float:
    """EPS 超預期分數。"""
    try:
        if eps_beat is None or pd.isna(eps_beat):
            return 50.0
        eps_beat = float(eps_beat)
    except Exception:
        return 50.0
    if eps_beat >= 15: return 100.0
    if eps_beat >= 10: return 80.0
    if eps_beat >= 0:  return 55.0
    if eps_beat >= -10: return 25.0
    return 0.0


def _score_sector(value) -> float:
    """板塊 ETF 站上週線 EMA50 為強。"""
    if value is True:   return 100.0
    if value is False:  return 25.0
    return 50.0


# ── FinTasticRS 核心計算函數 ──────────────────────────────────────────────────

def _linear_slope(series: pd.Series, period: int) -> float:
    """
    正規化線性迴歸斜率（% of avg price per day）。
    數值越大 = 趨勢越陡峭、動能越旺盛。
    這是 FinTasticRS 的核心指標（60日佔50%、120日佔30%）。
    """
    n = len(series)
    if n < period:
        return 0.0
    y = series.values[-period:].astype(float)
    x = np.arange(period, dtype=float)
    try:
        slope = float(np.polyfit(x, y, 1)[0])
        avg   = float(np.mean(y))
        return (slope / avg * 100) if avg > 0 else 0.0
    except Exception:
        return 0.0


def _ud_ratio(close: pd.Series, period: int = 60) -> float:
    """
    60日上行/下行波動標準差比（風報比）。
    取代 R²，跳空上漲使比值上升（正面），跳空下跌使比值下降（負面）。
    門檻 ≥ 1.4 為合格，≥ 1.5 為高品質動能。
    """
    if len(close) < period + 2:
        return 1.0
    rets = close.iloc[-period:].pct_change().dropna()
    up   = rets[rets > 0]
    dn   = rets[rets < 0]
    if len(up) < 3 or len(dn) < 3:
        return 1.0
    up_std = float(up.std())
    dn_std = float(abs(dn).std())
    if dn_std == 0 or pd.isna(dn_std):
        return 3.0
    return float(np.clip(up_std / dn_std, 0.1, 5.0))


def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    delta    = prices.diff().dropna()
    if len(delta) < period:
        return 50.0
    gain     = delta.clip(lower=0).iloc[-period:]
    loss     = (-delta).clip(lower=0).iloc[-period:]
    avg_gain = gain.mean()
    avg_loss = loss.mean()
    if avg_loss == 0:
        return 100.0
    return float(100 - (100 / (1 + avg_gain / avg_loss)))


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _macd_score(close: pd.Series) -> float:
    macd_line   = _ema(close, 12) - _ema(close, 26)
    signal_line = _ema(macd_line, 9)
    histogram   = macd_line - signal_line
    m      = float(macd_line.iloc[-1])
    s      = float(signal_line.iloc[-1])
    h      = float(histogram.iloc[-1])
    h_prev = float(histogram.iloc[-2]) if len(histogram) >= 2 else h
    if m > 0 and m > s:  return 100.0
    if m > s:            return 60.0
    if h > h_prev:       return 40.0
    return 20.0


def _ema_alignment_score(close: pd.Series) -> float:
    """
    EMA 多頭排列評分（0-100）。
    JSON 策略要求 EMA 21/50 排列 + 股價站上。
    """
    price  = float(close.iloc[-1])
    ema21  = float(_ema(close, 21).iloc[-1])
    ema50  = float(_ema(close, 50).iloc[-1])
    ema100 = float(_ema(close, 100).iloc[-1])
    score  = 0.0
    if price > ema21:   score += 34.0   # 股價站上短期均線
    if ema21  > ema50:  score += 33.0   # 短中期金叉
    if ema50  > ema100: score += 33.0   # 完整多頭排列
    return score


def _passes_quality_filter(close: pd.Series) -> bool:
    """Layer 1 only keeps rows with enough valid price history."""
    return len(close) >= 120 and float(close.iloc[-1]) > 0


def _passes_technical_filter(close: pd.Series) -> bool:
    """
    技術面加成硬性過濾：
    1. 資料至少 105 天
    2. price > EMA50 × 0.98、EMA21 > EMA50、price > EMA100 任一成立
    """
    if len(close) < 105:
        return False
    price  = float(close.iloc[-1])
    ema21  = float(_ema(close, 21).iloc[-1])
    ema50  = float(_ema(close, 50).iloc[-1])
    ema100 = float(_ema(close, 100).iloc[-1])
    # 季線守護：站上 EMA50 或在 2% 緩衝內
    above_60ma = price > ema50 * 0.98
    # 或者已有短期金叉
    short_cross = ema21 > ema50
    # 或者中期趨勢良好
    mid_trend = price > ema100
    return above_60ma or short_cross or mid_trend


def _setup_score(
    close: pd.Series, high: pd.Series, low: pd.Series, volume: pd.Series,
) -> float:
    v5  = float(volume.iloc[-5:].mean())
    v20 = float(volume.iloc[-20:].mean())
    vol_ratio   = v5 / v20 if v20 > 0 else 1.0
    vol_score   = float(np.clip(100 - vol_ratio * 60, 0, 100))

    atr5  = float((high.iloc[-5:]  - low.iloc[-5:]).mean())
    atr20 = float((high.iloc[-20:] - low.iloc[-20:]).mean())
    range_ratio  = atr5 / atr20 if atr20 > 0 else 1.0
    range_score  = float(np.clip(100 - range_ratio * 80, 0, 100))

    high20 = float(high.iloc[-20:].max())
    pct_from_high = max(0.0, (high20 - float(close.iloc[-1])) / high20)
    near_high_score = float(np.clip(100 - pct_from_high * 1000, 0, 100))

    rsi = calculate_rsi(close)
    rsi_score = float(np.clip(100 - abs(rsi - 52) * 3.5, 0, 100))

    return vol_score * 0.30 + range_score * 0.30 + near_high_score * 0.25 + rsi_score * 0.15


def _breakout_confirm_score(
    close: pd.Series, high: pd.Series, low: pd.Series, volume: pd.Series,
) -> float:
    vol_20d   = float(volume.iloc[-20:].mean())
    vol_today = float(volume.iloc[-1])
    vol_surge = vol_today / vol_20d if vol_20d > 0 else 1.0
    surge_score = float(np.clip((vol_surge - 1.0) * 67, 0, 100))

    prior_high    = float(high.iloc[-21:-1].max())
    px_ratio      = float(close.iloc[-1]) / prior_high if prior_high > 0 else 0.0
    breakout_score = float(np.clip((px_ratio - 0.95) * 1200, 0, 100))

    day_ret   = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) if len(close) >= 2 else 0.0
    day_score = float(np.clip(day_ret * 100 * 18, 0, 100))

    return surge_score * 0.40 + breakout_score * 0.35 + day_score * 0.25


def _breakout_score(
    close: pd.Series, high: pd.Series, low: pd.Series, volume: pd.Series,
) -> float:
    n = len(close)
    if n < 21 or len(high) < 21 or len(low) < 21 or len(volume) < 20:
        return 50.0
    return float(max(_setup_score(close, high, low, volume),
                     _breakout_confirm_score(close, high, low, volume)))


# ── 每支股票的指標計算 ────────────────────────────────────────────────────────

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
    high   = df["High"].dropna() if "High" in df.columns else close
    low    = df["Low"].dropna()  if "Low"  in df.columns else close

    if not _passes_quality_filter(close):
        return None

    n     = len(close)
    price = float(close.iloc[-1])
    pass_technical = _passes_technical_filter(close)

    day_return = float((close.iloc[-1] - close.iloc[-2])  / close.iloc[-2])  if n >= 2  else 0.0
    ret_10d    = float((close.iloc[-1] - close.iloc[-11]) / close.iloc[-11]) if n >= 11 else 0.0
    ret_20d    = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0

    # RS vs SPY
    rs_vs_spy = 0.0
    if n >= 22:
        ret_21d   = float((close.iloc[-1] - close.iloc[-22]) / close.iloc[-22])
        rs_vs_spy = ret_21d - spy_ret_21d
    rs_5d_vs_spy = 0.0
    if n >= 6:
        ret_5d        = float((close.iloc[-1] - close.iloc[-6]) / close.iloc[-6])
        rs_5d_vs_spy  = ret_5d - spy_ret_5d

    # 量比
    vol_ratio = 1.0
    if len(volume) >= 20:
        v5  = float(volume.iloc[-5:].mean())
        v20 = float(volume.iloc[-20:].mean())
        if v20 > 0:
            vol_ratio = v5 / v20

    vol_1d20d = 1.0
    if len(volume) >= 21:
        denom = float(volume.iloc[-21:-1].mean())
        if denom > 0:
            vol_1d20d = float(volume.iloc[-1]) / denom

    # EMA 位置
    ema100_val   = float(_ema(close, 100).iloc[-1])
    ema_above200 = float((price - ema100_val) / ema100_val) if ema100_val > 0 else 0.0

    # ── FinTasticRS 新指標 ───────────────────────────────────────────────────
    slope_20d  = _linear_slope(close, 20)
    slope_60d  = _linear_slope(close, 60)
    slope_120d = _linear_slope(close, min(120, n)) if n >= 60  else 0.0
    slope_9m   = _linear_slope(close, min(189, n)) if n >= 120 else 0.0   # ← 9M 長線

    ud_60d = _ud_ratio(close, 60)

    # RTF 超額報酬各區間（等等在 compute_scores 扣掉 SPY 基準）
    ret_63d  = float((close.iloc[-1] - close.iloc[-64])  / close.iloc[-64])  if n >= 64  else 0.0
    ret_126d = float((close.iloc[-1] - close.iloc[-127]) / close.iloc[-127]) if n >= 127 else 0.0
    ret_189d = float((close.iloc[-1] - close.iloc[-190]) / close.iloc[-190]) if n >= 190 else 0.0
    ret_252d = float((close.iloc[-1] - close.iloc[-253]) / close.iloc[-253]) if n >= 253 else 0.0

    ema20_val = float(_ema(close, 20).iloc[-1])
    ema50_val = float(_ema(close, 50).iloc[-1])
    ema60_val = float(_ema(close, 60).iloc[-1])
    p_20ma    = price / ema20_val if ema20_val > 0 else 1.0
    p_60ma    = price / ema60_val if ema60_val > 0 else 1.0

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
        "rsi":          calculate_rsi(close),
        "breakout_score": _breakout_score(close, high, low, volume),
        # FinTasticRS 新欄位
        "slope_20d":    slope_20d,
        "slope_60d":    slope_60d,
        "slope_120d":   slope_120d,
        "slope_9m":     slope_9m,
        "ud_ratio_60d": ud_60d,
        "p_20ma":       p_20ma,
        "p_60ma":       p_60ma,
        "ret_63d":      ret_63d,
        "ret_126d":     ret_126d,
        "ret_189d":     ret_189d,
        "ret_252d":     ret_252d,
        "pass_technical": pass_technical,
        # 內部評分（前綴 _ 在輸出時會被移除）
        "_macd_score": _macd_score(close),
        "_ema_align":  _ema_alignment_score(close),
    }


# ── 全市場評分 ────────────────────────────────────────────────────────────────

def compute_scores(ticker_data: dict) -> pd.DataFrame:
    _cols  = ["ticker", "price", "ema_above200", "day_return", "vol_1d20d",
              "ret_10d", "ret_20d", "rs_vs_spy", "rs_5d_vs_spy",
              "vol_ratio", "rsi", "breakout_score",
              "slope_20d", "slope_60d", "slope_120d", "ud_ratio_60d",
              "p_20ma", "p_60ma",
              "rs_rating", "momentum_shift", "rs_trend", "rs_breakout",
              "pass_technical",
              "momentum_score"]
    _empty = pd.DataFrame(columns=_cols)

    if not ticker_data:
        return _empty

    # SPY 基準（短期 RS + IBD 四季超額報酬計算用）
    spy_ret_21d = spy_ret_5d = 0.0
    spy_ret_63d = spy_ret_126d = spy_ret_189d = spy_ret_252d = 0.0
    spy_df = ticker_data.get("SPY")
    if spy_df is not None and not spy_df.empty:
        spy_close = spy_df["Close"].dropna()
        n_spy = len(spy_close)
        if n_spy >= 22:
            spy_ret_21d  = float((spy_close.iloc[-1] - spy_close.iloc[-22])  / spy_close.iloc[-22])
        if n_spy >= 6:
            spy_ret_5d   = float((spy_close.iloc[-1] - spy_close.iloc[-6])   / spy_close.iloc[-6])
        if n_spy >= 64:
            spy_ret_63d  = float((spy_close.iloc[-1] - spy_close.iloc[-64])  / spy_close.iloc[-64])
        if n_spy >= 127:
            spy_ret_126d = float((spy_close.iloc[-1] - spy_close.iloc[-127]) / spy_close.iloc[-127])
        if n_spy >= 190:
            spy_ret_189d = float((spy_close.iloc[-1] - spy_close.iloc[-190]) / spy_close.iloc[-190])
        if n_spy >= 253:
            spy_ret_252d = float((spy_close.iloc[-1] - spy_close.iloc[-253]) / spy_close.iloc[-253])

    rows = [
        _metrics_for(t, df, spy_ret_21d, spy_ret_5d)
        for t, df in ticker_data.items()
        if t != "SPY"
    ]
    rows = [r for r in rows if r is not None]
    if not rows:
        return _empty

    df = pd.DataFrame(rows)

    # ── IBD 四季超額報酬（個股 - SPY，供前端 IBD 排名使用）─────────────────
    df["excess_63d"]  = df["ret_63d"]  - spy_ret_63d
    df["excess_126d"] = df["ret_126d"] - spy_ret_126d
    df["excess_189d"] = df["ret_189d"] - spy_ret_189d
    df["excess_252d"] = df["ret_252d"] - spy_ret_252d

    # ── 百分位排名（原有指標）────────────────────────────────────────────────
    df["_rs_rank"]    = df["rs_vs_spy"].rank(pct=True) * 100
    df["_rs5_rank"]   = df["rs_5d_vs_spy"].rank(pct=True) * 100
    df["_vol_rank"]   = df["vol_ratio"].rank(pct=True) * 100
    df["_vol1d_rank"] = df["vol_1d20d"].rank(pct=True) * 100
    df["_ret20_rank"] = df["ret_20d"].rank(pct=True) * 100
    df["_ret10_rank"] = df["ret_10d"].rank(pct=True) * 100
    df["_rsi_rank"]   = (100 - (df["rsi"] - 60).abs() * 3).clip(0, 100)
    # EPS beat placeholder（server.py 合入基本面後才有值）
    df["_eps_beat_rank"] = 50.0

    # ── FinTasticRS RS Rating（v3.1 純斜率版）──────────────────────────────────
    slope60_pct  = df["slope_60d"].rank(pct=True)
    slope120_pct = df["slope_120d"].rank(pct=True)
    slope20_pct  = df["slope_20d"].rank(pct=True)
    p20ma_pct    = df["p_20ma"].rank(pct=True)
    ud_pct       = df["ud_ratio_60d"].rank(pct=True)

    rs_raw = (
        slope60_pct  * 0.35 +
        slope120_pct * 0.25 +
        slope20_pct  * 0.25 +
        p20ma_pct    * 0.08 +
        ud_pct       * 0.07
    )
    rs_min, rs_max = rs_raw.min(), rs_raw.max()
    df["rs_rating"] = (
        ((rs_raw - rs_min) / (rs_max - rs_min) * 100).clip(0, 100)
        if rs_max > rs_min else pd.Series(50.0, index=df.index)
    )
    df["_rs_rating_rank"] = df["rs_rating"]  # 已是 0-100，純斜率趨勢強度

    # ── breakout_score 獨立排名（_WEIGHTS 的獨立管道）────────────────────────
    df["_breakout_rank"] = df["breakout_score"].rank(pct=True) * 100

    # ── Momentum Shift → RS 趨勢 ─────────────────────────────────────────────
    # momentum_shift = (S + 5 − M) / 2 + (M − L) + (L − XL)
    S_RS  = p20ma_pct    * 100
    M_RS  = slope20_pct  * 100
    L_RS  = slope60_pct  * 100
    XL_RS = slope120_pct * 100
    df["momentum_shift"] = (S_RS + 5 - M_RS) / 2 + (M_RS - L_RS) + (L_RS - XL_RS)
    df["rs_trend"] = df["momentum_shift"].apply(
        lambda x: "↑" if x > 5 else ("↓" if x < -5 else "→")
    )

    # rs_trend → 評分（↑=100, →=60, ↓=20）
    trend_map = {"↑": 100.0, "→": 60.0, "↓": 20.0}
    df["_rs_trend_score"] = df["rs_trend"].map(trend_map)

    # ── RS 翻強旗標 ──────────────────────────────────────────────────────────
    # 簡化版：RS > 70 + momentum_shift > 15 + 量比 ≥ 1.5
    df["rs_breakout"] = (
        (df["rs_rating"] > 70) &
        (df["momentum_shift"] > 15) &
        (df["vol_1d20d"] >= 1.5)
    )
    df["_rs_breakout_score"] = df["rs_breakout"].map({True: 100.0, False: 0.0})

    # ── 技術分 ───────────────────────────────────────────────────────────────
    score_cols = list(_WEIGHTS.keys())
    weights    = np.array(list(_WEIGHTS.values()))
    df["technical_score"] = (df[score_cols].values @ weights).clip(0, 100)
    df["momentum_score"]  = df["technical_score"]

    result = (
        df.drop(columns=[c for c in df.columns if c.startswith("_")])
        .sort_values("rs_rating", ascending=False)
        .head(TOP_N)
        .reset_index(drop=True)
    )
    result["rank"] = range(1, len(result) + 1)
    return result


# ── 突破觀察：全 universe 原始篩選（不受 RS 排名 / TOP_N 限制）──────────────

def compute_breakout_candidates(ticker_data: dict) -> pd.DataFrame:
    """
    掃描全 universe 所有股票，篩出反轉突破型候選。
    不套用 RS Rating 排名或 TOP_N 截斷，直接用原始技術指標過濾。

    篩選條件：
      slope_20d > 0       近期 20 日趨勢翻多
      slope_60d < 0.05    前期 60 日仍偏弱（反轉前期）
      breakout_score > 50 量價突破確認
    """
    rows = []
    for ticker, df in ticker_data.items():
        if ticker == "SPY" or df is None or df.empty:
            continue
        close = df["Close"].dropna()
        if len(close) < 60 or float(close.iloc[-1]) <= 0:
            continue

        high = df["High"].dropna() if "High" in df.columns else close
        low  = df["Low"].dropna()  if "Low"  in df.columns else close
        vol  = df["Volume"].dropna()

        s20 = _linear_slope(close, 20)
        s60 = _linear_slope(close, 60)
        bk  = _breakout_score(close, high, low, vol)

        if s20 <= 0 or s60 >= 0.05 or bk <= 50:
            continue

        n = len(close)
        price = float(close.iloc[-1])
        day_return = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) if n >= 2 else 0.0
        ret_20d    = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0
        rsi        = calculate_rsi(close)

        rows.append({
            "ticker":        ticker,
            "price":         price,
            "day_return":    day_return,
            "ret_20d":       ret_20d,
            "slope_20d":     s20,
            "slope_60d":     s60,
            "breakout_score": bk,
            "rsi":           rsi,
        })

    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .sort_values("breakout_score", ascending=False)
        .reset_index(drop=True)
    )


# ── 板塊加成後的最終評分 ──────────────────────────────────────────────────────

def apply_contextual_scoring(scores_df: pd.DataFrame) -> pd.DataFrame:
    """
    Stage 2 scoring：加入基本面與板塊加成。
    在 server.py 取得 PE/PEG/sector 後呼叫。
    """
    if scores_df is None or scores_df.empty:
        return scores_df

    df = scores_df.copy()
    if "technical_score" not in df.columns:
        df["technical_score"] = df["momentum_score"]

    df["_sector_score"] = df.get(
        "sector_above_ema50", pd.Series([None] * len(df), index=df.index)
    ).map(_score_sector)

    pe_score  = df.get("pe", pd.Series([None] * len(df), index=df.index)).map(_score_pe)
    peg_score = df.get("peg_ratio", pd.Series([None] * len(df), index=df.index)).map(_score_peg)
    df["_valuation_score"] = pe_score * 0.55 + peg_score * 0.45

    eps_score = df.get("eps_beat", pd.Series([None] * len(df), index=df.index)).map(_score_eps_beat)
    consec    = df.get(
        "eps_consecutive_beats", pd.Series([None] * len(df), index=df.index)
    ).map(lambda v: 100.0 if v is True else (40.0 if v is False else 50.0))
    df["_earnings_score"] = eps_score * 0.60 + consec * 0.40

    # P/E 排雷：P/E 負值或 >200 時將 momentum_score 歸零
    if "pe" in df.columns:
        bad_pe = df["pe"].apply(
            lambda v: (v is not None and not pd.isna(v) and
                       (float(v) <= 0 or float(v) > 200))
        )
        df.loc[bad_pe, "technical_score"] = 0.0

    df["momentum_score"] = (
        df["technical_score"] * _CONTEXT_WEIGHTS["technical_score"] +
        df["_sector_score"]    * _CONTEXT_WEIGHTS["_sector_score"] +
        df["_valuation_score"] * _CONTEXT_WEIGHTS["_valuation_score"] +
        df["_earnings_score"]  * _CONTEXT_WEIGHTS["_earnings_score"]
    ).clip(0, 100)

    # sector_multiplier 加成 → adjusted_score
    if "sector_multiplier" in df.columns:
        df["adjusted_score"] = (df["momentum_score"] * df["sector_multiplier"]).clip(0, 100)
    else:
        df["adjusted_score"] = df["momentum_score"]

    result = (
        df.drop(columns=[c for c in df.columns if c.startswith("_")])
        .sort_values("adjusted_score", ascending=False)
        .head(TOP_N)
        .reset_index(drop=True)
    )
    result["rank"] = range(1, len(result) + 1)
    return result
