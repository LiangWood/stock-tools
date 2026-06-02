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

    # 補充基本面：從 data/fund_cache.json 載入（不受每日 TTL 限制）
    try:
        from server import _apply_fund_dict, _FUNDAMENTAL_COLUMNS, SECTOR_ZH
        fund_cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "data", "fund_cache.json")
        fund_cache = {}
        if os.path.exists(fund_cache_path):
            with open(fund_cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            fund_cache = cached.get("fund", {})
            logger.info("基本面快取載入：%d 檔（日期：%s）", len(fund_cache), cached.get("date"))

        if fund_cache:
            scores_df = _apply_fund_dict(scores_df, fund_cache)
            scores_df = apply_contextual_scoring(scores_df)
            logger.info("基本面補充完成：pe/peg/sector_zh 已套用")
        else:
            logger.warning("無基本面快取（data/fund_cache.json），pe/peg/sector 欄位為空")
    except Exception as e:
        logger.warning("基本面補充失敗（不影響主資料）：%s", e)

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
    from scoring.tw_engine import (
        compute_tw_scores, compute_tw_rs_scores, compute_tw_breakout_candidates
    )

    logger.info("Fetching TW stocks…")
    tw_raw = fetch_tw_all()

    logger.info("Computing TW chips scores…")
    tw_chips_df = compute_tw_scores(tw_raw)

    logger.info("Computing TW RS scores…")
    tw_rs_df = compute_tw_rs_scores(tw_raw)

    logger.info("Computing TW breakout candidates…")
    tw_bk_df = compute_tw_breakout_candidates(tw_raw)

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
    logger.info("台股完成：籌碼 %d 檔，RS %d 檔，突破 %d 檔",
                len(tw_chips_df), len(tw_rs_df), len(tw_bk_df))
except Exception as e:
    logger.error("台股資料抓取失敗：%s", e)
    save("tw_scores",    {"universe": "tw", "scores": [], "last_updated": now, "count": 0})
    save("tw_rs_scores", {"scores": [], "last_updated": now})
    save("tw_breakout",  {"candidates": [], "last_updated": now})

# ── meta ──────────────────────────────────────────────────────────────────────
save("meta", {"last_updated": now, "generated_at": datetime.utcnow().isoformat() + "Z"})
logger.info("=== 全部完成：%s ===", now)
