# 台股動能篩選器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 整合台股（上市+上櫃）籌碼面動能篩選到現有 App，透過 TWSE/TPEx 官方 API 取得三大法人、融資等資料，輸出前 20 名排行表。

**Architecture:** `data/twse_fetcher.py` 呼叫 TWSE/TPEx API 取得當日快照並合併 yfinance OHLCV，`scoring/tw_engine.py` 以 7 項指標百分位加權輸出 TOP 20，`ui/table.py` 新增 TW mode 欄位，`ui/app.py` 宇宙選擇器加入「台股」分流。

**Tech Stack:** Python 3.11+、requests、yfinance、pandas、numpy、customtkinter、pytest、pytest-mock

---

## 檔案結構

| 動作 | 檔案 | 職責 |
|------|------|------|
| 新增 | `data/twse_fetcher.py` | TWSE/TPEx API + yfinance OHLCV，回傳 `dict[yf_ticker, dict]` |
| 新增 | `scoring/tw_engine.py` | 台股評分引擎，輸出 TOP 20 DataFrame |
| 新增 | `tests/data/test_twse_fetcher.py` | twse_fetcher 單元測試 |
| 新增 | `tests/scoring/test_tw_engine.py` | tw_engine 單元測試 |
| 修改 | `ui/table.py` | 新增 `TW_COLUMNS`，`TablePanel` 支援 `set_mode("tw"/"us")` |
| 修改 | `ui/app.py` | 宇宙選擇器加「台股」，`_fetch_worker` 依模式分流 |

---

## Task 1: data/twse_fetcher.py — 台股資料抓取

**Files:**
- Create: `data/twse_fetcher.py`
- Create: `tests/data/test_twse_fetcher.py`

- [ ] **Step 1: 撰寫失敗測試**

