"""Mutation-test `scripts/test_vrp_strategy.py`: seed real faults, demand the suite catches them.

WHY. `--selftest` prints and never fails — nine seeded faults went straight through it on
2026-08-14, including turning the cost guard off entirely. And a suite that merely *looks* like a
test is no better: four assertions in test_risk_guard.py had been vacuous since they were written
because they were handed objects with no `__bool__`. The only way to know a test can fail is to
break the code and watch it.

Every fault below must be CAUGHT. A survivor means that case is decoration. Non-zero exit, so
this can gate a release.

Run: python3 scripts/mutate_vrp_strategy.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "options_vrp" / "strategy.py"
SUITE = ["python3", "scripts/test_vrp_strategy.py"]

MUTATIONS = [
    # --- COST GUARD: the load-bearing arithmetic ---
    ("    comm_cost = 4.0 * commission_per_contract",
     "    comm_cost = 2.0 * commission_per_contract",
     "commission counts 2 sides not 4 (halves the cost of every thin name)"),
    ("    comm_cost = 4.0 * commission_per_contract",
     "    comm_cost = 0.0",
     "commission dropped entirely (PFE would pass)"),
    ("    spread_cost = (hi - lo) * multiplier",
     "    spread_cost = (hi - lo) * multiplier / 2.0",
     "half-spread instead of full round trip (CAT would pass)"),
    ("    ratio = (spread_cost + comm_cost) / credit",
     "    ratio = spread_cost / credit",
     "ratio ignores commission"),
    ("    return ratio <= max_frac, ratio, {\"spread\": spread_cost / credit,",
     "    return True, ratio, {\"spread\": spread_cost / credit,",
     "guard always passes (the 'guard OFF' fault the selftest missed)"),
    ("    if not (np.isfinite(bid) and np.isfinite(ask)):",
     "    if False:",
     "non-finite quote check removed (NaN ratio leaks to the caller)"),
    ("    if mid <= 0 or hi <= lo:",
     "    if mid < 0:",
     "zero/locked quote no longer rejected"),
    ("        return False, None, empty\n    lo, hi = min(bid, ask), max(bid, ask)",
     "        return True, 0.0, empty\n    lo, hi = min(bid, ask), max(bid, ask)",
     "missing quote treated as free rather than unusable"),

    # --- SIZING ---
    ("    contracts = max(int((cfg.risk_per_trade * cfg.budget) // (max_loss * 100)), 0)",
     "    contracts = int((cfg.risk_per_trade * cfg.budget) // (max_loss * 100))",
     "negative-budget floor removed (emits SELL -1 contracts)"),
    ("    contracts = max(int((cfg.risk_per_trade * cfg.budget) // (max_loss * 100)), 0)",
     "    contracts = max(int(-(-(cfg.risk_per_trade * cfg.budget) // (max_loss * 100))), 0)",
     "contracts rounded UP (an unsizeable name becomes 1 contract)"),

    # --- EXPIRY / EARNINGS ---
    ("        if before is not None and pd.Timestamp(e) >= before:",
     "        if before is not None and pd.Timestamp(e) <= before:",
     "earnings filter inverted (keeps only expiries AFTER the event)"),
    ("        if before is not None and pd.Timestamp(e) >= before:",
     "        if False:",
     "earnings filter disabled entirely"),
    ("        if dte_min <= dte <= dte_max:",
     "        if dte_min <= dte:",
     "expiry window upper bound removed"),

    # --- MANAGEMENT ---
    ("    if current_value <= cfg.profit_target * entry_credit:",
     "    if current_value <= 0.10 * entry_credit:",
     "profit target 50% -> 10% (holds winners far too long)"),
    ("    if cfg.stop_mult > 0 and current_value >= cfg.stop_mult * entry_credit:",
     "    if current_value >= 2.0 * entry_credit:",
     "the removed 2x stop reinstated regardless of config"),

    # --- stale prices (wired 2026-08-16) ---
    ("        if tk_name in stale_px:",
     "        if False:",
     "stale-name skip removed (a frozen feed's inflated VRP becomes a trade)"),
    ("        stale_px, _sc = stale_columns(prices[[c for c in prices.columns if c in cfg.basket]],",
     "        stale_px, _sc = ({}, None) or stale_columns(prices[[c for c in prices.columns if c in cfg.basket]],",
     "stale detection returns nothing (every name looks live)"),

    # --- LIQUIDITY SCREEN ---
    ("    if puts is None or \"openInterest\" not in getattr(puts, \"columns\", ()):",
     "    if puts is None:",
     "missing openInterest column no longer handled (screen crashes/leaks)"),
]


def main() -> int:
    original = TARGET.read_text()
    results = []
    print("=" * 96)
    print(f"MUTATION TEST — {TARGET.relative_to(ROOT)} against {SUITE[1]}")
    print("=" * 96)
    print(f"  {len(MUTATIONS)} seeded faults; every one must be CAUGHT\n")
    try:
        for find, repl, why in MUTATIONS:
            if find not in original:
                results.append((why, None))
                print(f"  [ ?? ] {why:76} PATTERN MISSING")
                continue
            TARGET.write_text(original.replace(find, repl, 1))
            for pyc in ROOT.rglob("*.pyc"):
                pyc.unlink(missing_ok=True)
            r = subprocess.run(SUITE, cwd=ROOT, capture_output=True, text=True)
            caught = r.returncode != 0
            results.append((why, caught))
            print(f"  [{'ok  ' if caught else 'FAIL'}] {why:76} "
                  f"{'CAUGHT' if caught else '*** SURVIVED ***'}")
    finally:
        TARGET.write_text(original)
        for pyc in ROOT.rglob("*.pyc"):
            pyc.unlink(missing_ok=True)

    survived = [w for w, c in results if c is False]
    missing = [w for w, c in results if c is None]
    print("\n" + "=" * 96)
    if missing:
        print(f"{len(missing)} mutation(s) could not be applied — the code moved, update this file:")
        for w in missing:
            print("   " + w)
    if survived:
        print(f"{len(survived)} MUTATION(S) SURVIVED — those cases cannot fail and are decoration:")
        for w in survived:
            print("   " + w)
        return 1
    if missing:
        return 1
    r = subprocess.run(SUITE, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("RESTORE FAILED — the suite does not pass on the original file")
        return 1
    print(f"all {len(MUTATIONS)} seeded faults were caught; suite restored and green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
