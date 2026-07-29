"""Basket diversification screen — find liquid, high-VRP names OUTSIDE mega-cap tech.

The current single-name basket (AAPL/NVDA/TSLA/AMD/META) is all mega-cap tech → a tech-wide
vol spike hits all five at once. This screens a cross-sector candidate list on the two things
that matter for premium selling:
  - VRP richness: ATM implied vol (nearest ~35 DTE) minus 20d realized vol — is the premium fat?
  - Diversification: correlation of daily returns to the existing tech single-names — is it new risk?

Liquidity (tight option chains) is the hard constraint we CAN'T measure from free data, so the
candidate list is pre-curated to known heavily-optioned names. Run: python scripts/screen_basket.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from options_vrp import data, signal  # noqa: E402

TECH_CORE = ["AAPL", "NVDA", "TSLA", "AMD", "META"]           # current single-name basket

# Curated liquid/optioned candidates, by sector.
CANDIDATES = {
    "SPY": "index", "QQQ": "index", "IWM": "index",
    "AAPL": "tech", "NVDA": "tech", "TSLA": "tech", "AMD": "tech", "META": "tech",
    "NFLX": "comm", "GOOGL": "comm", "DIS": "comm",
    "XLE": "energy_etf", "XOM": "energy", "OXY": "energy", "SLB": "energy",
    "XLF": "fin_etf", "JPM": "financials", "GS": "financials", "BAC": "financials",
    "XLV": "health_etf", "LLY": "health", "PFE": "health", "UNH": "health",
    "HD": "consumer", "NKE": "consumer", "SBUX": "consumer", "MCD": "consumer",
    "BA": "industrials", "CAT": "industrials", "DE": "industrials",
    "GLD": "gold", "FCX": "materials", "TLT": "rates", "EEM": "em",
    "COIN": "crypto", "PLTR": "hi_iv",
}


def main():
    tickers = list(CANDIDATES)
    print(f"pulling {len(tickers)} candidates' prices + option chains …")
    prices = data.price_history(tickers, lookback_days=420)
    rets = prices.pct_change(fill_method=None)
    tech_ret = rets[[t for t in TECH_CORE if t in rets.columns]].mean(axis=1)  # existing-basket proxy

    rows = []
    today = pd.Timestamp.today().normalize()
    for tk_name in tickers:
        if tk_name not in prices.columns:
            continue
        spot = float(prices[tk_name].iloc[-1])
        rv = signal.realized_vol(prices[tk_name])
        corr = rets[tk_name].tail(252).corr(tech_ret.tail(252))
        iv = float("nan")
        try:
            tk, expiries = data.option_expiries(tk_name)
            # nearest expiry ~30-45 DTE
            best = None
            for e in expiries:
                d = (pd.Timestamp(e) - today).days
                if 25 <= d <= 50 and (best is None or abs(d - 37) < abs(best[1] - 37)):
                    best = (e, d)
            if best:
                iv = signal.atm_iv(data.puts(tk, best[0]), spot)
        except Exception:  # noqa: BLE001
            pass
        vrp = signal.vrp(iv, rv) if iv == iv else float("nan")
        rows.append((tk_name, CANDIDATES[tk_name], rv, iv, vrp, corr))

    df = pd.DataFrame(rows, columns=["ticker", "sector", "rv", "iv", "vrp", "corr_tech"])
    df = df.sort_values("vrp", ascending=False, na_position="last")

    print("\n" + "=" * 74)
    print("BASKET SCREEN — VRP richness + correlation to existing tech single-names")
    print("=" * 74)
    print(f"  {'ticker':7s} {'sector':12s} {'RV':>7s} {'IV':>7s} {'VRP':>7s} {'corr→tech':>10s}")
    print("  " + "-" * 60)
    for _, r in df.iterrows():
        iv = f"{r['iv']:.0%}" if r['iv'] == r['iv'] else "   —"
        vrp = f"{r['vrp']:+.1%}" if r['vrp'] == r['vrp'] else "   —"
        rv = f"{r['rv']:.0%}" if r['rv'] == r['rv'] else "  —"
        co = f"{r['corr_tech']:+.2f}" if r['corr_tech'] == r['corr_tech'] else "  —"
        print(f"  {r['ticker']:7s} {r['sector']:12s} {rv:>7s} {iv:>7s} {vrp:>7s} {co:>10s}")
    print("\n  Want: POSITIVE VRP (rich premium) + LOW corr→tech (diversifying). Tech names ~1.0 by")
    print("  construction. Sector ETFs (XLE/XLF/XLV) = reliable liquidity, lower corr, thinner VRP.")


if __name__ == "__main__":
    main()