`tests/data/test_twse_fetcher.py`:
```python
from unittest.mock import patch, MagicMock
import pandas as pd
import pytest
from data.twse_fetcher import fetch_tw_all, _parse_twse_quote, _parse_twse_chips, _parse_twse_margin


# ── Sample TWSE API fixtures ──────────────────────────────────────────────────

TWSE_QUOTE_OK = {
    "stat": "OK",
    "fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額",
               "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差",
               "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比"],
    "data": [
        ["2330", "台積電", "32,150,000", "45,678", "31,507,000,000",
         "975.00", "985.00", "972.00", "980.00", "+", "5.00",
         "980.00", "100", "981.00", "200", "22.10"],
        ["2454", "聯發科", "8,420,000", "12,345", "10,146,100,000",
         "1200.00", "1210.00", "1195.00", "1205.00", "-", "15.00",
         "1205.00", "50", "1206.00", "100", "18.50"],
    ],
}

TWSE_CHIPS_OK = {
    "stat": "OK",
    "fields": ["證券代號", "證券名稱",
               "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
               "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
               "外陸資買進股數", "外陸資賣出股數", "外陸資買賣超股數",
               "投信買進股數", "投信賣出股數", "投信買賣超股數",
               "自營商買賣超股數(自行買賣)", "自營商買賣超股數(避險)", "自營商買賣超股數",
               "三大法人買賣超股數"],
    "data": [
        ["2330", "台積電",
         "80,000,000", "5,000,000", "75,000,000",
         "5,000,000", "1,000,000", "4,000,000",
         "85,000,000", "6,000,000", "79,000,000",
         "10,000,000", "2,000,000", "8,000,000",
         "3,000,000", "1,000,000", "4,000,000",
         "91,000,000"],
    ],
}

TWSE_MARGIN_OK = {
    "stat": "OK",
    "fields": ["股票代號", "名稱",
               "融資(借錢買股)前日餘額", "融資買進", "融資賣出", "現金償還", "融資今日餘額", "融資限額",
               "融券(借股賣出)前日餘額", "融券賣出", "融券買進", "現券償還", "融券今日餘額", "融券限額",
               "資券相抵"],
    "data": [
        ["2330", "台積電",
         "5,000", "200", "1,000", "240", "3,960", "100,000",
         "1,000", "100", "200", "50", "850", "50,000",
         "150"],
    ],
}

TPEX_QUOTE_OK = {
    "reportDate": "113/05/07",
    "aaData": [
        ["6488", "環球晶", "300.00", "5.00", "1.69",
         "295.00", "302.00", "293.00", "300.00",
         "5,420", "1,626,000", "--"],
    ],
}

TPEX_CHIPS_OK = {
    "iTotalRecords": 1,
    "aaData": [
        ["6488", "環球晶", "2,000,000", "500,000", "1,500,000",
         "800,000", "200,000", "600,000",
         "300,000", "100,000", "200,000", "2,300,000"],
    ],
}

TPEX_MARGIN_OK = {
    "iTotalRecords": 1,
    "aaData": [
        ["6488", "環球晶", "1,000", "50", "200", "30", "820", "10,000",
         "200", "30", "60", "10", "160", "5,000", "50"],
    ],
}


# ── Parser unit tests ─────────────────────────────────────────────────────────

def test_parse_twse_quote_price():
    result = _parse_twse_quote(TWSE_QUOTE_OK)
    assert "2330" in result
    assert result["2330"]["price"] == 980.0
    assert result["2330"]["name"] == "台積電"


def test_parse_twse_quote_day_return_positive():
    result = _parse_twse_quote(TWSE_QUOTE_OK)
    assert result["2330"]["day_return"] == pytest.approx(5.0 / (980.0 - 5.0), rel=1e-3)


def test_parse_twse_quote_day_return_negative():
    result = _parse_twse_quote(TWSE_QUOTE_OK)
    assert result["2454"]["day_return"] == pytest.approx(-15.0 / (1205.0 + 15.0), rel=1e-3)


def test_parse_twse_quote_pe_none_when_dash():
    data = {
        "stat": "OK",
        "fields": TWSE_QUOTE_OK["fields"],
        "data": [["9999", "測試股", "1,000,000", "100", "1,000,000",
                  "10.00", "10.00", "10.00", "10.00", "+", "0.10",
                  "10.00", "10", "10.10", "5", "--"]],
    }
    result = _parse_twse_quote(data)
    assert result["9999"]["pe"] is None


def test_parse_twse_quote_stat_not_ok():
    result = _parse_twse_quote({"stat": "FAIL", "data": []})
    assert result == {}


def test_parse_twse_chips_fi_and_it():
    result = _parse_twse_chips(TWSE_CHIPS_OK)
    assert "2330" in result
    assert result["2330"]["fi_net"] == 79_000_000
    assert result["2330"]["it_net"] == 8_000_000


def test_parse_twse_margin_chg():
    result = _parse_twse_margin(TWSE_MARGIN_OK)
    assert "2330" in result
    # 融資今日餘額(3960) - 融資前日餘額(5000) = -1040
    assert result["2330"]["margin_chg"] == -1040


# ── fetch_tw_all integration test ─────────────────────────────────────────────

def _mock_get(url, **kwargs):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if "MI_INDEX" in url:
        resp.json.return_value = TWSE_QUOTE_OK
    elif "T86" in url:
        resp.json.return_value = TWSE_CHIPS_OK
    elif "MI_MARGN" in url:
        resp.json.return_value = TWSE_MARGIN_OK
    elif "otc_quotes" in url:
        resp.json.return_value = TPEX_QUOTE_OK
    elif "3itrade" in url:
        resp.json.return_value = TPEX_CHIPS_OK
    elif "margin_bal" in url:
        resp.json.return_value = TPEX_MARGIN_OK
    else:
        resp.json.return_value = {"stat": "FAIL"}
    return resp


def _make_ohlcv(n=130):
    import numpy as np
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": np.linspace(100, 150, n),
        "High": np.linspace(110, 160, n),
        "Low": np.linspace(90, 140, n),
        "Close": np.linspace(100, 150, n),
        "Volume": [1_000_000] * n,
    }, index=idx)


def test_fetch_tw_all_returns_dict():
    ohlcv_mock = {
        "2330.TW": _make_ohlcv(),
        "6488.TWO": _make_ohlcv(),
    }
    with patch("data.twse_fetcher.requests.get", side_effect=_mock_get), \
         patch("data.twse_fetcher._fetch_tw_ohlcv", return_value=ohlcv_mock):
        result = fetch_tw_all()
    assert "2330.TW" in result
    assert result["2330.TW"]["price"] == 980.0


def test_fetch_tw_all_api_failure_graceful():
    def fail_get(url, **kwargs):
        raise Exception("network error")
    with patch("data.twse_fetcher.requests.get", side_effect=fail_get), \
         patch("data.twse_fetcher._fetch_tw_ohlcv", return_value={}):
        result = fetch_tw_all()
    assert isinstance(result, dict)


def test_fetch_tw_all_missing_chips_uses_zero():
    ohlcv_mock = {"2454.TW": _make_ohlcv()}
    # 2454 not in chips response (only 2330 is)
    with patch("data.twse_fetcher.requests.get", side_effect=_mock_get), \
         patch("data.twse_fetcher._fetch_tw_ohlcv", return_value=ohlcv_mock):
        result = fetch_tw_all()
    assert result["2454.TW"]["fi_net"] == 0
    assert result["2454.TW"]["it_net"] == 0
```

