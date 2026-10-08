"""Tests for the early-assignment fix (2026-10-07): detect, value, unwind, alert.

Replaces the earlier suite that PINNED the old behaviour (spread unmarked, unmanaged, invisible to
the breaker and the NAV snapshot) -- that behaviour is now deliberately changed. Fake IB / fake
broker only; no network. scripts/mutate_assignment.py seeds the faults.

Run: python scripts/test_assignment.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from options_vrp import assignment as asg  # noqa: E402
from options_vrp.broker import OptionsBroker  # noqa: E402
from options_vrp.state import OpenSpread, OptionsState  # noqa: E402
import run_options_paper as runner  # noqa: E402

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


def sp(n=2, **kw) -> OpenSpread:
    return OpenSpread(ticker="IWM", expiry="2026-11-20", short_strike=263.0, long_strike=256.0,
                      contracts=n, entry_credit=0.74, max_loss=6.26, entry_date="2026-10-07",
                      entry_spot=270.0, **kw)


S, L = ("IWM", "20261120", 263.0), ("IWM", "20261120", 256.0)

# =============================================================================================
print("DETECT — the fingerprint: short leg drops by n AND 100 x n shares at the short strike")
a = asg.detect([sp()], {S: 0.0, L: 2.0}, {"IWM": (200.0, 263.0)})
check("full assignment, exact quantity and price -> exact", len(a) == 1 and a[0].exact and a[0].assigned == 2
      and a[0].shares_expected == 200, str(a))
a = asg.detect([sp()], {S: -1.0, L: 2.0}, {"IWM": (100.0, 263.0)})
check("partial assignment (1 of 2) -> exact for the assigned contract", len(a) == 1 and a[0].exact
      and a[0].assigned == 1, str(a))
a = asg.detect([sp()], {S: 0.0, L: 2.0}, {"IWM": (250.0, 230.0)})
check("more shares than delivered / blended cost (magic-formula holds some) -> NOT exact",
      len(a) == 1 and not a[0].exact and "NOT unwound automatically" in a[0].note, str(a))
a = asg.detect([sp()], {S: 0.0, L: 2.0}, {"IWM": (200.0, 250.0)})
check("right quantity but the wrong price -> NOT exact", len(a) == 1 and not a[0].exact, str(a))
a = asg.detect([sp()], {S: 0.0, L: 2.0}, {"IWM": (250.0, 263.0)})
check("right price but MORE shares than delivered -> NOT exact (never sell what is not provably ours)",
      len(a) == 1 and not a[0].exact, str(a))
a = asg.detect([sp()], {S: 0.0, L: 2.0}, {})
check("short leg gone with NO stock -> flagged for manual review", len(a) == 1 and not a[0].exact
      and "manual review" in a[0].note, str(a))
check("both legs intact -> nothing", asg.detect([sp()], {S: -2.0, L: 2.0}, {"IWM": (200.0, 263.0)}) == [], "")
check("positions unavailable -> no claim", safe(asg.detect, [sp()], None, {}) == []
      and safe(asg.detect, [sp()], {S: 0.0}, None) == [], "")
check("a spread already marked ASSIGNED is not detected twice",
      asg.detect([sp(assigned_contracts=2)], {S: 0.0, L: 2.0}, {"IWM": (200.0, 263.0)}) == [], "")

# =============================================================================================
print("\nVALUE — the assigned position is in the breaker and the NAV snapshot")
pm = {S: 9.0, L: 3.5}
sm = {"IWM": 254.0}
check("an intact spread is valued exactly as before: (c - (Ps - Pl)) x 100 x N",
      abs(asg.unrealized(sp(), pm, sm) - (0.74 - 5.5) * 200) < 1e-9, str(asg.unrealized(sp(), pm, sm)))
u = asg.unrealized(sp(assigned_contracts=2), {L: 3.5}, sm)
check("fully assigned: (c + S - Ks + Pl) x 100 x n", u is not None and abs(u - (0.74 + 254 - 263 + 3.5) * 200) < 1e-9,
      str(u))
u = asg.unrealized(sp(assigned_contracts=1), pm, sm)
check("partially assigned: intact part + assigned part",
      u is not None and abs(u - ((0.74 - 5.5) + (0.74 + 254 - 263 + 3.5)) * 100) < 1e-9, str(u))
u = asg.unrealized(sp(assigned_contracts=2, assigned_stock_sold=True), {L: 3.5}, {})
check("shares already sold: only the long puts remain (stock leg realised, not double-counted)",
      u is not None and abs(u - 3.5 * 200) < 1e-9, str(u))
check("a missing mark -> None (cannot value), never a silent zero",
      asg.unrealized(sp(assigned_contracts=2), {L: 3.5}, {}) is None
      and asg.unrealized(sp(), {L: 3.5}, sm) is None, "")
st = OptionsState(open_spreads=[sp(assigned_contracts=2)])
check("book_unrealized includes the ASSIGNED spread (breaker + snapshot input)",
      abs(runner.book_unrealized(st, {}, {L: 3.5}, sm) - (0.74 + 254 - 263 + 3.5) * 200) < 1e-9, "")
st2 = OptionsState(open_spreads=[sp(), OpenSpread(ticker="XLE", expiry="2026-11-20", short_strike=90.0,
                                                    long_strike=85.0, contracts=3, entry_credit=0.7,
                                                    max_loss=4.3, entry_date="2026-10-07",
                                                    entry_spot=95.0, assigned_contracts=3)])
_v = {sp().key: 5.5}
_u = runner.book_unrealized(st2, _v, {("XLE", "20261120", 85.0): 0.5}, {"XLE": 88.0})
check("an INTACT spread in book_unrealized keeps the old formula (c - value) x 100 x N, "
      "summed with an assigned one",
      abs(_u - ((0.74 - 5.5) * 200 + (0.7 + 88 - 90 + 0.5) * 300)) < 1e-9, str(_u))
_W: list[str] = []
_ow = runner.logging.warning
runner.logging.warning = lambda f, *a: _W.append(f % a if a else f)
_u2 = runner.book_unrealized(st2, _v, {}, {})
runner.logging.warning = _ow
check("an ASSIGNED spread that cannot be marked is left out WITH a warning (never silently)",
      abs(_u2 - (0.74 - 5.5) * 200) < 1e-9 and any("cannot value ASSIGNED" in w for w in _W), str((_u2, _W)))
src = inspect.getsource(runner.run_live)
check("the circuit breaker's drawdown uses book_unrealized",
      "_unreal = book_unrealized(state, values, *broker.marks())" in src, "")
check("the daily NAV snapshot uses book_unrealized",
      "unreal = book_unrealized(state, values, *_marks)\n        state.record_snapshot" in src, "")

# =============================================================================================
print("\nUNWIND — sell the shares FIRST, then the paired long puts; SAFETY, never blocked")


class FakeBroker:
    def __init__(self, puts, stocks, fills=("Filled", "Filled"), marks=None):
        self.puts, self.stocks, self.fills, self.calls = puts, stocks, list(fills), []
        self._marks = marks or ({L: 3.5}, {"IWM": 254.0})

    def put_positions(self):
        return self.puts

    def stock_positions_detail(self):
        return self.stocks

    def marks(self):
        return self._marks

    def _fill(self, what, price):
        st = self.fills.pop(0) if self.fills else "Filled"
        self.calls.append(what)
        return {"status": st, "price": price if st == "Filled" else None, "label": what[3],
                "exec_ids": [f"x{len(self.calls)}"], "order_ref": "options-vrp:R"}

    def order_progress(self, perm_id):
        """A tracked order from the previous run: by default it ended unfilled (cancelled at the
        close), so the unwind places a fresh one. test_unwind_tracking.py covers the rest."""
        return {"status": "Cancelled", "filled": 0.0, "avg_price": None, "exec_ids": []}

    def sell_stock(self, ticker, shares, label="SAFETY: assignment unwind"):
        return self._fill(("STK", ticker, shares, label), 255.0)

    def sell_put(self, ticker, expiry, strike, contracts, label="SAFETY: assignment unwind"):
        return self._fill(("PUT", ticker, strike, contracts, label), 1.5)


LOG: list[tuple[str, str]] = []
_orig = (runner.logging.error, runner.logging.warning)


def _fmt(f, a):
    try:                       # like real logging: a bad format never raises into the caller
        return f % a if a else f
    except Exception:  # noqa: BLE001
        return f"{f} {a}"


runner.logging.error = lambda f, *a: LOG.append(("E", _fmt(f, a)))
runner.logging.warning = lambda f, *a: LOG.append(("W", _fmt(f, a)))

st = OptionsState(open_spreads=[sp()])
fb = FakeBroker({S: 0.0, L: 2.0}, {"IWM": (200.0, 263.0)})
acts = safe(runner.handle_assignments, fb, st, "2026-10-08")
check("full assignment: SELL 200 shares, then SELL 2 long 256P — in that order",
      [c[:3] for c in fb.calls] == [("STK", "IWM", 200), ("PUT", "IWM", 256.0)]
      and fb.calls[1][3] == 2, str(fb.calls))
check("every unwind order is labelled SAFETY", all("SAFETY" in c[-1] for c in fb.calls), str(fb.calls))
check("the spread is gone once both legs are sold", st.open_spreads == [], str(st.open_spreads))
pnl = st.realized_pnl
check("P&L = (c + S_sale - Ks) x 200 + Pl_sale x 200 = -1452 + 300",
      abs(pnl - ((0.74 + 255 - 263) * 200 + 1.5 * 200)) < 1e-9, str(pnl))
check("...and never worse than the spread's max loss (the wing still capped it)",
      pnl >= -(263 - 256 - 0.74) * 200 - 1e-9, str(pnl))
acts_rows = [r["action"] for r in st.trade_log]
check("ledger: ASSIGNED, ASSIGNED_STOCK_SOLD, ASSIGNED_LONG_SOLD rows, with tag + exec ids",
      acts_rows == ["ASSIGNED", "ASSIGNED_STOCK_SOLD", "ASSIGNED_LONG_SOLD"]
      and all(r.get("order_ref") and r.get("exec_ids") for r in st.trade_log[1:]), str(st.trade_log))
check("the unwind's fills are returned for the email", isinstance(acts, list) and len(acts) == 2, str(acts))
check("same-day alert at ERROR with specifics (key, shares, strike, exposure)",
      any(lv == "E" and "ASSIGNED IWM_2026-11-20_263_256" in m and "200" in m and "263" in m
          and "$50,800" in m for lv, m in LOG), str(LOG))

LOG.clear()
st = OptionsState(open_spreads=[sp()])
fb = FakeBroker({S: -1.0, L: 2.0}, {"IWM": (100.0, 263.0)})
safe(runner.handle_assignments, fb, st, "2026-10-08")
check("partial (1 of 2): sells 100 shares and ONE long put",
      [c[:4] for c in fb.calls] == [("STK", "IWM", 100, "SAFETY: assignment unwind"),
                                    ("PUT", "IWM", 256.0, 1)], str(fb.calls))
check("...and the remaining contract stays a normal, paired spread",
      len(st.open_spreads) == 1 and st.open_spreads[0].contracts == 1
      and st.open_spreads[0].assigned_contracts == 0, str(st.open_spreads))

st = OptionsState(open_spreads=[sp()])
fb = FakeBroker({S: 0.0, L: 2.0}, {"IWM": (200.0, 263.0)}, fills=("Submitted",))
safe(runner.handle_assignments, fb, st, "2026-10-08")
check("share sale NOT filled -> the long put is NOT sold (it protects the shares)",
      [c[0] for c in fb.calls] == ["STK"] and st.open_spreads[0].assigned_contracts == 2
      and not st.open_spreads[0].assigned_stock_sold, str(fb.calls))
fb2 = FakeBroker({S: 0.0, L: 2.0}, {"IWM": (200.0, 263.0)})
safe(runner.handle_assignments, fb2, st, "2026-10-09")
check("...the order ended unfilled (cancelled): retried on the next run and completed", [c[0] for c in fb2.calls] == ["STK", "PUT"]
      and st.open_spreads == [], str(fb2.calls))

st = OptionsState(open_spreads=[sp()])
fb = FakeBroker({S: 0.0, L: 2.0}, {"IWM": (200.0, 263.0)}, fills=("Filled", "Submitted"))
safe(runner.handle_assignments, fb, st, "2026-10-08")
check("shares sold but the long-put sale NOT filled: stock leg booked, spread kept for the retry",
      len(st.open_spreads) == 1 and st.open_spreads[0].assigned_stock_sold
      and [r["action"] for r in st.trade_log] == ["ASSIGNED", "ASSIGNED_STOCK_SOLD"], str(st.trade_log))
fb2 = FakeBroker({S: 0.0, L: 2.0}, {"IWM": (0.0, None)})
safe(runner.handle_assignments, fb2, st, "2026-10-09")
check("...the put order ended unfilled: next run sells ONLY the long puts and completes",
      [c[0] for c in fb2.calls] == ["PUT"] and st.open_spreads == [], str(fb2.calls))

st = OptionsState(open_spreads=[sp(assigned_contracts=2, assigned_auto=True)])
fb = FakeBroker({S: 0.0, L: 2.0}, {})
safe(runner.handle_assignments, fb, st, "2026-10-09")
check("shares no longer held at IB -> NOT sold (selling would open a short)", fb.calls == []
      and any("NOT selling" in m for _, m in LOG), str(fb.calls))
st = OptionsState(open_spreads=[sp(assigned_contracts=2, assigned_auto=True, assigned_stock_sold=True)])
fb = FakeBroker({S: 0.0, L: 1.0}, {})
safe(runner.handle_assignments, fb, st, "2026-10-09")
check("fewer long puts held than needed -> NOT sold (selling would write a naked put)", fb.calls == []
      and any("naked put" in m for _, m in LOG), str(fb.calls))
st = OptionsState(open_spreads=[sp(assigned_contracts=2, assigned_auto=False)])
fb = FakeBroker({S: 0.0}, {})
safe(runner.handle_assignments, fb, st, "2026-10-10")
check("long puts gone at IB (unwound by hand / expired) -> spread retired, logged, no orders",
      fb.calls == [] and st.open_spreads == [] and st.trade_log
      and st.trade_log[-1]["action"] == "ASSIGNED_CLOSED_OUTSIDE", str(st.trade_log))
st = OptionsState(open_spreads=[sp(assigned_contracts=2, assigned_auto=False)])
fb = FakeBroker(None, None)
safe(runner.handle_assignments, fb, st, "2026-10-10")
check("...but NOT when IB positions are unavailable (unknown is not 'gone')",
      len(st.open_spreads) == 1, str(st.open_spreads))

LOG.clear()
st = OptionsState(open_spreads=[sp()])
fb = FakeBroker({S: 0.0, L: 2.0}, {"IWM": (250.0, 230.0)})
safe(runner.handle_assignments, fb, st, "2026-10-08")
check("NOT exact (magic-formula also holds IWM): no automatic orders, valued, MANUAL alert",
      fb.calls == [] and st.open_spreads[0].assigned_contracts == 2
      and not st.open_spreads[0].assigned_auto
      and any("MANUAL" in m for lv, m in LOG if lv == "E"), str(LOG))
LOG.clear()
safe(runner.handle_assignments, fb, st, "2026-10-09")
check("...and the manual alert repeats every run until it is resolved",
      any("MANUAL unwind" in m for lv, m in LOG if lv == "E"), str(LOG))
st = OptionsState(open_spreads=[sp()])
safe(runner.handle_assignments, FakeBroker({S: 0.0, L: 2.0}, {}), st, "2026-10-08")
check("short gone with NO stock: alerted, not marked (nothing to value or sell)",
      st.open_spreads[0].assigned_contracts == 0, str(st.open_spreads))

hsrc = inspect.getsource(runner.handle_assignments)
check("SAFETY: the unwind consults no automated guard (cfg, check_order, liquidity, breaker)",
      not any(g in hsrc for g in ("cfg.", "check_order", "liquidity_check", "circuit_breaker",
                                  "max_positions", "halt")), "")
_h, _m = src.find("handle_assignments(broker, state, today)"), src.find("# 1) MANAGE open spreads")
check("run_live runs it BEFORE management and the guards' effects (opens only) do not reach it",
      0 <= _h < _m, f"{_h} {_m}")
check("the manage loop skips ASSIGNED spreads (no combo close on a broken pair)",
      "if getattr(sp, \"assigned_contracts\", 0) or getattr(sp, \"assign_suspected\", 0):\n                continue" in src, "")
check("the reconcile expects an assigned short leg to be gone (no daily false PHANTOM)",
      "exp[(sp.ticker, e, float(sp.short_strike))] = -float(sp.contracts - _n_a)" in src, "")
runner.logging.error, runner.logging.warning = _orig

# =============================================================================================
print("\nBROKER READS AND ORDERS (fake IB)")


def pos(sym, qty, cost, sec="STK"):
    return NS(contract=NS(secType=sec, symbol=sym), position=qty, avgCost=cost)


b = OptionsBroker(dry_run=False)
b.ib = NS(positions=lambda: [pos("IWM", 100, 263.0), pos("IWM", 100, 263.0), pos("XLE", 0, 1.0),
                             pos("IWM", 3, 1.0, sec="OPT")])
check("stock_positions_detail: shares summed, average cost per share, options ignored",
      b.stock_positions_detail() == {"IWM": (200.0, 263.0)}, str(b.stock_positions_detail()))
b.ib = NS(positions=lambda: (_ for _ in ()).throw(ConnectionError("down")))
check("a failing positions read -> None, never raises", safe(b.stock_positions_detail) is None, "")
d = OptionsBroker(dry_run=True)
d.ib = NS(positions=lambda: [pos("IWM", 200, 263.0)])
_dr = safe(d.sell_stock, "IWM", 200)
check("dry-run never reads or trades", d.stock_positions_detail() is None
      and isinstance(_dr, dict) and _dr.get("status") == "dryrun", str(_dr))


class OrderIB:
    def __init__(self):
        self.sent = []

    def qualifyContracts(self, c):
        return [c]

    def placeOrder(self, c, o):
        self.sent.append((c, o))
        return NS(order=o, orderStatus=NS(status="Filled", avgFillPrice=255.0),
                  fills=[NS(execution=NS(execId="E1"))])

    def sleep(self, s):
        pass


ob = OptionsBroker(dry_run=False)
ob.ib, ob.order_ref = OrderIB(), "options-vrp:R9"
f = ob.sell_stock("IWM", 200)
c, o = ob.ib.sent[-1]
check("sell_stock: a tagged SELL market order for the shares, fill + exec ids returned",
      c.secType == "STK" and o.action == "SELL" and o.totalQuantity == 200 and o.orderRef == "options-vrp:R9"
      and f["status"] == "Filled" and f["exec_ids"] == ["E1"] and f["price"] == 255.0, str((c, o, f)))
check("...labelled SAFETY by default (the runner passes no label)", f["label"].startswith("SAFETY"), f["label"])
f = ob.sell_put("IWM", "2026-11-20", 256.0, 2)
c, o = ob.ib.sent[-1]
check("sell_put: SELL the long put (right P, strike, expiry), tagged",
      c.secType == "OPT" and c.right == "P" and c.strike == 256.0 and c.lastTradeDateOrContractMonth == "20261120"
      and o.action == "SELL" and o.totalQuantity == 2 and o.orderRef == "options-vrp:R9", str((c, o)))

mk = OptionsBroker(dry_run=False)
mk.ib = NS(portfolio=lambda: [
    NS(contract=NS(secType="OPT", right="P", symbol="IWM", lastTradeDateOrContractMonth="20261120",
                   strike=256.0), marketPrice=3.5),
    NS(contract=NS(secType="OPT", right="C", symbol="IWM", lastTradeDateOrContractMonth="20261120",
                   strike=300.0), marketPrice=9.9),
    NS(contract=NS(secType="STK", symbol="IWM"), marketPrice=254.0),
    NS(contract=NS(secType="FUT", symbol="MCL"), marketPrice=88.0)])
pm2, sm2 = mk.marks()
check("marks(): put marks keyed (symbol, expiry, strike), stock marks by symbol; calls and futures ignored",
      pm2 == {("IWM", "20261120", 256.0): 3.5} and sm2 == {"IWM": 254.0}, str((pm2, sm2)))
mk.ib = NS(portfolio=lambda: (_ for _ in ()).throw(ConnectionError("down")))
check("marks(): a failing read returns empty marks (-> 'cannot value'), never raises",
      safe(mk.marks) == ({}, {}), str(safe(mk.marks)))
check("marks(): dry-run returns empty marks", OptionsBroker(dry_run=True).marks() == ({}, {}), "")
qb = OptionsBroker(dry_run=False)
qb.ib = NS(qualifyContracts=lambda c: [], placeOrder=lambda c, o: (_ for _ in ()).throw(AssertionError("placed")))
qf = safe(qb.sell_stock, "IWM", 200)
check("an unqualifiable contract places NO order and reports qualify_failed",
      isinstance(qf, dict) and qf.get("status") == "qualify_failed", str(qf))

print("\nOLDER STATE STILL LOADS")
old = {"ticker": "IWM", "expiry": "2026-11-20", "short_strike": 263.0, "long_strike": 256.0,
       "contracts": 2, "entry_credit": 0.74, "max_loss": 6.26, "entry_date": "2026-10-07",
       "entry_spot": 270.0, "peak_value": 0.9}
o2 = safe(OpenSpread, **old)
check("a spread saved before the assignment fields existed loads with them at 0/False",
      not isinstance(o2, str) and o2.assigned_contracts == 0 and not o2.assigned_stock_sold, str(o2))

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} assignment checks behaved as expected")
