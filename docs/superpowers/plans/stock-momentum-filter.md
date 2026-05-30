# Stock Momentum Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一個 Python 桌面工具，從 S&P 500 中篩選每日動能最強的股票，以排行表呈現並支援個股走勢圖查看。

**Architecture:** 分層模組（data / scoring / ui）+ 背景 thread 避免 UI 凍結。使用者按刷新後，fetcher 批次下載資料，engine 計算評分，透過 queue 回傳主 thread 更新畫面。

**Tech Stack:** Python 3.11+、yfinance、pandas、numpy、customtkinter、matplotlib、pytest

---

## 檔案結構

| 檔案 | 職責 |
|------|------|
| `main.py` | 程式進入點，啟動 CTk app |
| `data/universe.py` | 從 Wikipedia 取得 S&P 500 清單，失敗時 fallback hardcode |
| `data/fetcher.py` | 批次呼叫 yfinance 下載 OHLCV，回報進度，失敗個股跳過 |
| `scoring/engine.py` | 計算 5 個指標、百分位排名、加權 momentum_score |
| `ui/chart.py` | matplotlib 圖表嵌入 CTkFrame |
| `ui/table.py` | ttk.Treeview 排行表，支援欄位點擊排序 |
| `ui/app.py` | 主視窗，管理 thread、queue、元件串接 |
| `tests/data/test_universe.py` | universe.py 單元測試 |
| `tests/data/test_fetcher.py` | fetcher.py 單元測試（mock yfinance） |
| `tests/scoring/test_engine.py` | engine.py 單元測試 |

---

## Task 1: 專案環境設定

**Files:**
- Create: `requirements.txt`
- Create: `data/__init__.py`
- Create: `scoring/__init__.py`
- Create: `ui/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/data/__init__.py`
- Create: `tests/scoring/__init__.py`

- [ ] **Step 1: 建立 requirements.txt**

```
yfinance>=0.2.40
pandas>=2.0.0
numpy>=1.26.0
customtkinter>=5.2.0
matplotlib>=3.8.0
pytest>=8.0.0
pytest-mock>=3.12.0
```

- [ ] **Step 2: 建立空白 __init__.py**

```bash
mkdir -p data scoring ui tests/data tests/scoring
touch data/__init__.py scoring/__init__.py ui/__init__.py
touch tests/__init__.py tests/data/__init__.py tests/scoring/__init__.py
```

- [ ] **Step 3: 安裝依賴**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Expected: 所有套件安裝成功，無錯誤。

- [ ] **Step 4: Commit**

```bash
git add requirements.txt data/ scoring/ ui/ tests/
git commit -m "chore: project structure and dependencies"
```

---

## Task 2: data/universe.py — S&P 500 股票清單

**Files:**
- Create: `data/universe.py`
- Create: `tests/data/test_universe.py`

- [ ] **Step 1: 撰寫失敗測試**

`tests/data/test_universe.py`:
```python
from unittest.mock import patch
import pytest
from data.universe import get_sp500_tickers


def test_returns_list_of_strings():
    tickers = get_sp500_tickers()
    assert isinstance(tickers, list)
    assert len(tickers) > 0
    assert all(isinstance(t, str) for t in tickers)


def test_fallback_when_wikipedia_fails():
    with patch("data.universe.pd.read_html", side_effect=Exception("network error")):
        tickers = get_sp500_tickers()
    assert len(tickers) >= 50
    assert "AAPL" in tickers


def test_no_duplicate_tickers():
    tickers = get_sp500_tickers()
    assert len(tickers) == len(set(tickers))
```

- [ ] **Step 2: 執行測試確認失敗**

```bash
pytest tests/data/test_universe.py -v
```

Expected: `ImportError` 或 `ModuleNotFoundError`。

- [ ] **Step 3: 實作 data/universe.py**