- [ ] **Step 2: 執行測試確認失敗**

```bash
pytest tests/data/test_twse_fetcher.py -v
```

Expected: `ImportError` — `data.twse_fetcher` 不存在。

- [ ] **Step 3: 實作 data/twse_fetcher.py**

```python
import logging
from typing import Optional, Callable
import requests
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_BATCH_SIZE = 100

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


# ── TWSE parsers ──────────────────────────────────────────────────────────────

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
            vol   = _to_int(row[si]) // 1000          # 股 → 張
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
        # 外陸資買賣超（含自營商），排除「不含」的那欄
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
            code   = str(row[0]).strip()
            chg    = _to_int(row[today_i]) - _to_int(row[prev_i])
            result[code] = {"margin_chg": chg}
        except IndexError:
            continue
    return result


# ── TPEx parsers ──────────────────────────────────────────────────────────────

def _parse_tpex_quote(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    rows = data.get("aaData", [])
    for row in rows:
        try:
            code  = str(row[0]).strip()
            name  = str(row[1]).strip()
            price = _to_float(row[2])
            diff  = _to_float(row[3])
            prev  = price - diff
            day_r = (diff / prev) if prev != 0 else 0.0
            vol   = _to_int(row[8]) if len(row) > 8 else 0  # 成交量(張)
            result[code] = {
                "code": code, "name": name, "price": price,
                "volume": vol, "day_return": day_r, "pe": None,
            }
        except (IndexError, ZeroDivisionError):
            continue
    return result


def _parse_tpex_chips(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    rows = data.get("aaData", [])
    for row in rows:
        try:
            code   = str(row[0]).strip()
            # aaData cols: 代號, 名稱, 外資買進, 外資賣出, 外資買賣超, 自營買賣超, 投信買進, 投信賣出, 投信買賣超, ...
            fi_net = _to_float(row[4])
            it_net = _to_float(row[8]) if len(row) > 8 else 0.0
            result[code] = {"fi_net": fi_net, "it_net": it_net}
        except IndexError:
            continue
    return result


def _parse_tpex_margin(data: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    rows = data.get("aaData", [])
    for row in rows:
        try:
            code  = str(row[0]).strip()
            today = _to_int(row[6])
            prev  = _to_int(row[2])
            result[code] = {"margin_chg": today - prev}
        except IndexError:
            continue
    return result


# ── OHLCV fetch (reuses yfinance batch pattern) ───────────────────────────────

def _fetch_tw_ohlcv(
    tickers: list[str],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Optional[pd.DataFrame]]:
    results: dict[str, Optional[pd.DataFrame]] = {}
    total = len(tickers)
    for start in range(0, total, _BATCH_SIZE):
        batch = tickers[start: start + _BATCH_SIZE]
        try:
            raw = yf.download(batch, period="6mo", progress=False, auto_adjust=True)
            if isinstance(raw.columns, pd.MultiIndex):
                for t in batch:
                    try:
                        df = raw.xs(t, axis=1, level=1).dropna(how="all")
                        results[t] = df if not df.empty else None
                    except KeyError:
                        results[t] = None
            else:
                results[batch[0]] = raw if not raw.empty else None
        except Exception as exc:
            logger.warning("TW OHLCV batch %d failed: %s", start // _BATCH_SIZE, exc)
            for t in batch:
                results[t] = None
        done = min(start + _BATCH_SIZE, total)
        if progress_callback:
            progress_callback(done, total)
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_tw_all(
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, dict]:
    """
    Fetch all TWSE + TPEx stocks.
    Returns dict keyed by yfinance ticker (e.g. "2330.TW", "6488.TWO").
    Each value: {code, name, price, volume, day_return, pe, fi_net, it_net,
                 margin_chg, ohlcv: DataFrame | None}
    """
    twse_quote = twse_chips = twse_margin = {}
    tpex_quote = tpex_chips = tpex_margin = {}

    for url, parser, target in [
        (_TWSE_QUOTE,  _parse_twse_quote,  "twse_quote"),
        (_TWSE_CHIPS,  _parse_twse_chips,  "twse_chips"),
        (_TWSE_MARGIN, _parse_twse_margin, "twse_margin"),
        (_TPEX_QUOTE,  _parse_tpex_quote,  "tpex_quote"),
        (_TPEX_CHIPS,  _parse_tpex_chips,  "tpex_chips"),
        (_TPEX_MARGIN, _parse_tpex_margin, "tpex_margin"),
    ]:
        try:
            parsed = parser(_get(url))
        except Exception as exc:
            logger.warning("TW fetch %s failed: %s", target, exc)
            parsed = {}
        if target == "twse_quote":   twse_quote  = parsed
        elif target == "twse_chips": twse_chips  = parsed
        elif target == "twse_margin":twse_margin = parsed
        elif target == "tpex_quote": tpex_quote  = parsed
        elif target == "tpex_chips": tpex_chips  = parsed
        elif target == "tpex_margin":tpex_margin = parsed

    twse_tickers = [f"{c}.TW"  for c in twse_quote]
    tpex_tickers = [f"{c}.TWO" for c in tpex_quote]
    ohlcv = _fetch_tw_ohlcv(twse_tickers + tpex_tickers, progress_callback)

    result: dict[str, dict] = {}
    for code, quote in twse_quote.items():
        yft = f"{code}.TW"
        chips  = twse_chips.get(code,  {"fi_net": 0.0, "it_net": 0.0})
        margin = twse_margin.get(code, {"margin_chg": 0})
        result[yft] = {**quote, **chips, **margin, "ohlcv": ohlcv.get(yft)}
    for code, quote in tpex_quote.items():
        yft = f"{code}.TWO"
        chips  = tpex_chips.get(code,  {"fi_net": 0.0, "it_net": 0.0})
        margin = tpex_margin.get(code, {"margin_chg": 0})
        result[yft] = {**quote, **chips, **margin, "ohlcv": ohlcv.get(yft)}

    return result
```

