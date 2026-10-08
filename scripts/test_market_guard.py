"""The run time anchored to US Eastern, and the market-open guard (2026-10-08, decision on #16).

  ET slot : the task starts at 20:30 AND 21:30 local (CET box) with --et-slot 15:30; exactly one
            start runs each day, through both daylight-saving gaps.
  Guard   : a closed (or unknown) market blocks NEW spreads and warns; management closes and the
            assignment unwind still run -- no guard may block a close.
Drives the real main() with a fake broker for the guard. scripts/mutate_market_guard.py seeds the
faults.

Run: python scripts/test_market_guard.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import risk_guard as rg  # noqa: E402
import run_options_paper as runner  # noqa: E402
from options_vrp import market_hours as mh  # noqa: E402
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


Z = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731

# =============================================================================================
print("ET SLOT — 15:30 New York time, whatever the box's clock says")
check("8 Oct 2026, 21:30 CEST = 15:30 EDT -> runs", mh.in_et_slot("15:30", Z("2026-10-08 19:30")), "")
check("8 Oct 2026, 20:30 CEST = 14:30 EDT -> does not", not mh.in_et_slot("15:30", Z("2026-10-08 18:30")), "")
check("27 Oct 2026 (gap week), 20:30 CET = 15:30 EDT -> runs", mh.in_et_slot("15:30", Z("2026-10-27 19:30")), "")
check("27 Oct 2026 (gap week), 21:30 CET = 16:30 EDT, after the close -> does not",
      not mh.in_et_slot("15:30", Z("2026-10-27 20:30")), "")
check("3 Nov 2026, 21:30 CET = 15:30 EST -> runs", mh.in_et_slot("15:30", Z("2026-11-03 20:30")), "")
check("10 Mar 2026 (spring gap), 20:30 CET = 15:30 EDT -> runs", mh.in_et_slot("15:30", Z("2026-03-10 19:30")), "")
check("10 Mar 2026 (spring gap), 21:30 CET = 16:30 EDT -> does not",
      not mh.in_et_slot("15:30", Z("2026-03-10 20:30")), "")
check("tolerance: 15:50 ET runs, 15:51 ET does not",
      mh.in_et_slot("15:30", Z("2026-10-08 19:50")) and not mh.in_et_slot("15:30", Z("2026-10-08 19:51")), "")
bad = []
for d in pd.bdate_range("2026-01-01", "2027-12-31"):
    hits = [hhmm for hhmm in ("20:30", "21:30")
            if mh.in_et_slot("15:30", pd.Timestamp(f"{d:%Y-%m-%d} {hhmm}", tz="Europe/Zurich"))]
    if len(hits) != 1:
        bad.append((f"{d:%Y-%m-%d}", hits))
check("every weekday of 2026-2027: EXACTLY ONE of the two local starts (20:30 / 21:30 Zurich) runs",
      bad == [], str(bad[:5]))
check("a naive timestamp is refused (never guess a zone)", "crashed" in str(safe(mh.et_now, pd.Timestamp("2026-10-08 15:30"))), "")

# =============================================================================================
print("\nIB TRADING HOURS (liquidHours) — holidays and early closes")
SPEC = "20261008:0930-20261008:1600;20261009:0930-20261009:1600"
E = lambda s: pd.Timestamp(s, tz="America/New_York")  # noqa: E731
check("inside the session -> open", mh.parse_liquid_hours(SPEC, "US/Eastern", E("2026-10-08 15:30")) is True, "")
check("before 09:30 -> closed", mh.parse_liquid_hours(SPEC, "US/Eastern", E("2026-10-08 09:00")) is False, "")
check("at 16:00 -> closed (end is exclusive)", mh.parse_liquid_hours(SPEC, "US/Eastern", E("2026-10-08 16:00")) is False, "")
check("a holiday listed as CLOSED -> closed",
      mh.parse_liquid_hours("20261126:CLOSED;20261127:0930-20261127:1300", "US/Eastern",
                            E("2026-11-26 15:30")) is False, "")
check("an early close (13:00) at 15:30 -> closed",
      mh.parse_liquid_hours("20261126:CLOSED;20261127:0930-20261127:1300", "US/Eastern",
                            E("2026-11-27 15:30")) is False, "")
check("today not listed -> None (unknown, falls back to the clock)",
      mh.parse_liquid_hours(SPEC, "US/Eastern", E("2026-10-12 15:30")) is None, "")
check("unparseable -> None", mh.parse_liquid_hours("garbage-in", "US/Eastern", E("2026-10-08 15:30")) is None, "")
check("clock fallback: weekday 15:30 open, 16:00 closed, Saturday closed",
      mh.clock_open(E("2026-10-08 15:30")) and not mh.clock_open(E("2026-10-08 16:00"))
      and not mh.clock_open(E("2026-10-10 12:00")), "")


class Cal:
    def __init__(self, ret):
        self.ret = ret

    def liquid_hours(self, sym):
        if isinstance(self.ret, Exception):
            raise self.ret
        return self.ret


ok, how = mh.market_open(Cal((SPEC, "US/Eastern")), E("2026-10-08 15:30"))
check("market_open uses IB's calendar when it can", ok is True and "IB trading hours" in how, how)
ok, how = mh.market_open(Cal(("20261126:CLOSED", "US/Eastern")), E("2026-11-26 15:30"))
check("...and IB's holiday beats the clock (Thanksgiving 15:30 is closed)", ok is False and "IB" in how, how)
ok, how = mh.market_open(Cal((SPEC, "US/Eastern")), E("2026-10-12 15:30"))
check("IB answers but does not list today -> the clock decides (open at 15:30), not 'closed'",
      ok is True and "clock fallback" in how, how)
ok, how = mh.market_open(Cal(RuntimeError("down")), E("2026-10-08 15:30"))
check("IB read fails -> the clock decides, and says so", ok is True and "clock fallback" in how, how)
ok, how = mh.market_open(Cal(None), E("2026-10-08 16:30"))
check("...clock fallback after the close -> closed", ok is False, how)

# =============================================================================================
print("\nmain() WITH --et-slot")
made: list[int] = []
runner.make_broker = lambda **kw: made.append(1) or (_ for _ in ()).throw(AssertionError("no broker"))
runner.code_version = lambda *a, **k: ("test", 0)
runner.halt_state = lambda root: (rg.HALT_NONE, "")
_real_slot = mh.in_et_slot
mh.in_et_slot = lambda slot, now=None, tolerance_min=20: False
sys.argv = ["run_options_paper.py", "--live", "--force", "--et-slot", "15:30"]
r = safe(runner.main)
check("outside the slot: main() returns before anything (no broker, no run)", r is None and made == [],
      str((r, made)))
mh.in_et_slot = _real_slot

# =============================================================================================
print("\nTHE GUARD IN A LIVE PASS (real main() -> run_live, fake broker)")
TODAY = datetime.now().strftime("%Y-%m-%d")
EXP = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
EX = EXP.replace("-", "")


class SimBroker:
    def __init__(self, market: str):
        self.market, self.calls = market, []
        self.puts = {("XLE", EX, 59.0): -8.0, ("XLE", EX, 57.0): 8.0,        # at its profit target
                     ("IWM", EX, 263.0): 0.0, ("IWM", EX, 256.0): 2.0}       # assigned
        self.stocks = {"IWM": (200.0, 263.0)}
        self.pm = {("XLE", EX, 59.0): 0.15, ("XLE", EX, 57.0): 0.05, ("IWM", EX, 256.0): 3.5}
        self.dry_run, self.ib = False, NS(sleep=lambda s: None)

    def connect(self):
        return True

    def disconnect(self):
        pass

    def margin_cushion(self):
        return (30_000.0, 50_000.0)

    def put_positions(self):
        return {k: v for k, v in self.puts.items() if v}

    def stock_positions_detail(self):
        return {k: v for k, v in self.stocks.items() if v[0]}

    def marks(self):
        return ({k: v for k, v in self.pm.items() if self.puts.get(k)},
                {"IWM": 254.0} if self.stocks["IWM"][0] else {})

    def spread_values(self, spreads):
        out = {}
        for sp in spreads:
            a, b = (sp.ticker, EX, float(sp.short_strike)), (sp.ticker, EX, float(sp.long_strike))
            if self.puts.get(a) and self.puts.get(b):
                out[sp.key] = self.pm[a] - self.pm[b]
        return out

    def liquid_hours(self, sym):
        self.calls.append(("liquid_hours",))
        d = pd.Timestamp.now(tz="America/New_York").strftime("%Y%m%d")
        return (f"{d}:0000-{d}:2359" if self.market == "open" else f"{d}:CLOSED", "US/Eastern")

    def sell_stock(self, t, n, label="SAFETY: assignment unwind"):
        self.calls.append(("sell_stock", t, n))
        self.stocks[t] = (self.stocks[t][0] - n, 263.0)
        return {"label": label, "action": "SELL", "qty": n, "status": "Filled", "price": 254.1,
                "exec_ids": ["s"], "order_ref": "options-vrp:T"}

    def sell_put(self, t, e, k, n, label="SAFETY: assignment unwind"):
        self.calls.append(("sell_put", t, k, n))
        self.puts[(t, e.replace("-", ""), float(k))] -= n
        return {"label": label, "action": "SELL", "qty": n, "status": "Filled", "price": 3.45,
                "exec_ids": ["p"], "order_ref": "options-vrp:T"}

    def close_spread(self, sp):
        self.calls.append(("close_spread", sp.ticker))
        self.puts[(sp.ticker, EX, float(sp.short_strike))] += sp.contracts
        self.puts[(sp.ticker, EX, float(sp.long_strike))] -= sp.contracts
        return {"key": sp.key, "ticker": sp.ticker, "action": "CLOSE", "contracts": sp.contracts,
                "net_price": 0.11, "status": "Filled", "permId": 1, "exec_ids": ["c"],
                "order_ref": "options-vrp:T"}

    def quote_spread(self, sp):
        self.calls.append(("quote_spread", sp.ticker))
        return (0.70, 0.74)

    def open_spread(self, sp):
        self.calls.append(("open_spread", sp.ticker))
        return {"key": sp.key, "ticker": sp.ticker, "action": "OPEN", "contracts": sp.contracts,
                "net_price": 0.72, "status": "Filled", "permId": 2, "exec_ids": ["o"],
                "order_ref": "options-vrp:T"}

    def order_fill(self, perm):
        return None


TARGET = NS(ticker="QQQ", expiry=EXP, short_strike=480.0, long_strike=473.0, contracts=2,
            credit=0.72, max_loss=6.28, spot=500.0, dte=60, vrp=0.05, short_delta=-0.16,
            long_delta=-0.10)
runner.target_book = lambda cfg: NS(regime_open=True, regime_ratio=0.9, targets=[TARGET],
                                    blocked=[], corr_overlap={}, diagnostics=[])
runner.write_equity = lambda *a, **k: None
runner.book_drawdown = lambda *a, **k: (None, None, None, "")
runner.book_vol = lambda *a, **k: None
runner.send_report = lambda *a, **k: None
runner.push_if_alerts = lambda *a, **k: None
runner.RECORDS_DIR = Path(tempfile.mkdtemp())
os.environ.pop("BUDGET", None)
_tmp = Path(tempfile.mkdtemp())


def live(market: str) -> SimBroker:
    fb = SimBroker(market)
    runner.STATE_FILE = _tmp / f"{market}.json"
    OptionsState(open_spreads=[
        OpenSpread("XLE", EXP, 59.0, 57.0, 8, 0.26, 1.74, "2026-10-01", 62.0),
        OpenSpread("IWM", EXP, 263.0, 256.0, 2, 0.74, 6.26, "2026-10-01", 276.0)]).save(runner.STATE_FILE)
    runner.make_broker = lambda **kw: fb
    runner.ALERTS.records.clear()
    sys.argv = ["run_options_paper.py", "--live", "--force"]
    r = safe(runner.main)
    if r is not None:
        print("    main() returned:", r)
    return fb


fb = live("closed")
kinds = [c[0] for c in fb.calls]
check("market CLOSED: no new spread is opened and nothing is even quoted for one",
      "open_spread" not in kinds and "quote_spread" not in kinds, str(fb.calls))
check("...the profit-take CLOSE still runs (no guard may block a close)", ("close_spread", "XLE") in fb.calls,
      str(fb.calls))
check("...the SAFETY assignment unwind still runs (shares, then the long puts)",
      ("sell_stock", "IWM", 200) in fb.calls and ("sell_put", "IWM", 256.0, 2) in fb.calls, str(fb.calls))
check("...and a WARNING says so, naming how it was decided",
      any(lv == "WARNING" and "MARKET CLOSED (IB trading hours" in m and "no new spreads opened" in m
          for lv, m in runner.ALERTS.records), str(runner.ALERTS.records))
fb = live("open")
check("market OPEN: the same pass opens the target spread",
      ("open_spread", "QQQ") in fb.calls and ("close_spread", "XLE") in fb.calls, str(fb.calls))
check("...with no market warning", not any("MARKET CLOSED" in m for _, m in runner.ALERTS.records),
      str(runner.ALERTS.records))

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} market-guard checks behaved as expected")