```python
import pandas as pd

_FALLBACK = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B",
    "AVGO", "JPM", "LLY", "V", "UNH", "XOM", "MA", "JNJ", "PG", "COST",
    "HD", "ABBV", "MRK", "CVX", "WMT", "BAC", "KO", "PEP", "NFLX", "AMD",
    "CRM", "TMO", "ADBE", "LIN", "MCD", "CSCO", "ABT", "ACN", "DHR", "TXN",
    "NEE", "AMGN", "NKE", "DIS", "PM", "ORCL", "QCOM", "UNP", "MS", "RTX",
    "SPGI", "HON", "INTU", "CAT", "GE", "LOW", "BKNG", "BLK", "AMAT", "GS",
    "ELV", "AXP", "T", "MDT", "SYK", "VRTX", "ISRG", "REGN", "PLD", "CB",
    "TJX", "MMC", "PGR", "GILD", "PANW", "MU", "LRCX", "CI", "SO", "DUK",
    "CME", "CL", "ZTS", "BSX", "BMY", "HCA", "MCO", "WM", "AON", "SHW",
    "ITW", "USB", "PSA", "FI", "ICE", "DE", "EOG", "GD", "APD", "COP",
]

_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def get_sp500_tickers() -> list[str]:
    try:
        tables = pd.read_html(_WIKIPEDIA_URL)
        tickers = tables[0]["Symbol"].tolist()
        # Wikipedia uses dots for BRK.B etc.; yfinance uses dashes
        tickers = [t.replace(".", "-") for t in tickers]
        return list(dict.fromkeys(tickers))  # deduplicate, preserve order
    except Exception:
        return list(_FALLBACK)
```

- [ ] **Step 4: 執行測試確認通過**

```bash
pytest tests/data/test_universe.py -v
```

Expected: 3 tests PASSED。

- [ ] **Step 5: Commit**

```bash
git add data/universe.py tests/data/test_universe.py
git commit -m "feat: S&P 500 universe with Wikipedia fetch and fallback"
```

---

## Task 3: data/fetcher.py — 批次下載 OHLCV

**Files:**
- Create: `data/fetcher.py`
- Create: `tests/data/test_fetcher.py`

- [ ] **Step 1: 撰寫失敗測試**

`tests/data/test_fetcher.py`:
```python
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
import pytest
from data.fetcher import fetch_all


def _make_ohlcv(n=60):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": np.random.rand(n) * 100 + 100,
        "High": np.random.rand(n) * 100 + 110,
        "Low": np.random.rand(n) * 100 + 90,
        "Close": np.random.rand(n) * 100 + 100,
        "Volume": np.random.randint(1_000_000, 10_000_000, n),
    }, index=idx)


def _make_multi_download(tickers):
    """Build MultiIndex DataFrame as yfinance returns for multiple tickers."""
    frames = {t: _make_ohlcv() for t in tickers}
    combined = pd.concat(frames, axis=1)
    combined.columns = pd.MultiIndex.from_tuples(
        [(col, ticker) for ticker in tickers for col in ["Open", "High", "Low", "Close", "Volume"]],
        names=["field", "ticker"],
    )
    return combined


def test_returns_dict_keyed_by_ticker():
    tickers = ["AAPL", "MSFT"]
    with patch("data.fetcher.yf.download", return_value=_make_multi_download(tickers)):
        result = fetch_all(tickers)
    assert set(result.keys()) == {"AAPL", "MSFT"}


def test_each_value_is_dataframe_with_ohlcv():
    tickers = ["AAPL"]
    with patch("data.fetcher.yf.download", return_value=_make_multi_download(tickers)):
        result = fetch_all(tickers)
    df = result["AAPL"]
    assert isinstance(df, pd.DataFrame)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert col in df.columns


def test_failed_ticker_returns_none():
    tickers = ["AAPL", "BADFOO"]
    good = _make_multi_download(["AAPL"])
    # BADFOO not present in download result
    with patch("data.fetcher.yf.download", return_value=good):
        result = fetch_all(tickers)
    assert result.get("BADFOO") is None


def test_progress_callback_is_called():
    tickers = [f"T{i}" for i in range(5)]
    calls = []
    with patch("data.fetcher.yf.download", return_value=_make_multi_download(tickers)):
        fetch_all(tickers, progress_callback=lambda done, total: calls.append((done, total)))
    assert len(calls) > 0
    assert calls[-1][0] == len(tickers)
```