- [ ] **Step 4: 執行測試確認通過**

```bash
pytest tests/data/test_twse_fetcher.py -v
```

Expected: 全部 PASSED。

- [ ] **Step 5: Commit**

```bash
git add data/twse_fetcher.py tests/data/test_twse_fetcher.py
git commit -m "feat: TWSE/TPEx fetcher with chips and OHLCV data"
```

---

## Task 2: scoring/tw_engine.py — 台股評分引擎

**Files:**
- Create: `scoring/tw_engine.py`
- Create: `tests/scoring/test_tw_engine.py`

- [ ] **Step 1: 撰寫失敗測試**

`tests/scoring/test_tw_engine.py`:
```python
import pandas as pd
import numpy as np
import pytest
from scoring.tw_engine import compute_tw_scores

_N = 130


def _make_ohlcv(n=_N, trend="up"):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = pd.Series(np.linspace(100, 150, n) if trend == "up" else np.linspace(150, 100, n), index=idx)
    return pd.DataFrame({
        "Open": close * 0.99, "High": close * 1.01,
        "Low": close * 0.98, "Close": close,
        "Volume": [1_000_000] * n,
    }, index=idx)


def _make_stock(fi_net=0.0, it_net=0.0, margin_chg=0,
                day_return=0.0, price=100.0, volume=1000,
                ohlcv=None, pe=None):
    return {
        "code": "1234", "name": "測試股",
        "price": price, "volume": volume,
        "day_return": day_return,
        "fi_net": fi_net, "it_net": it_net,
        "margin_chg": margin_chg, "pe": pe,
        "ohlcv": ohlcv if ohlcv is not None else _make_ohlcv(),
    }


def test_returns_dataframe():
    data = {f"T{i}.TW": _make_stock() for i in range(10)}
    result = compute_tw_scores(data)
    assert isinstance(result, pd.DataFrame)


def test_has_required_columns():
    data = {"A.TW": _make_stock(), "B.TW": _make_stock()}
    result = compute_tw_scores(data)
    for col in ["ticker", "name", "price", "volume", "day_return", "pe",
                "fi_net", "it_net", "margin_chg", "ret_20d",
                "amount_ratio", "rsi", "tw_score"]:
        assert col in result.columns, f"Missing column: {col}"


def test_top_20_limit():
    data = {f"T{i}.TW": _make_stock() for i in range(50)}
    result = compute_tw_scores(data)
    assert len(result) <= 20


def test_sorted_descending():
    data = {f"T{i}.TW": _make_stock(fi_net=float(i)) for i in range(30)}
    result = compute_tw_scores(data)
    scores = result["tw_score"].tolist()
    assert scores == sorted(scores, reverse=True)


def test_score_between_0_and_100():
    data = {f"T{i}.TW": _make_stock(fi_net=float(i * 100)) for i in range(20)}
    result = compute_tw_scores(data)
    assert result["tw_score"].between(0, 100).all()


def test_margin_chg_reverse_scoring():
    """高融資增減的股票評分應低於低融資增減的股票（反向計分）"""
    data = {
        "HIGH_MARGIN.TW": _make_stock(margin_chg=100_000, fi_net=1.0),
        "LOW_MARGIN.TW":  _make_stock(margin_chg=-100_000, fi_net=1.0),
    }
    result = compute_tw_scores(data)
    high_score = result[result["ticker"] == "HIGH_MARGIN.TW"]["tw_score"].iloc[0]
    low_score  = result[result["ticker"] == "LOW_MARGIN.TW"]["tw_score"].iloc[0]
    assert low_score > high_score


def test_none_ohlcv_handled():
    data = {
        "GOOD.TW": _make_stock(ohlcv=_make_ohlcv()),
        "BAD.TW":  _make_stock(ohlcv=None),
    }
    result = compute_tw_scores(data)
    assert len(result) > 0


def test_fewer_than_20_stocks():
    data = {f"T{i}.TW": _make_stock() for i in range(5)}
    result = compute_tw_scores(data)
    assert len(result) == 5
```

