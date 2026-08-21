"""Record, day by day, how much of the basket actually passes the filters — and how much
of the budget would really be deployed.

WHY. Every capital-level number we have for this strategy is extrapolated from SPX. The
per-trade backtest holds ONE spread on ONE instrument and models no budget, so the basket's
deployment rate — the thing that decides whether the capital Sharpe is ~0.2 or ~1.8 — rests on a
Monte Carlo assuming every name behaves like SPX at SPX's costs. Both assumptions are unverified
and both bias upward.

This needs no historical option data. It runs the REAL `target_book` against live chains and
records what actually happens, so that after a few weeks the deployment rate is measured rather
than assumed. It places no orders and touches no state.

WHAT IT LOGS, per run: names surviving each filter stage in turn, the spreads that would be
opened, and the resulting capital-at-risk as a fraction of budget — the quantity to compare
against the Monte Carlo's 14.9% and the SPX-only extrapolation's 2-3%.

⚠ MARKET HOURS ONLY. The VRP filter needs implied vol, which needs live chains; off-hours the
provider returns a constant junk IV and every name fails chain-sanity. A run outside US hours is
recorded with `usable=False` and excluded from the averages rather than counted as a zero-
deployment day, which would silently drag the estimate toward nothing.

    python scripts/track_deployment.py            # append one observation
    python scripts/track_deployment.py --report   # summarise what has accumulated
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from options_vrp import OptionsConfig, target_book  # noqa: E402
from risk_guard import NOMINAL_NAV, allocated_budget  # noqa: E402
from options_vrp.strategy import OVERLAP, SECTOR  # noqa: E402

load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.WARNING, format="%(message)s")
OUT = ROOT / "results" / "paper" / "deployment_log.csv"


def observe(cfg: OptionsConfig) -> dict:
    today = pd.Timestamp.today().normalize()
    res = target_book(cfg, today)
    diags = pd.DataFrame(res.diagnostics)
    n_basket = len(cfg.basket)

    note = diags["note"].fillna("") if "note" in diags else pd.Series(dtype=str)
    bad_chain = int(note.str.contains("ATM IV|bid is zero", regex=True).sum())
    no_expiry = int(note.str.contains("no expiry clears", regex=True).sum())
    vrp_fail = int(note.str.contains("VRP", regex=True).sum())
    no_spread = int(note.str.contains("no valid spread|sized to 0", regex=True).sum())
    # Names excluded for EARNINGS never reach the chain check, so comparing bad_chain against
    # the whole basket wrongly calls a run usable when every readable chain failed. Compare
    # against the names that actually got a chain. Getting this backwards would record an
    # off-hours run as a genuine zero-deployment day and drag the estimate toward nothing.
    reached_chain = n_basket - no_expiry
    usable = reached_chain > 0 and bad_chain < reached_chain

    # Apply the same selection constraints the live loop does, so the count is what would
    # actually be OPENED rather than what merely passed the signal.
    picked, sect, tick = [], {}, set()
    for s in res.targets:
        if len(picked) >= cfg.max_positions:
            break
        ov = (res.corr_overlap or OVERLAP).get(s.ticker, set())
        if ov & tick:
            continue
        se = SECTOR.get(s.ticker, s.ticker)
        if sect.get(se, 0) >= cfg.max_per_sector:
            continue
        picked.append(s)
        tick.add(s.ticker)
        sect[se] = sect.get(se, 0) + 1

    risk = sum(s.max_loss * 100 * s.contracts for s in picked)
    return {"date": str(today.date()), "usable": usable, "gate_open": bool(res.regime_open),
            "regime_ratio": round(float(res.regime_ratio), 4), "n_basket": n_basket,
            "bad_chain": bad_chain, "no_expiry_earnings": no_expiry, "vrp_fail": vrp_fail,
            "no_spread": no_spread, "n_targets": len(res.targets), "n_after_limits": len(picked),
            "tickers": "|".join(s.ticker for s in picked),
            "risk_usd": round(risk, 0), "risk_pct_budget": round(risk / cfg.budget, 4),
            "budget": cfg.budget}


def report() -> None:
    if not OUT.exists():
        print(f"no log yet at {OUT}")
        return
    df = pd.read_csv(OUT)
    ok = df[df["usable"]]
    print(f"\nDEPLOYMENT LOG — {len(df)} observations, {len(ok)} usable "
          f"({len(df)-len(ok)} off-hours/unreadable, excluded)")
    if ok.empty:
        print("  no usable observations yet — run during US market hours")
        return
    print(f"  span {ok['date'].min()} -> {ok['date'].max()}\n")
    print(f"  gate open on                     {ok['gate_open'].mean():>6.0%} of usable days")
    print(f"  names passing VRP + structure    {ok['n_targets'].mean():>6.2f} of "
          f"{int(ok['n_basket'].iloc[-1])} (median {ok['n_targets'].median():.0f})")
    print(f"  after limits/overlap/sector      {ok['n_after_limits'].mean():>6.2f} "
          f"(cap {int(df['n_basket'].iloc[-1] and 6)})")
    print(f"  CAPITAL AT RISK                  {ok['risk_pct_budget'].mean():>6.2%} of budget "
          f"(median {ok['risk_pct_budget'].median():.2%}, max {ok['risk_pct_budget'].max():.2%})")
    print(f"  days with nothing deployable     {(ok['n_after_limits'] == 0).mean():>6.0%}")
    print("\n  BENCHMARKS: Monte Carlo (13 names, SPX-like) predicted 14.9% avg capital at risk;")
    print("  the SPX-only extrapolation implied 2-3%. Which this lands nearer is the point.")
    if len(ok) < 10:
        print(f"\n  ⚠ only {len(ok)} usable observations — far too few to conclude anything.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report()
        return
    # SAME BUDGET AS LIVE. The tracker exists to predict what the live book will deploy, and
    # whole-contract rounding is budget-dependent -- a name that fits one contract at $75k may
    # fit none at $50k. Measuring at the OptionsConfig default while the runner sizes off
    # `allocated_budget` would produce a deployment rate for a book that does not exist.
    _b, _note = allocated_budget("options-vrp", None,
                                 float(os.getenv("NOMINAL_NAV", str(NOMINAL_NAV))))
    _b = float(os.getenv("BUDGET", _b))
    logging.info("tracking at budget $%s (%s)", f"{_b:,.0f}", _note)
    row = observe(OptionsConfig(budget=_b))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(OUT) if OUT.exists() else pd.DataFrame()
    df = df[df["date"] != row["date"]] if "date" in df else df       # idempotent per day
    pd.concat([df, pd.DataFrame([row])], ignore_index=True).to_csv(OUT, index=False)
    state = "usable" if row["usable"] else "UNUSABLE (off-hours / chains unreadable)"
    print(f"{row['date']}  {state}  targets {row['n_targets']} -> after limits "
          f"{row['n_after_limits']}  risk {row['risk_pct_budget']:.2%} of budget"
          f"  [{row['tickers'] or '-'}]")


if __name__ == "__main__":
    main()