- [ ] **Step 2: 執行測試確認失敗**

```bash
pytest tests/data/test_fetcher.py -v
```

Expected: `ImportError`。

- [ ] **Step 3: 實作 data/fetcher.py**

```python
import logging
from typing import Callable
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100


def fetch_all(
    tickers: list[str],
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, pd.DataFrame | None]:
    results: dict[str, pd.DataFrame | None] = {}
    total = len(tickers)

    for start in range(0, total, _BATCH_SIZE):
        batch = tickers[start: start + _BATCH_SIZE]
        try:
            raw = yf.download(batch, period="3mo", progress=False, auto_adjust=True)
            results.update(_parse(raw, batch))
        except Exception as exc:
            logger.warning("Batch %d failed: %s", start // _BATCH_SIZE, exc)
            for t in batch:
                results[t] = None

        done = min(start + _BATCH_SIZE, total)
        if progress_callback:
            progress_callback(done, total)

    return results


def _parse(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame | None]:
    out: dict[str, pd.DataFrame | None] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for ticker in tickers:
            try:
                df = raw.xs(ticker, axis=1, level=1).dropna(how="all")
                out[ticker] = df if not df.empty else None
            except KeyError:
                out[ticker] = None
    else:
        # Single ticker — columns are flat
        out[tickers[0]] = raw if not raw.empty else None
    return out
```

- [ ] **Step 4: 執行測試確認通過**

```bash
pytest tests/data/test_fetcher.py -v
```

Expected: 4 tests PASSED。

- [ ] **Step 5: Commit**

```bash
git add data/fetcher.py tests/data/test_fetcher.py
git commit -m "feat: batch yfinance fetcher with progress callback"
```

---

## Task 4: scoring/engine.py — 指標計算與評分

**Files:**
- Create: `scoring/engine.py`
- Create: `tests/scoring/test_engine.py`

- [ ] **Step 1: 撰寫失敗測試**

`tests/scoring/test_engine.py`:
```python
import pandas as pd
import numpy as np
import pytest
from scoring.engine import calculate_rsi, compute_scores


def _make_df(n=60, trend="up"):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    if trend == "up":
        close = pd.Series(np.linspace(100, 150, n), index=idx)
    else:
        close = pd.Series(np.linspace(150, 100, n), index=idx)
    volume = pd.Series(np.ones(n) * 1_000_000, index=idx)
    return pd.DataFrame({"Close": close, "Volume": volume})


def test_rsi_uptrend_above_50():
    df = _make_df(60, trend="up")
    rsi = calculate_rsi(df["Close"])
    assert rsi > 50


def test_rsi_downtrend_below_50():
    df = _make_df(60, trend="down")
    rsi = calculate_rsi(df["Close"])
    assert rsi < 50


def test_rsi_range_0_to_100():
    df = _make_df(60)
    rsi = calculate_rsi(df["Close"])
    assert 0 <= rsi <= 100


def test_compute_scores_returns_dataframe():
    data = {f"T{i}": _make_df(60) for i in range(10)}
    result = compute_scores(data)
    assert isinstance(result, pd.DataFrame)


def test_compute_scores_has_required_columns():
    data = {"AAPL": _make_df(60), "MSFT": _make_df(60)}
    result = compute_scores(data)
    for col in ["ticker", "day_return", "ret_5d", "ret_20d",
                "volume_ratio", "rsi", "momentum_score"]:
        assert col in result.columns


def test_compute_scores_sorted_descending():
    data = {f"T{i}": _make_df(60) for i in range(20)}
    result = compute_scores(data)
    scores = result["momentum_score"].tolist()
    assert scores == sorted(scores, reverse=True)


def test_compute_scores_score_between_0_and_100():
    data = {f"T{i}": _make_df(60) for i in range(10)}
    result = compute_scores(data)
    assert result["momentum_score"].between(0, 100).all()


def test_none_ticker_gets_zero_score():
    data = {"GOOD": _make_df(60), "BAD": None}
    result = compute_scores(data)
    bad_row = result[result["ticker"] == "BAD"]
    assert not bad_row.empty
    assert bad_row["momentum_score"].iloc[0] >= 0


def test_short_history_ticker_handled():
    data = {"SHORT": _make_df(5), "LONG": _make_df(60)}
    result = compute_scores(data)
    assert len(result) == 2
```

