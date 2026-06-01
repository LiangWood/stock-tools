from unittest.mock import patch, MagicMock
import pandas as pd
import pytest
from data.twse_fetcher import (
    fetch_tw_all,
    _fetch_twse_t86,
    _parse_twse_quote,
    _parse_twse_margin,
    _parse_tpex_quote,
    _parse_tpex_chips,
    _parse_tpex_margin,
)


# ── Sample TWSE API fixtures ──────────────────────────────────────────────────

TWSE_QUOTE_OK = [
    {
        "Code": "2330",
        "Name": "台積電",
        "TradeVolume": "32,150,000",
        "TradeValue": "31,507,000,000",
        "OpeningPrice": "975.00",
        "HighestPrice": "985.00",
        "LowestPrice": "972.00",
        "ClosingPrice": "980.00",
        "Change": "5.00",
        "Transaction": "45,678",
    },
    {
        "Code": "2454",
        "Name": "聯發科",
        "TradeVolume": "8,420,000",
        "TradeValue": "10,146,100,000",
        "OpeningPrice": "1200.00",
        "HighestPrice": "1210.00",
        "LowestPrice": "1195.00",
        "ClosingPrice": "1205.00",
        "Change": "-15.00",
        "Transaction": "12,345",
    },
]

TWSE_MI_INDEX_OK = {
    "stat": "OK",
    "date": "20260601",
    "tables": [
        {
            "title": "115年06月01日 價格指數(臺灣證券交易所)",
            "fields": ["指數", "收盤指數"],
            "data": [["寶島股價指數", "50,741.92"]],
        },
        {
            "title": "115年06月01日 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))",
            "fields": [
                "證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額",
                "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差",
                "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比",
            ],
            "data": [
                ["2330", "台積電", "32,150,000", "45,678", "31,507,000,000",
                 "975.00", "985.00", "972.00", "980.00", "<p style= color:red>+</p>", "5.00",
                 "979.00", "22", "980.00", "118", "25.12"],
                ["2454", "聯發科", "8,420,000", "12,345", "10,146,100,000",
                 "1200.00", "1210.00", "1195.00", "1205.00", "<p style= color:green>-</p>", "15.00",
                 "1200.00", "5", "1205.00", "8", "18.5"],
            ],
        },
    ],
}

TWSE_CHIPS_OK = {
    "stat": "OK",
    "fields": ["證券代號", "證券名稱",
               "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
               "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
               "投信買進股數", "投信賣出股數", "投信買賣超股數",
               "自營商買賣超股數(自行買賣)", "自營商買賣超股數(避險)", "自營商買賣超股數",
               "三大法人買賣超股數"],
    "data": [
        ["2330", "台積電",
         "80,000,000", "5,000,000", "75,000,000",
         "5,000,000", "1,000,000", "4,000,000",
         "10,000,000", "2,000,000", "8,000,000",
         "3,000,000", "1,000,000", "4,000,000",
         "91,000,000"],
    ],
}

TWSE_MARGIN_OK = [
    {"股票代號": "2330", "融資前日餘額": "5,000", "融資今日餘額": "3,960"},
]

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


def test_parse_twse_quote_mi_index_table_latest_close():
    result = _parse_twse_quote(TWSE_MI_INDEX_OK)

    assert result["2330"]["price"] == 980.0
    assert result["2330"]["volume"] == 32150
    assert result["2330"]["high"] == 985.0
    assert result["2330"]["low"] == 972.0
    assert result["2330"]["turnover_10k"] == 3_150_700.0
    assert result["2330"]["day_return"] == pytest.approx(5.0 / (980.0 - 5.0), rel=1e-3)
    assert result["2454"]["day_return"] == pytest.approx(-15.0 / (1205.0 + 15.0), rel=1e-3)


def test_parse_twse_quote_pe_none_when_dash():
    data = [
        {
            "Code": "9999",
            "Name": "測試股",
            "TradeVolume": "1,000,000",
            "TradeValue": "1,000,000",
            "HighestPrice": "10.00",
            "LowestPrice": "10.00",
            "ClosingPrice": "10.00",
            "Change": "0.10",
        }
    ]
    result = _parse_twse_quote(data)
    assert result["9999"]["pe"] is None


def test_parse_twse_quote_stat_not_ok():
    result = _parse_twse_quote([])
    assert result == {}


def test_fetch_twse_t86_fi_and_it():
    with patch("data.twse_fetcher._get", return_value=TWSE_CHIPS_OK):
        result = _fetch_twse_t86()
    assert "2330" in result
    assert result["2330"]["fi_net"] == 75_000_000  # 外陸資(不含外資自營商)，與實際 API 一致
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
    assert result["6488"]["it_net"] == 200_000  # row[10] = 投信買賣超


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
        resp.json.return_value = TWSE_MI_INDEX_OK
    elif "T86" in url:
        resp.json.return_value = TWSE_CHIPS_OK
    elif "MI_MARGN" in url:
        resp.json.return_value = TWSE_MARGIN_OK
    elif "TWT84U" in url:
        resp.json.return_value = []
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
