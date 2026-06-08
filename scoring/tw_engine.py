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

from scoring.engine import calculate_rsi, _linear_slope, _breakout_score
from scoring.screener import apply_hard_filters, compute_scores, generate_reason, load_config
from data.chips_cache import load_chips_history

_CONFIG_PATH = Path(__file__).parent / "config.json"
_SECTOR_FLOW_WEIGHT = 0.15

_TW_SECTOR_GROUPS = {
    "AI伺服器": ["2317", "6669", "2376", "2382", "3231", "4938", "2356", "3706", "2353", "3017", "8050", "7711"],
    "散熱": ["3324", "3017", "8261", "6276", "2421", "6230", "3653", "3338"],
    "PCB載板": ["3037", "8046", "3189", "6213", "3149", "8213", "2367", "3044", "6201", "6155", "6141", "6224", "3294", "3021"],
    "半導體設備": ["3680", "3131", "8027", "3563", "6191", "4749", "6196", "6139", "6223", "3413"],
    "晶圓代工": ["2330", "2303", "5347", "6770", "3707", "3035", "6515"],
    "封測": ["3711", "2449", "6147", "8150", "3680", "2369", "2329", "6271", "6435", "6205"],
    "記憶體": ["2408", "5269", "5388", "2344", "3260", "2451", "3661", "9102", "5289", "4967", "8271", "8299"],
    "高速光通訊": ["3081", "4979", "8121", "4906", "6177", "3450", "6243", "6588", "6207"],
    "IC設計": ["2454", "2379", "3034", "3014", "3443", "6533", "3035", "5269", "5274", "3529"],
    "被動元件": ["2327", "2492", "2375", "2438", "6112", "2456", "3533", "6147", "6285", "3094"],
    "電源儲能": ["2308", "6412", "3015", "3017", "3211", "1519", "6431", "4931", "5227", "6121"],
    "電器電纜": ["1604", "1605", "1614", "1503", "1519", "1513", "1612", "1609"],
    "連接器": ["2392", "3533", "6177", "3023", "2313", "3030", "6271", "5383", "6422"],
    "面板": ["2409", "3481", "6116", "3149", "3504", "2406", "3009", "8069"],
    "金融": ["2881", "2882", "2891", "2880", "2884", "2885", "2886", "2887", "2890", "2892", "5880", "5876"],
    "航運": ["2603", "2609", "2615", "2637", "2605", "2618", "2606", "5608", "2617", "2208"],
    "工業自動化": ["2049", "4526", "3563", "1597", "2059", "3017", "1589", "1590", "2395"],
    "太陽能風電": ["6443", "3576", "3691", "5227", "6806", "1519", "1503", "6753", "9958"],
}

_STOCK_TO_SECTOR = {
    code: sector
    for sector, codes in _TW_SECTOR_GROUPS.items()
    for code in codes
}


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