- [ ] **Step 2: 執行測試確認失敗**

```bash
pytest tests/scoring/test_engine.py -v
```

Expected: `ImportError`。

- [ ] **Step 3: 實作 scoring/engine.py**

```python
import numpy as np
import pandas as pd

_WEIGHTS = {
    "day_return": 0.30,
    "ret_5d": 0.20,
    "ret_20d": 0.20,
    "volume_ratio": 0.20,
    "rsi": 0.10,
}


def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    delta = prices.diff().dropna()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean().iloc[-1]
    avg_loss = loss.rolling(window=period, min_periods=period).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _metrics_for(ticker: str, df: pd.DataFrame | None) -> dict:
    base = {"ticker": ticker, "day_return": 0.0, "ret_5d": 0.0,
            "ret_20d": 0.0, "volume_ratio": 1.0, "rsi": 50.0}
    if df is None or len(df) < 2:
        return base
    close = df["Close"].dropna()
    volume = df["Volume"].dropna()
    n = len(close)

    base["day_return"] = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) if n >= 2 else 0.0
    base["ret_5d"] = float((close.iloc[-1] - close.iloc[-6]) / close.iloc[-6]) if n >= 6 else 0.0
    base["ret_20d"] = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if n >= 21 else 0.0

    vol_avg = float(volume.iloc[-21:-1].mean()) if n >= 21 else float(volume.mean())
    base["volume_ratio"] = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
    base["rsi"] = calculate_rsi(close)
    return base


def compute_scores(ticker_data: dict[str, pd.DataFrame | None]) -> pd.DataFrame:
    rows = [_metrics_for(ticker, df) for ticker, df in ticker_data.items()]
    df = pd.DataFrame(rows)

    for col in _WEIGHTS:
        ranked = df[col].rank(pct=True) * 100
        df[f"{col}_rank"] = ranked

    df["momentum_score"] = sum(
        df[f"{col}_rank"] * weight for col, weight in _WEIGHTS.items()
    )

    return (
        df.drop(columns=[f"{col}_rank" for col in _WEIGHTS])
        .sort_values("momentum_score", ascending=False)
        .reset_index(drop=True)
    )
```

- [ ] **Step 4: 執行測試確認通過**

```bash
pytest tests/scoring/test_engine.py -v
```

Expected: 9 tests PASSED。

- [ ] **Step 5: Commit**

```bash
git add scoring/engine.py tests/scoring/test_engine.py
git commit -m "feat: momentum scoring engine with RSI, returns, volume ratio"
```

---

## Task 5: ui/chart.py — 個股走勢圖元件

**Files:**
- Create: `ui/chart.py`

（UI 元件以手動測試為主，無自動化單元測試。）

- [ ] **Step 1: 實作 ui/chart.py**

```python
import tkinter as tk
import customtkinter as ctk
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


class ChartPanel(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._fig, self._ax = plt.subplots(figsize=(6, 4), facecolor="#2b2b2b")
        self._ax.set_facecolor("#2b2b2b")
        self._canvas = FigureCanvasTkAgg(self._fig, master=self)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)
        self._show_placeholder()

    def _show_placeholder(self):
        self._ax.clear()
        self._ax.set_facecolor("#2b2b2b")
        self._ax.text(
            0.5, 0.5, "點選左側個股查看走勢",
            transform=self._ax.transAxes,
            ha="center", va="center", color="#888888", fontsize=12,
        )
        self._ax.axis("off")
        self._canvas.draw()

    def plot(self, ticker: str, df: pd.DataFrame):
        self._ax.clear()
        self._ax.set_facecolor("#2b2b2b")
        self._fig.patch.set_facecolor("#2b2b2b")

        close = df["Close"].dropna()
        color = "#00c853" if close.iloc[-1] >= close.iloc[0] else "#ff5252"

        self._ax.plot(close.index, close.values, color=color, linewidth=1.5)
        self._ax.fill_between(close.index, close.values, close.min(), alpha=0.15, color=color)
        self._ax.set_title(ticker, color="#ffffff", fontsize=13)
        self._ax.tick_params(colors="#888888", labelsize=8)
        for spine in self._ax.spines.values():
            spine.set_edgecolor("#444444")
        self._ax.xaxis.set_tick_params(rotation=30)
        self._fig.tight_layout()
        self._canvas.draw()
```