- [ ] **Step 2: 執行測試確認失敗**

```bash
pytest tests/scoring/test_tw_engine.py -v
```

Expected: `ImportError`。

- [ ] **Step 3: 實作 scoring/tw_engine.py**

```python
import numpy as np
import pandas as pd
from scoring.engine import calculate_rsi

_TW_WEIGHTS = {
    "fi_net":       0.30,
    "it_net":       0.20,
    "margin_chg":   0.15,  # 反向
    "day_return":   0.10,
    "ret_20d":      0.10,
    "amount_ratio": 0.10,
    "rsi":          0.05,
}


def _tech_metrics(ohlcv: pd.DataFrame | None) -> dict:
    defaults = {"ret_20d": 0.0, "amount_ratio": 1.0, "rsi": 50.0}
    if ohlcv is None or len(ohlcv) < 2:
        return defaults
    close  = ohlcv["Close"].dropna()
    volume = ohlcv["Volume"].dropna()
    n = len(close)

    ret_20d = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0

    amount = close * volume
    amt_avg = float(amount.iloc[-21:-1].mean()) if n >= 21 else float(amount.mean())
    amount_ratio = float(amount.iloc[-1] / amt_avg) if amt_avg > 0 else 1.0

    rsi = calculate_rsi(close)
    return {"ret_20d": ret_20d, "amount_ratio": amount_ratio, "rsi": rsi}


def compute_tw_scores(ticker_data: dict) -> pd.DataFrame:
    rows = []
    for ticker, d in ticker_data.items():
        if d is None:
            continue
        tech = _tech_metrics(d.get("ohlcv"))
        rows.append({
            "ticker":       ticker,
            "name":         d.get("name", ""),
            "price":        d.get("price", 0.0),
            "volume":       d.get("volume", 0),
            "day_return":   d.get("day_return", 0.0),
            "pe":           d.get("pe"),
            "fi_net":       d.get("fi_net", 0.0),
            "it_net":       d.get("it_net", 0.0),
            "margin_chg":   d.get("margin_chg", 0),
            "ret_20d":      tech["ret_20d"],
            "amount_ratio": tech["amount_ratio"],
            "rsi":          tech["rsi"],
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    weights = np.array(list(_TW_WEIGHTS.values()))
    metric_cols = list(_TW_WEIGHTS.keys())

    ranks = pd.DataFrame(index=df.index)
    for col in metric_cols:
        if col == "margin_chg":
            # 反向：融資增減越少 → 分數越高
            ranks[col] = (-df[col]).rank(pct=True) * 100
        else:
            ranks[col] = df[col].rank(pct=True) * 100

    df["tw_score"] = ranks[metric_cols].values @ weights

    return (
        df.sort_values("tw_score", ascending=False)
        .head(20)
        .reset_index(drop=True)
    )
```

