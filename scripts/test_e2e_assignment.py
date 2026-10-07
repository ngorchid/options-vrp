"""End-to-end: one normal live pass with an ASSIGNED spread in the book (2026-10-07).

Runs the REAL main() -> run_live() against a simulated IB account: detection, the SAFETY unwind,
management, the circuit breaker, the NAV snapshot, the reconcile and the emailed report, in a
single pass. The fake broker models IB's portfolio feed: positions change when an order fills,
and a mark or spread value exists only for a leg that is actually held. Asserts the FULL ordered
list of broker calls, the breaker's and snapshot's values against the assignment formula
computed by hand, and what the report shows.

Cases: full assignment; partial (1 of 2); the share sale partially filling; the share sale not
filling (then completed next run); nothing assigned.
scripts/mutate_e2e_assignment.py seeds wiring faults.

Run: python scripts/test_e2e_assignment.py
"""
from __future__ import annotations

import os
import smtplib
import sys
import tempfile
from datetime import datetime, timedelta
from email import message_from_string
from email.header import decode_header, make_header
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import risk_guard as rg  # noqa: E402
import run_options_paper as runner  # noqa: E402
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


# ------------------------------------------------------------------ the simulated account
TODAY = datetime.now().strftime("%Y-%m-%d")
EXP = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")    # > 21 DTE: no time stop
E = EXP.replace("-", "")
C, KS, KL = 0.74, 263.0, 256.0               # IWM 263/256p, entry credit 0.74
S, PS, PL = 254.0, 9.2, 3.5                  # stock mark, short put mark, long put mark
S_FILL, PL_FILL = 254.10, 3.45               # unwind fill prices
XC, XS, XL = 0.26, 0.15, 0.05                # XLE 59/57p x8: credit, short mark, long mark
X_FILL = 0.11                                # XLE combo close fill (profit take)


def iwm(n=2, **kw):
    return OpenSpread(ticker="IWM", expiry=EXP, short_strike=KS, long_strike=KL, contracts=n,
                      entry_credit=C, max_loss=KS - KL - C, entry_date="2026-10-01",
                      entry_spot=276.0, **kw)


def xle():
    return OpenSpread(ticker="XLE", expiry=EXP, short_strike=59.0, long_strike=57.0, contracts=8,
                      entry_credit=XC, max_loss=2 - XC, entry_date="2026-10-01", entry_spot=62.0)


class SimBroker:
    """IB as the runner sees it. `stock_fill`: 'full' | 'partial' (120 of the shares, order left
    working) | 'none' (cancelled, nothing filled)."""

    def __init__(self, puts: dict, stocks: dict, stock_fill: str = "full"):
        self.puts, self.stocks, self.stock_fill = dict(puts), dict(stocks), stock_fill
        self.put_marks = {("IWM", E, KS): PS, ("IWM", E, KL): PL,
                          ("XLE", E, 59.0): XS, ("XLE", E, 57.0): XL}
        self.stock_marks = {"IWM": S}
        self.calls: list[tuple] = []
        self.dry_run = False
        self.ib = NS(sleep=lambda s: None)

    def _held(self, k):
        return abs(self.puts.get(k, 0.0)) > 1e-9

    # session
    def connect(self):
        self.calls.append(("connect",))
        return True

    def disconnect(self):
        self.calls.append(("disconnect",))

    def margin_cushion(self):
        self.calls.append(("margin_cushion",))
        return (30_000.0, 50_000.0)

    # reads (the portfolio feed: only HELD legs have marks)
    def put_positions(self):
        self.calls.append(("put_positions",))
        return {k: v for k, v in self.puts.items() if self._held(k)}

    def stock_positions_detail(self):
        self.calls.append(("stock_positions_detail",))
        return {s: v for s, v in self.stocks.items() if v[0]}

    def marks(self):
        self.calls.append(("marks",))
        return ({k: v for k, v in self.put_marks.items() if self._held(k)},
                {s: m for s, m in self.stock_marks.items() if self.stocks.get(s, (0,))[0]})

    def spread_values(self, spreads):
        self.calls.append(("spread_values",))
        out = {}
        for sp in spreads:
            ks, kl = (sp.ticker, E, float(sp.short_strike)), (sp.ticker, E, float(sp.long_strike))
            if self._held(ks) and self._held(kl):
                out[sp.key] = self.put_marks[ks] - self.put_marks[kl]
        return out

    def order_fill(self, perm):
        self.calls.append(("order_fill", perm))
        return None

    # orders
    def sell_stock(self, ticker, shares, label="SAFETY: assignment unwind"):
        self.calls.append(("sell_stock", ticker, shares))
        base = {"label": label, "action": "SELL", "qty": shares, "order_ref": "options-vrp:T"}
        q, avg = self.stocks[ticker]
        if self.stock_fill == "full":
            self.stocks[ticker] = (q - shares, avg)
            return {**base, "status": "Filled", "price": S_FILL, "exec_ids": ["stk1"]}
        if self.stock_fill == "partial":
            self.stocks[ticker] = (q - 120, avg)          # 120 filled, the rest still working
            return {**base, "status": "Submitted", "price": S_FILL, "exec_ids": []}
        return {**base, "status": "Cancelled", "price": None, "exec_ids": []}

    def sell_put(self, ticker, expiry, strike, contracts, label="SAFETY: assignment unwind"):
        self.calls.append(("sell_put", ticker, strike, contracts))
        k = (ticker, expiry.replace("-", ""), float(strike))
        self.puts[k] = self.puts.get(k, 0.0) - contracts
        return {"label": label, "action": "SELL", "qty": contracts, "status": "Filled",
                "price": PL_FILL, "exec_ids": ["put1"], "order_ref": "options-vrp:T"}

    def close_spread(self, sp):
        self.calls.append(("close_spread", sp.ticker, sp.contracts))
        ks, kl = (sp.ticker, E, float(sp.short_strike)), (sp.ticker, E, float(sp.long_strike))
        self.puts[ks] = self.puts.get(ks, 0.0) + sp.contracts
        self.puts[kl] = self.puts.get(kl, 0.0) - sp.contracts
        return {"key": sp.key, "ticker": sp.ticker, "action": "CLOSE", "contracts": sp.contracts,
                "net_price": X_FILL, "status": "Filled", "permId": 1, "exec_ids": ["c1"],
                "order_ref": "options-vrp:T", "conids": [1, 2], "commission": -10.4,
                "currency": "USD"}

    def __getattr__(self, name):          # any OTHER broker call is recorded, and fails the case
        def rec(*a, **k):
            self.calls.append(("UNEXPECTED", name))
            raise AssertionError(f"unexpected broker.{name}")
        return rec