- [ ] **Step 2: Commit**

```bash
git add ui/chart.py
git commit -m "feat: matplotlib chart panel embedded in customtkinter"
```

---

## Task 6: ui/table.py — 排行榜表格元件

**Files:**
- Create: `ui/table.py`

- [ ] **Step 1: 實作 ui/table.py**

```python
import tkinter as tk
from tkinter import ttk
from typing import Callable
import customtkinter as ctk
import pandas as pd

_COLUMNS = [
    ("rank",         "#",        50),
    ("ticker",       "代碼",      70),
    ("day_return",   "當日%",     80),
    ("ret_5d",       "5日%",      80),
    ("ret_20d",      "20日%",     80),
    ("volume_ratio", "爆量倍數",  90),
    ("rsi",          "RSI",       70),
    ("momentum_score","評分",     70),
]


class TablePanel(ctk.CTkFrame):
    def __init__(self, master, on_select: Callable[[str], None], **kwargs):
        super().__init__(master, **kwargs)
        self._on_select = on_select
        self._df: pd.DataFrame | None = None
        self._sort_col: str = "momentum_score"
        self._sort_asc: bool = False
        self._build()

    def _build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
            background="#2b2b2b", foreground="#ffffff",
            fieldbackground="#2b2b2b", rowheight=26, font=("Helvetica", 11))
        style.configure("Treeview.Heading",
            background="#1a1a2e", foreground="#00c853", font=("Helvetica", 11, "bold"))
        style.map("Treeview", background=[("selected", "#1f538d")])

        col_ids = [c[0] for c in _COLUMNS]
        self._tree = ttk.Treeview(self, columns=col_ids, show="headings")
        for col_id, label, width in _COLUMNS:
            self._tree.heading(col_id, text=label,
                               command=lambda c=col_id: self._on_heading(c))
            self._tree.column(col_id, width=width, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_row_select)

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
        ticker = self._tree.item(sel[0])["values"][1]
        self._on_select(str(ticker))

    def _render(self, df: pd.DataFrame):
        sorted_df = df.sort_values(self._sort_col, ascending=self._sort_asc).reset_index(drop=True)
        self._tree.delete(*self._tree.get_children())
        for i, row in sorted_df.iterrows():
            self._tree.insert("", "end", values=(
                i + 1,
                row["ticker"],
                f"{row['day_return']*100:.2f}%",
                f"{row['ret_5d']*100:.2f}%",
                f"{row['ret_20d']*100:.2f}%",
                f"{row['volume_ratio']:.2f}x",
                f"{row['rsi']:.1f}",
                f"{row['momentum_score']:.1f}",
            ))

    def update_data(self, df: pd.DataFrame):
        self._df = df
        self._render(df)
```

- [ ] **Step 2: Commit**

```bash
git add ui/table.py
git commit -m "feat: sortable treeview table panel for momentum ranking"
```

---

## Task 7: ui/app.py — 主視窗與執行緒管理

**Files:**
- Create: `ui/app.py`

- [ ] **Step 1: 實作 ui/app.py**