- [ ] **Step 4: 執行測試確認通過**

```bash
pytest tests/scoring/test_tw_engine.py -v
```

Expected: 全部 PASSED。

- [ ] **Step 5: Commit**

```bash
git add scoring/tw_engine.py tests/scoring/test_tw_engine.py
git commit -m "feat: Taiwan stock scoring engine with chips-weighted TOP 20"
```

---

## Task 3: ui/table.py — 新增台股模式

**Files:**
- Modify: `ui/table.py`

（UI 元件以手動測試為主，無自動化單元測試）

- [ ] **Step 1: 在 table.py 頂部加入台股欄位定義與顏色**

在 `_COLUMNS` 定義後，加入：

```python
# 台股色彩慣例：漲紅跌綠（與美股相反）
_TW_RED   = "#ff5252"   # 漲
_TW_GREEN = "#00c853"   # 跌

TW_COLUMNS = [
    ("rank",        "#",        45),
    ("ticker",      "代碼",      65),
    ("name",        "名稱",      90),
    ("price",       "現價(NT$)", 85),
    ("day_return",  "當日%",     70),
    ("ret_20d",     "20日%",     70),
    ("volume",      "成交量(張)", 90),
    ("amount_ratio","金額比",    70),
    ("pe",          "PE",        60),
    ("fi_net",      "外資(億)",  80),
    ("it_net",      "投信(億)",  80),
    ("margin_chg",  "融資增減",  85),
    ("rsi",         "RSI",       60),
]

_TW_TICKER_IDX = next(i for i, (col_id, _, _) in enumerate(TW_COLUMNS) if col_id == "ticker")
```

- [ ] **Step 2: 新增 `set_mode()` 方法，支援動態切換欄位**

在 `TablePanel` 類別中，修改 `__init__` 加入 `mode` 參數，並新增 `set_mode()` 與對應的 render 邏輯：

完整替換 `ui/table.py`：

