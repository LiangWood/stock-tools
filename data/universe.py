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
