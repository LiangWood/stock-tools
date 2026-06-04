"""
台股波段選股篩選器
==================
架構說明：
  Layer 1 - 前置硬性過濾 (Hard Filters)：不合格直接排除，不進入評分
  Layer 2 - 加權評分    (Weighted Scoring)：通過過濾的標的進行百分位排名後加權
  Layer 3 - 輸出        (Output)：排序後取前 N 名，附帶排名理由

設計原則：
  - 外資/投信籌碼為主 (50%)，技術動能為輔 (35%)，融資輔助確認 (15%)
  - RSI 作為硬性過濾器，不計入加權（避免追高）
  - 使用百分位排名 (percentile rank) 而非原始數值，確保各指標尺度一致
"""

import json
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path


# ──────────────────────────────────────────────
# 1. 載入設定檔
# ──────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────
# 2. 前置硬性過濾 (Layer 1)
# ──────────────────────────────────────────────

def apply_hard_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """
    硬性條件：任何一條不過就直接剔除。
    這一層的目的是移除 ETF、小型無流動性股票、RSI 過熱標的。

    參數
    ----
    df      : 原始資料，每列為一支股票
    filters : 來自 config.json 的 hard_filters 區塊

    回傳
    ----
    通過所有硬性條件的子集 DataFrame
    """
    original_count = len(df)
    reasons = {}

    # --- 只保留普通股（排除 ETF、ETN、存託憑證、特別股）---
    if "stock_type" in df.columns:
        mask = df["stock_type"] == filters["stock_type"]
        reasons["stock_type"] = original_count - mask.sum()
        df = df[mask]

    # --- 市值下限（單位：億元）---
    if "market_cap_b" in df.columns:
        mask = df["market_cap_b"] >= filters["min_market_cap_b"]
        reasons["market_cap"] = len(df) - mask.sum()
        df = df[mask]

    # --- 當日成交金額下限（單位：萬元）---
    if "turnover_10k" in df.columns:
        mask = df["turnover_10k"] >= filters["min_turnover_10k"]
        reasons["turnover"] = len(df) - mask.sum()
        df = df[mask]

    # --- 當日成交量下限（單位：張）---
    if "volume" in df.columns and "min_volume" in filters:
        mask = df["volume"] >= filters["min_volume"]
        reasons["volume"] = len(df) - mask.sum()
        df = df[mask]

    # --- RSI 區間過濾（不追高、不買弱勢）---
    if "rsi" in df.columns and "rsi_range" in filters:
        rsi_cfg = filters["rsi_range"]
        mask = (df["rsi"] >= rsi_cfg["min"]) & (df["rsi"] <= rsi_cfg["max"])
        reasons["rsi"] = len(df) - mask.sum()
        df = df[mask]

    # --- 融資注意：融資減少但股價當日必須是正報酬（避免選到弱勢被殺融資）---
    # 只有在兩個欄位都存在時才做這個複合過濾
    if "margin_chg" in df.columns and "day_return" in df.columns:
        if filters.get("require_positive_return_on_margin_decrease", True):
            # 邏輯：融資增加的不管（不在這裡過濾）；融資減少的，確認當日報酬是正的
            # 換句話說：剔除「融資減少 AND 當日報酬 < 0」的標的（弱勢被殺倉）
            bad_mask = (df["margin_chg"] < 0) & (df["day_return"] < 0)
            reasons["margin_direction"] = bad_mask.sum()
            df = df[~bad_mask]

    print(f"[過濾結果] 原始 {original_count} 支 → 通過 {len(df)} 支")
    for k, v in reasons.items():
        if v > 0:
            print(f"  - 因 {k} 剔除：{v} 支")

    return df.copy()


# ──────────────────────────────────────────────
# 3. 百分位排名轉換
# ──────────────────────────────────────────────

def percentile_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """
    將原始數值轉換為 0～100 的百分位排名。

    為什麼要用百分位排名而不是原始數值？
    因為各指標的尺度完全不同：
      - fi_net 可能是數十億元
      - rsi 是 0～100 的數字
      - amount_ratio 是 1.0 附近的倍數
    直接加權會讓大數字的指標主導一切。百分位化之後，所有指標都在同一個尺度。

    ascending=True  → 原始值越大，百分位越高（越高越好的指標）
    ascending=False → 原始值越小，百分位越高（越低越好的反向指標，例如融資增加）
    """
    if ascending:
        return series.rank(pct=True) * 100
    else:
        return (1 - series.rank(pct=True)) * 100


# ──────────────────────────────────────────────
# 4. 加權評分 (Layer 2)
# ──────────────────────────────────────────────

def compute_scores(df: pd.DataFrame, scoring: dict) -> pd.DataFrame:
    """
    對每個指標計算百分位排名，然後按權重加總，得出最終分數。

    scoring 格式（來自 config.json）：
    {
      "fi_net":         { "weight": 0.30, "ascending": true  },
      "it_consec_days": { "weight": 0.20, "ascending": true  },
      ...
    }
    """
    df = df.copy()
    total_score = pd.Series(0.0, index=df.index)
    score_details = {}

    for col, cfg in scoring.items():
        if col not in df.columns:
            print(f"  [警告] 欄位 '{col}' 不存在，跳過")
            continue

        weight = cfg["weight"]
        ascending = cfg.get("ascending", True)

        # 有 NaN 的欄位先填中位數（避免遺漏資料影響整體排名）
        filled = df[col].fillna(df[col].median())

        # 百分位排名
        prank = percentile_rank(filled, ascending=ascending)

        # 加權累計
        total_score += prank * weight
        score_details[f"{col}_prank"] = prank

        # 順便儲存各指標的百分位分數，方便後續解釋用
        df[f"_pr_{col}"] = prank.round(1)

    df["score"] = total_score.round(2)
    return df


