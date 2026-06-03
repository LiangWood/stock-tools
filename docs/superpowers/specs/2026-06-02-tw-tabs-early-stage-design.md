# 台股 Tab 重構 + 起漲觀察功能設計

**日期**：2026-06-02  
**狀態**：已核准

---

## 背景

目前台股 tabs 以 `S.twSelected`（Set 多選）管理三個切換按鈕（RS排名、籌碼加乘、突破觀察），邏輯複雜且不直覺。

本次重構目標：
1. 對齊美股 tab 架構（左側 view 切換 ＋ 右側 strategy 選擇）
2. 新增「起漲觀察」獨立 view，捕捉尚未大漲但法人已進場的股票
3. 各 view 下方顯示篩選條件說明列

---

## UI 架構

### Tab 佈局

```
美股：[個股視角] [板塊視角] [突破觀察]    RS排名 | 技術面加乘 | 基本面加乘
台股：[起漲觀察] [突破觀察]              RS排名 | 籌碼加乘
```

- **左側 view buttons**（`#tw-view-tabs`，新 DOM 元素）
  - `起漲觀察`（data-view="early"）
  - `突破觀察`（data-view="breakout"）
  - 預設皆未選中（主表顯示 RS排名或籌碼加乘）
  - 點擊已選中的按鈕 → 取消選中，回到主表
- **右側 strategy buttons**（`#tw-strategy-tabs`，保留現有）
  - `RS排名`、`籌碼加乘`（互斥單選）
  - 僅在主表（`S.twView === 'stock'`）時顯示

### 篩選條件說明列

每個觀察 view 上方顯示一列說明文字（類似美股突破觀察現有的 filter bar）：

- **起漲觀察**：`slope_20d > 0（近期翻多）＋ slope_60d < 0（尚未大漲）＋ breakout < 50（蓄勢中）＋ RS Score 30~65 ＋ 法人進場 — 布局期觀察清單，非動能排行`
- **突破觀察**：維持現有文字（已有）

---

## State 管理

### 廢棄

- `S.twSelected`（Set 多選邏輯，過於複雜）
- `S.twStrategy`（舊單選字串）

### 新增

```javascript
S.twView     = 'stock'   // 'stock' | 'breakout' | 'early'
S.twStrategy = 'rs'      // 'rs' | 'chips'（主表策略，不影響觀察 view）
```

### updateStrategyTabs() 更新

- `S.twView === 'stock'`：顯示 `#tw-strategy-tabs`，隱藏 `#tw-view-tabs` active 狀態外的樣式
- `S.twView !== 'stock'`：隱藏 `#tw-strategy-tabs`

---

## 後端變更

### 1. `scoring/tw_engine.py`

新增函數 `compute_tw_early_stage_candidates(ticker_data: dict) -> pd.DataFrame`：

**篩選條件**：
```python
slope_20d > 0               # 近期翻多（動能轉正）
slope_60d < 0               # 前期弱（尚未大漲，仍有空間）
breakout_score < 50         # 蓄勢中（與突破觀察互斥）
rs_score in [30, 65]        # 中低 RS 位（未噴發，但開始爬升）
fi_net > 0 OR it_consec_days >= 3   # 法人進場
```

**RS Score 計算**（內嵌，不依賴 compute_tw_rs_scores）：
- 直接在函數內計算 Q1×50% + Q2×25% + Q3×15% + Q4×10% 百分位排名

**起漲潛力分（排序依據）**：
```python
score = rs_score * 0.40
      + breakout_score * 0.30
      + (30 if it_consec_days >= 3 else 0)
      + (20 if fi_net > 0 else 0)
```

**輸出欄位**：
`ticker, stock_id, name, price, day_return, rs_score, breakout_score, slope_20d, slope_60d, fi_net, it_consec_days, is_limit_up, early_score`

**限制**：最多回傳 50 筆，依 `early_score` 降序

---

### 2. `server.py`

新增 `/api/tw-early-stage` endpoint：

```python
elif route == "/api/tw-early-stage":
    with _lock:
        candidates = _state.get("tw_early_stage", [])
    if not candidates:
        data = _read_static("tw_early_stage.json")
        if data:
            self._json(data); return
    self._json({"candidates": candidates})
```

