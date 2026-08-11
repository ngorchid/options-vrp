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
    """OTM-relevant put chain for one expiry.

    `openInterest` drives the liquidity screen in `strategy._nearest_delta_strike` — a strike
    nobody holds gets no competitive quote (EEM 2026-08-10: the 1-sigma strike quoted 0.30/0.90
    while BOTH neighbours quoted ~0.10 wide on the SAME mid). `volume` is carried for diagnostics
    only: it is a same-day FLOW that reads 0 early in the session on perfectly liquid strikes,
    whereas open interest is a STOCK and stays meaningful. Note OI is published once daily by the
    OCC overnight, so intraday it is always the previous close — stale for a signal, fine for a
    liquidity screen.
    """
    ch = tk.option_chain(expiry)
    cols = ["strike", "bid", "ask", "lastPrice", "impliedVolatility", "openInterest", "volume"]
    have = [c for c in cols if c in ch.puts.columns]
    out = ch.puts[have].copy()
    # Tolerate a provider that stops returning these: a missing column disables the screen
    # (handled in _nearest_delta_strike) rather than crashing the daily run.
    for c in ("openInterest", "volume"):
        if c not in out.columns:
            out[c] = float("nan")
    return out


def next_earnings(ticker: str) -> pd.Timestamp | None:
    """Next scheduled earnings date, or None if the name has none (ETF) or the lookup fails.

    Uses `Ticker.calendar`, NOT `get_earnings_dates`: the latter returns only HISTORY, so
    filtering it for future dates yields an empty result that reads exactly like "this name has
    no earnings" — a silent failure that would disable the filter on precisely the single names
    it exists for.

    CANNOT distinguish "genuinely no earnings" from "lookup failed" — both return None, and the
    caller must decide. `OptionsConfig.skip_if_earnings_unknown` treats None as PENDING for names
    that are not known ETFs, matching `skip_if_no_quote`: a missed trade costs nothing.
    """
    try:
        cal = yf.Ticker(ticker).calendar or {}
        dates = cal.get("Earnings Date") or []
        if not dates:
            return None
        return pd.Timestamp(min(dates))
    except Exception:  # noqa: BLE001 — any provider failure is "unknown", handled by the caller
        return None
