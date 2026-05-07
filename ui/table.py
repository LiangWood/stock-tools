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
_TW_RED   = "#ff5252"   # 台股漲（紅）
_TW_GREEN = "#00c853"   # 台股跌（綠）


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
            self._tree.tag_configure("tw_up",   foreground=_TW_RED)
            self._tree.tag_configure("tw_down", foreground=_TW_GREEN)
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
            tag = "tw_up" if row.day_return > 0 else "tw_down"
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
                f"{row.fi_net/1e8:+.2f}",
                f"{row.it_net/1e8:+.2f}",
                f"{row.margin_chg:+,}",
                f"{row.rsi:.1f}",
            ), tags=(tag,))

    def update_data(self, df: pd.DataFrame):
        self._df = df
        self._render(df)
