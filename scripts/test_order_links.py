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

    def __init__(self, fills_after: int = 0):
        self.sent, self.n, self.fills_after, self._pending = [], 0, fills_after, None

    def qualifyContracts(self, *cs):
        for i, c in enumerate(cs):
            c.conId = 100 + i
        return list(cs)

    def placeOrder(self, contract, order):
        self.n += 1
        self.sent.append(order)
        fills = [NS(execution=NS(execId=f"0002.{self.n}.0{k}.01")) for k in (1, 2, 3)]
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
r = nb.open_spread(spread(), wait=2)
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

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} order-link checks behaved as expected")
