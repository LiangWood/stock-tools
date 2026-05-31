"""
台股波段選股引擎

三層架構（定義於 config.json + screener.py）：
  Layer 1 — 硬性過濾：ETF、低流動性、RSI 過熱/弱勢、弱勢殺融資
  Layer 2 — 加權評分：法人籌碼(50%) + 技術動能(35%) + 融資(15%)
  Layer 3 — 輸出 Top-N，附排名理由
"""

from pathlib import Path

import numpy as np
import pandas as pd

from scoring.engine import calculate_rsi
from scoring.screener import apply_hard_filters, compute_scores, generate_reason, load_config

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _macd_hist(close: pd.Series) -> float:
    """MACD 柱狀圖值（MACD線 - 訊號線），以收盤價百分比表示，便於跨股票比較。"""
    if len(close) < 26:
        return 0.0
    macd_line   = _ema(close, 12) - _ema(close, 26)
    signal_line = _ema(macd_line, 9)
    hist        = float((macd_line - signal_line).iloc[-1])
    price       = float(close.iloc[-1])
    return (hist / price * 100) if price > 0 else 0.0


def _vol_accel(volume: pd.Series) -> float:
    """量能加速度：近5日均量 / 近20日均量。
    > 1.2 代表近期量能明顯放大（加速），< 0.8 代表量縮。"""
    n = len(volume)
    if n < 20:
        return 1.0
    v5  = float(volume.iloc[-5:].mean())
    v20 = float(volume.iloc[-20:].mean())
    return (v5 / v20) if v20 > 0 else 1.0


def _tw_breakout_score(
    close: pd.Series,
    high: pd.Series,
    volume: pd.Series,
) -> float:
    """
    突破確認評分（0–100）：捕捉「剛起漲」型態。
    無需法人籌碼配合，純粹從量價行為判斷是否正在發動。

      量能爆發    40%  今日量 / 近20日均量，放量越大越確認
      突破前高    35%  收盤 vs 前20日最高（排除今日），站上 = 突破
      當日強漲    25%  今日漲幅越大越強（搭配量才有效）
    """
    n = len(close)
    if n < 21 or len(volume) < 21 or len(high) < 21:
        return 0.0

    # 今日量能爆發
    vol_20d   = float(volume.iloc[-21:-1].mean())
    vol_today = float(volume.iloc[-1])
    vol_surge = vol_today / vol_20d if vol_20d > 0 else 1.0
    surge_score = float(np.clip((vol_surge - 1.0) * 50, 0, 100))  # 2x=50, 3x=100

    # 突破前20日最高點（排除今日，偵測真正的新高突破）
    prior_high    = float(high.iloc[-21:-1].max())
    px_ratio      = float(close.iloc[-1]) / prior_high if prior_high > 0 else 0.0
    break_score   = float(np.clip((px_ratio - 0.95) * 1200, 0, 100))

    # 當日強漲
    day_ret = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) if n >= 2 else 0.0
    day_score = float(np.clip(day_ret * 100 * 15, 0, 100))  # +1%=15, +5%=75, +7%=100

    return surge_score * 0.40 + break_score * 0.35 + day_score * 0.25


def _rsi_trend(close: pd.Series, period: int = 14) -> float:
    """
    RSI 5日趨勢：RSI(今日) - RSI(5天前)。
    正值 → RSI 在上升（動能加強，符合 RSI 50 中線突破精神）
    負值 → RSI 在下降（動能減弱）
    """
    n = len(close)
    if n < period + 6:
        return 0.0
    rsi_now = calculate_rsi(close, period)
    rsi_5d  = calculate_rsi(close.iloc[:-5], period)
    return float(rsi_now - rsi_5d)


def _rsi_divergence(close: pd.Series, period: int = 14, lookback: int = 20) -> float:
    """
    簡化版背離偵測（0–100 分）：

    看漲背離（bullish divergence）：
      過去 lookback 天內股價更低，但當前 RSI 卻更高 → 80 分
      → 跌勢力道減弱，可能醞釀反彈（逢低布局訊號）

    看跌背離（bearish divergence）：
      過去 lookback 天內股價更高，但當前 RSI 卻更低 → 20 分
      → 漲勢力道減弱，注意追高風險

    無背離 → 50 分（中性）
    """
    n = len(close)
    if n < period + lookback + 3:
        return 50.0

    current_price = float(close.iloc[-1])
    current_rsi   = calculate_rsi(close, period)
    past          = close.iloc[-lookback - 1:-1]

    # ── 看漲背離：股價更低，但 RSI 更高 ──────────────────────────
    past_low_pos   = int(past.argmin())
    past_low_price = float(past.iloc[past_low_pos])
    if current_price <= past_low_price * 1.05:   # 目前價格在前低附近或以下
        end = n - lookback - 1 + past_low_pos + 1
        if end >= period + 1:
            rsi_at_low = calculate_rsi(close.iloc[:end], period)
            if current_rsi > rsi_at_low + 5:      # RSI 卻明顯更高 → 看漲背離
                return 80.0

    # ── 看跌背離：股價更高，但 RSI 更低 ──────────────────────────
    past_high_pos   = int(past.argmax())
    past_high_price = float(past.iloc[past_high_pos])
    if current_price >= past_high_price * 0.95:  # 目前價格在前高附近或以上
        end = n - lookback - 1 + past_high_pos + 1
        if end >= period + 1:
            rsi_at_high = calculate_rsi(close.iloc[:end], period)
            if current_rsi < rsi_at_high - 5:     # RSI 卻明顯更低 → 看跌背離
                return 20.0

    return 50.0