# ------------------------------------------------------------------ harness
SENT: list[tuple[str, str]] = []


class FakeSMTP:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        pass

    def sendmail(self, frm, to, raw):
        m = message_from_string(raw)
        part = m.get_payload()[0] if m.is_multipart() else m
        SENT.append((str(make_header(decode_header(m["Subject"]))), part.get_payload(decode=True).decode("utf-8", "replace")))


smtplib.SMTP_SSL = FakeSMTP
os.environ.update(EMAIL_USER="u@x", EMAIL_PASS="p", TO_EMAIL="t@x", BOOK_LABEL="LIVE")
os.environ.pop("PUSHBULLET_API_KEY", None)
os.environ.pop("BUDGET", None)
EQUITY: list[float] = []
runner.write_equity = lambda root, strat, eq, peak: EQUITY.append(eq)
runner.book_drawdown = lambda *a, **k: (None, None, None, "")
runner.book_vol = lambda *a, **k: None
runner.code_version = lambda *a, **k: ("test", 0)
runner.halt_state = lambda root: (rg.HALT_NONE, "")
runner.RECORDS_DIR = Path(tempfile.mkdtemp())
runner.target_book = lambda cfg: NS(regime_open=True, regime_ratio=0.9, targets=[], blocked=[],
                                    corr_overlap=None, diagnostics=[])
_TMP = Path(tempfile.mkdtemp())


def live_pass(state: OptionsState, broker: SimBroker, name: str, fresh: bool = True):
    """One `--live` run of the real main(). Returns the new state."""
    runner.STATE_FILE = _TMP / f"{name}.json"
    if fresh:
        state.save(runner.STATE_FILE)
    SENT.clear()
    EQUITY.clear()
    runner.ALERTS.records.clear()
    runner.make_broker = lambda **kw: broker
    sys.argv = ["run_options_paper.py", "--live", "--force"]
    r = safe(runner.main)
    if r is not None:
        print("    main() returned:", r)
    return OptionsState.load(runner.STATE_FILE)


def breaker_unreal(state):
    """The breaker's unrealised term, recovered from the equity it wrote: eq - budget - realised."""
    return EQUITY[-1] - 50_000.0 - state.realized_pnl if EQUITY else None


def snapshot(state):
    return state.nav_history[-1][1] if state.nav_history else None


def report():
    return SENT[-1] if SENT else ("", "")


def alerts(needle):
    return [m for _, m in runner.ALERTS.records if needle in m]


X_CLOSE_PNL = (XC - X_FILL) * 100 * 8
HEAD = [("connect",), ("margin_cushion",), ("put_positions",), ("stock_positions_detail",),
        ("marks",)]
TAIL = [("marks",), ("spread_values",), ("marks",), ("put_positions",), ("disconnect",)]

