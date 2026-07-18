"""Data access (yfinance) — prices for realized vol, the VIX complex for the regime gate,
and option chains for strike selection. Keeps the "yfinance for data, IB for fills" pattern
of the sibling paper systems, so no IB market-data subscription is needed.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf


def price_history(tickers: list[str], lookback_days: int = 160) -> pd.DataFrame:
    """Adjusted-close panel [date × ticker] for realized-vol estimation."""
    start = (pd.Timestamp.today() - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    raw = yf.download(list(dict.fromkeys(tickers)), start=start, auto_adjust=True,
                      progress=False, threads=True)["Close"]
    if isinstance(raw, pd.Series):
        raw = raw.to_frame(tickers[0])
    return raw.dropna(how="all").ffill()


def regime() -> tuple[float, float, float]:
    """Latest (VIX/VIX3M ratio, VIX level, VIX3M level). Ratio < 1 = contango (calm)."""
    raw = yf.download(["^VIX", "^VIX3M"], period="1mo", auto_adjust=True,
                      progress=False)["Close"].dropna()
    vix, vix3m = float(raw["^VIX"].iloc[-1]), float(raw["^VIX3M"].iloc[-1])
    return vix / vix3m, vix, vix3m


def option_expiries(ticker: str) -> tuple[object, tuple[str, ...]]:
    """Return (yfinance Ticker, tuple of available expiry strings 'YYYY-MM-DD')."""
    tk = yf.Ticker(ticker)
    return tk, tuple(tk.options or ())


def puts(tk, expiry: str) -> pd.DataFrame:
    """OTM-relevant put chain for one expiry: strike, bid, ask, lastPrice, impliedVolatility."""
    ch = tk.option_chain(expiry)
    cols = ["strike", "bid", "ask", "lastPrice", "impliedVolatility"]
    return ch.puts[cols].copy()
