import logging
import warnings
from typing import Optional, Callable
import requests
import pandas as pd

from data.fetcher import fetch_all

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# ── TWSE openAPI（不需日期，永遠回傳最新交易日資料）────────────────────────
_TWSE_QUOTE  = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
_TWSE_MARGIN = "https://openapi.twse.com.tw/v1/marginTrading/MI_MARGN"
_TWSE_LIMIT  = "https://openapi.twse.com.tw/v1/exchangeReport/TWT84U"   # 今日漲跌停價
_TWSE_T86    = "https://www.twse.com.tw/rwd/zh/fund/T86"  # 三大法人個股（需帶日期）

# ── TPEX（SSL 憑證缺 SKI，用 verify=False 繞過）──────────────────────────
_TPEX_QUOTE  = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&o=json&se=EW"
_TPEX_CHIPS  = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json"
_TPEX_MARGIN = "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=json"


def _get(url: str, verify: bool = True) -> object:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS, verify=verify)
    r.raise_for_status()
    return r.json()


def _to_float(s, default: float = 0.0) -> float:
    try:
        return float(str(s).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return default


def _to_int(s, default: int = 0) -> int:
    try:
        return int(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def _find_col(fields: list[str], *keywords: str) -> int:
    for i, f in enumerate(fields):
        if all(k in f for k in keywords):
            return i
    raise KeyError(f"Column {keywords!r} not found in {fields}")


def _fetch_twse_limits() -> dict[str, dict]:
    """
    從 TWT84U 取得上市股票今日漲跌停價（精確值，由交易所計算）。
    回傳 {code: {"limit_up": float, "limit_down": float}}
    """
    try:
        data = _get(_TWSE_LIMIT)
        result: dict[str, dict] = {}
        for row in data:
            code = str(row.get("Code", "")).strip()
            lu   = _to_float(row.get("TodayLimitUp",   "0"))
            ld   = _to_float(row.get("TodayLimitDown",  "0"))
            if code and lu > 0:
                result[code] = {"limit_up": lu, "limit_down": ld}
        logger.info("TWSE limits (TWT84U): %d records", len(result))
        return result
    except Exception as exc:
        logger.warning("TWSE limits fetch failed: %s", exc)
        return {}


def _fetch_twse_t86() -> dict[str, dict]:
    """
    從 TWSE T86 取得上市股票三大法人買賣超（股數）。
    自動往前找最近 5 個交易日，避免假日/收盤前無資料的問題。
    回傳 {code: {"fi_net": float, "it_net": float}}
    """
    from datetime import date, timedelta
    for delta in range(5):
        d = (date.today() - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            url = (f"{_TWSE_T86}?response=json&date={d}"
                   "&selectType=ALLBUT0999")
            data = _get(url)
            rows = data.get("data", [])
            if not rows:
                continue
            result: dict[str, dict] = {}
            for row in rows:
                try:
                    code     = str(row[0]).strip()
                    fi_net   = _to_float(row[4])   # 外陸資買賣超股數
                    it_net   = _to_float(row[10])  # 投信買賣超股數
                    dealer   = _to_float(row[11])  # 自營商買賣超股數
                    inst_net = fi_net + it_net + dealer  # 三大法人合計
                    if code:
                        result[code] = {"fi_net": fi_net, "it_net": it_net,
                                        "dealer_net": dealer, "inst_net": inst_net}
                except (IndexError, Exception):
                    continue
            logger.info("TWSE T86 %s: %d records", d, len(result))
            return result
        except Exception as exc:
            logger.warning("TWSE T86 %s failed: %s", d, exc)
    logger.warning("TWSE T86: 近 5 日均無資料")
    return {}


def _detect_stock_type(code: str) -> str:
    """Classify a TWSE/TPEX code as stock / etf / bond / warrant / other."""
    c = code.strip()
    if not c:
        return "other"
    # Suffix-based: bonds end in B, warrants in W
    if c[-1].isalpha():
        if c[-1].upper() == "B":
            return "bond"
        if c[-1].upper() == "W":
            return "warrant"
        return "other"
    # All-numeric from here
    if c.startswith("00"):
        return "etf"
    if len(c) == 6 and c.isdigit():
        return "etf"
    if len(c) in (4, 5) and c.isdigit():
        return "stock"
    return "other"


# ── TWSE parsers（openAPI list 格式）─────────────────────────────────────

def _parse_twse_quote(data: list) -> dict[str, dict]:
    """
    STOCK_DAY_ALL 格式：
    [{ Code, Name, TradeVolume, TradeValue,
       OpeningPrice, HighestPrice, LowestPrice, ClosingPrice,
       Change, Transaction }, ...]
    Change 欄位已帶正負號（字串，如 "0.43", "-0.38"）
    """
    result: dict[str, dict] = {}
    for row in data:
        try:
            code    = str(row.get("Code", "")).strip()
            name    = str(row.get("Name", "")).strip()
            price   = _to_float(row.get("ClosingPrice"))
            high    = _to_float(row.get("HighestPrice"))
            low     = _to_float(row.get("LowestPrice"))
            vol_str = str(row.get("TradeVolume", "0")).replace(",", "")
            vol     = int(vol_str) // 1000 if vol_str.isdigit() else 0
            change  = _to_float(row.get("Change", "0"))
            prev    = price - change
            day_r   = (change / prev) if prev != 0 else 0.0
            trade_value = _to_float(str(row.get("TradeValue", "0")).replace(",", ""))
            turnover_10k = trade_value / 10_000  # 元 → 萬元
            if not code or price <= 0:
                continue
            result[code] = {
                "code": code, "name": name, "price": price,
                "volume": vol, "day_return": day_r, "pe": None,
                "high": high, "low": low,
                "turnover_10k": turnover_10k,
                "stock_type":   _detect_stock_type(code),
            }
        except Exception:
            continue
    return result


def _parse_twse_margin(data: list) -> dict[str, dict]:
    """
    MI_MARGN 格式：
    [{ 股票代號, 融資前日餘額, 融資今日餘額, ... }, ...]
    """
    result: dict[str, dict] = {}
    for row in data:
        try:
            code  = str(row.get("股票代號", "")).strip()
            today = _to_int(row.get("融資今日餘額"))
            prev  = _to_int(row.get("融資前日餘額"))
            if code:
                result[code] = {"margin_chg": today - prev}
        except Exception:
            continue
    return result


# ── TPEX parsers（原有格式，支援 aaData 與 tables 兩種結構）─────────────

def _parse_tpex_quote(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    # 新格式：tables list
    rows_src = None
    if "tables" in data:
        for t in data["tables"]:
            if "代號" in t.get("fields", []) or "代號" in str(t.get("fields", [])):
                rows_src = t.get("data", [])
                break
    if rows_src is None:
        rows_src = data.get("aaData", [])

    for row in rows_src:
        try:
            code  = str(row[0]).strip()
            name  = str(row[1]).strip()
            price = _to_float(row[2])
            diff  = _to_float(row[3])
            high  = _to_float(row[5]) if len(row) > 5 else 0.0
            low   = _to_float(row[6]) if len(row) > 6 else 0.0
            prev  = price - diff
            day_r = (diff / prev) if prev != 0 else 0.0
            # 成交股數在 index 7（成交金額在 index 8，不要用）
            vol = _to_int(row[7]) // 1000 if len(row) > 7 else 0
            if not code or price <= 0:
                continue
            # Approximate turnover: price × lots × 1000 shares / 10000 = price × lots / 10
            turnover_10k = price * vol / 10
            last_bid = _to_float(row[10]) if len(row) > 10 else None
            last_ask = _to_float(row[12]) if len(row) > 12 else None
            next_limit_up = _to_float(row[15]) if len(row) > 15 else None
            next_limit_down = _to_float(row[16]) if len(row) > 16 else None
            result[code] = {
                "code": code, "name": name, "price": price,
                "volume": vol, "day_return": day_r, "pe": None,
                "high": high, "low": low,
                "last_bid": last_bid, "last_ask": last_ask,
                "next_limit_up_price": next_limit_up if next_limit_up and next_limit_up < 9999 else None,
                "next_limit_down_price": next_limit_down if next_limit_down and next_limit_down > 0.01 else None,
                "turnover_10k": turnover_10k,
                "stock_type":   _detect_stock_type(code),
            }
        except (IndexError, ZeroDivisionError):
            continue
    return result


def _current_limit_flags(quote: dict) -> tuple[bool, bool, str]:
    """
    Infer today's limit-up/down state from exchange quote data exposed through
    tw-stock-agent. TWSE daily quote does not expose today's limit price after
    close, and TWT84U / TPEX limit fields describe the next trading day, so
    today's state is detected from current return and close-at-high/low status.
    """
    price = quote.get("price", 0.0) or 0.0
    day_return = quote.get("day_return", 0.0) or 0.0
    high = quote.get("high", 0.0) or 0.0
    low = quote.get("low", 0.0) or 0.0
    last_ask = quote.get("last_ask")
    last_bid = quote.get("last_bid")

    at_high = price > 0 and high > 0 and abs(price - high) <= 0.02
    at_low = price > 0 and low > 0 and abs(price - low) <= 0.02

    if day_return >= 0.095 and (at_high or last_ask == 0):
        basis = "tw-stock-agent:exchange_quote_current_limit"
        if last_ask == 0:
            basis = "tw-stock-agent:tpex_quote_best_ask_zero"
        return True, False, basis
    if day_return <= -0.095 and (at_low or last_bid == 0):
        basis = "tw-stock-agent:exchange_quote_current_limit"
        if last_bid == 0:
            basis = "tw-stock-agent:tpex_quote_best_bid_zero"
        return False, True, basis
    return False, False, "unavailable"


def _parse_tpex_chips(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    rows_src = data.get("aaData", [])
    if not rows_src and "tables" in data:
        for t in data["tables"]:
            rows_src = t.get("data", [])
            if rows_src:
                break
    for row in rows_src:
        try:
            code     = str(row[0]).strip()
            fi_net   = _to_float(row[4])
            it_net   = _to_float(row[10]) if len(row) > 10 else 0.0  # [10]=淨買，[8]=買進
            inst_net = _to_float(row[23]) if len(row) > 23 else fi_net + it_net  # 三大法人合計
            if code:
                result[code] = {"fi_net": fi_net, "it_net": it_net, "inst_net": inst_net}
        except IndexError:
            continue
    return result


def _parse_tpex_margin(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    rows_src = data.get("aaData", [])
    if not rows_src and "tables" in data:
        for t in data["tables"]:
            rows_src = t.get("data", [])
            if rows_src:
                break
    for row in rows_src:
        try:
            code  = str(row[0]).strip()
            today = _to_int(row[6])
            prev  = _to_int(row[2])
            if code:
                result[code] = {"margin_chg": today - prev}
        except IndexError:
            continue
    return result


def _fetch_tw_ohlcv(
    tickers: list[str],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Optional[pd.DataFrame]]:
    return fetch_all(tickers, progress_callback=progress_callback)


def fetch_tw_all(
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, dict]:
    fetched: dict[str, dict] = {}

    # TWSE — openAPI（verify=True，憑證正常）
    for key, url, parser, verify in [
        ("twse_quote",  _TWSE_QUOTE,  _parse_twse_quote,  True),
        ("twse_margin", _TWSE_MARGIN, _parse_twse_margin, True),
    ]:
        try:
            fetched[key] = parser(_get(url, verify=verify))
            logger.info("TW fetch %s: %d records", key, len(fetched[key]))
        except Exception as exc:
            logger.warning("TW fetch %s failed: %s", key, exc)
            fetched[key] = {}

    # TPEX — verify=False 繞過 SSL 憑證問題
    for key, url, parser in [
        ("tpex_quote",  _TPEX_QUOTE,  _parse_tpex_quote),
        ("tpex_chips",  _TPEX_CHIPS,  _parse_tpex_chips),
        ("tpex_margin", _TPEX_MARGIN, _parse_tpex_margin),
    ]:
        try:
            fetched[key] = parser(_get(url, verify=False))
            logger.info("TW fetch %s: %d records", key, len(fetched[key]))
        except Exception as exc:
            logger.warning("TW fetch %s failed: %s", key, exc)
            fetched[key] = {}

    twse_quote  = fetched["twse_quote"]
    twse_margin = fetched["twse_margin"]
    tpex_quote  = fetched["tpex_quote"]
    tpex_chips  = fetched["tpex_chips"]
    tpex_margin = fetched["tpex_margin"]

    # ── TWSE 三大法人（T86）+ 漲跌停價（TWT84U）────────────────────────
    twse_fi     = _fetch_twse_t86()
    twse_limits = _fetch_twse_limits()

    # ── 前置過濾：只對「普通股 + 成交金額 ≥ 500 萬」下載 OHLCV ──────────
    # 可排除 ETF/債券/權證（~400 檔）與極度無流動性標的，
    # 把 OHLCV 下載量從 ~2300 壓到 ~1200，速度提升約 50%。
    _MIN_TURNOVER = 2000  # 萬元（硬性過濾門檻 3000 萬的 2/3，保留安全邊際）
    twse_tickers = [
        f"{c}.TW" for c, q in twse_quote.items()
        if q.get("stock_type") == "stock" and q.get("turnover_10k", 0) >= _MIN_TURNOVER
    ]
    tpex_tickers = [
        f"{c}.TWO" for c, q in tpex_quote.items()
        if q.get("stock_type") == "stock" and q.get("turnover_10k", 0) >= _MIN_TURNOVER
    ]
    logger.info(
        "OHLCV 前置過濾：TWSE %d → %d，TPEX %d → %d",
        len(twse_quote), len(twse_tickers),
        len(tpex_quote), len(tpex_tickers),
    )
    ohlcv = _fetch_tw_ohlcv(twse_tickers + tpex_tickers, progress_callback)

    # ── 更新籌碼歷史快取（TWSE T86 + TPEX chips 合併）──────────────────
    from data.chips_cache import update_chips_history, compute_it_consec_days, compute_fi_consec_days
    all_chips = {**twse_fi, **tpex_chips}   # TPEX 同名時覆蓋 TWSE（不重疊）
    chips_history = update_chips_history(all_chips)

    result: dict[str, dict] = {}
    for code, quote in twse_quote.items():
        yft    = f"{code}.TW"
        fi     = twse_fi.get(code, {"fi_net": 0.0, "it_net": 0.0})
        margin = twse_margin.get(code, {"margin_chg": 0})
        it_consec = compute_it_consec_days(chips_history.get(code, []))
        fi_consec = compute_fi_consec_days(chips_history.get(code, []))
        # 今日漲跌停：由 tw-stock-agent 交易所行情資料判斷。
        # TWT84U 價格為次日參考價，保留顯示但不作今日 limit 判斷。
        lim   = twse_limits.get(code, {})
        lu    = lim.get("limit_up",   0.0)
        ld    = lim.get("limit_down", 0.0)
        is_lu, is_ld, limit_basis = _current_limit_flags(quote)
        result[yft] = {
            **quote,
            "fi_net":         fi["fi_net"],
            "it_net":         fi["it_net"],
            "inst_net":       fi.get("inst_net", fi["fi_net"] + fi["it_net"]),
            "it_consec_days": it_consec,
            "fi_consec_days": fi_consec,
            "is_limit_up":    is_lu,
            "is_limit_down":  is_ld,
            "limit_up_price": lu if lu > 0 else None,
            "limit_down_price": ld if ld > 0 else None,
            "limit_basis":    limit_basis,
            **margin,
            "ohlcv": ohlcv.get(yft),
        }
    for code, quote in tpex_quote.items():
        yft    = f"{code}.TWO"
        chips  = tpex_chips.get(code,  {"fi_net": 0.0, "it_net": 0.0})
        margin = tpex_margin.get(code, {"margin_chg": 0})
        it_consec = compute_it_consec_days(chips_history.get(code, []))
        fi_consec = compute_fi_consec_days(chips_history.get(code, []))
        inst  = chips.get("inst_net", chips.get("fi_net", 0.0) + chips.get("it_net", 0.0))
        is_lu, is_ld, limit_basis = _current_limit_flags(quote)
        result[yft] = {
            **quote, **chips, **margin,
            "inst_net":       inst,
            "it_consec_days": it_consec,
            "fi_consec_days": fi_consec,
            "is_limit_up":    is_lu,
            "is_limit_down":  is_ld,
            "limit_up_price": quote.get("next_limit_up_price"),
            "limit_down_price": quote.get("next_limit_down_price"),
            "limit_basis":    limit_basis,
            "ohlcv": ohlcv.get(yft),
        }

    return result
