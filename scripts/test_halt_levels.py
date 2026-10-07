"""Tests for the kill-switch levels (2026-10-07), options-vrp.

  HALT_HARD : exits before connecting; the open-assignment alert still fires (from saved state).
  HALT_ALL  : no new risk, no management -- ONLY the SAFETY assignment unwind, selling exactly what
              the assigned spread implies (100 x n shares, then the n matching long puts).
  Any level : an open assigned position raises a daily, ESCALATING alert.
scripts/mutate_halt_levels.py seeds the faults. Fake broker, no network.

Run: python scripts/test_halt_levels.py
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import risk_guard as rg  # noqa: E402
import run_options_paper as runner  # noqa: E402
from options_vrp.assignment import escalation_notes  # noqa: E402
from options_vrp.state import OpenSpread, OptionsState  # noqa: E402

_fails: list[str] = []
_ran = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _ran
    _ran += 1
    if not cond:
        _fails.append(f"{label}  | {detail}")
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + ("" if cond else f"   <- {detail}"))


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001 -- a crash is a FAILED check here, not a test crash
        return f"crashed: {type(e).__name__}: {e}"


TODAY = datetime.now().strftime("%Y-%m-%d")


def bday(n: int) -> str:
    return str(np.busday_offset(np.datetime64(TODAY), -n, roll="backward"))


def spread(t, ks, kl, n, **kw):
    return OpenSpread(ticker=t, expiry="2026-11-20", short_strike=ks, long_strike=kl, contracts=n,
                      entry_credit=0.7, max_loss=ks - kl - 0.7, entry_date="2026-10-01",
                      entry_spot=ks * 1.05, **kw)


def book():
    """IWM intact; XLE fully assigned (exact print: 300 sh @ 90); NKE assigned but blended."""
    return OptionsState(open_spreads=[spread("IWM", 263.0, 256.0, 2),
                                      spread("XLE", 90.0, 85.0, 3),
                                      spread("NKE", 70.0, 67.5, 2)])


E = "20261120"
PUTS = {("IWM", E, 263.0): -2.0, ("IWM", E, 256.0): 2.0,
        ("XLE", E, 90.0): 0.0, ("XLE", E, 85.0): 3.0,
        ("NKE", E, 70.0): 0.0, ("NKE", E, 67.5): 2.0}
STOCK = {"XLE": (300.0, 90.0), "NKE": (260.0, 64.0)}     # NKE: magic-formula also holds some


class FakeBroker:
    def __init__(self):
        self.calls: list[tuple] = []

    def connect(self):
        self.calls.append(("connect",))
        return True

    def disconnect(self):
        self.calls.append(("disconnect",))

    def put_positions(self):
        return dict(PUTS)

    def stock_positions_detail(self):
        return dict(STOCK)

    def marks(self):
        return ({("XLE", E, 85.0): 0.5, ("NKE", E, 67.5): 0.6}, {"XLE": 88.0, "NKE": 65.0})

    def sell_stock(self, ticker, shares, label="SAFETY: assignment unwind"):
        self.calls.append(("sell_stock", ticker, shares, label))
        return {"status": "Filled", "price": 88.0, "exec_ids": ["s"], "order_ref": "options-vrp:R"}

    def sell_put(self, ticker, expiry, strike, contracts, label="SAFETY: assignment unwind"):
        self.calls.append(("sell_put", ticker, expiry, strike, contracts, label))
        return {"status": "Filled", "price": 0.5, "exec_ids": ["p"], "order_ref": "options-vrp:R"}

    def __getattr__(self, name):           # ANY other broker call is recorded (and must not happen)
        def rec(*a, **k):
            self.calls.append((name,) + a)
            raise AssertionError(f"HALT_ALL must not call broker.{name}")
        return rec


LOG: list[tuple[str, str]] = []
runner.logging.error = lambda f, *a: LOG.append(("E", f % a if a else f))
runner.logging.warning = lambda f, *a: LOG.append(("W", f % a if a else f))
runner.push_if_alerts = lambda *a, **k: None

# =============================================================================================
print("LEVELS IN risk_guard (shared)")
_t = Path(tempfile.mkdtemp())
os.environ.pop("TRADING_HALT", None)
(_t / "HALT").write_text("x")
(_t / "HALT_ALL").write_text("x")
check("HALT_ALL beats HALT", rg.halt_state(_t)[0] == rg.HALT_ALL, "")
(_t / "HALT_HARD").write_text("x")
check("HALT_HARD beats HALT_ALL", rg.halt_state(_t)[0] == rg.HALT_HARD, "")
os.environ["TRADING_HALT"] = "hard"
check("TRADING_HALT=hard -> hard", rg.halt_state(Path(tempfile.mkdtemp()))[0] == rg.HALT_HARD, "")
os.environ.pop("TRADING_HALT")

# =============================================================================================
print("HALT_ALL — the SAFETY assignment unwind ONLY")
tmp = Path(tempfile.mkdtemp())
runner.STATE_FILE = tmp / "state.json"
book().save(runner.STATE_FILE)
fb = FakeBroker()
runner.make_broker = lambda **kw: fb
acts = safe(runner.run_safety_only, runner._cfg(None), 4001, 7)
trades = [c for c in fb.calls if c[0] not in ("connect", "disconnect")]
check("the only orders: SELL 300 XLE, then SELL 3 XLE 85P (exactly what the assignment implies)",
      trades == [("sell_stock", "XLE", 300, "SAFETY: assignment unwind"),
                 ("sell_put", "XLE", "2026-11-20", 85.0, 3, "SAFETY: assignment unwind")], str(trades))
check("no opening order, no combo close, no margin/quote call (any other broker call is recorded)",
      not any(c[0] in ("open_spread", "close_spread", "margin_cushion", "quote_spread",
                       "spread_values") for c in fb.calls), str(fb.calls))
st = OptionsState.load(runner.STATE_FILE)
keys = sorted(s.key for s in st.open_spreads)
check("the intact IWM spread is left exactly as it was (not managed, not closed)",
      "IWM_2026-11-20_263_256" in keys and next(s for s in st.open_spreads if s.ticker == "IWM").contracts == 2,
      str(keys))
check("the blended NKE assignment is recorded but NOT sold (cannot prove the shares are VRP's)",
      any(s.ticker == "NKE" and s.assigned_contracts == 2 and not s.assigned_auto for s in st.open_spreads)
      and not any(c[0] == "sell_stock" and c[1] == "NKE" for c in fb.calls), str(st.open_spreads))
check("XLE is fully unwound and gone from state; the result is saved",
      "XLE_2026-11-20_90_85" not in keys and isinstance(acts, list) and len(acts) == 2, str(keys))
check("the open NKE assignment raises the escalating alert, naming HALT_ALL",
      any(lv == "E" and "ASSIGNED POSITION" in m and "NKE" in m and "under HALT_ALL" in m for lv, m in LOG),
      str(LOG))

print("\nmain() DISPATCH")
called: list[str] = []
_RUN_LIVE_SRC = inspect.getsource(runner.run_live)
runner.run_live = lambda *a, **k: called.append("run_live")
runner.run_safety_only = lambda *a, **k: called.append("run_safety_only")
made: list[int] = []
runner.make_broker = lambda **kw: made.append(1)
sys.argv = ["run_options_paper.py", "--live", "--force"]
runner.halt_state = lambda root: (rg.HALT_ALL, "test")
safe(runner.main)
check("HALT_ALL -> run_safety_only, never run_live", called == ["run_safety_only"], str(called))
called.clear()
LOG.clear()
s2 = book()
s2.open_spreads[2].assigned_contracts, s2.open_spreads[2].assigned_date = 2, bday(3)
s2.save(runner.STATE_FILE)
runner.halt_state = lambda root: (rg.HALT_HARD, "test")
r = safe(runner.main)
check("HALT_HARD -> nothing runs, no broker is even made", r is None and called == [] and made == [],
      str((r, called, made)))
check("HALT_HARD still raises the open-assignment alert from saved state (URGENT on day 4)",
      any(lv == "E" and "URGENT" in m and "day 4" in m and "under HALT_HARD" in m
          and "unwind by hand" in m for lv, m in LOG), str(LOG))
runner.halt_state = lambda root: (rg.HALT_NONE, "")
safe(runner.main)
check("no halt -> run_live", called == ["run_live"], str(called))
check("run_live raises the escalating alert every run too",
      "escalate_assignments(state, today)" in _RUN_LIVE_SRC, "")

print("\nESCALATION LADDER")
s3 = spread("BAC", 40.0, 37.5, 6, assigned_contracts=6, assigned_auto=True, assigned_date=TODAY)
n1 = escalation_notes([s3], TODAY)
s3.assigned_date = bday(1)
n2 = escalation_notes([s3], TODAY)
s3.assigned_date = bday(2)
n3 = escalation_notes([s3], TODAY)
check("day 1 OPEN -> day 2 ESCALATION -> day 3 URGENT",
      "OPEN — day 1" in n1[0] and "ESCALATION — day 2" in n2[0] and "URGENT — day 3" in n3[0],
      str((n1, n2, n3)))
check("an intact spread raises nothing", escalation_notes([spread("IWM", 263.0, 256.0, 2)], TODAY) == [], "")

print("\nDOCUMENTED SIZING")
os.environ.pop("BUDGET", None)
cfg = runner._cfg(None)
check("budget comes from config/capital_bases.json (50,000)", cfg.budget == 50_000, str(cfg.budget))
check("_cfg logs the budget in use through log_sizing", "log_sizing(\"options-vrp\"" in inspect.getsource(runner._cfg), "")

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} halt-level checks behaved as expected")
