"""
台股官方產業別對照表（補充 scoring/tw_engine.py 主題式板塊清單的 fallback）。

主題式板塊清單（_TW_SECTOR_GROUPS）只手動收錄熱門概念股，未收錄的個股會顯示
「未分類」。這裡改用證交所／櫃買中心官方公告的標準產業別分類（約 33 類，
涵蓋幾乎全部上市櫃個股），當主題式清單查無結果時當作 fallback 顯示。

資料來源（皆為公開 OpenAPI，無需驗證）：
  - 上市：https://openapi.twse.com.tw/v1/opendata/t187ap03_L
  - 上櫃：https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O

磁碟快取於 data/tw_industry_cache.json，產業別分類極少變動，快取 30 天即可。
"""

import json
import logging
import os
import warnings
from datetime import date

import requests

logger = logging.getLogger(__name__)

_TIMEOUT  = 15
_HEADERS  = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

_TWSE_INDUSTRY = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
_TPEX_INDUSTRY = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

_CACHE_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tw_industry_cache.json")
_CACHE_MAX_DAYS = 30

# 證交所／櫃買中心標準產業別代碼 → 中文名稱
INDUSTRY_NAMES = {
    "01": "水泥工業",       "02": "食品工業",       "03": "塑膠工業",
    "04": "紡織纖維",       "05": "電機機械",       "06": "電器電纜",
    "08": "玻璃陶瓷",       "09": "造紙工業",       "10": "鋼鐵工業",
    "11": "橡膠工業",       "12": "汽車工業",       "14": "建材營造業",
    "15": "航運業",         "16": "觀光餐旅",       "17": "金融保險業",
    "18": "貿易百貨業",     "20": "其他業",         "21": "化學工業",
    "22": "生技醫療業",     "23": "油電燃氣業",     "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業",       "27": "通信網路業",
    "28": "電子零組件業",   "29": "電子通路業",     "30": "資訊服務業",
    "31": "其他電子業",     "32": "文化創意業",     "33": "農業科技業",
    "34": "電子商務業",     "35": "綠能環保業",     "36": "數位雲端",
    "37": "運動休閒業",     "38": "居家生活業",
    "80": "公司債",         "91": "存託憑證",
}

_industry_map: dict[str, str] | None = None


def _get(url: str) -> object:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
    r.raise_for_status()
    return r.json()


def _fetch_remote() -> dict[str, str]:
    """回傳 {股票代號: 官方產業別中文名稱}，合併上市（TWSE）與上櫃（TPEx）。"""
    result: dict[str, str] = {}

    try:
        for row in _get(_TWSE_INDUSTRY):
            code = str(row.get("公司代號", "")).strip()
            name = INDUSTRY_NAMES.get(str(row.get("產業別", "")).strip())
            if code and name:
                result[code] = name
    except Exception as exc:
        logger.warning("TWSE 產業別 fetch failed: %s", exc)

    try:
        for row in _get(_TPEX_INDUSTRY):
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            name = INDUSTRY_NAMES.get(str(row.get("SecuritiesIndustryCode", "")).strip())
            if code and name:
                result[code] = name
    except Exception as exc:
        logger.warning("TPEx 產業別 fetch failed: %s", exc)

    logger.info("台股官方產業別：合併 %d 檔", len(result))
    return result


def _load_cache() -> tuple[str | None, dict[str, str]]:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("date"), data.get("map", {})
    except Exception:
        return None, {}


def _save_cache(date_str: str, map_: dict[str, str]) -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": date_str, "map": map_}, f, ensure_ascii=False)
    except Exception as exc:
        logger.warning("產業別快取寫入失敗：%s", exc)


def _cache_age_days(date_str: str | None) -> int:
    if not date_str:
        return _CACHE_MAX_DAYS + 1
    try:
        return (date.today() - date.fromisoformat(date_str)).days
    except ValueError:
        return _CACHE_MAX_DAYS + 1


def get_industry_map() -> dict[str, str]:
    """回傳 {股票代號: 官方產業別中文名稱}（純讀取磁碟快取，不發出網路請求）。"""
    global _industry_map
    if _industry_map is None:
        _, cached_map = _load_cache()
        _industry_map = cached_map
    return _industry_map


def refresh_industry_map_if_stale() -> dict[str, str]:
    """快取超過 _CACHE_MAX_DAYS 天或不存在時，向 TWSE / TPEx 重新抓取並落盤。

    供資料更新流程（_fetch_worker）呼叫；單純查詢請用 get_industry_map()。
    """
    global _industry_map
    cached_date, cached_map = _load_cache()
    if _cache_age_days(cached_date) <= _CACHE_MAX_DAYS and cached_map:
        _industry_map = cached_map
        return _industry_map

    fresh = _fetch_remote()
    if fresh:
        _industry_map = fresh
        _save_cache(date.today().isoformat(), fresh)
    else:
        _industry_map = cached_map  # 抓取失敗就沿用舊快取（即使過期）

    return _industry_map
