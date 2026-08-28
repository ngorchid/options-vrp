"""Tests for the options-VRP core logic — cost guard, sizing, expiry/earnings, management.

WHY THIS EXISTS. `--selftest` prints and never fails: on 2026-08-14 nine seeded faults went
undetected through it, including turning the cost guard OFF entirely and dropping the liquidity
screen. It is a demonstration, not a test. This is the real one, and it gates go-live.

⚠ THE COST GUARD IS FIRST AND GETS THE MOST CASES because it is the single most load-bearing
piece of this strategy. Every basket decision reduces to it: 8 of 10 names passed on market-hours
combos and 2 failed, CAT on spread and PFE on commission, and the whole "expand the universe"
question turned on this arithmetic. Get it wrong in the permissive direction and the book trades
names that cannot pay for themselves; wrong in the strict direction and it trades nothing.

⚠ EVERY CASE USES THE REAL `Check`. Bare `type("C", (), {...})()` objects define no `__bool__`,
are always truthy, and four such assertions in test_risk_guard.py had never been able to fail.

⚠ MUTATION-TESTED. Run `python3 scripts/mutate_vrp_strategy.py`; a case that survives its own
mutation is decoration.

Run: python3 scripts/test_vrp_strategy.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from options_vrp.strategy import (OptionsConfig, build_spread,  # noqa: E402
                                  correlated_pairs, cost_ok, manage_action,
                                  oi_threshold, pick_expiry)
from risk_guard import Check  # noqa: E402

CFG = OptionsConfig()
fails, ran = [], 0


def expect(label: str, got, want_ok: bool = True) -> None:
    """`got` MUST define __bool__ (use Check). A bare object is always truthy."""
    global ran
    ran += 1
    ok = bool(got)
    if ok != want_ok:
        fails.append(f"{label}: expected {'PASS' if want_ok else 'REJECT'}, got "
                     f"{'PASS' if ok else 'REJECT'} ({getattr(got, 'reason', '')})")
    print(f"  [{'ok ' if ok == want_ok else 'FAIL'}] {label:62} -> {'PASS' if ok else 'REJECT'}"
          f"{'' if ok == want_ok else '  | ' + getattr(got, 'reason', '')}")


def close(a, b, tol=1e-9) -> Check:
    return Check(a is not None and abs(a - b) <= tol, f"{a!r} vs {b!r}")


print("=" * 96)
print("OPTIONS-VRP CORE LOGIC")
print("=" * 96)

# =====================================================================================
print("\n--- COST GUARD: the arithmetic, against the REAL market-hours combos (2026-08-10) ---")
# Quotes as measured. These four are the empirical anchor for the whole basket decision.
MEASURED = [
    # (label, bid, ask, expected total cost, verdict at the 25% guard)
    ("SPX-like  16.20/17.10", 16.20, 17.10, 0.06, True),
    ("SPY-like   1.72/ 1.86", 1.72, 1.86, 0.09, True),
    ("CAT-like   4.40/ 5.90", 4.40, 5.90, 0.30, False),
    ("PFE-like   0.02/ 0.04", 0.02, 0.04, 1.5333, False),
]
print(f"  {'quote':26}{'credit/ct':>11}{'spread':>9}{'comm':>8}{'total':>8}  verdict")
print("  " + "-" * 72)
for lab, b, a, want_ratio, want_ok in MEASURED:
    ok, ratio, br = cost_ok(b, a, CFG.max_cost_frac, CFG.commission_per_contract,
                            CFG.option_multiplier)
    print(f"  {lab:26}{br['credit']:>11,.0f}{br['spread']:>9.0%}{br['commission']:>8.0%}"
          f"{ratio:>8.0%}  {'TRADE' if ok else 'SKIP'}")
    expect(f"cost ratio matches the measured combo — {lab.split()[0]}",
           Check(abs(ratio - want_ratio) < 0.005, f"{ratio:.4f} vs {want_ratio}"))
    expect(f"verdict at the {CFG.max_cost_frac:.0%} guard — {lab.split()[0]}",
           Check(ok == want_ok, f"got {'TRADE' if ok else 'SKIP'}"))

# Decomposition: the two components fail in DIFFERENT ways and must be separable, because the
# fix differs -- a wide spread is a liquidity problem, a large commission share is a
# credit-per-contract problem that does NOT amortise with size.
_, _, br_cat = cost_ok(4.40, 5.90, 0.25, 0.65, 100.0)
_, _, br_pfe = cost_ok(0.02, 0.04, 0.25, 0.65, 100.0)
expect("CAT fails on SPREAD, not commission",
       Check(br_cat["spread"] > 0.25 and br_cat["commission"] < 0.05,
             f"spread {br_cat['spread']:.2f} comm {br_cat['commission']:.2f}"))
expect("PFE fails on COMMISSION, not spread",
       Check(br_pfe["commission"] > 0.50, f"comm {br_pfe['commission']:.2f}"))
expect("spread + commission == total (no third term hiding)",
       close(br_cat["spread"] + br_cat["commission"],
             cost_ok(4.40, 5.90, 0.25, 0.65, 100.0)[1], 1e-12))

print("\n--- COST GUARD: size-independence, the property the whole basket argument rests on ---")
# Contract count cancels from every term. If it did not, the guard would be a function of
# position size and the per-name pass/fail decisions would not transfer across account sizes.
_r1 = cost_ok(1.72, 1.86, 0.25, 0.65, 100.0)[1]
expect("ratio is independent of multiplier scaling of BOTH legs",
       close(cost_ok(17.2, 18.6, 0.25, 6.5, 100.0)[1], _r1, 1e-12))
expect("commission share scales INVERSELY with credit (does not amortise with size)",
       Check(cost_ok(0.86, 0.93, 0.25, 0.65, 100.0)[2]["commission"] >
             2 * cost_ok(1.72, 1.86, 0.25, 0.65, 100.0)[2]["commission"] - 1e-9, ""))
expect("IB vs LYNX commission changes the verdict on a thin name",
       Check(cost_ok(0.30, 0.34, 0.25, 0.65, 100.0)[0] is True
             and cost_ok(0.30, 0.34, 0.25, 3.50, 100.0)[0] is False,
             "LYNX $3.50/ct must fail where IB $0.65 passes"))

print("\n--- COST GUARD: unusable quotes must SKIP, never trade on a guess ---")
for lab, b, a in [("no bid", None, 1.50), ("no ask", 1.50, None), ("both None", None, None),
                  ("zero mid", 0.0, 0.0),
                  ("locked (bid==ask)", 1.50, 1.50), ("non-numeric", "x", 1.5),
                  ("NaN bid", float("nan"), 1.5), ("NaN ask", 1.5, float("nan")),
                  ("inf", 1.5, float("inf"))]:
    ok, ratio, _ = cost_ok(b, a, CFG.max_cost_frac, CFG.commission_per_contract,
                           CFG.option_multiplier)
    expect(f"unusable quote -> not ok, ratio None — {lab}",
           Check(ok is False and ratio is None, f"ok={ok} ratio={ratio}"))

# A genuinely CROSSED market cannot be rejected here, and that is deliberate rather than an
# oversight: a credit combo quotes NEGATIVE, so cost_ok takes abs() and then min/max to normalise
# — which makes (-1.86, -1.72) work correctly but leaves a real (1.90, 1.70) indistinguishable
# from it. Pinned so the behaviour is a decision on the record, not an accident.
_okx, _rx, _ = cost_ok(1.90, 1.70, CFG.max_cost_frac, CFG.commission_per_contract,
                       CFG.option_multiplier)
expect("crossed quote is NORMALISED, not rejected (abs+min/max serves the sign convention)",
       Check(_okx is True and _rx is not None, f"ok={_okx} ratio={_rx}"))
expect("  ... and gives the same ratio as the same quote the right way round",
       close(_rx, cost_ok(1.70, 1.90, CFG.max_cost_frac, CFG.commission_per_contract,
                          CFG.option_multiplier)[1], 1e-12))

print("\n--- COST GUARD: the threshold itself ---")
expect("a ratio exactly AT the guard passes (<=, not <)",
       Check(cost_ok(1.0, 1.0 * (1 + 2 * (0.25 - 4 * 0.65 / 100.0) / (2 - (0.25 - 4 * 0.65 / 100.0))),
                     0.25, 0.65, 100.0)[0] is True, "boundary must be inclusive"))
expect("guard at 0 rejects everything (no free lunch)",
       Check(cost_ok(1.72, 1.86, 0.0, 0.65, 100.0)[0] is False, ""))
expect("guard at 9.99 admits even PFE (the A/B used for 'guard OFF')",
       Check(cost_ok(0.02, 0.04, 9.99, 0.65, 100.0)[0] is True, ""))

# =====================================================================================
print("\n--- SIZING: whole contracts, and the unsizeable-name floor ---")
# Driven through the REAL build_spread, not a local copy of its sizing line. An earlier version
# of this file mirrored the arithmetic here instead, and BOTH sizing mutations survived — the
# test could not see strategy.py at all. Testing a re-implementation tests nothing.
def _mkchain(spot=100.0, lo=60.0, step=1.0, oi=5000, iv=0.25, dte=35, r=0.04):
    """A dense, liquid put chain priced with Black-Scholes.

    Priced properly rather than with a made-up curve: a first attempt used a linear function of
    (spot - K), which prices deeper-OTM puts HIGHER, so `credit = short - long` came out negative
    and build_spread correctly refused every spread. A fixture that violates put monotonicity
    tests nothing except the code's willingness to reject nonsense.
    """
    from math import erf, log, sqrt
    ks = np.arange(lo, spot, step)
    T = dte / 365.0

    def _n(x):                                   # standard normal CDF
        return 0.5 * (1.0 + erf(x / sqrt(2.0)))

    mid = []
    for K in ks:
        d1 = (log(spot / K) + (r + iv * iv / 2) * T) / (iv * sqrt(T))
        d2 = d1 - iv * sqrt(T)
        mid.append(max(K * np.exp(-r * T) * _n(-d2) - spot * _n(-d1), 0.01))
    mid = np.array(mid)
    return pd.DataFrame({"strike": ks, "bid": mid * 0.98, "ask": mid * 1.02,
                         "impliedVolatility": np.full(len(ks), iv),
                         "openInterest": np.full(len(ks), oi)})


def sized(budget, spot=100.0, cfg_kw=None):
    cfg = OptionsConfig(budget=budget, **(cfg_kw or {}))
    return build_spread("T", _mkchain(spot), spot, "2026-09-18", 35, 0.25, 0.18, cfg)


_ref = sized(75_000)
expect("build_spread produces a real spread on a dense liquid chain",
       Check(_ref is not None and _ref.contracts >= 1, f"{_ref}"))
expect("long leg is strictly BELOW the short leg (the collision fix)",
       Check(_ref is not None and _ref.long_strike < _ref.short_strike,
             f"{_ref.long_strike} vs {_ref.short_strike}" if _ref else "no spread"))
expect("credit is positive and max loss is width - credit",
       Check(_ref is not None and _ref.credit > 0
             and abs(_ref.max_loss - (_ref.width - _ref.credit)) < 1e-9, f"{_ref}"))
expect("quote_width is populated and equals the summed leg bid/ask widths (cost-guard telemetry)",
       Check(_ref is not None and _ref.quote_width > 0, f"quote_width={_ref.quote_width if _ref else None}"))
expect("peak margin is structurally max_positions x risk_per_trade",
       close(CFG.max_positions * CFG.risk_per_trade, 0.18, 1e-12))

_big = sized(750_000)
expect("contracts scale with budget", Check(_big.contracts > _ref.contracts,
                                            f"{_big.contracts} vs {_ref.contracts}"))
expect("contracts are WHOLE (an int, never fractional)",
       Check(isinstance(_ref.contracts, int), f"{type(_ref.contracts)}"))
# The unsizeable floor: a name whose ONE contract exceeds the per-position budget is not sized
# small, it is not traded. This is the $35k capital cliff.
_tiny = sized(1_000)
expect("budget too small for one contract -> 0 contracts, not 1 (rounds DOWN)",
       Check(_tiny is not None and _tiny.contracts == 0,
             f"{_tiny.contracts if _tiny else 'None'}"))
expect("  ... and the risk it would imply exceeds the per-position budget (so 0 is correct)",
       Check(_tiny is not None
             and _tiny.max_loss * 100 > CFG.risk_per_trade * 1_000, f"{_tiny}"))
for b in (0.0, -1_000.0, -1e9):
    _neg = sized(b)
    expect(f"budget {b:,.0f} -> contracts never negative",
           Check(_neg is not None and _neg.contracts >= 0,
                 f"{_neg.contracts if _neg else 'None'}"))

# =====================================================================================
print("\n--- EXPIRY + EARNINGS: the sentinel that was once backwards ---")
TODAY = pd.Timestamp("2026-08-14")
EXPS = tuple(str((TODAY + pd.Timedelta(days=d)).date()) for d in (7, 21, 31, 38, 45, 60))
# ---- monthly-first expiry selection (2026-08-28) -------------------------------------
# Single names carry usable OI only on 3rd-Friday monthlies; the weeklies between them are
# near-dead, which is what made every single name return "no spread" on 2026-08-28.
from options_vrp.strategy import is_monthly  # noqa: E402
expect("is_monthly: 3rd Friday", Check(is_monthly(pd.Timestamp("2026-09-18")), "2026-09-18"))
expect("is_monthly: 4th Friday is NOT", Check(not is_monthly(pd.Timestamp("2026-09-25")), "2026-09-25"))
expect("is_monthly: 1st Friday is NOT", Check(not is_monthly(pd.Timestamp("2026-10-02")), "2026-10-02"))
# A month starting ON a Friday must still resolve to the THIRD Friday, not the 15th.
expect("is_monthly: month starting on a Friday",
       Check(is_monthly(pd.Timestamp("2027-01-15")), "2027-01-15 (Jan 2027 starts Fri)"))

_MIX = ("2026-09-18", "2026-09-25", "2026-10-02", "2026-10-09", "2026-10-16", "2026-10-23")
_T = pd.Timestamp("2026-08-28")
_pick = pick_expiry(_MIX, _T, 30, 50)
expect("prefers the MONTHLY over a nearer-the-midpoint weekly",
       Check(_pick == ("2026-10-16", 49), f"{_pick} (10-02 at 35d is nearer the 40 midpoint)"))
_pick45 = pick_expiry(_MIX, _T, 30, 45)
expect("no monthly in range -> falls back to the midpoint weekly (unchanged behaviour)",
       Check(_pick45 == ("2026-10-02", 35), f"{_pick45}"))
# The earnings filter must still dominate: a monthly settling AFTER the event is excluded
# entirely, and selection then falls back to the nearest-midpoint WEEKLY among what is left.
# (10-16 monthly is filtered out; 10-02 at 35d and 10-09 at 42d remain, midpoint is 40, so
# 10-09 wins. Getting this wrong on the first attempt is exactly why the assertion is here.)
_pick_ev = pick_expiry(_MIX, _T, 30, 50, before=pd.Timestamp("2026-10-10"))
expect("earnings filter still outranks the monthly preference",
       Check(_pick_ev == ("2026-10-09", 42), f"{_pick_ev}"))

got = pick_expiry(EXPS, TODAY, CFG.dte_min, CFG.dte_max)
expect("picks an expiry inside [dte_min, dte_max]",
       Check(got is not None and CFG.dte_min <= got[1] <= CFG.dte_max, f"{got}"))
expect("picks the one NEAREST the window midpoint",
       Check(got is not None and got[1] == 38, f"{got}"))
expect("no expiry in the window -> None (name simply not tradeable)",
       Check(pick_expiry(("2026-08-16", "2026-12-31"), TODAY, 30, 45) is None, ""))
expect("empty expiry tuple -> None", Check(pick_expiry((), TODAY, 30, 45) is None, ""))
# `before` restricts to expiries settling BEFORE the (buffered) earnings date.
_bef = TODAY + pd.Timedelta(days=40)      # clears +31 and +38, not +45
g2 = pick_expiry(EXPS, TODAY, CFG.dte_min, CFG.dte_max, before=_bef)
expect("earnings inside the window -> picks the latest expiry that still clears it",
       Check(g2 is not None and pd.Timestamp(g2[0]) < _bef and g2[1] == 38, f"{g2}"))
expect("  ... and that is EARLIER than the unconstrained pick would allow through the event",
       Check(g2 is not None and pd.Timestamp(g2[0]) < _bef, f"{g2}"))
expect("earnings before the whole window -> None, not 'any expiry'",
       Check(pick_expiry(EXPS, TODAY, CFG.dte_min, CFG.dte_max,
                         before=pd.Timestamp("2026-08-20")) is None,
             "a Timestamp.max-style sentinel here would admit EVERY expiry"))
expect("before=None behaves as no restriction",
       Check(pick_expiry(EXPS, TODAY, 30, 45, before=None) == got, ""))

print("\n--- LIQUIDITY SCREEN (open interest) ---")
_chain = pd.DataFrame({"strike": [40, 45, 50, 55], "openInterest": [0, 1100, 800, 5]})
thr = oi_threshold(_chain, CFG.min_open_interest, 0.25)
expect("threshold is the max of the absolute floor and the chain's own percentile",
       Check(thr >= CFG.min_open_interest, f"{thr}"))
expect("missing openInterest column -> screen disabled (0.0), not a crash",
       close(oi_threshold(pd.DataFrame({"strike": [1]}), 50, 0.25), 0.0))
expect("None chain -> 0.0", close(oi_threshold(None, 50, 0.25), 0.0))
expect("all-NaN openInterest -> 0.0 (pd.to_numeric(None) is NaN, not None)",
       close(oi_threshold(pd.DataFrame({"openInterest": [None, None]}), 50, 0.25), 0.0))
expect("an all-zero-OI chain still returns the absolute floor",
       Check(oi_threshold(pd.DataFrame({"openInterest": [0, 0, 0]}), 50, 0.25) >= 0.0, ""))

# =====================================================================================
print("\n--- MANAGEMENT: profit / time / stop ---")
EC = 0.75
expect("50% profit target closes", Check(manage_action(EC, 0.37, 30, CFG) == "profit", ""))
expect("exactly AT the target closes (<=)",
       Check(manage_action(EC, CFG.profit_target * EC, 30, CFG) == "profit", ""))
expect("just above the target holds",
       Check(manage_action(EC, CFG.profit_target * EC + 0.01, 30, CFG) is None, ""))
expect("21-DTE time stop closes", Check(manage_action(EC, 0.60, 20, CFG) == "time", ""))
expect("exactly at 21 DTE closes", Check(manage_action(EC, 0.60, 21, CFG) == "time", ""))
expect("22 DTE holds", Check(manage_action(EC, 0.60, 22, CFG) is None, ""))
# The 2x stop was REMOVED on real-price evidence (it realises losses that revert). Default OFF.
expect("stop is OFF by default (stop_mult <= 0)", Check(CFG.stop_mult <= 0, f"{CFG.stop_mult}"))
expect("a big loss does NOT stop out with the default config",
       Check(manage_action(EC, 1.55, 30, CFG) is None, "the long wing caps the loss already"))
expect("stop fires only when explicitly enabled",
       Check(manage_action(EC, 1.55, 30, OptionsConfig(stop_mult=2.0)) == "stop", ""))
expect("profit takes precedence over time when both apply",
       Check(manage_action(EC, 0.10, 5, CFG) == "profit", ""))

# =====================================================================================
print("\n--- OVERLAP: correlation computed fresh, cheapest-of-group ordering ---")
idx = pd.bdate_range("2025-01-01", periods=300)
rng = np.random.default_rng(0)
base = rng.standard_normal(len(idx)).cumsum()
prices = pd.DataFrame({
    "SPY": 400 + base,
    "QQQ": 350 + base * 1.02 + rng.standard_normal(len(idx)) * 0.05,   # ~ same factor
    "GLD": 180 + rng.standard_normal(len(idx)).cumsum(),               # independent
}, index=idx)
pairs = correlated_pairs(prices, threshold=0.80)
expect("near-identical names are flagged as overlapping",
       Check("QQQ" in pairs.get("SPY", set()), f"{pairs}"))
expect("an independent name is NOT flagged",
       Check("GLD" not in pairs.get("SPY", set()), f"{pairs}"))
expect("the relation is symmetric",
       Check(("QQQ" in pairs.get("SPY", set())) == ("SPY" in pairs.get("QQQ", set())), ""))
expect("a name is never its own overlap", Check("SPY" not in pairs.get("SPY", set()), ""))
expect("too little history -> empty map (fall back to the static one), not a false pair",
       Check(correlated_pairs(prices.head(5), threshold=0.80) in ({}, {"SPY": set(), "QQQ": set(),
                                                                   "GLD": set()}), ""))

# =====================================================================================
print("\n--- stale prices: a frozen feed must not inflate VRP into a fake signal ---")
print("    (data_fresh sees only the INDEX; RV20 collapsing makes VRP = IV - RV look RICH)")

# The data layer is STUBBED because it is a collaborator (network IO), not the thing under
# test -- target_book's stale handling is. Nothing about the stubs' behaviour is asserted.
from options_vrp import data as _data, strategy as _strat  # noqa: E402

_idx = pd.bdate_range(end=pd.Timestamp("2026-08-17"), periods=300)
_rng = np.random.default_rng(11)
_live = 100 * np.exp(np.cumsum(_rng.standard_normal(len(_idx)) * 0.012))
_panel = pd.DataFrame({"SPY": _live, "QQQ": _live * 1.01,
                       "FROZEN": np.full(len(_idx), 80.0)}, index=_idx)

_orig = (_data.regime, _data.price_history, _data.option_expiries)
try:
    _data.regime = lambda: (0.90, 15.0, 16.7)
    _data.price_history = lambda basket: _panel[[c for c in _panel.columns if c in basket]]
    _data.option_expiries = lambda t: (None, ())          # no chains -> stop after the skip
    _cfg_st = OptionsConfig(basket=("SPY", "QQQ", "FROZEN"), budget=75_000.0)
    _res = _strat.target_book(_cfg_st, pd.Timestamp("2026-08-17"))
finally:
    _data.regime, _data.price_history, _data.option_expiries = _orig

_notes = {d["ticker"]: d["note"] for d in _res.diagnostics}
print(f"    diagnostics: {_notes}")
expect("the FROZEN name is skipped with a stale-price note",
       Check("stale price" in _notes.get("FROZEN", ""), f"{_notes}"))
expect("  ... and never reaches the target book",
       Check(all(t.ticker != "FROZEN" for t in _res.targets), f"{[t.ticker for t in _res.targets]}"))
expect("  ... while the LIVE names are still processed (not a blanket halt)",
       Check(all(n in _notes for n in ("SPY", "QQQ"))
             and "stale price" not in _notes.get("SPY", ""), f"{_notes}"))
expect("a frozen series really would have collapsed RV (so the skip is load-bearing)",
       Check(float(_strat.signal.realized_vol(_panel["FROZEN"])) < 0.01,
             f"rv {float(_strat.signal.realized_vol(_panel['FROZEN'])):.6f} — near-zero RV "
             f"inflates VRP = IV - RV"))


print("\n--- self-heal: pending orders resolved against the real IB position on the next run ---")
sys.path.insert(0, str(ROOT / "scripts"))
from dataclasses import asdict as _asdict                                     # noqa: E402
from run_options_paper import _resolve_pending                               # noqa: E402
from options_vrp.state import OptionsState, OpenSpread                        # noqa: E402


def _mkS(t, ss, ls, cr, ct):
    return OpenSpread(t, "2026-09-11", ss, ls, ct, cr, 4.0, "2026-08-07", 100.0, 0.0)


_sbux, _tlt, _xom, _cat = (_mkS("SBUX", 99, 95, 0.18, 8), _mkS("TLT", 80, 79, 0.10, 5),
                           _mkS("XOM", 145, 140, 0.30, 6), _mkS("CAT", 715, 685, 0.25, 1))
_st = OptionsState()
_st.open_spreads = [_sbux, _tlt, _cat]                       # xom not booked yet; cat booked optimistically
_st.pending_orders = [
    {"action": "close", "permId": 101, "spread": _asdict(_sbux), "placed_date": "2026-08-26"},  # filled -> book
    {"action": "close", "permId": 102, "spread": _asdict(_tlt), "placed_date": "2026-08-26"},   # still open -> leave
    {"action": "open", "permId": 103, "spread": _asdict(_xom), "placed_date": "2026-08-26"},     # filled -> book
    {"action": "open", "permId": 104, "spread": _asdict(_cat), "placed_date": "2026-08-26"}]     # never filled -> reverse
_E = "20260911"
_pos = {("TLT", _E, 80.0): -5, ("TLT", _E, 79.0): 5, ("XOM", _E, 145.0): -6, ("XOM", _E, 140.0): 6}
_fills = {101: ("Filled", 0.40), 102: ("Submitted", 0.0), 103: ("Filled", 0.30), 104: ("Cancelled", 0.0)}


class _FakeBroker:
    dry_run = False
    def put_positions(self): return dict(_pos)
    def order_fill(self, p): return _fills.get(p)
    def spread_values(self, sp): return {}


_resolve_pending(_FakeBroker(), _st, "2026-08-27")
_tk = sorted(s.ticker for s in _st.open_spreads)
expect("late CLOSE booked (SBUX filled at IB) -> -176 realized", close(_st.realized_pnl, -176.0, 0.01))
expect("still-open CLOSE left alone (TLT held at IB)", Check("TLT" in _tk))
expect("late OPEN booked (XOM filled at IB)", Check("XOM" in _tk))
expect("OPEN that never filled reversed (CAT flat at IB)", Check("CAT" not in _tk))
expect("resolved pendings cleared", Check(len(_st.pending_orders) == 0))


print("\n" + "=" * 96)
if fails:
    print(f"{len(fails)} FAILURE(S) of {ran}:")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print(f"all {ran} options-vrp checks behaved as expected")
