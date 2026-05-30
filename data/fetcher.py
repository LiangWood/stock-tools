from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_BATCH_SIZE  = 100
_MAX_WORKERS = 4   # 並行下載批次數（過高會被 Yahoo Finance rate-limit）


def fetch_all(
    tickers: list[str],
    period: str = "6mo",
    interval: str = "1d",
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, pd.DataFrame | None]:
    total   = len(tickers)
    batches = [tickers[i: i + _BATCH_SIZE] for i in range(0, total, _BATCH_SIZE)]

    results: dict[str, pd.DataFrame | None] = {}
    done_count = 0
    lock = threading.Lock()

    def _fetch_batch(batch: list[str]) -> dict[str, pd.DataFrame | None]:
        try:
            raw = yf.download(
                batch,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
            )
            return _parse(raw, batch)
        except Exception as exc:
            logger.warning("Batch failed: %s", exc)
            return {t: None for t in batch}

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        future_map = {pool.submit(_fetch_batch, b): b for b in batches}
        for future in as_completed(future_map):
            batch_result = future.result()
            with lock:
                results.update(batch_result)
                done_count += len(future_map[future])
                if progress_callback:
                    progress_callback(min(done_count, total), total)

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
        out[tickers[0]] = raw if not raw.empty else None
    return out