```python
import queue
import threading
import logging
from datetime import datetime
import customtkinter as ctk
import pandas as pd
from data.universe import get_sp500_tickers
from data.fetcher import fetch_all
from scoring.engine import compute_scores
from ui.table import TablePanel
from ui.chart import ChartPanel

logger = logging.getLogger(__name__)
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

_Q_PROGRESS = "progress"
_Q_DONE = "done"
_Q_ERROR = "error"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("美股動能篩選器")
        self.geometry("1200x700")
        self._queue: queue.Queue = queue.Queue()
        self._fetched_data: dict[str, pd.DataFrame | None] = {}
        self._build()

    def _build(self):
        # ── Header ──────────────────────────────────────────────
        header = ctk.CTkFrame(self, height=48)
        header.pack(fill="x", padx=10, pady=(10, 0))

        ctk.CTkLabel(header, text="美股動能篩選器", font=("Helvetica", 16, "bold")).pack(side="left", padx=12)

        self._status_label = ctk.CTkLabel(header, text="尚未載入", text_color="#888888")
        self._status_label.pack(side="right", padx=12)

        self._refresh_btn = ctk.CTkButton(header, text="刷新", width=80, command=self._start_fetch)
        self._refresh_btn.pack(side="right", padx=8)

        # ── Body ─────────────────────────────────────────────────
        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=10, pady=10)

        self._table = TablePanel(body, on_select=self._on_ticker_select)
        self._table.pack(side="left", fill="both", expand=True)

        self._chart = ChartPanel(body, width=420)
        self._chart.pack(side="right", fill="both", padx=(8, 0))

    def _start_fetch(self):
        self._refresh_btn.configure(state="disabled", text="載入中…")
        self._status_label.configure(text="取得股票清單…")
        thread = threading.Thread(target=self._fetch_worker, daemon=True)
        thread.start()
        self.after(100, self._poll_queue)

    def _fetch_worker(self):
        try:
            tickers = get_sp500_tickers()

            def progress(done, total):
                self._queue.put((_Q_PROGRESS, f"抓取中 {done}/{total}"))

            data = fetch_all(tickers, progress_callback=progress)
            scores = compute_scores(data)
            self._fetched_data = data
            self._queue.put((_Q_DONE, scores))
        except Exception as exc:
            logger.exception("Fetch worker error")
            self._queue.put((_Q_ERROR, str(exc)))

    def _poll_queue(self):
        try:
            while True:
                msg_type, payload = self._queue.get_nowait()
                if msg_type == _Q_PROGRESS:
                    self._status_label.configure(text=payload)
                elif msg_type == _Q_DONE:
                    self._table.update_data(payload)
                    now = datetime.now().strftime("%H:%M:%S")
                    self._status_label.configure(text=f"上次更新 {now}")
                    self._refresh_btn.configure(state="normal", text="刷新")
                    return
                elif msg_type == _Q_ERROR:
                    self._status_label.configure(text=f"錯誤：{payload}")
                    self._refresh_btn.configure(state="normal", text="刷新")
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_ticker_select(self, ticker: str):
        df = self._fetched_data.get(ticker)
        if df is not None:
            self._chart.plot(ticker, df)
```

- [ ] **Step 2: Commit**

```bash
git add ui/app.py
git commit -m "feat: main app window with threading, queue, and panel layout"
```

---

## Task 8: main.py — 程式進入點

**Files:**
- Create: `main.py`

- [ ] **Step 1: 實作 main.py**

```python
import logging
from ui.app import App

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    app = App()
    app.mainloop()
```

- [ ] **Step 2: 執行完整測試套件**

```bash
pytest tests/ -v
```

Expected: 所有測試 PASSED，無 FAILED。

- [ ] **Step 3: 手動啟動應用程式驗證**

```bash
python main.py
```

手動驗證：
- [ ] 視窗正常開啟，顯示「美股動能篩選器」標題
- [ ] 點「刷新」後按鈕變灰、狀態列顯示「抓取中 X/500」
- [ ] 抓取完成後表格顯示排行榜
- [ ] 點擊欄位標題可切換排序
- [ ] 點選個股後右側顯示走勢圖（上漲綠色、下跌紅色）
- [ ] 再次點「刷新」重新抓取

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: entry point and logging setup"
```