`_state` 新增 `"tw_early_stage": []` 初始值。

`_fetch_worker` Taiwan 分支新增呼叫 `compute_tw_early_stage_candidates(raw)`。

---

### 3. `scripts/generate_static_data.py`

新增產生 `tw_early_stage.json`：

```python
tw_early_df = compute_tw_early_stage_candidates(tw_raw)
save("tw_early_stage", {
    "candidates": to_records(tw_early_df),
    "last_updated": now,
})
```

---

## 前端變更

### HTML 結構

在 `#tw-strategy-tabs` 前新增 `#tw-view-tabs`：

```html
<!-- 台股觀察 view 切換（左側，類美股 [突破觀察] 按鈕） -->
<div id="tw-view-tabs">
  <button class="tw-view-btn" data-view="early">起漲觀察</button>
  <button class="tw-view-btn" data-view="breakout">突破觀察</button>
</div>

<!-- 台股策略（右側，只在主表顯示） -->
<div id="tw-strategy-tabs">
  <button class="tw-strategy-btn active" data-strategy="rs">RS排名</button>
  <button class="tw-strategy-btn" data-strategy="chips">籌碼加乘</button>
</div>
```

`#tw-view-tabs` 使用與美股 `.view-btn` 相同的樣式類別。

### 新增 DOM 元素

```html
<!-- 起漲觀察 view（類比 tw-breakout-view） -->
<div id="tw-early-stage-view" style="display:none">
  <div id="tw-early-filter-bar">...</div>  <!-- 篩選條件說明 -->
  <table id="tw-early-table">
    <thead id="tw-early-head"></thead>
    <tbody id="tw-early-body"></tbody>
  </table>
  <div id="tw-early-empty" style="display:none">...</div>
</div>
```

### 欄位定義（起漲觀察 table）

```
# | 標的 | PRICE | 漲跌 | 當日% | BREAKOUT | slope_20d | slope_60d | RS Score | 法人進場
```

**法人進場欄位**顯示規則：
- `fi_net > 0 AND it_consec_days >= 3` → `外資+投信`（teal 色）
- `it_consec_days >= 3` → `投信 Nd`（N = 天數）
- `fi_net > 0` → `外資買超`
- 均不符合 → 不應出現（已被篩選掉）

### 新增函數

`async function renderTwEarlyStageView()`：
- 取 `/api/tw-early-stage` 或 `${STATIC_BASE}/tw_early_stage.json`
- 渲染 table，邏輯類比 `renderTwBreakoutView()`
- 漲停套白字紅底（與突破觀察一致）

### 修改函數

`loadScores()`：台股分支改用新 state：
```javascript
if (S.twView === 'early')    { renderTwEarlyStageView(); }
else if (S.twView === 'breakout') { renderTwBreakoutView(); }
else                          { await loadTwStrategy(S.twStrategy); }
```

`updateStrategyTabs()`：
```javascript
// tw-view-tabs 永遠顯示（台股模式下）
// tw-strategy-tabs 只在主表（twView === 'stock'）顯示
twStratTabs.style.display = (isTw && S.twView === 'stock') ? 'flex' : 'none';
```

---

## 資料流

```
GitHub Actions / server.py fetch
  ├── compute_tw_rs_scores()        → tw_rs_scores.json  (RS排名主表)
  ├── compute_tw_scores()           → tw_scores.json     (籌碼加乘主表)
  ├── compute_tw_breakout_candidates() → tw_breakout.json (突破觀察)
  └── compute_tw_early_stage_candidates() → tw_early_stage.json (起漲觀察) ← NEW

前端 S.twView
  'stock'   → loadTwStrategy(S.twStrategy)  → 主表
  'breakout' → renderTwBreakoutView()        → 突破觀察 table
  'early'   → renderTwEarlyStageView()       → 起漲觀察 table ← NEW
```

---

## 不在本次範圍內

- 起漲觀察策略參數調整（threshold 微調留待上線後觀察）
- 起漲觀察欄位排序功能（視需求再加）
- 美股起漲觀察（僅台股）