```python
from tkinter import ttk
from typing import Callable, Optional
import customtkinter as ctk
import pandas as pd

_COLUMNS = [
    ("rank",         "#",       50),
    ("ticker",       "代碼",     70),
    ("price",        "現價",     80),
    ("day_return",   "當日%",    80),
    ("ret_5d",       "5日%",     80),
    ("ret_20d",      "20日%",    80),
    ("volume_ratio", "爆量倍數", 90),
    ("rsi",          "RSI",      70),
]

TW_COLUMNS = [
    ("rank",         "#",        45),
    ("ticker",       "代碼",      65),
    ("name",         "名稱",      90),
    ("price",        "現價(NT$)", 85),
    ("day_return",   "當日%",     70),
    ("ret_20d",      "20日%",     70),
    ("volume",       "成交量(張)", 90),
    ("amount_ratio", "金額比",    70),
    ("pe",           "PE",        60),
    ("fi_net",       "外資(億)",  80),
    ("it_net",       "投信(億)",  80),
    ("margin_chg",   "融資增減",  85),
    ("rsi",          "RSI",       60),
]

_TICKER_IDX    = next(i for i, (c, _, _) in enumerate(_COLUMNS)    if c == "ticker")
_TW_TICKER_IDX = next(i for i, (c, _, _) in enumerate(TW_COLUMNS)  if c == "ticker")

_BG          = "#2b2b2b"
_FG          = "#ffffff"
_HEADING_BG  = "#1a1a2e"
_HEADING_FG  = "#00c853"
_SELECTED_BG = "#1f538d"


class TablePanel(ctk.CTkFrame):
    def __init__(self, master, on_select: Callable[[str], None], **kwargs):
        super().__init__(master, **kwargs)
        self._on_select = on_select
        self._df: Optional[pd.DataFrame] = None
        self._sort_col: str = "momentum_score"
        self._sort_asc: bool = False
        self._mode: str = "us"
        self._build(_COLUMNS)

    def _build(self, columns: list):
        for w in self.winfo_children():
            w.destroy()

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
            background=_BG, foreground=_FG,
            fieldbackground=_BG, rowheight=26, font=("Helvetica", 11))
        style.configure("Treeview.Heading",
            background=_HEADING_BG, foreground=_HEADING_FG, font=("Helvetica", 11, "bold"))
        style.map("Treeview", background=[("selected", _SELECTED_BG)])

        col_ids = [c[0] for c in columns]
        self._tree = ttk.Treeview(self, columns=col_ids, show="headings")
        for col_id, label, width in columns:
            self._tree.heading(col_id, text=label,
                               command=lambda c=col_id: self._on_heading(c))
            self._tree.column(col_id, width=width, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_row_select)

    def set_mode(self, mode: str):
        """Switch between 'us' and 'tw' column layouts."""
        if mode == self._mode:
            return
        self._mode = mode
        self._df = None
        if mode == "tw":
            self._sort_col = "tw_score"
            self._build(TW_COLUMNS)
        else:
            self._sort_col = "momentum_score"
            self._build(_COLUMNS)

    def _on_heading(self, col: str):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = False
        if self._df is not None:
            self._render(self._df)

    def _on_row_select(self, _event):
        sel = self._tree.selection()
        if not sel:
            return
        idx = _TW_TICKER_IDX if self._mode == "tw" else _TICKER_IDX
        ticker = self._tree.item(sel[0])["values"][idx]
        self._on_select(str(ticker))

    def _render(self, df: pd.DataFrame):
        sort_col = self._sort_col if self._sort_col in df.columns else df.columns[-1]
        sorted_df = df.sort_values(sort_col, ascending=self._sort_asc).reset_index(drop=True)
        self._tree.delete(*self._tree.get_children())
        if self._mode == "tw":
            self._render_tw(sorted_df)
        else:
            self._render_us(sorted_df)

    def _render_us(self, df: pd.DataFrame):
        for row in df.itertuples():
            self._tree.insert("", "end", values=(
                row.Index + 1,
                row.ticker,
                f"${row.price:.2f}",
                f"{row.day_return*100:.2f}%",
                f"{row.ret_5d*100:.2f}%",
                f"{row.ret_20d*100:.2f}%",
                f"{row.volume_ratio:.2f}x",
                f"{row.rsi:.1f}",
            ))

    def _render_tw(self, df: pd.DataFrame):
        for row in df.itertuples():
            pe_str = f"{row.pe:.1f}" if pd.notna(row.pe) else "—"
            # 台股漲跌顯示（紅漲綠跌，與美股相反）
            self._tree.insert("", "end", values=(
                row.Index + 1,
                row.ticker,
                getattr(row, "name", ""),
                f"NT${row.price:.1f}",
                f"{row.day_return*100:.2f}%",
                f"{row.ret_20d*100:.2f}%",
                f"{row.volume:,}",
                f"{row.amount_ratio:.2f}x",
                pe_str,
                f"{row.fi_net/1e8:+.1f}",
                f"{row.it_net/1e8:+.1f}",
                f"{row.margin_chg:+,}",
                f"{row.rsi:.1f}",
            ))

    def update_data(self, df: pd.DataFrame):
        self._df = df
        self._render(df)
```

- [ ] **Step 3: Commit**

```bash
git add ui/table.py
git commit -m "feat: table panel TW mode with chips columns"
```

---

## Task 4: ui/app.py — 串接台股流程

**Files:**
- Modify: `ui/app.py`

- [ ] **Step 1: 更新 import 與 universe 對應表**

完整替換 `ui/app.py`：

