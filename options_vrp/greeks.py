"""Black-Scholes helpers — just enough to place strikes by delta and price a spread.

yfinance gives us implied vol per strike but not greeks, so we compute the put delta
ourselves (using each strike's own IV, which respects the skew) to locate the ~16-delta
short strike and the further-OTM long strike. Dividends are ignored (q=0) — a small effect
on 1-month near-the-money deltas, fine for a paper MVP.
"""
from __future__ import annotations

from math import log, sqrt, exp
from statistics import NormalDist

_N = NormalDist().cdf


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))


def put_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Put delta in [-1, 0]. A 16-delta short put has delta ≈ -0.16."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return -1.0 if K > S else 0.0
    return -_N(-_d1(S, K, T, r, sigma))


def bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes put price (used only as a sanity fallback when a quote is missing)."""
    if T <= 0:
        return max(K - S, 0.0)
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * sqrt(T)
    return K * exp(-r * T) * _N(-d2) - S * _N(-d1)