def _percentile_scores(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    s = pd.Series(values, dtype=float)
    return (s.rank(pct=True, na_option="bottom") * 100).to_dict()


def _sector_flow_status(net_5d_yi: float, accel_yi: float) -> str:
    if net_5d_yi > 0 and accel_yi > 0:
        return "主力"
    if net_5d_yi > 0 and accel_yi <= 0:
        return "輪動"
    if net_5d_yi <= 0 and net_5d_yi > -0.5:
        return "觀望"
    return "退潮"


def compute_tw_sector_rotation(ticker_data: dict, history: dict | None = None) -> dict:
    """
    板塊資金輪動分數。

    X 軸概念：近 5 日法人資金淨流入。
    Y 軸概念：近 5 日平均流入速度 - 前 5 日平均流入速度。
    泡泡大小概念：近 20 日法人累計資金。
    """
    history = history if history is not None else load_chips_history()
    sector_daily: dict[str, dict[str, float]] = {}
    sector_stocks: dict[str, list[dict]] = {}

    for ticker, d in ticker_data.items():
        if not d:
            continue
        code = ticker.split(".")[0]
        sector = _STOCK_TO_SECTOR.get(code)
        if not sector:
            continue
        price = float(d.get("price") or 0.0)
        if price <= 0:
            continue
        today_net_shares = float(d.get("fi_net", 0.0) or 0.0) + float(d.get("it_net", 0.0) or 0.0)
        today_net_yi = today_net_shares * price / 1e8
        day_return = d.get("day_return")
        ret_5d = None
        ohlcv = d.get("ohlcv")
        if ohlcv is not None and not ohlcv.empty and "Close" in ohlcv:
            close = ohlcv["Close"].dropna()
            if len(close) >= 6 and float(close.iloc[-6]) > 0:
                ret_5d = float((close.iloc[-1] - close.iloc[-6]) / close.iloc[-6])
        sector_stocks.setdefault(sector, []).append({
            "ticker": ticker,
            "stock_id": code,
            "name": d.get("name", ""),
            "price": price,
            "day_return": float(day_return) if day_return is not None else None,
            "ret_5d": ret_5d,
            "net_1d_yi": today_net_yi,
            "fi_net": float(d.get("fi_net", 0.0) or 0.0),
            "it_net": float(d.get("it_net", 0.0) or 0.0),
        })
        records = history.get(code) or []
        if not records:
            records = [{
                "date": "_today",
                "fi_net": d.get("fi_net", 0.0),
                "it_net": d.get("it_net", 0.0),
            }]
        for record in records[-20:]:
            date = str(record.get("date", ""))
            net_shares = float(record.get("fi_net", 0.0) or 0.0) + float(record.get("it_net", 0.0) or 0.0)
            net_yi = net_shares * price / 1e8
            sector_daily.setdefault(sector, {})
            sector_daily[sector][date] = sector_daily[sector].get(date, 0.0) + net_yi

    raw = {}
    for sector, by_date in sector_daily.items():
        values = [v for _, v in sorted(by_date.items())][-20:]
        if not values:
            continue
        last5 = values[-5:]
        prev5 = values[-10:-5]
        net_5d_yi = float(sum(last5))
        net_20d_yi = float(sum(values))
        accel_yi = float(sum(last5) / max(len(last5), 1) - (sum(prev5) / len(prev5) if prev5 else 0.0))
        stocks = sorted(sector_stocks.get(sector, []), key=lambda s: s.get("net_1d_yi") or 0.0, reverse=True)
        ret_5d_values = [s["ret_5d"] for s in stocks if s.get("ret_5d") is not None]
        raw[sector] = {
            "sector_theme": sector,
            "sector_flow_status": _sector_flow_status(net_5d_yi, accel_yi),
            "sector_net_1d_yi": float(sum(s.get("net_1d_yi") or 0.0 for s in stocks)),
            "sector_net_5d_yi": net_5d_yi,
            "sector_net_20d_yi": net_20d_yi,
            "sector_accel_yi": accel_yi,
            "sector_ret_5d": float(np.mean(ret_5d_values)) if ret_5d_values else None,
            "stocks": stocks,
        }

    if not raw:
        return {}

    net5_rank = _percentile_scores({k: v["sector_net_5d_yi"] for k, v in raw.items()})
    net20_rank = _percentile_scores({k: v["sector_net_20d_yi"] for k, v in raw.items()})
    accel_rank = _percentile_scores({k: v["sector_accel_yi"] for k, v in raw.items()})
    status_base = {"主力": 90.0, "輪動": 72.0, "觀望": 52.0, "退潮": 25.0}

    for sector, row in raw.items():
        flow_score = (
            status_base[row["sector_flow_status"]] * 0.35 +
            net5_rank.get(sector, 50.0) * 0.30 +
            accel_rank.get(sector, 50.0) * 0.25 +
            net20_rank.get(sector, 50.0) * 0.10
        )
        row["sector_flow_score"] = round(float(np.clip(flow_score, 0, 100)), 1)
    return raw


def _attach_sector_rotation(df: pd.DataFrame, ticker_data: dict) -> pd.DataFrame:
    sector_stats = compute_tw_sector_rotation(ticker_data)
    df = df.copy()

    def _sector(code: str) -> str | None:
        return _STOCK_TO_SECTOR.get(str(code).split(".")[0])

    df["sector_theme"] = df["ticker"].map(_sector)
    df["sector_flow_status"] = df["sector_theme"].map(lambda s: sector_stats.get(s, {}).get("sector_flow_status") if s else None)
    df["sector_flow_score"] = df["sector_theme"].map(lambda s: sector_stats.get(s, {}).get("sector_flow_score") if s else None)
    df["sector_net_1d_yi"] = df["sector_theme"].map(lambda s: sector_stats.get(s, {}).get("sector_net_1d_yi") if s else None)
    df["sector_net_5d_yi"] = df["sector_theme"].map(lambda s: sector_stats.get(s, {}).get("sector_net_5d_yi") if s else None)
    df["sector_net_20d_yi"] = df["sector_theme"].map(lambda s: sector_stats.get(s, {}).get("sector_net_20d_yi") if s else None)
    df["sector_accel_yi"] = df["sector_theme"].map(lambda s: sector_stats.get(s, {}).get("sector_accel_yi") if s else None)
    df["sector_ret_5d"] = df["sector_theme"].map(lambda s: sector_stats.get(s, {}).get("sector_ret_5d") if s else None)
    df["sector_theme"] = df["sector_theme"].fillna("未分類")
    df["sector_flow_status"] = df["sector_flow_status"].fillna("未分類")
    df["sector_flow_score"] = df["sector_flow_score"].fillna(50.0)
    return df


def compute_tw_rs_scores(ticker_data: dict) -> pd.DataFrame:
    """
    台股 RS Score（Minervini 方法）
    RS Score = Q1×50% + Q2×25% + Q3×15% + Q4×10%

    Q1-Q4 各為 63 個交易日區間的漲幅，在全市場所有股票中百分位排名後加權。
    需要 1y OHLCV 資料（twse_fetcher 已改為 period='1y'）。
    """
    records = []
    for ticker, d in ticker_data.items():
        if d is None:
            continue
        ohlcv = d.get("ohlcv")
        if ohlcv is None or ohlcv.empty:
            continue
        close = ohlcv["Close"].dropna()
        n = len(close)
        if n < 63 or float(close.iloc[-1]) <= 0:
            continue

        # 四個季度漲幅（各 ~63 個交易日）
        q1 = float((close.iloc[-1]   - close.iloc[-64])  / close.iloc[-64])  if n >= 64  else None
        q2 = float((close.iloc[-64]  - close.iloc[-127]) / close.iloc[-127]) if n >= 127 else None
        q3 = float((close.iloc[-127] - close.iloc[-190]) / close.iloc[-190]) if n >= 190 else None
        q4 = float((close.iloc[-190] - close.iloc[-253]) / close.iloc[-253]) if n >= 253 else None

        day_return = d.get("day_return")
        if day_return is None:
            day_return = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) if n >= 2 else 0.0
        ret_20d    = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0

        records.append({
            "ticker":      ticker,
            "stock_id":    ticker.split(".")[0],
            "name":        d.get("name", ""),
            "price":       d.get("price", float(close.iloc[-1])),
            "day_return":  day_return,
            "ret_20d":     ret_20d,
            "volume":      d.get("volume", 0),
            "is_limit_up": d.get("is_limit_up", False),
            "is_limit_down": d.get("is_limit_down", False),
            "q1_ret": q1 if q1 is not None else 0.0,
            "q2_ret": q2 if q2 is not None else 0.0,
            "q3_ret": q3 if q3 is not None else 0.0,
            "q4_ret": q4 if q4 is not None else 0.0,
            "_q1_ok": q1 is not None,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # ETF / 低流動性過濾（只留 stock 類型 + 成交量 > 0）
    df = df[df["volume"] > 0].copy()
    if df.empty:
        return pd.DataFrame()

    # 全市場百分位排名（0-99）
    df["_q1_rank"] = df["q1_ret"].rank(pct=True) * 99
    df["_q2_rank"] = df["q2_ret"].rank(pct=True) * 99
    df["_q3_rank"] = df["q3_ret"].rank(pct=True) * 99
    df["_q4_rank"] = df["q4_ret"].rank(pct=True) * 99

    # RS Score（Minervini 加權）
    df["rs_score"] = (
        df["_q1_rank"] * 0.50 +
        df["_q2_rank"] * 0.25 +
        df["_q3_rank"] * 0.15 +
        df["_q4_rank"] * 0.10
    ).clip(0, 99).round(1)

    # 保留四季度漲幅供顯示
    df.rename(columns={"q1_ret": "q1_pct", "q2_ret": "q2_pct",
                        "q3_ret": "q3_pct", "q4_ret": "q4_pct"}, inplace=True)

    result = (
        df.drop(columns=[c for c in df.columns if c.startswith("_")])
        .sort_values("rs_score", ascending=False)
        .head(100)
        .reset_index(drop=True)
    )
    result["rank"] = range(1, len(result) + 1)
    return result


def compute_tw_observation_candidates(ticker_data: dict) -> tuple:
    """
    單次全市場掃描，同時產出突破觀察 + 起漲觀察（避免重複計算）。

    Returns:
        (breakout_df, early_stage_df)
    """
    raw_records = []
    for ticker, d in ticker_data.items():
        if d is None:
            continue
        ohlcv = d.get("ohlcv")
        if ohlcv is None or ohlcv.empty:
            continue
        close  = ohlcv["Close"].dropna()
        volume = ohlcv["Volume"].dropna() if "Volume" in ohlcv else close
        high   = ohlcv["High"].dropna()  if "High"  in ohlcv else close
        low    = ohlcv["Low"].dropna()   if "Low"   in ohlcv else close
        n = len(close)

        if n < 63 or float(close.iloc[-1]) <= 0:
            continue
        if d.get("volume", 0) <= 0:
            continue
        if d.get("stock_type", "stock") != "stock":
            continue

        s20 = _linear_slope(close, 20)
        s60 = _linear_slope(close, 60)
        # 只計算 slope 有翻多跡象的才繼續（提前 early exit）
        if s20 <= 0 or s60 >= 0.05:
            continue

        bk  = _breakout_score(close, high, low, volume)
        va  = _vol_accel(volume)
        rsi = calculate_rsi(close)

        # Q1-Q4 漲幅（供起漲觀察 RS Score 計算）
        q1 = float((close.iloc[-1]   - close.iloc[-64])  / close.iloc[-64])  if n >= 64  else None
        q2 = float((close.iloc[-64]  - close.iloc[-127]) / close.iloc[-127]) if n >= 127 else None
        q3 = float((close.iloc[-127] - close.iloc[-190]) / close.iloc[-190]) if n >= 190 else None
        q4 = float((close.iloc[-190] - close.iloc[-253]) / close.iloc[-253]) if n >= 253 else None

        day_return = d.get("day_return")
        if day_return is None:
            day_return = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) if n >= 2 else 0.0
        ret_20d = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0

        raw_records.append({
            "ticker":          ticker,
            "stock_id":        ticker.split(".")[0],
            "name":            d.get("name", ""),
            "price":           d.get("price", float(close.iloc[-1])),
            "day_return":      day_return,
            "ret_20d":         ret_20d,
            "q1_ret":          q1,
            "q2_ret":          q2,
            "q3_ret":          q3,
            "q4_ret":          q4,
            "slope_20d":       s20,
            "slope_60d":       s60,
            "breakout_score":  bk,
            "vol_accel":       va,
            "rsi":             rsi,
            "volume":          d.get("volume", 0),
            "fi_net":          d.get("fi_net", 0.0),
            "it_consec_days":  d.get("it_consec_days", 0),
            "is_limit_up":     d.get("is_limit_up", False),
        })

    if not raw_records:
        return pd.DataFrame(), pd.DataFrame()

    df = pd.DataFrame(raw_records)

    # ── 突破觀察：breakout > 50 ───────────────────────────────────────────────
    bk_df = (
        df[df["breakout_score"] > 50]
        [["ticker", "stock_id", "name", "price", "day_return", "ret_20d",
          "slope_20d", "slope_60d", "breakout_score", "rsi", "is_limit_up"]]
        .sort_values("breakout_score", ascending=False)
        .reset_index(drop=True)
    )

    # ── 起漲觀察：計算 RS Score 後篩選 ───────────────────────────────────────
    for col in ["q1_ret", "q2_ret", "q3_ret", "q4_ret"]:
        df[f"_{col}_rank"] = df[col].rank(pct=True, na_option="bottom") * 99
    df["rs_score"] = (
        df["_q1_ret_rank"] * 0.50 +
        df["_q2_ret_rank"] * 0.25 +
        df["_q3_ret_rank"] * 0.15 +
        df["_q4_ret_rank"] * 0.10
    )

    early_mask = (
        (df["breakout_score"] >= 15) & (df["breakout_score"] < 50) &
        (df["rs_score"] >= 30) & (df["rs_score"] <= 80) &
        (df["vol_accel"] > 0.9) &
        ((df["fi_net"] > 0) | (df["it_consec_days"] >= 3))
    )
    early = df[early_mask].copy()
    if not early.empty:
        early["early_score"] = (
            early["rs_score"]       * 0.40 +
            early["breakout_score"] * 0.30 +
            early["it_consec_days"].apply(lambda x: 30 if x >= 3 else 0) +
            early["fi_net"].apply(lambda x: 20 if x > 0 else 0)
        )
        early_df = (
            early[["ticker", "stock_id", "name", "price", "day_return",
                   "volume", "rs_score", "breakout_score", "slope_20d", "slope_60d",
                   "fi_net", "it_consec_days", "is_limit_up", "early_score"]]
            .sort_values("early_score", ascending=False)
            .head(50)
            .reset_index(drop=True)
        )
    else:
        early_df = pd.DataFrame()

    return bk_df, early_df


def compute_tw_breakout_candidates(ticker_data: dict) -> pd.DataFrame:
    """突破觀察（向後相容入口，實際由 compute_tw_observation_candidates 計算）"""
    bk_df, _ = compute_tw_observation_candidates(ticker_data)
    return bk_df


def compute_tw_early_stage_candidates(ticker_data: dict) -> pd.DataFrame:
    """起漲觀察（向後相容入口，實際由 compute_tw_observation_candidates 計算）"""
    _, early_df = compute_tw_observation_candidates(ticker_data)
    return early_df


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
    df_scored = _attach_sector_rotation(df_scored, ticker_data)
    df_scored["score"] = (
        df_scored["score"] * (1 - _SECTOR_FLOW_WEIGHT) +
        df_scored["sector_flow_score"].astype(float) * _SECTOR_FLOW_WEIGHT
    ).clip(0, 100)

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