# =============================================================================================
print("FULL ASSIGNMENT — IWM 263/256 x2 assigned (200 sh @ 263); XLE intact and at its profit target")
fb = SimBroker({("IWM", E, KS): 0.0, ("IWM", E, KL): 2.0, ("XLE", E, 59.0): -8.0, ("XLE", E, 57.0): 8.0},
               {"IWM": (200.0, KS)})
st = live_pass(OptionsState(open_spreads=[iwm(), xle()]), fb, "full")
want = HEAD + [("sell_stock", "IWM", 200), ("sell_put", "IWM", KL, 2), ("spread_values",),
               ("close_spread", "XLE", 8)] + TAIL
check("ordered broker calls: read -> SELL 200 IWM -> SELL 2 IWM 256P -> manage (close XLE) -> "
      "breaker marks -> snapshot -> reconcile; nothing else", fb.calls == want,
      f"\n      got  {fb.calls}\n      want {want}")
iwm_pnl = (C + S_FILL - KS) * 200 + PL_FILL * 200
check("ledger: ASSIGNED, ASSIGNED_STOCK_SOLD, ASSIGNED_LONG_SOLD, then the XLE CLOSE",
      [t["action"] for t in st.trade_log] == ["ASSIGNED", "ASSIGNED_STOCK_SOLD", "ASSIGNED_LONG_SOLD",
                                              "CLOSE"], str([t["action"] for t in st.trade_log]))
check("realised = IWM unwind (c + S_sale - Ks + Pl_sale) x 200 + XLE profit take",
      abs(st.realized_pnl - (iwm_pnl + X_CLOSE_PNL)) < 1e-6, f"{st.realized_pnl} vs {iwm_pnl + X_CLOSE_PNL}")
check("book empty after the pass -> breaker and snapshot unrealised are 0, snapshot = realised",
      abs(breaker_unreal(st)) < 1e-6 and abs(snapshot(st) - st.realized_pnl) < 1e-6,
      str((breaker_unreal(st), snapshot(st))))
subj, body = report()
check("report: both unwind orders with quantity, price and the SAFETY label",
      "SELL 200</td><td>IWM" in body and "@ 254.10" in body and "SELL 2</td><td>IWM" in body
      and "@ 3.45" in body and body.count("SAFETY: assignment unwind") >= 2, body[:3000])
check("report: the day's ASSIGNED alert is in it and the subject is flagged",
      "ASSIGNED IWM" in body and subj.startswith("[ERROR"), subj)
check("report: total P&L in the subject = the NAV snapshot", f"(${snapshot(st):,.0f})" in subj,
      f"{subj} vs {snapshot(st):,.0f}")
check("report: the Open assignments block says 'none' once the unwind completed",
      "<h3>Open assignments</h3><p>none</p>" in body, "")

# =============================================================================================
print("\nPARTIAL — 1 of 2 contracts assigned (100 sh); the other stays a normal paired spread")
fb = SimBroker({("IWM", E, KS): -1.0, ("IWM", E, KL): 2.0}, {"IWM": (100.0, KS)})
st = live_pass(OptionsState(open_spreads=[iwm()]), fb, "partial")
want = HEAD + [("sell_stock", "IWM", 100), ("sell_put", "IWM", KL, 1), ("spread_values",)] + TAIL
check("ordered calls: SELL 100 IWM, SELL 1 IWM 256P, then management sees the rest; nothing else",
      fb.calls == want, f"\n      got  {fb.calls}\n      want {want}")
check("the remaining contract is a normal spread (1 contract, not assigned)",
      len(st.open_spreads) == 1 and st.open_spreads[0].contracts == 1
      and not st.open_spreads[0].assigned_contracts, str(st.open_spreads))
intact = (C - (PS - PL)) * 100 * 1
check("breaker and snapshot value the remaining contract as an intact spread: (c - (Ps - Pl)) x 100",
      abs(breaker_unreal(st) - intact) < 1e-6 and abs(snapshot(st) - (st.realized_pnl + intact)) < 1e-6,
      str((breaker_unreal(st), intact)))
subj, body = report()
check("report: the remaining spread at its spread mark, and both unwind orders",
      "263/256p ×1" in body and f"${(PS - PL) * 100:,.0f}" in body and "SELL 100</td>" in body
      and "SELL 1</td>" in body, body[:3000])

# =============================================================================================
print("\nSHARE SALE PARTIALLY FILLS — 120 of 200 sold, order left working (status Submitted)")
fb = SimBroker({("IWM", E, KS): 0.0, ("IWM", E, KL): 2.0}, {"IWM": (200.0, KS)}, stock_fill="partial")
st = live_pass(OptionsState(open_spreads=[iwm()]), fb, "pfill")
want = HEAD + [("sell_stock", "IWM", 200), ("spread_values",)] + TAIL
check("ordered calls: SELL 200 IWM only — the long puts are NOT sold while shares remain",
      fb.calls == want, f"\n      got  {fb.calls}\n      want {want}")
