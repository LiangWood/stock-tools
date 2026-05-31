import matplotlib
import matplotlib.pyplot as plt
import customtkinter as ctk
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# CTkFrame internally uses self._canvas; use self._mpl_canvas to avoid collision
matplotlib.rcParams["font.family"] = ["Heiti TC", "STHeiti", "PingFang SC", "DejaVu Sans"]

_BG = "#060b12"
_MUTED = "#5e7490"
_WHITE = "#dde6f0"
_BORDER = "#162135"
_GREEN = "#22c55e"
_RED = "#ef4444"
_EMA_COLORS = {20: "#f59e0b", 50: "#38bdf8", 120: "#c084fc"}


class ChartPanel(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._ticker: str = ""
        self._df: pd.DataFrame | None = None
        self._period: str = "D"
        self._ema_on: dict = {20: False, 50: False, 120: False}
        self._build_controls()
        self._build_chart()
        self.after(200, self._show_placeholder)

    def _build_controls(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=6, pady=(6, 2))

        self._period_btn = ctk.CTkSegmentedButton(
            bar, values=["日K", "週K"],
            command=self._on_period_change,
            width=110, height=26,
        )
        self._period_btn.set("日K")
        self._period_btn.pack(side="left")

        for span in reversed([20, 50, 120]):
            cb = ctk.CTkCheckBox(
                bar, text=f"EMA{span}",
                checkmark_color=_EMA_COLORS[span],
                border_color=_EMA_COLORS[span],
                fg_color=_EMA_COLORS[span],
                hover_color=_EMA_COLORS[span],
                command=lambda s=span: self._toggle_ema(s),
                width=80, height=26,
            )
            cb.pack(side="right", padx=2)

    def _build_chart(self):
        self._fig, self._ax = plt.subplots(figsize=(6, 3.6), facecolor=_BG)
        self._mpl_canvas = FigureCanvasTkAgg(self._fig, master=self)
        self._mpl_canvas.get_tk_widget().pack(fill="both", expand=True)

    def _on_period_change(self, value: str):
        self._period = "W" if value == "週K" else "D"
        if self._df is not None:
            self._render()

    def _toggle_ema(self, span: int):
        self._ema_on[span] = not self._ema_on[span]
        if self._df is not None:
            self._render()

    def _reset_ax(self):
        self._ax.clear()
        self._ax.set_facecolor(_BG)
        self._fig.patch.set_facecolor(_BG)

    def _show_placeholder(self):
        self._reset_ax()
        self._ax.text(
            0.5, 0.5, "點選左側個股查看走勢",
            transform=self._ax.transAxes,
            ha="center", va="center", color=_MUTED, fontsize=12,
        )
        self._ax.axis("off")
        self._mpl_canvas.draw()

    def _get_ohlc(self) -> pd.DataFrame:
        if self._period == "W":
            return self._df.resample("W").agg(
                Open=("Open", "first"),
                High=("High", "max"),
                Low=("Low", "min"),
                Close=("Close", "last"),
            ).dropna()
        return self._df[["Open", "High", "Low", "Close"]].dropna()

    def _render(self):
        self._reset_ax()
        ohlc = self._get_ohlc()

        for i, (_, row) in enumerate(ohlc.iterrows()):
            o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
            color = _GREEN if c >= o else _RED
            self._ax.vlines(i, l, h, color=color, linewidth=0.8)
            self._ax.bar(i, abs(c - o), bottom=min(o, c), color=color, width=0.6, linewidth=0)

        close = ohlc["Close"]
        for span, on in self._ema_on.items():
            if on and len(close) >= span:
                ema = close.ewm(span=span, adjust=False).mean()
                self._ax.plot(range(len(ema)), ema.values,
                              color=_EMA_COLORS[span], linewidth=1.2, label=f"EMA{span}")

        if any(self._ema_on.values()):
            self._ax.legend(facecolor=_BG, edgecolor=_BORDER,
                            labelcolor=_WHITE, fontsize=8, loc="upper left")

        step = max(1, len(ohlc) // 8)
        ticks = list(range(0, len(ohlc), step))
        fmt = "%m/%d" if self._period == "D" else "%y/%m"
        self._ax.set_xticks(ticks)
        self._ax.set_xticklabels([ohlc.index[i].strftime(fmt) for i in ticks], rotation=30)

        self._ax.set_title(self._ticker, color=_WHITE, fontsize=13)
        self._ax.tick_params(colors=_MUTED, labelsize=8)
        for spine in self._ax.spines.values():
            spine.set_edgecolor(_BORDER)
        self._fig.tight_layout()
        self._mpl_canvas.draw()

    def plot(self, ticker: str, df: pd.DataFrame):
        self._ticker = ticker
        self._df = df
        self._render()

    def destroy(self):
        plt.close(self._fig)
        super().destroy()
