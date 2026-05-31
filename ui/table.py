from tkinter import ttk
from typing import Callable, Optional
import customtkinter as ctk
import pandas as pd

_COLUMNS = [
    ("rank",           "#",           45),
    ("ticker",         "代碼",         65),
    ("price",          "現價",         75),
    ("day_return",     "當日%",        72),
    ("vol_1d20d",      "量比日/20",    82),
    ("ret_10d",        "10日%",        72),
    ("ret_20d",        "20日%",        72),
    ("vol_ratio",      "量比5/20",     82),
    ("rs_vs_spy",      "RS/SPY(1M)",   88),
    ("rs_5d_vs_spy",   "RS/SPY(5D)",   88),
    ("rsi",            "RSI",          58),
    ("pe",             "P/E",          60),
    ("peg_ratio",      "PEG",          60),
    ("breakout_score", "BRE",          60),
    ("momentum_score", "SCORE",        65),
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

_BG          = "#060b12"
_BG_EVEN     = "#080f18"
_FG          = "#dde6f0"
_HEADING_BG  = "#0d1421"
_HEADING_FG  = "#5e7490"
_SELECTED_BG = "#0a1f18"
_ACCENT      = "#00d4aa"
_MUTED_FG    = "#5e7490"
_UP          = "#22c55e"
_DOWN        = "#ef4444"
_GOLD        = "#f59e0b"
_TW_RED        = "#ef4444"   # 台股漲（紅）
_TW_GREEN      = "#22c55e"   # 台股跌（綠）
_TW_LIMIT_UP   = "#7f1d1d"   # 漲停背景（深紅）
_TW_LIMIT_DOWN = "#14532d"   # 跌停背景（深綠）


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
            fieldbackground=_BG, rowheight=28, font=("Helvetica", 11))
        style.configure("Treeview.Heading",
            background=_HEADING_BG, foreground=_HEADING_FG,
            font=("Helvetica", 10, "bold"), relief="flat")
        style.map("Treeview",
            background=[("selected", _SELECTED_BG)],
            foreground=[("selected", _ACCENT)])
        style.configure("Vertical.TScrollbar",
            background=_HEADING_BG, troughcolor=_BG, bordercolor=_BG, arrowcolor=_MUTED_FG)
        style.configure("Horizontal.TScrollbar",
            background=_HEADING_BG, troughcolor=_BG, bordercolor=_BG, arrowcolor=_MUTED_FG)

        col_ids = [c[0] for c in columns]
        self._tree = ttk.Treeview(self, columns=col_ids, show="headings")
        self._tree.tag_configure("even", background=_BG_EVEN)
        self._tree.tag_configure("tw_up",         foreground=_TW_RED)
        self._tree.tag_configure("tw_down",        foreground=_TW_GREEN)
        self._tree.tag_configure("even_tw_up",     background=_BG_EVEN, foreground=_TW_RED)
        self._tree.tag_configure("even_tw_down",   background=_BG_EVEN, foreground=_TW_GREEN)
        self._tree.tag_configure("tw_limit_up",    background=_TW_LIMIT_UP,   foreground=_TW_RED)
        self._tree.tag_configure("tw_limit_down",  background=_TW_LIMIT_DOWN, foreground=_TW_GREEN)
        for col_id, label, width in columns:
            self._tree.heading(col_id, text=label,
                               command=lambda c=col_id: self._on_heading(c))
            self._tree.column(col_id, width=width, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
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
        def _opt(row, col, fmt):
            v = getattr(row, col, None)
            try:
                if v is None or pd.isna(v):
                    return "—"
            except Exception:
                pass
            return fmt % v

        for row in df.itertuples():
            tag = "even" if row.Index % 2 == 0 else ""
            self._tree.insert("", "end", values=(
                row.Index + 1,
                row.ticker,
                f"${row.price:.2f}",
                f"{row.day_return*100:.2f}%",
                f"{row.vol_1d20d:.2f}x",
                f"{row.ret_10d*100:.2f}%",
                f"{row.ret_20d*100:.2f}%",
                f"{row.vol_ratio:.2f}x",
                f"{row.rs_vs_spy*100:.1f}%",
                f"{row.rs_5d_vs_spy*100:.1f}%",
                f"{row.rsi:.1f}",
                _opt(row, "pe",        "%.1f"),
                _opt(row, "peg_ratio", "%.2f"),
                f"{row.breakout_score:.1f}",
                f"{row.momentum_score:.1f}",
            ), tags=(tag,) if tag else ())

    def _render_tw(self, df: pd.DataFrame):
        for row in df.itertuples():
            pe_str = f"{row.pe:.1f}" if pd.notna(row.pe) else "—"
            dr = row.day_return
            is_up = dr > 0
            limit_up   = dr >=  0.097   # 漲停（台股因價格精度約 9.7–10%）
            limit_down = dr <= -0.097   # 跌停

            if limit_up:
                tag = "tw_limit_up"
            elif limit_down:
                tag = "tw_limit_down"
            elif is_up:
                tag = "even_tw_up" if row.Index % 2 == 0 else "tw_up"
            else:
                tag = "even_tw_down" if row.Index % 2 == 0 else "tw_down"

            self._tree.insert("", "end", values=(
                row.Index + 1,
                row.ticker,
                getattr(row, "name", ""),
                f"NT${row.price:.1f}",
                f"{dr*100:.2f}%",
                f"{row.ret_20d*100:.2f}%",
                f"{row.volume:,}",
                f"{row.amount_ratio:.2f}x",
                pe_str,
                f"{row.fi_net/1e8:+.2f}",
                f"{row.it_net/1e8:+.2f}",
                f"{row.margin_chg:+,}",
                f"{row.rsi:.1f}",
            ), tags=(tag,))

    def update_data(self, df: pd.DataFrame):
        self._df = df
        self._render(df)
