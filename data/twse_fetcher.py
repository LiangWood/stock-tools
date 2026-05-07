import logging
from typing import Optional, Callable
import requests
import pandas as pd

from data.fetcher import fetch_all

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0"}

_TWSE_QUOTE  = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&type=ALLBUT0999"
_TWSE_CHIPS  = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALL"
_TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=ALL"
_TPEX_QUOTE  = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&o=json&se=EW"
_TPEX_CHIPS  = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json"
_TPEX_MARGIN = "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=json"


def _get(url: str) -> dict:
    r = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
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


def _parse_twse_quote(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if data.get("stat") != "OK" or not data.get("data"):
        return result
    fields = data.get("fields", [])
    try:
        ci  = _find_col(fields, "收盤")
        si  = _find_col(fields, "成交股數")
        di  = _find_col(fields, "漲跌價差")
        sgi = _find_col(fields, "漲跌(+/-)")
        pei = _find_col(fields, "本益比")
    except KeyError as e:
        logger.warning("TWSE quote field error: %s", e)
        return result

    for row in data["data"]:
        try:
            code  = str(row[0]).strip()
            name  = str(row[1]).strip()
            price = _to_float(row[ci])
            vol   = _to_int(row[si]) // 1000
            diff  = _to_float(row[di])
            sign  = 1 if "+" in str(row[sgi]) else -1
            prev  = price - sign * diff
            day_r = (sign * diff / prev) if prev != 0 else 0.0
            pe_s  = str(row[pei]).strip()
            pe    = _to_float(pe_s) if pe_s not in ("-", "--", "") else None
            result[code] = {
                "code": code, "name": name, "price": price,
                "volume": vol, "day_return": day_r, "pe": pe,
            }
        except (IndexError, ZeroDivisionError):
            continue
    return result


def _parse_twse_chips(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if data.get("stat") != "OK" or not data.get("data"):
        return result
    fields = data.get("fields", [])
    try:
        fi_i = next(
            i for i, f in enumerate(fields)
            if "外陸資買賣超股數" in f and "不含" not in f and "投信" not in f
        )
        it_i = _find_col(fields, "投信買賣超股數")
    except (StopIteration, KeyError) as e:
        logger.warning("TWSE chips field error: %s", e)
        return result

    for row in data["data"]:
        try:
            code   = str(row[0]).strip()
            fi_net = _to_float(row[fi_i])
            it_net = _to_float(row[it_i])
            result[code] = {"fi_net": fi_net, "it_net": it_net}
        except IndexError:
            continue
    return result


def _parse_twse_margin(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if data.get("stat") != "OK" or not data.get("data"):
        return result
    fields = data.get("fields", [])
    try:
        today_i = _find_col(fields, "融資今日餘額")
        prev_i  = _find_col(fields, "融資", "前日餘額")
    except KeyError as e:
        logger.warning("TWSE margin field error: %s", e)
        return result

    for row in data["data"]:
        try:
            code = str(row[0]).strip()
            chg  = _to_int(row[today_i]) - _to_int(row[prev_i])
            result[code] = {"margin_chg": chg}
        except IndexError:
            continue
    return result


def _parse_tpex_quote(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in data.get("aaData", []):
        try:
            code  = str(row[0]).strip()
            name  = str(row[1]).strip()
            price = _to_float(row[2])
            diff  = _to_float(row[3])
            prev  = price - diff
            day_r = (diff / prev) if prev != 0 else 0.0
            vol   = _to_int(row[8]) if len(row) > 8 else 0
            result[code] = {
                "code": code, "name": name, "price": price,
                "volume": vol, "day_return": day_r, "pe": None,
            }
        except (IndexError, ZeroDivisionError):
            continue
    return result


def _parse_tpex_chips(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in data.get("aaData", []):
        try:
            code   = str(row[0]).strip()
            fi_net = _to_float(row[4])
            it_net = _to_float(row[8]) if len(row) > 8 else 0.0
            result[code] = {"fi_net": fi_net, "it_net": it_net}
        except IndexError:
            continue
    return result


def _parse_tpex_margin(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in data.get("aaData", []):
        try:
            code  = str(row[0]).strip()
            today = _to_int(row[6])
            prev  = _to_int(row[2])
            result[code] = {"margin_chg": today - prev}
        except IndexError:
            continue
    return result


def _fetch_tw_ohlcv(
    tickers: list[str],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Optional[pd.DataFrame]]:
    return fetch_all(tickers, progress_callback)


def fetch_tw_all(
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, dict]:
    fetched: dict[str, dict] = {}
    for key, url, parser in [
        ("twse_quote",  _TWSE_QUOTE,  _parse_twse_quote),
        ("twse_chips",  _TWSE_CHIPS,  _parse_twse_chips),
        ("twse_margin", _TWSE_MARGIN, _parse_twse_margin),
        ("tpex_quote",  _TPEX_QUOTE,  _parse_tpex_quote),
        ("tpex_chips",  _TPEX_CHIPS,  _parse_tpex_chips),
        ("tpex_margin", _TPEX_MARGIN, _parse_tpex_margin),
    ]:
        try:
            fetched[key] = parser(_get(url))
        except Exception as exc:
            logger.warning("TW fetch %s failed: %s", key, exc)
            fetched[key] = {}

    twse_quote  = fetched["twse_quote"]
    twse_chips  = fetched["twse_chips"]
    twse_margin = fetched["twse_margin"]
    tpex_quote  = fetched["tpex_quote"]
    tpex_chips  = fetched["tpex_chips"]
    tpex_margin = fetched["tpex_margin"]

    twse_tickers = [f"{c}.TW"  for c in twse_quote]
    tpex_tickers = [f"{c}.TWO" for c in tpex_quote]
    ohlcv = _fetch_tw_ohlcv(twse_tickers + tpex_tickers, progress_callback)

    result: dict[str, dict] = {}
    for code, quote in twse_quote.items():
        yft    = f"{code}.TW"
        chips  = twse_chips.get(code,  {"fi_net": 0.0, "it_net": 0.0})
        margin = twse_margin.get(code, {"margin_chg": 0})
        result[yft] = {**quote, **chips, **margin, "ohlcv": ohlcv.get(yft)}
    for code, quote in tpex_quote.items():
        yft    = f"{code}.TWO"
        chips  = tpex_chips.get(code,  {"fi_net": 0.0, "it_net": 0.0})
        margin = tpex_margin.get(code, {"margin_chg": 0})
        result[yft] = {**quote, **chips, **margin, "ohlcv": ohlcv.get(yft)}

    return result