# ──────────────────────────────────────────────
# 5. 產生排名理由（可讀性解釋）
# ──────────────────────────────────────────────

def generate_reason(row: pd.Series, scoring: dict) -> str:
    """
    對每支股票，找出貢獻最高的前 3 個指標，輸出人類可讀的理由。
    這讓你在看清單時能快速判斷這支股票是「外資主導」還是「投信連買主導」。
    """
    label_map = {
        "fi_net":         "外資買超",
        "it_net":         "投信買超金額",
        "it_consec_days": "投信連買天數",
        "margin_chg":     "融資健康",
        "day_return":     "當日強勢",
        "ret_20d":        "20日動能",
        "amount_ratio":   "爆量突破",
        "macd_hist":      "MACD動能",
        "vol_accel":      "量能加速",
        "volume":         "全市場成交量排名",
        "rsi_trend":      "RSI動能方向",
        "rsi_divergence": "RSI背離",
        "breakout_score": "突破確認",
    }

    contributions = {}
    for col, cfg in scoring.items():
        pr_col = f"_pr_{col}"
        if pr_col in row.index:
            contributions[col] = row[pr_col] * cfg["weight"]

    # 依貢獻度排序，取前 3 名
    top3 = sorted(contributions.items(), key=lambda x: x[1], reverse=True)[:3]
    parts = [f"{label_map.get(k, k)}({v:.0f}分)" for k, v in top3]
    return "、".join(parts)


# ──────────────────────────────────────────────
# 6. 主流程 (Layer 3)
# ──────────────────────────────────────────────

def run_screener(df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
    """
    完整執行三層篩選流程，回傳前 N 名候選清單。

    使用方式：
        df_raw = pd.read_csv("your_data.csv")   # 你的每日資料
        result = run_screener(df_raw)
        print(result)
    """
    if config is None:
        config = load_config()

    print(f"\n{'='*50}")
    print(f"台股波段選股篩選器  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")

    # Layer 1：硬性過濾
    print("\n[Layer 1] 硬性過濾中...")
    df_filtered = apply_hard_filters(df, config["hard_filters"])

    if len(df_filtered) == 0:
        print("⚠️  過濾後無任何股票，請檢查資料或放寬條件")
        return pd.DataFrame()

    # Layer 2：加權評分
    print("\n[Layer 2] 加權評分中...")
    df_scored = compute_scores(df_filtered, config["scoring"])

    # Layer 3：排序輸出
    top_n = config.get("output", {}).get("top_n", 20)
    df_result = df_scored.sort_values("score", ascending=False).head(top_n).copy()

    # 加入排名序號
    df_result.insert(0, "rank", range(1, len(df_result) + 1))

    # 加入排名理由
    df_result["reason"] = df_result.apply(
        lambda row: generate_reason(row, config["scoring"]), axis=1
    )

    # 只輸出使用者關心的欄位（去掉中間計算欄位）
    output_cols = config.get("output", {}).get("columns", [])
    final_cols = ["rank", "stock_id", "stock_name", "score", "reason"] + [
        c for c in output_cols if c in df_result.columns
    ]
    final_cols = [c for c in final_cols if c in df_result.columns]

    print(f"\n[結果] 前 {top_n} 名候選清單：")
    print(df_result[final_cols].to_string(index=False))

    return df_result[final_cols]


# ──────────────────────────────────────────────
# 7. 範例入口
# ──────────────────────────────────────────────

if __name__ == "__main__":
    """
    這裡示範如何用假資料測試整個流程。
    實際使用時，將 df_sample 換成你從 API 或資料庫取得的真實 DataFrame。

    必要欄位說明：
      stock_id       : 股票代號（字串，如 "2330"）
      stock_name     : 股票名稱
      stock_type     : 類型（"stock" / "etf" / "bond" 等）
      market_cap_b   : 市值（億元）
      turnover_10k   : 當日成交金額（萬元）
      fi_net         : 外資買賣超（元）
      it_net         : 投信買賣超（元）
      it_consec_days : 投信連續買超天數（正數=連買, 負數=連賣）
      margin_chg     : 融資餘額變化（張）
      day_return     : 當日漲跌幅（%）
      ret_20d        : 近 20 日漲跌幅（%）
      amount_ratio   : 今日成交金額 / 20日均量
      rsi            : RSI(14)
    """
    import numpy as np

    np.random.seed(42)
    n = 200  # 模擬 200 支股票

    df_sample = pd.DataFrame({
        "stock_id":       [f"{2000+i}" for i in range(n)],
        "stock_name":     [f"測試股{i}" for i in range(n)],
        # 約 80% 是普通股，20% 是 ETF（模擬真實比例）
        "stock_type":     np.random.choice(["stock", "etf"], n, p=[0.8, 0.2]),
        "market_cap_b":   np.random.lognormal(4, 1.2, n),        # 億元，對數常態分佈
        "turnover_10k":   np.random.lognormal(8, 1.5, n),        # 萬元
        "fi_net":         np.random.normal(0, 5e8, n),           # 元，有正有負
        "it_net":         np.random.normal(0, 1e8, n),           # 元
        "it_consec_days": np.random.randint(-10, 15, n),         # 天數
        "margin_chg":     np.random.normal(0, 500, n),           # 張
        "day_return":     np.random.normal(0.5, 2.5, n),         # %
        "ret_20d":        np.random.normal(3, 10, n),            # %
        "amount_ratio":   np.random.lognormal(0.1, 0.5, n),      # 倍數
        "rsi":            np.random.uniform(30, 85, n),          # RSI
    })

    result = run_screener(df_sample)
