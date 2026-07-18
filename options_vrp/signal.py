"""The timing/selection signals, validated on the free index study:

  - REGIME gate (VIX/VIX3M < threshold): a market-wide switch. Sell only in contango; stand
    down in backwardation, where short-vol blows up. First-order — the survival mechanism.
  - VRP richness (ATM implied vol − trailing realized vol): pick WHAT to sell. Positive VRP =
    you're overpaid. Uses realized vol from prices, so it needs NO IV history (unlike IV Rank,
    which the daily recorder will bootstrap over ~a year).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def realized_vol(prices: pd.Series, window: int = 20) -> float:
    """Annualised close-to-close realized vol over the last `window` days."""
    r = prices.pct_change().dropna()
    if len(r) < window:
        return float("nan")
    return float(r.iloc[-window:].std() * np.sqrt(252))


def atm_iv(puts: pd.DataFrame, spot: float) -> float:
    """Implied vol of the put strike nearest spot (proxy for at-the-money IV)."""
    valid = puts[(puts["impliedVolatility"] > 0.01) & (puts["impliedVolatility"] < 5)]
    if valid.empty:
        return float("nan")
    i = (valid["strike"] - spot).abs().idxmin()
    return float(valid.loc[i, "impliedVolatility"])


def vrp(iv: float, rv: float) -> float:
    """Variance risk premium in vol points (annualised): implied minus realized."""
    return iv - rv


def regime_open(ratio: float, threshold: float) -> bool:
    return ratio < threshold
