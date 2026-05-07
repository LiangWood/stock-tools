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
        self._fetched_data = {}

    def _start_fetch(self):
        universe = self._universe_btn.get()
        self._refresh_btn.configure(state="disabled", text="載入中…")
        self._universe_btn.configure(state="disabled")
        self._status_label.configure(text="取得股票清單…")
        thread = threading.Thread(target=self._fetch_worker, args=(universe,), daemon=True)
        thread.start()
        self._poll_id = self.after(100, self._poll_queue)

    def _fetch_worker(self, universe: str):
        try:
            def progress(done, total):
                self._queue.put((_Q_PROGRESS, f"抓取中 {done}/{total}"))

            if universe == "台股":
                data = fetch_tw_all(progress_callback=progress)
                scores = compute_tw_scores(data)
                if scores.empty:
                    self._queue.put((_Q_ERROR, "台股資料取得失敗，請稍後再試"))
                    return
                ohlcv_data = {
                    ticker: d.get("ohlcv") for ticker, d in data.items() if d is not None
                }
            else:
                tickers = _US_UNIVERSES[universe]()
                data = fetch_all(tickers, progress_callback=progress)
                scores = compute_scores(data)
                ohlcv_data = data

            self._queue.put((_Q_DONE, (scores, ohlcv_data)))
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
                scores, ohlcv_data = payload
                self._fetched_data = ohlcv_data
                self._table.update_data(scores)
                now = datetime.now().strftime("%H:%M:%S")
                self._status_label.configure(text=f"上次更新 {now}")
                self._reset_btn()
                return
            elif msg_type == _Q_ERROR:
                self._fetched_data = {}
                self._status_label.configure(text=f"錯誤：{payload}")
                self._reset_btn()
                return
        self._poll_id = self.after(100, self._poll_queue)

    def _reset_btn(self):
        self._refresh_btn.configure(state="normal", text="刷新")
        self._universe_btn.configure(state="normal")

    def _on_close(self):
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
        self.destroy()

    def _on_ticker_select(self, ticker: str):
        df = self._fetched_data.get(ticker)
        if df is not None:
            self._chart.plot(ticker, df)
