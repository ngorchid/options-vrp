"""Basket screen — VRP richness, diversification, AND execution cost.

WHAT THIS IS FOR. Two questions, one table.

  1. DIAGNOSTIC (cross-sectional). The sleeve has been opening nothing, vetoed by the cost
     guard rather than the VRP filter. The tracker answers "is cost rising?" through time,
     but needs weeks of market-hours runs. This answers a related question TODAY by looking
     across names instead: if the CANDIDATES are as expensive as the CURRENT BASKET, the
     problem is market-wide and adding names will not fix it. If some candidates are
     materially cheaper, it is a composition problem and better names exist.

  2. SHORTLIST. Names that clear the cost guard, carry positive VRP, and are not already
     duplicated by something held.

WHAT CHANGED (2026-08-28). The original version screened on VRP + correlation only, and
said so: "liquidity is the hard constraint we CAN'T measure from free data". That is no
longer true — `build_spread` now carries `quote_width`, so the same cost arithmetic the
live guard uses is computable offline. That gap was not academic: of the 14 names the last
run selected, TWO were later removed on cost grounds (LLY ~$1,200/share, spread too wide to
size; TLT $1 strike grid at ~10.5% IV, legs landing on adjacent strikes). Neither would
have survived a cost-aware screen.

Also fixed: correlation was measured against a hardcoded `TECH_CORE` basket that has not
been live since July 2026. It now measures against the CURRENT basket.

STRUCTURAL vs TRANSIENT FAILURE — the distinction that decides what to do about it:
  COMMISSION is fixed per contract, so a name failing on commission alone is permanently
    untradeable at this size. No execution improves it (this is why TLT was removed).
  SPREAD varies day to day, so a name failing on spread may clear on a fatter-premium day
    (this is why CAT was kept).
A name is only worth rejecting outright on the commission leg.

CAVEATS
  * RUN DURING US MARKET HOURS. Off-hours chains return bid=0/ask=0 and junk IV; screening
    on them once rejected 8 of 10 perfectly good names.
  * `quote_width` is the SUM OF THE TWO LEG widths, which is an UPPER BOUND on the combo
    width — a combo normally quotes tighter than its legs. Cost here is therefore
    conservative, and a name near the threshold may clear in reality.
  * One snapshot is one day. Do not prune or add on a single reading.

Run: python scripts/screen_basket.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from options_vrp import data, signal  # noqa: E402
from options_vrp.strategy import (DEFAULT_BASKET, OptionsConfig,  # noqa: E402
                                  build_spread, pick_expiry)

# Candidate pool. Deliberately includes non-equity underlyings: cost failure tracks price
# level, strike-grid density and IV — not sector — so diversifying by sector (which fixed
# the 2026-07 problem) does not fix this one. GLD is the existing proof that a non-equity
# name can clear the guard (8.4%) while being genuinely uncorrelated to the equity book.
CANDIDATES = {
    # index / broad
    "SPY": "index", "QQQ": "index", "IWM": "index", "DIA": "index",
    # tech / comm
    "AAPL": "tech", "NVDA": "tech", "TSLA": "tech", "AMD": "tech", "META": "tech",
    "MSFT": "tech", "AVGO": "tech", "NFLX": "comm", "GOOGL": "comm", "DIS": "comm",
    # energy
    "XLE": "energy_etf", "XOM": "energy", "OXY": "energy", "SLB": "energy",
    # financials
    "XLF": "fin_etf", "JPM": "financials", "GS": "financials", "BAC": "financials",
    # health
    "XLV": "health_etf", "LLY": "health", "PFE": "health", "UNH": "health",
    # consumer / industrial
    "HD": "consumer", "NKE": "consumer", "SBUX": "consumer", "MCD": "consumer",
    "BA": "industrials", "CAT": "industrials", "DE": "industrials",
    # NON-EQUITY — the direction that can resolve cost vs diversification
    "GLD": "gold", "SLV": "silver", "USO": "oil", "UNG": "natgas",
    "TLT": "rates", "IEF": "rates", "HYG": "credit", "LQD": "credit",
    "EEM": "em", "EFA": "dm_exus", "FXI": "china",
    # high-IV single names
    "FCX": "materials", "COIN": "crypto", "PLTR": "hi_iv", "MARA": "hi_iv",
}


def cost_breakdown(sp, cfg: OptionsConfig) -> tuple[float, float, float]:
    """(spread_frac, commission_frac, total) of credit — same arithmetic as `cost_ok`."""
    credit_usd = sp.credit * cfg.option_multiplier
    if credit_usd <= 0:
        return float("nan"), float("nan"), float("nan")
    spread_frac = sp.quote_width / sp.credit if sp.credit > 0 else float("nan")
    comm_frac = 4.0 * cfg.commission_per_contract / credit_usd
    return spread_frac, comm_frac, spread_frac + comm_frac


def classify(total: float, comm: float, cfg: OptionsConfig) -> str:
    if total != total:
        return "no quote"
    if total <= cfg.max_cost_frac:
        return "PASS"
    if comm > cfg.max_cost_frac:
        return "FAIL structural"      # commission alone breaches: no execution fixes it
    return "FAIL spread"              # may clear on a fatter-premium day


def main() -> None:
    cfg = OptionsConfig()
    held = set(DEFAULT_BASKET)
    tickers = sorted(set(CANDIDATES) | held)
    print(f"pulling {len(tickers)} names' prices + option chains "
          f"(cost guard {cfg.max_cost_frac:.0%}, commission ${cfg.commission_per_contract}/ct) …")

    prices = data.price_history(tickers, lookback_days=420)
    rets = prices.pct_change(fill_method=None)
    held_cols = [t for t in held if t in rets.columns]
    basket_ret = rets[held_cols].mean(axis=1)      # CURRENT basket, not the 2026-07 tech one

    today = pd.Timestamp.today().normalize()
    rows = []
    for tk_name in tickers:
        if tk_name not in prices.columns:
            continue
        spot = float(prices[tk_name].iloc[-1])
        rv = signal.realized_vol(prices[tk_name])
        corr = rets[tk_name].tail(252).corr(basket_ret.tail(252))
        iv = vrp = float("nan")
        sfrac = cfrac = tot = float("nan")
        credit = width = float("nan")
        verdict = "no chain"
        try:
            tk, expiries = data.option_expiries(tk_name)
            # Use the STRATEGY's own selector, not a local copy. An earlier version
            # reimplemented it as "nearest 37 DTE within a widened window", which silently
            # diverged from production the moment pick_expiry became monthly-first -- so the
            # screen was measuring an expiry the live system would never trade. A diagnostic
            # that reimplements the thing it is diagnosing stops being a diagnostic.
            best = pick_expiry(tuple(expiries), today, cfg.dte_min, cfg.dte_max)
            if best:
                puts = data.puts(tk, best[0])
                iv = signal.atm_iv(puts, spot)
                if iv == iv:
                    vrp = signal.vrp(iv, rv)
                    sp = build_spread(tk_name, puts, spot, best[0], best[1], iv, rv, cfg)
                    if sp is not None:
                        credit, width = sp.credit, sp.width
                        sfrac, cfrac, tot = cost_breakdown(sp, cfg)
                        verdict = classify(tot, cfrac, cfg)
                    else:
                        verdict = "no spread"
        except Exception as e:  # noqa: BLE001
            verdict = f"err {type(e).__name__}"
        rows.append({"ticker": tk_name, "sector": CANDIDATES.get(tk_name, "held"),
                     "held": tk_name in held, "rv": rv, "iv": iv, "vrp": vrp,
                     "corr_basket": corr, "credit": credit, "width": width,
                     "spread_frac": sfrac, "comm_frac": cfrac, "cost": tot,
                     "verdict": verdict})

    df = pd.DataFrame(rows).sort_values(["verdict", "cost"], na_position="last")

    # SELF-GUARD. This screen's own worst failure mode is being run off-hours: the provider
    # returns bid=0/ask=0 and a constant junk IV, every name yields "no spread", and the
    # output looks like a market verdict rather than a dead feed. Reuse the live guard's own
    # min_iv floor (5%) as the detector -- a real 30-45 DTE ATM IV below it does not occur.
    iv_ok = df["iv"].dropna()
    junk_frac = float((iv_ok < 0.05).mean()) if len(iv_ok) else 1.0
    off_hours = junk_frac > 0.5
    if off_hours:
        print("\n" + "!" * 108)
        print(f"  UNUSABLE RUN — {junk_frac:.0%} of names report ATM IV below the 5% min_iv floor.")
        print("  That is the off-hours junk-chain signature (bid=0/ask=0, constant IV), not a")
        print("  market condition. NOTHING BELOW IS A RESULT. Re-run during US market hours")
        print("  (09:30-16:00 ET). This is the same failure that once rejected 8 of 10 good names.")
        print("!" * 108)

    print("\n" + "=" * 108)
    print("BASKET SCREEN — VRP richness + diversification + EXECUTION COST")
    print("=" * 108)
    print(f"  {'ticker':7s} {'sector':11s} {'in':3s} {'RV':>6s} {'IV':>6s} {'VRP':>7s} "
          f"{'corr':>6s} {'credit':>7s} {'spread':>7s} {'comm':>6s} {'COST':>7s}  verdict")
    print("  " + "-" * 104)
    for _, r in df.iterrows():
        f = lambda v, s="{:.0%}": s.format(v) if v == v else "  -"  # noqa: E731
        print(f"  {r['ticker']:7s} {r['sector']:11s} {'Y' if r['held'] else ' ':3s} "
              f"{f(r['rv']):>6s} {f(r['iv']):>6s} {f(r['vrp'],'{:+.1%}'):>7s} "
              f"{f(r['corr_basket'],'{:+.2f}'):>6s} "
              f"{f(r['credit'],'${:.2f}'):>7s} {f(r['spread_frac']):>7s} {f(r['comm_frac']):>6s} "
              f"{f(r['cost']):>7s}  {r['verdict']}")

    # ---- THE DIAGNOSTIC: is cost a basket problem or a market problem? ----
    ok = df[df['cost'].notna()]
    cur, cand = ok[ok['held']], ok[~ok['held']]
    print("\n" + "=" * 108)
    print("DIAGNOSTIC — is the cost problem specific to the current basket, or market-wide?")
    print("=" * 108)
    for lbl, grp in (("CURRENT basket", cur), ("candidates", cand)):
        if not len(grp):
            print(f"  {lbl:16s} no usable quotes")
            continue
        print(f"  {lbl:16s} n={len(grp):2d}  median cost {grp['cost'].median():5.1%}  "
              f"passing {int((grp['verdict'] == 'PASS').sum()):2d}  "
              f"median credit ${grp['credit'].median():.2f}")
    if len(cur) and len(cand):
        gap = cand['cost'].median() - cur['cost'].median()
        print(f"\n  candidate median is {abs(gap):.1%} {'CHEAPER' if gap < 0 else 'more expensive'} "
              f"than the current basket")
        print("  -> materially cheaper candidates mean a COMPOSITION problem (better names exist).")
        print("  -> similar or worse means a MARKET-WIDE problem; adding names will not fix it.")

    add = df[(df['verdict'] == "PASS") & (~df['held']) & (df['vrp'] > cfg.vrp_min) & (df["corr_basket"] < 0.80)]
    print("\n" + "=" * 108)
    print(f"SHORTLIST — clears cost, VRP > {cfg.vrp_min:.0%}, corr < 0.80 to the current basket")
    print("=" * 108)
    if off_hours:
        print("  suppressed — run is off-hours (see banner above)")
    elif not len(add):
        print("  none today")
    else:
        for _, r in add.sort_values("vrp", ascending=False).iterrows():
            print(f"  {r['ticker']:7s} {r['sector']:11s} VRP {r['vrp']:+.1%}  "
                  f"corr {r['corr_basket']:+.2f}  cost {r['cost']:.0%}  "
                  f"credit ${r['credit']:.2f}")
    print("\n  NB one snapshot. Do not add or prune on a single reading, and never on "
          "off-hours quotes.")

    out = ROOT / "results" / "paper" / "basket_screen.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\n  wrote {out}")


if __name__ == "__main__":
    main()
