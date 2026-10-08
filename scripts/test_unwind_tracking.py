"""Tracked assignment-unwind orders (2026-10-08, decision #15).

An unfilled SAFETY sale is left working at IB and tracked on the spread; the next run books
whatever filled -- never cancelling it, never placing a second order while it may still work.
Covers the branches the live passes in test_e2e_assignment.py do not reach. Real
handle_assignments, fake broker. scripts/mutate_unwind_tracking.py seeds the faults.

Run: python scripts/test_unwind_tracking.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import run_options_paper as runner  # noqa: E402
from options_vrp import assignment as asg  # noqa: E402
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


E, KS, KL, C = "20261120", 263.0, 256.0, 0.74
TODAY = "2026-10-08"
S_KEY, L_KEY = ("IWM", E, KS), ("IWM", E, KL)


def bday(n: int) -> str:
    return str(np.busday_offset(np.datetime64(TODAY), -n, roll="backward"))


def spread(order: dict, **kw) -> OpenSpread:
    sp = OpenSpread(ticker="IWM", expiry="2026-11-20", short_strike=KS, long_strike=KL, contracts=2,
                    entry_credit=C, max_loss=KS - KL - C, entry_date="2026-10-01", entry_spot=276.0,
                    assigned_contracts=2, assigned_date=bday(1), assigned_auto=True, **kw)
    sp.unwind_order = dict(order)
    return sp


def stock_order(**kw) -> dict:
    return {"leg": "stock", "permId": 7, "qty": 200.0, "held_before": 200.0, "placed": bday(1),
            "booked": 0.0, "status": "Submitted", "order_ref": "options-vrp:R", **kw}


class FakeBroker:
    def __init__(self, progress, stocks, puts=None, marks=None):
        self.progress, self.stocks, self.calls = progress, stocks, []
        self.puts = puts if puts is not None else {S_KEY: 0.0, L_KEY: 2.0}
        self._marks = marks if marks is not None else ({L_KEY: 3.5}, {"IWM": 254.0})

    def order_progress(self, perm):
        self.calls.append(("order_progress", perm))
        return self.progress

    def put_positions(self):
        return self.puts

    def stock_positions_detail(self):
        return self.stocks

    def marks(self):
        return self._marks

    status = "Filled"
    put_status = "Filled"

    def sell_stock(self, t, n, label="SAFETY: assignment unwind"):
        self.calls.append(("sell_stock", t, n))
        if self.status != "Filled":
            return {"status": self.status, "price": None, "exec_ids": [], "permId": 8,
                    "order_ref": "options-vrp:R", "label": label}
        return {"status": "Filled", "price": 254.1, "exec_ids": ["s"], "order_ref": "options-vrp:R",
                "permId": 8, "label": label}

    def sell_put(self, t, e, k, n, label="SAFETY: assignment unwind"):
        self.calls.append(("sell_put", t, k, n))
        if self.put_status != "Filled":
            return {"status": self.put_status, "price": None, "exec_ids": [], "permId": 9,
                    "order_ref": "options-vrp:R", "label": label}
        return {"status": "Filled", "price": 3.45, "exec_ids": ["p"], "order_ref": "options-vrp:R",
                "permId": 9, "label": label}


LOG: list[tuple[str, str]] = []
runner.logging.error = lambda f, *a: LOG.append(("E", f % a if a else f))
runner.logging.warning = lambda f, *a: LOG.append(("W", f % a if a else f))


def run(st, fb):
    LOG.clear()
    return safe(runner.handle_assignments, fb, st, TODAY)


def rows(st, action="ASSIGNED_STOCK_SOLD"):
    return [t for t in st.trade_log if t["action"] == action]


def first(st):
    """The first open spread, or an empty stand-in so a fault fails a check instead of crashing."""
    from types import SimpleNamespace as NS
    return st.open_spreads[0] if st.open_spreads else NS(
        assigned_contracts=0, contracts=0, assigned_shares_sold=0.0, assigned_stock_sold=None,
        unwind_order={})


# =============================================================================================
print("WHAT IS FILLED — IB's own count, else the position change")
st = OptionsState(open_spreads=[spread(stock_order())])
fb = FakeBroker({"status": "Submitted", "filled": None, "avg_price": None, "exec_ids": []},
                {"IWM": (50.0, KS)})
run(st, fb)
r = rows(st)
check("IB reports no count -> the position drop (200 -> 50 = 150 sold) is booked",
      len(r) == 1 and r[0]["shares"] == 150.0, str(st.trade_log))
check("...priced at the MARK, and the ledger row says so", r and r[0]["price"] == 254.0
      and "MARK (IB fill price not available)" in r[0].get("note", ""), str(r))
check("...still working -> nothing new is placed, the order stays tracked with 150 booked",
      [c[0] for c in fb.calls] == ["order_progress"] and first(st).unwind_order.get("booked") == 150.0,
      str(fb.calls))
check("...and the spread now counts 150 shares sold (50 left to value)",
      first(st).assigned_shares_sold == 150.0 and not first(st).assigned_stock_sold, "")
u = safe(asg.unrealized, first(st), {L_KEY: 3.5}, {"IWM": 254.0})
u = u if isinstance(u, (int, float)) else None
check("...valued on the 50 shares still held + the 2 long puts: (c + S - Ks) x 50 + Pl x 200",
      u is not None and abs(u - ((C + 254.0 - KS) * 50 + 3.5 * 200)) < 1e-6, str(u))
fb.progress = {"status": "Submitted", "filled": 150.0, "avg_price": 254.2, "exec_ids": ["a"]}
run(st, fb)
check("next run, nothing new filled -> nothing booked twice", len(rows(st)) == 1, str(st.trade_log))

st = OptionsState(open_spreads=[spread(stock_order())])
fb = FakeBroker({"status": "Filled", "filled": 0.0, "avg_price": None, "exec_ids": []}, {"IWM": (0.0, None)},
                marks=({L_KEY: 3.5}, {"IWM": 254.0}))
run(st, fb)
check("IB says Filled but counts 0, while the shares are gone -> the larger (position: 200) is booked",
      [x["shares"] for x in rows(st)] == [200.0], str(st.trade_log))
check("...a filled stock order lets the long puts go in the same run",
      ("sell_put", "IWM", KL, 2) in fb.calls and st.open_spreads == [], str(fb.calls))

st = OptionsState(open_spreads=[spread(stock_order(booked=50.0))])
st.open_spreads[0].assigned_shares_sold = 50.0
fb = FakeBroker({"status": "Filled", "filled": 260.0, "avg_price": 254.0, "exec_ids": []}, {"IWM": (0.0, None)})
run(st, fb)
check("a count above the order size never books more than the order (150 left of 200)",
      [x["shares"] for x in rows(st)] == [150.0], str(st.trade_log))

# =============================================================================================
print("\nWHEN IB CANNOT SHOW THE ORDER")
st = OptionsState(open_spreads=[spread(stock_order(placed=bday(1)))])
fb = FakeBroker(None, {"IWM": (200.0, KS)})
run(st, fb)
check("not visible, placed yesterday -> NOT placing another (two orders could sell twice), ERROR",
      [c[0] for c in fb.calls] == ["order_progress"] and first(st).unwind_order
      and any(lv == "E" and "not visible at IB — NOT placing another" in m for lv, m in LOG), str(LOG))
st = OptionsState(open_spreads=[spread(stock_order(placed=bday(2)))])
fb = FakeBroker(None, {"IWM": (200.0, KS)})
run(st, fb)
check("not visible, placed two sessions ago (a DAY order is certainly dead) -> expired, a fresh sale",
      ("sell_stock", "IWM", 200.0) in fb.calls and not rows(st, "ASSIGNED_STOCK_SOLD") == []
      and any("treated as expired" in m for _, m in LOG), str((fb.calls, LOG)))
st = OptionsState(open_spreads=[spread(stock_order())])
fb = FakeBroker({"status": "Submitted", "filled": 100.0, "avg_price": None, "exec_ids": []},
                {"IWM": (100.0, KS)}, marks=({L_KEY: 3.5}, {}))
run(st, fb)
check("filled but NO price at all (no fill price, no mark) -> nothing booked, kept for the next run",
      rows(st) == [] and first(st).unwind_order and any("no price" in m for _, m in LOG), str(LOG))

# =============================================================================================
print("\nTHE LONG-PUT LEG IS TRACKED THE SAME WAY")
put_order = {"leg": "put", "permId": 9, "qty": 2.0, "held_before": 2.0, "placed": bday(1),
             "booked": 0.0, "status": "Submitted", "order_ref": "options-vrp:R"}
st = OptionsState(open_spreads=[spread(put_order, assigned_stock_sold=True)])
st.open_spreads[0].assigned_shares_sold = 200.0
fb = FakeBroker({"status": "Submitted", "filled": 1.0, "avg_price": 3.4, "exec_ids": ["q"]},
                {}, puts={S_KEY: 0.0, L_KEY: 1.0})
run(st, fb)
r = rows(st, "ASSIGNED_LONG_SOLD")
check("1 of 2 long puts filled -> that one is booked; the spread keeps 1 assigned contract",
      len(r) == 1 and r[0]["contracts"] == 1 and first(st).assigned_contracts == 1
      and first(st).contracts == 1, str((st.trade_log, st.open_spreads)))
check("...still working -> no second put order", [c[0] for c in fb.calls] == ["order_progress"], str(fb.calls))
fb.progress = {"status": "Filled", "filled": 2.0, "avg_price": 3.4, "exec_ids": ["q", "q2"]}
fb.puts = {S_KEY: 0.0, L_KEY: 0.0}
run(st, fb)
check("...the rest fills -> booked, spread complete and gone; P&L = 2 x 3.40 x 100",
      st.open_spreads == [] and abs(sum(x["pnl"] for x in rows(st, "ASSIGNED_LONG_SOLD")) - 680.0) < 1e-6,
      str(st.trade_log))

# =============================================================================================
print("\nTRACKING STARTS WHEN A SALE DOES NOT FILL")
st = OptionsState(open_spreads=[spread({})])
fb = FakeBroker(None, {"IWM": (200.0, KS)})
fb.status = "PreSubmitted"                                   # e.g. the market is closed
run(st, fb)
uo = first(st).unwind_order
check("an unfilled share sale is tracked: leg stock, its permId, 200 shares, 200 held when placed",
      uo.get("leg") == "stock" and uo.get("permId") == 8 and uo.get("qty") == 200.0
      and uo.get("held_before") == 200.0 and uo.get("placed") == TODAY, str(uo))
st = OptionsState(open_spreads=[spread({}, assigned_stock_sold=True)])
st.open_spreads[0].assigned_shares_sold = 200.0
fb = FakeBroker(None, {}, puts={S_KEY: 0.0, L_KEY: 2.0})
fb.put_status = "Submitted"
run(st, fb)
uo = first(st).unwind_order
check("an unfilled long-put sale is tracked: leg put, its permId, 2 contracts, 2 held when placed",
      uo.get("leg") == "put" and uo.get("permId") == 9 and uo.get("qty") == 2.0
      and uo.get("held_before") == 2.0, str(uo))

# =============================================================================================
print("\nTHE ESCALATION SAYS AN ORDER IS WORKING")
st = OptionsState(open_spreads=[spread(stock_order())])
n = asg.escalation_notes(st.open_spreads, TODAY)
check("the daily line names the working order (permId)", n and "unwind order working at IB (permId 7)" in n[0],
      str(n))

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} unwind-tracking checks behaved as expected")