def _tech_metrics(ohlcv: pd.DataFrame | None) -> dict:
    defaults = {"ret_10d": 0.0, "ret_20d": 0.0, "amount_ratio": 1.0, "rsi": 50.0,
                "macd_hist": 0.0, "vol_accel": 1.0, "rsi_trend": 0.0,
                "rsi_divergence": 50.0, "breakout_score": 0.0}
    if ohlcv is None or len(ohlcv) < 2:
        return defaults
    ohlcv_clean = ohlcv[["Close", "Volume"]].dropna()
    close  = ohlcv_clean["Close"]
    volume = ohlcv_clean["Volume"]
    high   = ohlcv["High"].dropna() if "High" in ohlcv.columns else close
    n = len(close)
    if n < 2:
        return defaults

    ret_10d = float((close.iloc[-1] - close.iloc[-11]) / close.iloc[-11]) if n >= 11 else 0.0
    ret_20d = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0

    amount = close * volume
    amt_avg = float(amount.iloc[-21:-1].mean()) if n >= 21 else float(amount.mean())
    amount_ratio = float(amount.iloc[-1] / amt_avg) if amt_avg > 0 else 1.0

    rsi      = calculate_rsi(close)
    macd_h   = _macd_hist(close)
    vol_acc  = _vol_accel(volume)
    rsi_tr   = _rsi_trend(close)
    rsi_div  = _rsi_divergence(close)
    brk      = _tw_breakout_score(close, high, volume)

    return {
        "ret_10d":        ret_10d,
        "ret_20d":        ret_20d,
        "amount_ratio":   amount_ratio,
        "rsi":            rsi,
        "macd_hist":      macd_h,
        "vol_accel":      vol_acc,
        "rsi_trend":      rsi_tr,
        "rsi_divergence": rsi_div,
        "breakout_score": brk,
    }


def compute_tw_scores(ticker_data: dict) -> pd.DataFrame:
    config = load_config(_CONFIG_PATH)

    rows = []
    for ticker, d in ticker_data.items():
        if d is None:
            continue
        tech   = _tech_metrics(d.get("ohlcv"))
        it_net = d.get("it_net", 0.0)

        rows.append({
            # ── 識別欄位 ────────────────────────────────────────────
            "ticker":         ticker,
            "stock_id":       ticker.split(".")[0],
            "stock_name":     d.get("name", ""),
            "name":           d.get("name", ""),
            # ── 硬性過濾所需欄位 ─────────────────────────────────────
            "stock_type":     d.get("stock_type", "stock"),
            "turnover_10k":   d.get("turnover_10k", 0.0),
            # ── 籌碼 ─────────────────────────────────────────────────
            "fi_net":         d.get("fi_net", 0.0),
            "it_net":         it_net,
            "inst_net":       d.get("inst_net", 0.0),
            # 連續買超天數：只有今日資料，用買/賣方向作為近似值
            "it_consec_days": d.get("it_consec_days", 0),
            "fi_consec_days": d.get("fi_consec_days", 0),
            "margin_chg":     d.get("margin_chg", 0),
            # ── 技術面 ───────────────────────────────────────────────
            "day_return":     d.get("day_return", 0.0),
            "ret_10d":        tech["ret_10d"],
            "ret_20d":        tech["ret_20d"],
            "amount_ratio":   tech["amount_ratio"],
            "rsi":            tech["rsi"],
            "macd_hist":      tech["macd_hist"],
            "vol_accel":      tech["vol_accel"],
            "rsi_trend":      tech["rsi_trend"],
            "rsi_divergence": tech["rsi_divergence"],
            "breakout_score": tech["breakout_score"],
            # ── UI 顯示欄位（不進入評分） ───────────────────────────
            "price":          d.get("price", 0.0),
            "volume":         d.get("volume", 0),
            "pe":             d.get("pe"),
            "is_limit_up":    d.get("is_limit_up",   False),
            "is_limit_down":  d.get("is_limit_down",  False),
            "limit_up_price": d.get("limit_up_price"),
            "limit_down_price": d.get("limit_down_price"),
            "limit_basis":    d.get("limit_basis", "unavailable"),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # ── Layer 1：硬性過濾 ────────────────────────────────────────────
    df_filtered = apply_hard_filters(df, config["hard_filters"])
    if df_filtered.empty:
        return pd.DataFrame()

    # ── Layer 2：加權評分 ────────────────────────────────────────────
    df_scored = compute_scores(df_filtered, config["scoring"])

    # ── Layer 3：Top-N 輸出 ──────────────────────────────────────────
    top_n = config.get("output", {}).get("top_n", 20)
    result = (
        df_scored
        .sort_values("score", ascending=False)
        .head(top_n)
        .copy()
        .reset_index(drop=True)
    )
    result.insert(0, "rank", range(1, len(result) + 1))
    result["reason"] = result.apply(
        lambda row: generate_reason(row, config["scoring"]), axis=1
    )
    result.rename(columns={"score": "tw_score"}, inplace=True)

    return result
