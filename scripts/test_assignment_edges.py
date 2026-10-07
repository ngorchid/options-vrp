"""Detection edge cases for early assignment (2026-10-07): PINS what the code does TODAY.

Nothing here changes behaviour. Each case records the current outcome -- detected or not,
automatic or manual, alerted or not, valued or not -- so a change to any of them is deliberate.
Cases where a position can sit outside normal management are marked PROPOSAL in the label and
listed in the documentation (Appendix E); they are decisions for the owner, not fixes made here.
Drives the REAL detect / handle_assignments / book_unrealized with a fake broker.
scripts/mutate_assignment_edges.py seeds the faults.

Run: python scripts/test_assignment_edges.py
"""
from __future__ import annotations

import sys
from pathlib import Path

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


E = "20261120"


def sp(t="IWM", ks=263.0, kl=256.0, n=2, expiry="2026-11-20", **kw):
    return OpenSpread(ticker=t, expiry=expiry, short_strike=ks, long_strike=kl, contracts=n,
                      entry_credit=0.74, max_loss=ks - kl - 0.74, entry_date="2026-10-01",
                      entry_spot=ks * 1.05, **kw)


def puts(short, long_, t="IWM", ks=263.0, kl=256.0, e=E):
    return {(t, e, ks): short, (t, e, kl): long_}


class FakeBroker:
    def __init__(self, put_pos, stocks, marks=None):
        self.put_pos, self.stocks, self.calls = put_pos, stocks, []
        self._marks = marks or ({("IWM", E, 256.0): 3.5, ("IWM", E, 263.0): 9.0}, {"IWM": 254.0})

    def put_positions(self):
        return self.put_pos

    def stock_positions_detail(self):
        return self.stocks

    def marks(self):
        return self._marks

    def sell_stock(self, ticker, shares, label="SAFETY: assignment unwind"):
        self.calls.append(("STK", ticker, shares))
        return {"status": "Filled", "price": 254.0, "exec_ids": ["s"], "order_ref": "options-vrp:R"}

    def sell_put(self, ticker, expiry, strike, contracts, label="SAFETY: assignment unwind"):
        self.calls.append(("PUT", ticker, strike, contracts))
        return {"status": "Filled", "price": 3.5, "exec_ids": ["p"], "order_ref": "options-vrp:R"}


LOG: list[tuple[str, str]] = []
runner.logging.error = lambda f, *a: LOG.append(("E", f % a if a else f))
runner.logging.warning = lambda f, *a: LOG.append(("W", f % a if a else f))


def run(state, fb):
    LOG.clear()
    return safe(runner.handle_assignments, fb, state, "2026-10-08")


def first(state):
    """The first open spread, or an empty stand-in so a fault fails a check instead of crashing."""
    from types import SimpleNamespace as NS
    return state.open_spreads[0] if state.open_spreads else NS(
        assigned_contracts=0, assigned_auto=None, contracts=0, assigned_stock_sold=None)


def val(spread, pm, sm) -> float:
    r = safe(asg.unrealized, spread, pm, sm)
    return float(r) if isinstance(r, (int, float)) else float("nan")


def errors(needle=""):
    return [m for lv, m in LOG if lv == "E" and needle in m]


# =============================================================================================
print("1. COST BASIS vs STRIKE — absolute tolerance: |avg cost - short strike| < $0.01 per share")
d = lambda avg: asg.detect([sp()], puts(0.0, 2.0), {"IWM": (200.0, avg)})  # noqa: E731
check("avg = strike exactly -> exact (automatic unwind)", d(263.0)[0].exact, str(d(263.0)))
check("avg = strike + $0.005 (e.g. $1 fee on 200 sh) -> still exact", d(263.005)[0].exact, "")
check("avg = strike - $0.009 -> still exact", d(262.991)[0].exact, "")
check("avg = strike + $0.011 -> NOT exact -> MANUAL (a gap of exactly $0.01 still passes: float "
      "263.01 - 263 = 0.00999...)", not d(263.011)[0].exact and d(263.01)[0].exact, "")
check("$1.50 of fees on 100 delivered shares (+$0.015/sh) -> NOT exact -> MANUAL",
      not d(263.015)[0].exact, "")
check("avg unknown (None) -> NOT exact -> MANUAL", not d(None)[0].exact, "")
check("...and the detection says why: 'probably assigned ... NOT unwound automatically'",
      "NOT unwound automatically" in d(263.02)[0].note, d(263.02)[0].note)
