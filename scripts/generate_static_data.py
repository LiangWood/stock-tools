#!/usr/bin/env python3
"""
GitHub Pages 靜態資料生成器
執行後產生 web/data/*.json，供前端直接讀取（不需後端）。
"""
import json, math, os, sys, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from datetime import datetime

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "data")
os.makedirs(OUT_DIR, exist_ok=True)


def clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def to_records(df):
    if df is None or df.empty:
        return []
    return [{k: clean(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def save(name: str, data: dict):
    path = os.path.join(OUT_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    logger.info("Saved %s (%d bytes)", name, os.path.getsize(path))


now = datetime.now().strftime("%Y-%m-%d %H:%M")

# ── 美股 ──────────────────────────────────────────────────────────────────────
logger.info("=== 美股資料抓取開始 ===")
try:
    from data.universe import get_combined_tickers
    from data.fetcher import fetch_all
    from scoring.engine import (
        compute_scores, apply_contextual_scoring, compute_breakout_candidates
    )

    tickers = get_combined_tickers()
    if "SPY" not in tickers:
        tickers = ["SPY"] + tickers

    logger.info("Fetching %d US tickers OHLCV…", len(tickers))
    raw = fetch_all(tickers)

    logger.info("Computing US scores…")
    scores_df = compute_scores(raw)
    breakout_df = compute_breakout_candidates(raw)

    # ── 從 fund_cache.json 補充 pe / peg_ratio / sector_zh（不受 TTL 限制）──
    fund_cache_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "fund_cache.json"
    )
    if os.path.exists(fund_cache_path):
        with open(fund_cache_path, "r", encoding="utf-8") as _fc:
            _cached = json.load(_fc)
        _fund = _cached.get("fund", {})
        logger.info("基本面快取：%d 檔（日期 %s）", len(_fund), _cached.get("date", "?"))
        if _fund:
            from server import _apply_fund_dict, _compute_sector_metrics
            scores_df = _apply_fund_dict(scores_df, _fund)
            scores_df = _compute_sector_metrics(scores_df)   # ← sector_rank 在這裡算
            scores_df = apply_contextual_scoring(scores_df)
            logger.info("pe/peg/sector_zh/sector_rank 補充完成")
    else:
        logger.warning("data/fund_cache.json 不存在，pe/peg/sector_zh 欄位為空")

    save("us_scores", {
        "universe": "all",
        "scores": to_records(scores_df),
        "last_updated": now,
        "count": len(scores_df),
    })
    save("us_breakout", {
        "candidates": to_records(breakout_df),
        "last_updated": now,
    })
    logger.info("美股完成：%d 檔，突破 %d 檔", len(scores_df), len(breakout_df))
except Exception as e:
    logger.error("美股資料抓取失敗：%s", e)
    save("us_scores",  {"universe": "all", "scores": [], "last_updated": now, "count": 0})
    save("us_breakout", {"candidates": [], "last_updated": now})

# ── 台股 ──────────────────────────────────────────────────────────────────────
logger.info("=== 台股資料抓取開始 ===")
try:
    from data.twse_fetcher import fetch_tw_all
    from data.tw_industry import refresh_industry_map_if_stale
    from scoring.tw_engine import (
        compute_tw_scores, compute_tw_rs_scores,
        compute_tw_observation_candidates,
    )

    logger.info("Fetching TW stocks…")
    tw_raw = fetch_tw_all()

    logger.info("Refreshing official industry classification cache…")
    refresh_industry_map_if_stale()

    logger.info("Computing TW chips scores…")
    tw_chips_df = compute_tw_scores(tw_raw)

    logger.info("Computing TW RS scores…")
    tw_rs_df = compute_tw_rs_scores(tw_raw)

    logger.info("Computing TW observation candidates (breakout + early-stage)…")
    tw_bk_df, tw_early_df = compute_tw_observation_candidates(tw_raw)

    save("tw_scores", {
        "universe": "tw",
        "scores": to_records(tw_chips_df),
        "last_updated": now,
        "count": len(tw_chips_df),
    })
    save("tw_rs_scores", {
        "scores": to_records(tw_rs_df),
        "last_updated": now,
    })
    save("tw_breakout", {
        "candidates": to_records(tw_bk_df),
        "last_updated": now,
    })
    save("tw_early_stage", {
        "candidates": to_records(tw_early_df),
        "last_updated": now,
    })
    logger.info("台股完成：籌碼 %d 檔，RS %d 檔，突破 %d 檔，起漲 %d 檔",
                len(tw_chips_df), len(tw_rs_df), len(tw_bk_df), len(tw_early_df))
except Exception as e:
    logger.error("台股資料抓取失敗：%s", e)
    save("tw_scores",      {"universe": "tw", "scores": [], "last_updated": now, "count": 0})
    save("tw_rs_scores",   {"scores": [], "last_updated": now})
    save("tw_breakout",    {"candidates": [], "last_updated": now})
    save("tw_early_stage", {"candidates": [], "last_updated": now})

# ── 大盤指數（靜態模式 header 用）────────────────────────────────────────────
logger.info("=== 指數資料抓取 ===")
try:
    import yfinance as yf
    from server import _fetch_tw_futures, _fetch_vixtwn

    indices = []
    df_twii = yf.Ticker("^TWII").history(period="2d", auto_adjust=True, raise_errors=False)
    if df_twii is not None and not df_twii.empty:
        last = float(df_twii["Close"].iloc[-1])
        prev = float(df_twii["Close"].iloc[-2]) if len(df_twii) >= 2 else last
        chg = last - prev
        chg_pct = chg / prev * 100 if prev else 0.0
        indices.append({"name": "加權指數", "price": round(last, 0),
                        "change": round(chg, 2), "change_pct": round(chg_pct, 2)})
    fut = _fetch_tw_futures()
    if fut:
        indices.append({"name": "台指期", **fut})
    vix = _fetch_vixtwn()
    if vix:
        indices.append({"name": "恐慌指數", **vix})
    save("indices", {"indices": indices, "last_updated": now})
    logger.info("指數資料完成：%d 項", len(indices))
except Exception as e:
    logger.warning("指數資料抓取失敗：%s", e)
    save("indices", {"indices": [], "last_updated": now})

# ── meta ──────────────────────────────────────────────────────────────────────
save("meta", {"last_updated": now, "generated_at": datetime.utcnow().isoformat() + "Z"})
logger.info("=== 全部完成：%s ===", now)
