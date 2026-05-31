"""
每日法人籌碼歷史快取

儲存格式（data/chips_history.json）：
{
  "data": {
    "6547": [
      {"date": "2026-05-28", "it_net": 500000.0, "fi_net": 1000000.0},
      ...   ← 按日期升序，最多保留 MAX_DAYS 筆
    ]
  }
}

只有 TPEX 股票有 it_net / fi_net；TWSE 目前 openAPI 不提供，保持 0。
"""

import json
import logging
import os
from datetime import date

logger = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chips_history.json")
MAX_DAYS = 30


def load_chips_history() -> dict[str, list]:
    """回傳 {code: [{"date":..., "it_net":..., "fi_net":...}, ...]}。"""
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("data", {})
    except Exception:
        return {}


def save_chips_history(history: dict[str, list]) -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": history}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("chips_history 寫入失敗：%s", e)


def update_chips_history(
    today_chips: dict[str, dict],
    today: str | None = None,
) -> dict[str, list]:
    """
    將今日籌碼合併進歷史快取並回傳更新後的快取。

    today_chips: {code: {"it_net": float, "fi_net": float}}
    today:       "YYYY-MM-DD"，省略時使用今天
    """
    if today is None:
        today = date.today().isoformat()

    history = load_chips_history()

    for code, chips in today_chips.items():
        entry = {
            "date":   today,
            "it_net": float(chips.get("it_net", 0.0)),
            "fi_net": float(chips.get("fi_net", 0.0)),
        }
        records = history.get(code, [])

        # 當日已存在就更新，否則追加
        if records and records[-1]["date"] == today:
            records[-1] = entry
        else:
            records.append(entry)

        # 只保留最近 MAX_DAYS 筆（按日期升序）
        history[code] = records[-MAX_DAYS:]

    save_chips_history(history)
    logger.info("chips_history 更新：%d 支股票，日期 %s", len(today_chips), today)
    return history


def compute_fi_consec_days(records: list) -> int:
    """
    計算外資連續買超（正）或連續賣超（負）天數。
    同 compute_it_consec_days，但使用 fi_net 欄位。
    """
    if not records:
        return 0
    sorted_records = sorted(records, key=lambda x: x["date"], reverse=True)
    latest_fi = sorted_records[0].get("fi_net", 0.0)
    if latest_fi == 0:
        return 0
    buying = latest_fi > 0
    count = 0
    for r in sorted_records:
        fi = r.get("fi_net", 0.0)
        if (fi > 0) == buying and fi != 0:
            count += 1
        else:
            break
    return count if buying else -count


def compute_it_consec_days(records: list) -> int:
    """
    計算投信連續買超（正）或連續賣超（負）天數。

    records: [{"date":..., "it_net":..., "fi_net":...}]，日期升序
    回傳值：
      +N → 連買 N 天
      -N → 連賣 N 天
       0 → 今日 it_net = 0 或無資料
    """
    if not records:
        return 0

    # 由最新往舊計算
    sorted_records = sorted(records, key=lambda x: x["date"], reverse=True)
    latest_it = sorted_records[0]["it_net"]

    if latest_it == 0:
        return 0

    buying = latest_it > 0
    count = 0
    for r in sorted_records:
        if (r["it_net"] > 0) == buying:
            count += 1
        else:
            break

    return count if buying else -count
