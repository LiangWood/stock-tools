# 台股動能篩選器設計規格

**日期：** 2026-05-07  
**範圍：** 在現有美股動能篩選器 App 中整合台股籌碼面篩選功能

---

## 目標

從台灣上市（TWSE）+ 上櫃（TPEx）約 1800 檔股票中，以籌碼面為主、技術面為輔的綜合動能評分，篩選出當日前 20 強個股，整合進現有 App 的宇宙選擇器。

---

## 資料來源

### TWSE 上市（免費，無需帳號）

| 資料 | API 端點 |
|------|----------|
| 三大法人買賣超 | `https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALL` |
| 融資融券餘額 | `https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=ALL` |
| 個股收盤行情（含成交量） | `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&type=ALLBUT0999` |
| 本益比（PE） | `https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?response=json` |

### TPEx 上櫃（免費）

| 資料 | API 端點 |
|------|----------|
| 三大法人買賣超 | `https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json` |
| 融資融券 | `https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=json` |
| 個股收盤行情 | `https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&o=json&se=EW` |
| 本益比 | `https://www.tpex.org.tw/web/stock/aftertrading/peratio_pb/pera_result.php?l=zh-tw&o=json` |

每次刷新當日呼叫所有 API，合併為每檔股票一筆記錄。OHLCV 歷史資料（供 K 線圖用）透過 yfinance `.TW` / `.TWO` 抓取。

---

## 評分權重

| 指標 | 權重 | 方向 | 說明 |
|------|------|------|------|
| 外資買賣超 | 30% | 正向 | 單位：億元，正=買超 |
| 投信買賣超 | 20% | 正向 | 單位：億元 |
| 融資增減 | 15% | **反向** | 融資減少代表籌碼健康，反向百分位排名 |
| 當日漲跌幅 | 10% | 正向 | 百分比 |
| 20日報酬率 | 10% | 正向 | 百分比 |
| 金額比率 | 10% | 正向 | 今日成交金額 ÷ 20日均量金額 |
| RSI(14) | 5% | 正向 | 來自 yfinance `.TW` OHLCV |

每項指標先做全市場百分位排名（0–100），再加權加總得 `tw_score`，輸出前 20 名。

PE 與成交量（張）僅顯示，不參與評分。

---

## 表格欄位（13 欄）

| 欄位 | 顯示名稱 | 格式 | 說明 |
|------|----------|------|------|
| rank | # | 整數 | 動能排名 |
| code | 代碼 | 字串 | 股票代號（如 2330） |
| name | 名稱 | 字串 | 中文股票名稱 |
| price | 現價 | `NT$XXX` | 收盤價，台幣 |
| day_return | 當日% | `+X.XX%` | 漲跌幅，紅漲綠跌（台股慣例） |
| ret_20d | 20日% | `+X.XX%` | 20日報酬率 |
| volume | 成交量(張) | 整數，千分位 | 當日成交量 |
| amount_ratio | 金額比 | `X.Xx` | 成交金額比率 |
| pe | PE | 一位小數 | 本益比，無資料顯示 `—` |
| fi_net | 外資(億) | `+XX.X` | 外資買賣超，紅漲綠跌 |
| it_net | 投信(億) | `+XX.X` | 投信買賣超 |
| margin_chg | 融資增減 | 整數，千分位 | 融資增減張數 |
| rsi | RSI | 一位小數 | RSI(14) |

> 台股漲跌色彩慣例：漲為**紅色**，跌為**綠色**（與美股相反）。

---

## 架構

### 新增檔案

```
data/twse_fetcher.py          # TWSE + TPEx API 呼叫，回傳 dict[code, TwStockData]
scoring/tw_engine.py          # 台股評分引擎，輸出 TOP 20 DataFrame
tests/data/test_twse_fetcher.py
tests/scoring/test_tw_engine.py
```

### 修改檔案

```
data/universe.py              # 新增 get_tw_tickers() 回傳上市+上櫃代號列表
ui/table.py                   # 新增 TW_COLUMNS，TablePanel 支援 mode 參數切換
ui/app.py                     # 宇宙選擇器加「台股」，_fetch_worker 依 mode 分流
```

### yfinance 代號格式

- 上市（TWSE）：代號 + `.TW`，例如 `2330.TW`
- 上櫃（TPEx）：代號 + `.TWO`，例如 `6488.TWO`
- `get_tw_tickers()` 回傳已帶後綴的代號列表，供 yfinance OHLCV 抓取與 K 線圖使用

### close_history 取得方式

`TwStockData.close_history` 透過 yfinance 批次下載（與美股 `fetch_all` 相同機制），用於：
1. 計算 RSI(14) 與 20 日報酬率
2. K 線圖顯示

TWSE API 只提供當日快照；歷史 OHLCV 由 yfinance 負責。

### 資料流

```
[TWSE API] ──┐
[TPEx API] ──┤─→ twse_fetcher.fetch_tw_all() ─→ tw_engine.compute_tw_scores() ─→ TablePanel(mode="tw")
[yfinance ] ──┘                                                                  ↓ ChartPanel.plot()
```

### `TwStockData` 結構（TypedDict）

```python
class TwStockData(TypedDict):
    code: str
    name: str
    price: float
    day_return: float
    volume: int
    amount_ratio: float
    pe: float | None
    fi_net: float        # 外資買賣超（億）
    it_net: float        # 投信買賣超（億）
    margin_chg: int      # 融資增減（張）
    close_history: pd.DataFrame  # yfinance OHLCV，供 K 線圖用
```

---

## 錯誤處理

- 單一 API 失敗 → 該欄位填 0／None，不中斷整體流程
- 上市或上櫃整批失敗 → log warning，繼續跑另一批
- 股票代號無 yfinance 歷史 → K 線圖顯示 placeholder，不影響評分

---

## 測試範圍

| 測試檔案 | 覆蓋項目 |
|----------|----------|
| `test_twse_fetcher.py` | mock HTTP → 解析正確、API 失敗 graceful、上市上櫃合併去重 |
| `test_tw_engine.py` | 評分排序正確、融資反向、TOP 20 截斷、None 欄位安全處理 |

---

## 不在範圍內

- 自營商買賣超（不納入評分）
- 歷史籌碼趨勢圖
- 即時報價（盤中）
- 回測功能