st = OptionsState(open_spreads=[sp()])
run(st, FakeBroker(puts(0.0, 2.0), {"IWM": (200.0, 263.02)}))
check("...a cost-basis miss is still recorded, valued and alerted as a MANUAL unwind (never silent)",
      first(st).assigned_contracts == 2 and not first(st).assigned_auto
      and errors("MANUAL"), str(LOG))

# =============================================================================================
print("\n2. DELIVERY SPLIT — shares arrive in pieces, or over two days")
st = OptionsState(open_spreads=[sp()])
fb = FakeBroker(puts(0.0, 2.0), {"IWM": (100.0, 263.0)})              # short gone, half the shares
run(st, fb)
check("day 1: short leg gone, only 100 of 200 shares visible -> ERROR 'manual review', NOT recorded",
      errors("manual review") and first(st).assigned_contracts == 0 and fb.calls == [],
      str(LOG))
u = runner.book_unrealized(st, {}, *fb.marks())
check("PROPOSAL: ...the unrecorded spread then has no spread mark and is left OUT of the breaker / "
      "NAV snapshot with NO warning of its own (only the detection ERROR above)",
      u == 0.0 and not [m for lv, m in LOG if "cannot value" in m], str((u, LOG)))
fb.stocks = {"IWM": (200.0, 263.0)}
run(st, fb)
check("day 2: all 200 shares visible -> exact, recorded and unwound (200 sh, then 2 long puts)",
      fb.calls == [("STK", "IWM", 200), ("PUT", "IWM", 256.0, 2)] and st.open_spreads == [],
      str(fb.calls))

st = OptionsState(open_spreads=[sp()])
fb = FakeBroker(puts(-1.0, 2.0), {"IWM": (100.0, 263.0)})            # 1 of 2 assigned
run(st, fb)
check("1 of 2 assigned on day 1 -> exact for 1: sells 100 sh + 1 long put, 1 contract stays a spread",
      fb.calls == [("STK", "IWM", 100), ("PUT", "IWM", 256.0, 1)]
      and first(st).contracts == 1 and first(st).assigned_contracts == 0,
      str((fb.calls, st.open_spreads)))
fb.put_pos, fb.stocks, fb.calls = puts(0.0, 1.0), {"IWM": (100.0, 263.0)}, []
run(st, fb)
check("...the second contract assigned on day 2 -> detected and unwound the same way",
      fb.calls == [("STK", "IWM", 100), ("PUT", "IWM", 256.0, 1)] and st.open_spreads == [],
      str(fb.calls))

st = OptionsState(open_spreads=[sp(assigned_contracts=1, assigned_date="2026-10-07",
                                    assigned_auto=True)])           # day-1 unwind never filled
fb = FakeBroker(puts(0.0, 2.0), {"IWM": (200.0, 263.0)})             # day 2: the 2nd contract too
dd = asg.detect(st.open_spreads, fb.put_pos, fb.stocks)
check("a 2nd assignment while the 1st unwind is still open is NOT detected that run (spread skipped)",
      dd == [], str(dd))
run(st, fb)
check("...that run unwinds ONLY the recorded contract (100 sh + 1 long put)",
      fb.calls == [("STK", "IWM", 100), ("PUT", "IWM", 256.0, 1)], str(fb.calls))
check("...and the leftover contract is then an unmarked 'spread' whose short leg is gone",
      first(st).contracts == 1 and first(st).assigned_contracts == 0, "")
fb.stocks, fb.put_pos, fb.calls = {"IWM": (100.0, 263.0)}, puts(0.0, 1.0), []
run(st, fb)
check("...next run detects it (exact) and completes: one run late, never lost",
      fb.calls == [("STK", "IWM", 100), ("PUT", "IWM", 256.0, 1)] and st.open_spreads == [],
      str(fb.calls))

# =============================================================================================
print("\n3. TWO ASSIGNMENTS ON ONE NAME, SAME DAY")
a, b = sp(n=2), sp(ks=250.0, kl=245.0, n=1, expiry="2026-12-18")
pp = {**puts(0.0, 2.0), ("IWM", "20261218", 250.0): 0.0, ("IWM", "20261218", 245.0): 1.0}
dd = asg.detect([a, b], pp, {"IWM": (300.0, (200 * 263 + 100 * 250) / 300)})
check("two spreads on the same name (different strikes/expiries) both assigned -> both detected, "
      "both NOT exact (the share total belongs to two spreads)",
      len(dd) == 2 and not any(x.exact for x in dd), str(dd))
