from unittest.mock import patch, MagicMock
import pandas as pd
import pytest
from data.twse_fetcher import fetch_tw_all, _parse_twse_quote, _parse_twse_chips, _parse_twse_margin, _parse_tpex_quote, _parse_tpex_chips, _parse_tpex_margin


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


def test_parse_tpex_quote_price():
    result = _parse_tpex_quote(TPEX_QUOTE_OK)
    assert "6488" in result
    assert result["6488"]["price"] == 300.0
    assert result["6488"]["day_return"] == pytest.approx(5.0 / (300.0 - 5.0), rel=1e-3)


def test_parse_tpex_chips_fi_and_it():
    result = _parse_tpex_chips(TPEX_CHIPS_OK)
    assert "6488" in result
    assert result["6488"]["fi_net"] == 1_500_000
    assert result["6488"]["it_net"] == 300_000  # row[8] = "300,000" in aaData


def test_parse_tpex_margin_chg():
    result = _parse_tpex_margin(TPEX_MARGIN_OK)
    assert "6488" in result
    # today(row[6])=820 - prev(row[2])=1000 = -180
    assert result["6488"]["margin_chg"] == -180


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
