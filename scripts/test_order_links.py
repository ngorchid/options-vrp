"""Tests for the order -> IB record links: orderRef tag and execution ids. No IB, no network.

Every combo order must carry orderRef "options-vrp:<run id>" and every fill's IB execution ids
(= Flex `ibExecID`; one per leg, plus the combo's own) must reach trade_log, or the audit trail
cannot tie this sleeve's records to IB's. Driven through the REAL OptionsBroker and OptionsState
against a fake IB.

Run: python scripts/test_order_links.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from options_vrp.broker import OptionsBroker  # noqa: E402
from options_vrp.state import OpenSpread, OptionsState  # noqa: E402

REF = "options-vrp:20261006-213001"
_fails: list[str] = []
_ran = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _ran
    _ran += 1
    if not cond:
        _fails.append(f"{label}  | {detail}")
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + ("" if cond else f"   <- {detail}"))


class FakeIB:
    """Fills every combo at once with a BAG execution plus one per leg; `fills_after` sleeps
    before the execDetails arrive."""

    def __init__(self, fills_after: int = 0, reports: bool = True):
        self.sent, self.n, self.fills_after, self._pending = [], 0, fills_after, None
        self.reports = reports

    def qualifyContracts(self, *cs):
        for i, c in enumerate(cs):
            c.conId = int(float(getattr(c, "strike", 0) or 0) * 10) or (100 + i)   # leg-specific
        return list(cs)

    def placeOrder(self, contract, order):
        self.n += 1
        self.sent.append(order)
        fills = [NS(execution=NS(execId=f"0002.{self.n}.0{k}.01"),
                    commissionReport=NS(execId=f"0002.{self.n}.0{k}.01" if self.reports else "",
                                        commission=(0.0 if k == 1 else 1.30)))
                 for k in (1, 2, 3)]                 # the combo-level fill carries no commission
        t = NS(order=order, fills=[] if self.fills_after else fills,
               orderStatus=NS(status="Filled", avgFillPrice=-0.74, filled=order.totalQuantity))
        self._pending = [t, fills, self.fills_after]
        return t

    def sleep(self, s):
        if self._pending and not self._pending[0].fills:
            self._pending[2] -= 1
            if self._pending[2] <= 0:
                self._pending[0].fills = self._pending[1]

    def cancelOrder(self, o):
        pass


def spread() -> OpenSpread:
    vals = dict(key="IWM_2026-11-20_263_256", ticker="IWM", expiry="2026-11-20",
                short_strike=263.0, long_strike=256.0, contracts=2)
    ps = inspect.signature(OpenSpread).parameters
    return OpenSpread(**{k: vals.get(k, p.default if p.default is not inspect._empty else 0)
                         for k, p in ps.items()})


def broker(ib, set_ref=True) -> OptionsBroker:
    b = OptionsBroker(dry_run=False)
    b.ib = ib
    if set_ref:
        b.order_ref = REF
    return b


print("ORDER TAGS")
ib = FakeIB()
b = broker(ib)
op = b.open_spread(spread(), wait=2)
check("a combo OPEN carries orderRef", ib.sent[-1].orderRef == REF, repr(ib.sent[-1].orderRef))
cl = b.close_spread(spread(), wait=2)
check("a combo CLOSE carries orderRef", ib.sent[-1].orderRef == REF, repr(ib.sent[-1].orderRef))
nb = OptionsBroker.__new__(OptionsBroker)          # built without __init__: no order_ref attribute
nb.ib, nb.dry_run = FakeIB(), False
try:
    r = nb.open_spread(spread(), wait=2)
except Exception as e:  # noqa: BLE001 -- a crash is a FAILED check here, not a test crash
    r = {"status": f"crashed: {type(e).__name__}: {e}"}
check("a broker without a tag still places the order (a tag never blocks a trade)",
      r.get("status") == "Filled", str(r))

print("\nEXECUTION IDS")
check("a filled combo returns ALL its execution ids (combo + both legs)",
      op.get("exec_ids") == ["0002.1.01.01", "0002.1.02.01", "0002.1.03.01"], str(op))
check("...and its orderRef", op.get("order_ref") == REF, str(op))
late = broker(FakeIB(fills_after=3)).open_spread(spread(), wait=2)
check("execution details arriving just after 'Filled' are still captured",
      len(late.get("exec_ids") or []) == 3, str(late))

print("\nLEDGER")
st = OptionsState()
sp = spread()
st.record_open(sp, op["net_price"], "2026-10-06", order_ref=op["order_ref"], exec_ids=op["exec_ids"])
row = st.trade_log[-1]
check("OPEN keeps orderRef and all execution ids",
      row.get("order_ref") == REF and len(row.get("exec_ids") or []) == 3, str(row))
st.record_close(sp, cl["net_price"], "2026-10-20", "test", order_ref=cl["order_ref"],
                exec_ids=cl["exec_ids"])
row = st.trade_log[-1]
check("CLOSE keeps orderRef and all execution ids",
      row.get("order_ref") == REF and row.get("exec_ids") == cl["exec_ids"]
      and len(cl["exec_ids"]) == 3, str(row))

# ---------------------------------------------------------------------------------------------
# RUNNER WIRING. Everything above hands the broker its tag by hand, so it cannot see the runner
# forgetting to: deleting that one line in run_options_paper.py sent every order out untagged
# while all of the above stayed green. These pin the runner's own wiring.
print("\nCONID / COMMISSION / CURRENCY (additive, 2026-10-07)")
check("a combo fill carries both legs' conids (short, long), total commission and currency",
      op.get("conids") == [2630, 2560], str(op))
check("...commission = sum over the combo and leg reports (0 + 1.30 + 1.30)",
      abs((op.get("commission") or 0) - 2.60) < 1e-9 and op.get("currency") == "USD", str(op))
_nr = broker(FakeIB(reports=False)).open_spread(spread(), wait=2)
check("commission reports that never arrive -> None, the combo is still Filled",
      _nr.get("status") == "Filled" and _nr.get("commission") is None, str(_nr))

print("\nRUNNER WIRING")
import re  # noqa: E402
from dataclasses import asdict  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
import run_options_paper as runner  # noqa: E402

check("ORDER_REF is 'options-vrp:<YYYYmmdd-HHMMSS>'",
      re.fullmatch(r"options-vrp:\d{8}-\d{6}", runner.ORDER_REF) is not None, runner.ORDER_REF)
check("make_broker tags the broker with this run's ORDER_REF",
      runner.make_broker(dry_run=True).order_ref == runner.ORDER_REF, "")
_src = inspect.getsource(runner.run_live)
check("run_live builds its broker through make_broker, never OptionsBroker() directly",
      "make_broker(" in _src and "OptionsBroker(" not in _src, "")
check("run_live books opens and closes through book_open / book_close",
      "book_open(" in _src and "book_close(" in _src, "")

print("\nRUNNER BOOKING -> trade_log")
_st = OptionsState()
check("book_open books a filled OPEN", runner.book_open(_st, spread(), op, 0.50, "2026-10-06"), "")
_row = _st.trade_log[-1] if _st.trade_log else {}
check("...with its orderRef and all three execution ids",
      _row.get("order_ref") == REF and _row.get("exec_ids") == op["exec_ids"]
      and len(op["exec_ids"]) == 3, str(_row))
_sub = {**op, "status": "Submitted", "exec_ids": [], "permId": 77}
_st3 = OptionsState()
runner.book_open(_st3, spread(), _sub, 0.50, "2026-10-06")
check("an optimistically booked OPEN is tracked as pending WITH its orderRef",
      len(_st3.pending_orders) == 1 and _st3.pending_orders[0].get("order_ref") == REF,
      str(_st3.pending_orders))
check("a dead OPEN (Cancelled) is not booked",
      runner.book_open(OptionsState(), spread(), {**op, "status": "Cancelled"}, 0.5, "x") is False, "")
_row = runner.book_close(_st, spread(), cl, "profit_target", "2026-10-20")
_log = _st.trade_log[-1]
check("book_open writes conids, commission and currency into the OPEN row",
      _st.trade_log[0].get("conids") == op["conids"] and abs((_st.trade_log[0].get("commission") or 0) - 2.60) < 1e-9
      and _st.trade_log[0].get("currency") == "USD", str(_st.trade_log[0]))
check("book_close writes the CLOSE with its orderRef and all execution ids",
      _log.get("action") == "CLOSE" and _log.get("order_ref") == REF
      and _log.get("exec_ids") == cl["exec_ids"] and len(cl["exec_ids"]) == 3, str(_log))
_st4 = OptionsState()
runner.book_open(_st4, spread(), op, 0.50, "2026-10-06")
runner.book_close(_st4, spread(), {**cl, "status": "PreSubmitted", "permId": 88}, "dte", "x")
check("an unfilled CLOSE is not booked but tracked as pending WITH its orderRef",
      _st4.has(spread().key) and len(_st4.pending_orders) == 1
      and _st4.pending_orders[0].get("order_ref") == REF, str(_st4.pending_orders))

# ---------------------------------------------------------------------------------------------
# SELF-HEAL. A late fill booked on the NEXT run has no execution ids (the API returns only the
# current day's), so its orderRef is the one link to IB's records left. It must survive.
print("\nSELF-HEAL (pending orders booked on a later run)")
from options_vrp.broker import _ib_expiry  # noqa: E402


class HealBroker:
    """Collaborator stub: IB shows the legs `held` or flat, and the order filled at 0.70."""

    def __init__(self, held):
        self.held = held

    def put_positions(self):
        sp = spread()
        e = _ib_expiry(sp.expiry)
        q = 2.0 if self.held else 0.0
        return {(sp.ticker, e, float(sp.short_strike)): -q, (sp.ticker, e, float(sp.long_strike)): q}

    def order_fill(self, perm_id):
        return ("Filled", 0.70)

    def spread_values(self, sps):
        return {}


_h = OptionsState()
_h.pending_orders = [{"action": "open", "permId": 1, "spread": asdict(spread()),
                      "placed_date": "2026-10-05", "order_ref": REF}]
runner._resolve_pending(HealBroker(held=True), _h, "2026-10-06")
check("a late OPEN booked by self-heal keeps its orderRef",
      _h.has(spread().key) and _h.trade_log and _h.trade_log[-1].get("order_ref") == REF,
      str(_h.trade_log))
_h2 = OptionsState()
_h2.record_open(spread(), 0.74, "2026-10-01")
_h2.pending_orders = [{"action": "close", "permId": 2, "spread": asdict(spread()),
                       "placed_date": "2026-10-05", "order_ref": REF}]
runner._resolve_pending(HealBroker(held=False), _h2, "2026-10-06")
check("a late CLOSE booked by self-heal keeps its orderRef",
      not _h2.has(spread().key) and _h2.trade_log[-1].get("action") == "CLOSE"
      and _h2.trade_log[-1].get("order_ref") == REF, str(_h2.trade_log[-1:]))

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} order-link checks behaved as expected")