check("PROPOSAL: the 120 shares that DID fill are booked nowhere (only a 'Filled' sale is booked)",
      st.realized_pnl == 0.0 and not any(t["action"] == "ASSIGNED_STOCK_SOLD" for t in st.trade_log),
      str(st.trade_log))
held_val = (C + S - KS + PL) * 200
check("PROPOSAL: ...so the breaker and snapshot still value 200 shares while IB holds 80",
      abs(breaker_unreal(st) - held_val) < 1e-6 and abs(snapshot(st) - held_val) < 1e-6,
      str((breaker_unreal(st), held_val)))
check("alert: 'share sale NOT filled (Submitted)'", alerts("share sale NOT filled (Submitted)") != [],
      str(runner.ALERTS.records))
fb.calls = []
st = live_pass(st, fb, "pfill", fresh=False)
check("PROPOSAL: next run IB shows 80 < 200 -> refuses to sell (would open a short) — stuck until a "
      "hand unwind, alerting every run", ("sell_stock", "IWM", 200) not in fb.calls
      and alerts("NOT selling (would open a short)") != [] and alerts("ASSIGNED POSITION") != [],
      str((fb.calls, runner.ALERTS.records)))

# =============================================================================================
print("\nSHARE SALE DOES NOT FILL — then completes on the next run")
fb = SimBroker({("IWM", E, KS): 0.0, ("IWM", E, KL): 2.0}, {"IWM": (200.0, KS)}, stock_fill="none")
st = live_pass(OptionsState(open_spreads=[iwm()]), fb, "nofill")
want = HEAD + [("sell_stock", "IWM", 200), ("spread_values",)] + TAIL
check("ordered calls: SELL 200 IWM (cancelled), no put sale, management skips the assigned spread",
      fb.calls == want, f"\n      got  {fb.calls}\n      want {want}")
held_val = (C + S - KS + PL) * 200
check("breaker unrealised INCLUDES the assigned contracts at (c + S - Ks + Pl) x 100 x 2",
      abs(breaker_unreal(st) - held_val) < 1e-6, str((breaker_unreal(st), held_val)))
check("NAV snapshot = realised + the same assigned value", abs(snapshot(st) - held_val) < 1e-6,
      str((snapshot(st), held_val)))
subj, body = report()
check("report: the IWM row is marked 'ASSIGNED 2 of 2' with that value (not '—')",
      f"ASSIGNED 2 of 2</td><td>${held_val:,.0f}</td>" in body, body[:3000])
check("report: subject total = snapshot (assigned value included)", f"(${held_val:,.0f})" in subj, subj)
check("report: Open assignments block lists it (OPEN, day 1, retrying automatically)",
      "Open assignments (1)" in body and "OPEN — day 1" in body
      and "the automatic unwind retries each run" in body, body[:3000])
check("alert: 'share sale NOT filled (Cancelled)' and the escalating line",
      alerts("share sale NOT filled (Cancelled)") != [] and alerts("ASSIGNED POSITION OPEN") != [], "")
fb.stock_fill, fb.calls = "full", []
st = live_pass(st, fb, "nofill", fresh=False)
want = HEAD + [("sell_stock", "IWM", 200), ("sell_put", "IWM", KL, 2), ("spread_values",)] + TAIL
check("next run: SELL 200 IWM, SELL 2 IWM 256P — unwind complete, spread gone",
      fb.calls == want and st.open_spreads == [], f"\n      got  {fb.calls}\n      want {want}")

# =============================================================================================
print("\nNOTHING ASSIGNED — the block says so explicitly")
fb = SimBroker({("IWM", E, KS): -2.0, ("IWM", E, KL): 2.0}, {})
st = live_pass(OptionsState(open_spreads=[iwm()]), fb, "none")
want = HEAD + [("spread_values",)] + TAIL
check("ordered calls: no order at all (the IWM spread holds)", fb.calls == want,
      f"\n      got  {fb.calls}\n      want {want}")
subj, body = report()
check("report: 'Open assignments' with an explicit 'none'", "<h3>Open assignments</h3><p>none</p>" in body,
      body[:2000])
intact = (C - (PS - PL)) * 100 * 2
check("breaker and snapshot: the intact spread at its spread mark",
      abs(breaker_unreal(st) - intact) < 1e-6 and abs(snapshot(st) - intact) < 1e-6,
      str((breaker_unreal(st), intact)))

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} end-to-end assignment checks behaved as expected")