```python
import queue
import threading
import logging
from datetime import datetime
from typing import Optional
import customtkinter as ctk
from data.universe import get_sp500_tickers, get_nasdaq100_tickers, get_combined_tickers
from data.twse_fetcher import fetch_tw_all
from data.fetcher import fetch_all
from scoring.engine import compute_scores
from scoring.tw_engine import compute_tw_scores
from ui.table import TablePanel
from ui.chart import ChartPanel

logger = logging.getLogger(__name__)
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

_Q_PROGRESS = "progress"
_Q_DONE     = "done"
_Q_ERROR    = "error"
_MUTED      = "#888888"
_POLL_BATCH = 10

_US_UNIVERSES = {
    "S&P 500":    get_sp500_tickers,
    "NASDAQ 100": get_nasdaq100_tickers,
    "全部":        get_combined_tickers,
}
_ALL_UNIVERSES = list(_US_UNIVERSES.keys()) + ["台股"]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("動能篩選器")
        self.geometry("1280x720")
        self._queue: queue.Queue = queue.Queue()
        self._fetched_data: dict = {}
        self._poll_id: Optional[str] = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        header = ctk.CTkFrame(self, height=48)
        header.pack(fill="x", padx=10, pady=(10, 0))

        ctk.CTkLabel(header, text="動能篩選器", font=("Helvetica", 16, "bold")).pack(side="left", padx=12)

        self._status_label = ctk.CTkLabel(header, text="尚未載入", text_color=_MUTED)
        self._status_label.pack(side="right", padx=12)

        self._refresh_btn = ctk.CTkButton(header, text="刷新", width=80, command=self._start_fetch)
        self._refresh_btn.pack(side="right", padx=8)

        self._universe_btn = ctk.CTkSegmentedButton(
            header, values=_ALL_UNIVERSES, width=320, height=28,
            command=self._on_universe_change,
        )
        self._universe_btn.set("S&P 500")
        self._universe_btn.pack(side="right", padx=12)

        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=10, pady=10)

        self._table = TablePanel(body, on_select=self._on_ticker_select)
        self._table.pack(side="left", fill="both", expand=True)

        self._chart = ChartPanel(body, width=420)
        self._chart.pack(side="right", fill="both", padx=(8, 0))

    def _on_universe_change(self, value: str):
        mode = "tw" if value == "台股" else "us"
        self._table.set_mode(mode)

    def _start_fetch(self):
        self._refresh_btn.configure(state="disabled", text="載入中…")
        self._status_label.configure(text="取得股票清單…")
        thread = threading.Thread(target=self._fetch_worker, daemon=True)
        thread.start()
        self._poll_id = self.after(100, self._poll_queue)

    def _fetch_worker(self):
        try:
            universe = self._universe_btn.get()

            def progress(done, total):
                self._queue.put((_Q_PROGRESS, f"抓取中 {done}/{total}"))

            if universe == "台股":
                data = fetch_tw_all(progress_callback=progress)
                scores = compute_tw_scores(data)
                # 儲存 ohlcv 供 K 線圖使用
                self._fetched_data = {
                    ticker: d.get("ohlcv") for ticker, d in data.items() if d
                }
            else:
                tickers = _US_UNIVERSES[universe]()
                data = fetch_all(tickers, progress_callback=progress)
                scores = compute_scores(data)
                self._fetched_data = data

            self._queue.put((_Q_DONE, scores))
        except Exception as exc:
            logger.exception("Fetch worker error")
            self._queue.put((_Q_ERROR, str(exc)))

    def _poll_queue(self):
        for _ in range(_POLL_BATCH):
            try:
                msg_type, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if msg_type == _Q_PROGRESS:
                self._status_label.configure(text=payload)
            elif msg_type == _Q_DONE:
                self._table.update_data(payload)
                now = datetime.now().strftime("%H:%M:%S")
                self._status_label.configure(text=f"上次更新 {now}")
                self._reset_btn()
                return
            elif msg_type == _Q_ERROR:
                self._status_label.configure(text=f"錯誤：{payload}")
                self._reset_btn()
                return
        self._poll_id = self.after(100, self._poll_queue)

    def _reset_btn(self):
        self._refresh_btn.configure(state="normal", text="刷新")

    def _on_close(self):
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
        self.destroy()

    def _on_ticker_select(self, ticker: str):
        df = self._fetched_data.get(ticker)
        if df is not None:
            self._chart.plot(ticker, df)
```

- [ ] **Step 2: 執行完整測試套件**

```bash
pytest tests/ -v
```

Expected: 全部 PASSED（含既有 16 tests + 新增 tw 相關 tests）。

- [ ] **Step 3: 手動啟動驗證**

```bash
python main.py
```

手動驗證：
- [ ] 宇宙選擇器顯示 S&P 500 / NASDAQ 100 / 全部 / **台股**
- [ ] 切換至「台股」後表格欄位自動替換（顯示 代碼、名稱、現價(NT$)、外資、投信、融資增減等）
- [ ] 點「刷新」後抓取台股籌碼資料（若市場已收盤 API 仍可回傳最新日資料）
- [ ] 排行前 20 名正確顯示
- [ ] 點擊個股後右側顯示 K 線圖（使用 .TW / .TWO yfinance 資料）
- [ ] 切換回 S&P 500，表格欄位正常還原

- [ ] **Step 4: Commit**

```bash
git add ui/app.py
git commit -m "feat: integrate Taiwan stock screener with chips-weighted TOP 20"
```
