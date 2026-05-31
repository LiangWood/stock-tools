from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# yfinance 1.x 批次下載有 bug（AttributeError on .lower()），改用單檔平行下載。
_MAX_WORKERS = 20


def fetch_all(
    tickers: list[str],
    period: str = "6mo",
    interval: str = "1d",
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, pd.DataFrame | None]:
    total = len(tickers)
    results: dict[str, pd.DataFrame | None] = {}
    done_count = 0
    lock = threading.Lock()

    def _fetch_one(ticker: str) -> tuple[str, pd.DataFrame | None]:
        try:
            # 用 Ticker.history() 避免 yf.download() 在多線程下的 AttributeError bug
            df = yf.Ticker(ticker).history(
                period=period,
                interval=interval,
                auto_adjust=True,
                raise_errors=False,
            )
            if df is None or df.empty:
                return ticker, None
            # history() 欄位為 Open/High/Low/Close/Volume，無需處理 MultiIndex
            df = df.dropna(how="all")
            return ticker, (df if not df.empty else None)
        except Exception as exc:
            logger.debug("Download failed %s: %s", ticker, exc)
            return ticker, None

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        future_map = {pool.submit(_fetch_one, t): t for t in tickers}
        for future in as_completed(future_map):
            ticker, df = future.result()
            with lock:
                results[ticker] = df
                done_count += 1
                if progress_callback:
                    progress_callback(done_count, total)

    return results