st = OptionsState(open_spreads=[a, b])
fb = FakeBroker(pp, {"IWM": (300.0, 258.67)})
run(st, fb)
check("...both are recorded as MANUAL, nothing is sold automatically, both alert",
      all(s.assigned_contracts and not s.assigned_auto for s in st.open_spreads) and fb.calls == []
      and len(errors("still needs a MANUAL unwind")) == 2, str(LOG))
check("(the open loop allows one spread per ticker, so this needs a hand-opened second spread)",
      "if s.ticker in open_tickers" in Path(runner.__file__).read_text(encoding="utf-8"), "")

# =============================================================================================
print("\n4. SHORT LEG GONE, SHARES PRESENT, BUT NOT 100 x n")
dd = asg.detect([sp()], puts(0.0, 2.0), {"IWM": (250.0, 263.0)})
check("MORE shares than 100 x n (250 for 2 contracts) -> detected, NOT exact -> MANUAL",
      len(dd) == 1 and not dd[0].exact and dd[0].stock_qty == 250.0, str(dd))
st = OptionsState(open_spreads=[sp()])
run(st, FakeBroker(puts(0.0, 2.0), {"IWM": (250.0, 263.0)}))
check("...recorded and valued on the 200 delivered shares only", first(st).assigned_contracts == 2
      and abs(val(first(st), {("IWM", E, 256.0): 3.5}, {"IWM": 254.0})
              - (0.74 + 254 - 263 + 3.5) * 200) < 1e-6, "")
st = OptionsState(open_spreads=[sp()])
fb = FakeBroker(puts(0.0, 2.0), {"IWM": (150.0, 263.0)})
run(st, fb)
check("FEWER shares than 100 x n (150 for 2) -> ERROR every run, NOT recorded, nothing sold",
      errors("only 150") and first(st).assigned_contracts == 0 and fb.calls == [], str(LOG))
run(st, fb)
check("...and the ERROR repeats on the next run (it never goes quiet while the mismatch lasts)",
      errors("only 150"), str(LOG))
st = OptionsState(open_spreads=[sp()])
run(st, FakeBroker(puts(0.0, 2.0), {}))
check("short leg gone and NO shares -> ERROR 'closed by hand, or not visible' (manual review)",
      errors("closed by hand") and first(st).assigned_contracts == 0, str(LOG))

# =============================================================================================
print("\n5. BLENDED — magic-formula already holds the name")
st = OptionsState(open_spreads=[sp()])
fb = FakeBroker(puts(0.0, 2.0), {"IWM": (260.0, (60 * 150 + 200 * 263) / 260)})
run(st, fb)
s0 = first(st)
check("flagged: an ERROR naming the shares held vs expected and 'NOT unwound automatically'",
      errors("260 IWM held") and errors("NOT unwound automatically"), str(LOG))
check("recorded: ASSIGNED row, assigned_contracts 2, assigned_auto False",
      s0.assigned_contracts == 2 and not s0.assigned_auto
      and st.trade_log[-1]["action"] == "ASSIGNED" and st.trade_log[-1]["exact"] is False, "")
check("valued: on the 200 delivered shares at the stock mark + the long puts (magic's 60 excluded)",
      abs(val(s0, *fb.marks()) - (0.74 + 254 - 263 + 3.5) * 200) < 1e-6, "")
check("NOT traded: no share sale, no put sale", fb.calls == [], str(fb.calls))
check("alerted: 'still needs a MANUAL unwind', every run", errors("still needs a MANUAL unwind"), "")
notes = asg.escalation_notes(st.open_spreads, "2026-10-12")
check("escalates to URGENT and says MANUAL", notes and "URGENT" in notes[0]
      and "needs a MANUAL unwind" in notes[0], str(notes))
fb.stocks = {"IWM": (60.0, 150.0)}                                   # owner sold 200 by hand...
run(st, fb)
check("owner sold the 200 shares by hand but kept the long puts -> STILL recorded as holding the "
      "shares (the sleeve cannot tell), MANUAL alert continues", st.open_spreads
      and not first(st).assigned_stock_sold and errors("still needs a MANUAL unwind"), str(LOG))
fb.put_pos = puts(0.0, 0.0)                                          # ...then the long puts too
run(st, fb)
check("once the long puts are gone too -> spread retired (ASSIGNED_CLOSED_OUTSIDE), P&L NOT booked",
      st.open_spreads == [] and st.trade_log[-1]["action"] == "ASSIGNED_CLOSED_OUTSIDE"
      and st.realized_pnl == 0.0, str(st.trade_log[-1]))

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} assignment edge-case checks behaved as expected")
