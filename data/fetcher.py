from __future__ import annotations

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
