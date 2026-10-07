"""Early assignment of a short put: detect it, value it, and the unwind bookkeeping.

WHY (2026-10-07). An assigned short put delivers LONG stock: 100 x contracts shares bought at the
short strike. At this book's sizing that is roughly one to one-and-a-half times the account's NAV
in stock (contracts are sized off max loss, so notional = contracts x 100 x strike is large), held
with only the long put as protection. Before this module the sleeve could not see it: the spread
had no mark, the manage loop skipped it, and both the circuit breaker and the NAV snapshot left it
out. Dropping the 2x stop was safe because the long wing caps the loss -- but only while the wing
stays PAIRED with the short leg. Assignment breaks the pair, and it is most likely on spreads that
are already losing (deep in the money).

DETECT. The fingerprint of an assignment is near-unique: the short leg's quantity at IB drops by n
contracts AND the account holds exactly 100 x n more shares of the underlying, at an average cost
equal to the short strike. EXACT match -> automatic unwind. A stock position that is LARGER than
expected or priced differently (another sleeve also holds the name) is still flagged and valued, but
NOT unwound automatically: selling shares the sleeve cannot prove are its own could sell
magic-formula's stock. A vanished short leg with NO stock is alerted for manual review.

VALUE. Per contract, with entry credit c, short strike Ks, stock mark S and put marks Ps/Pl:
  intact contract   : c - (Ps - Pl)                     (the usual spread mark)
  assigned contract : c + (S - Ks) + Pl                 (shares bought at Ks, long put still held)
  after shares sold : Pl                                (c + S_sale - Ks already realised)
so a partially unwound position is never double-counted or dropped.

UNWIND (sequence; the runner executes it, flagged SAFETY so no automated guard can block it):
  1. SELL the delivered shares (100 x n). This removes the big exposure first. If it does not fill,
     stop: the long put stays on as the shares' protection, retry next run.
  2. Only after (1) filled: SELL the n long puts that paired the assigned contracts. They are now a
     plain long option (limited value, no risk); the remaining N - n contracts stay a normal,
     still-paired spread under normal management.
Exercising the long put instead (sell the shares at the long strike) is better only when the put is
deep in the money and its bid sits below intrinsic; that settles overnight and needs the account id,
so it is left as a manual alternative (DEPLOY.md, "Early assignment").
"""
from __future__ import annotations

from dataclasses import dataclass

MULT = 100


@dataclass
class Assignment:
    key: str
    ticker: str
    assigned: int          # contracts assigned (short leg drop)
    shares_expected: float  # MULT x assigned
    stock_qty: float       # shares held at IB
    stock_avg: float | None
    exact: bool            # quantity AND price fingerprint match -> automatic unwind
    note: str


def _ib_exp(expiry: str) -> str:
    return expiry.replace("-", "")


def detect(open_spreads, put_actual: dict | None, stock_detail: dict | None,
           mult: int = MULT) -> list[Assignment]:
    """Assignments not yet recorded on a spread. `put_actual` {(sym, yyyymmdd, strike): signed qty},
    `stock_detail` {sym: (signed shares, avg cost)}. None inputs -> [] (could not check)."""
    if put_actual is None or stock_detail is None:
        return []
    out = []
    for sp in open_spreads:
        if getattr(sp, "assigned_contracts", 0):
            continue                                   # already recorded; unwind is in progress
        short_held = -float(put_actual.get((sp.ticker, _ib_exp(sp.expiry), float(sp.short_strike)), 0.0))
        n = int(round(sp.contracts - max(short_held, 0.0)))
        if n <= 0:
            continue
        qty, avg = stock_detail.get(sp.ticker, (0.0, None))
        need = float(mult * n)
        exact = (abs(qty - need) < 1e-9 and avg is not None
                 and abs(float(avg) - float(sp.short_strike)) < 0.01)
        if exact:
            note = (f"{n} of {sp.contracts} short {sp.short_strike:g}P assigned: {need:g} {sp.ticker} "
                    f"delivered at {sp.short_strike:g} (quantity and price match)")
        elif qty >= need:
            note = (f"{n} of {sp.contracts} short {sp.short_strike:g}P gone and {qty:g} {sp.ticker} "
                    f"held (avg {avg}) vs {need:g} expected at {sp.short_strike:g} — probably "
                    f"assigned, but the position also holds other shares or a different price; NOT "
                    f"unwound automatically")
        else:
            note = (f"{n} of {sp.contracts} short {sp.short_strike:g}P gone with only {qty:g} "
                    f"{sp.ticker} held (expected {need:g}) — closed by hand, or the delivery is not "
                    f"visible yet; manual review")
        out.append(Assignment(sp.key, sp.ticker, n, need, float(qty), avg, exact, note))
    return out


def escalation_notes(open_spreads, today: str, level: str = "") -> list[str]:
    """One line per open ASSIGNED position, escalating with its age in business days (day 1 = the
    day it was recorded): OPEN -> ESCALATION (day 2) -> URGENT (day 3+). Raised on EVERY run,
    under any halt level -- including HALT_HARD, where nothing can be unwound -- so an assigned
    position can never sit forgotten behind a halt."""
    import numpy as np
    out = []
    for sp in open_spreads:
        n = int(getattr(sp, "assigned_contracts", 0) or 0)
        if not n:
            continue
        since = getattr(sp, "assigned_date", "") or today
        days = int(np.busday_count(since, today)) + 1 if since <= today else 1
        sev = "URGENT" if days >= 3 else ("ESCALATION" if days == 2 else "OPEN")
        will = ("nothing can be unwound under HALT_HARD — unwind by hand (DEPLOY.md)"
                if level == "HALT_HARD" else
                "the automatic unwind retries each run" if getattr(sp, "assigned_auto", False)
                else "needs a MANUAL unwind (DEPLOY.md)")
        out.append(f"ASSIGNED POSITION {sev} — day {days} since {since}: {sp.key}, {n} contract(s): "
                   f"{'shares sold, ' if getattr(sp, 'assigned_stock_sold', False) else f'{MULT * n} {sp.ticker} shares + '}"
                   f"{n} long {sp.long_strike:g}P still open"
                   + (f" under {level}" if level else "") + f"; {will}")
    return out


def mark_assigned(state, a: Assignment, today: str) -> None:
    """Record the assignment on the spread and in the trade log (bookkeeping only)."""
    sp = next(s for s in state.open_spreads if s.key == a.key)
    sp.assigned_contracts = a.assigned
    sp.assigned_date = today
    sp.assigned_auto = a.exact
    state.trade_log.append({"date": today, "action": "ASSIGNED", "key": a.key,
                            "contracts": a.assigned, "strike": sp.short_strike,
                            "shares": a.shares_expected, "exact": a.exact, "note": a.note})


def unrealized(sp, put_marks: dict, stock_marks: dict, mult: int = MULT) -> float | None:
    """Unrealised P&L of one spread, assigned or not (see module docstring). None if unmarkable."""
    e = _ib_exp(sp.expiry)
    pl = put_marks.get((sp.ticker, e, float(sp.long_strike)))
    if pl is None:
        return None
    n_a = int(getattr(sp, "assigned_contracts", 0) or 0)
    n_i = sp.contracts - n_a
    total = 0.0
    if n_i > 0:
        ps = put_marks.get((sp.ticker, e, float(sp.short_strike)))
        if ps is None:
            return None
        total += (sp.entry_credit - (ps - pl)) * n_i
    if n_a > 0:
        if getattr(sp, "assigned_stock_sold", False):
            total += pl * n_a
        else:
            s = stock_marks.get(sp.ticker)
            if s is None:
                return None
            total += (sp.entry_credit + (s - sp.short_strike) + pl) * n_a
    return total * mult


def book_stock_sale(state, sp, price: float, today: str, fill: dict, mult: int = MULT) -> float:
    """Realise the assigned contracts' stock leg: c + (S_sale - Ks) per share. Returns P&L."""
    n = sp.assigned_contracts
    pnl = (sp.entry_credit + (price - sp.short_strike)) * mult * n
    state.realized_pnl += pnl
    sp.assigned_stock_sold = True
    state.trade_log.append({"date": today, "action": "ASSIGNED_STOCK_SOLD", "key": sp.key,
                            "contracts": n, "shares": mult * n, "price": price, "pnl": pnl,
                            "reason": "SAFETY: assignment unwind",
                            "order_ref": fill.get("order_ref", ""),
                            "exec_ids": list(fill.get("exec_ids") or []),
                            "conids": list(fill.get("conids") or []),
                            "commission": fill.get("commission"),
                            "currency": fill.get("currency") or ""})
    return pnl


def book_long_sale(state, sp, price: float, today: str, fill: dict, mult: int = MULT) -> float:
    """Realise the assigned contracts' long puts and shrink (or remove) the spread. Returns P&L."""
    n = sp.assigned_contracts
    pnl = price * mult * n
    state.realized_pnl += pnl
    state.trade_log.append({"date": today, "action": "ASSIGNED_LONG_SOLD", "key": sp.key,
                            "contracts": n, "strike": sp.long_strike, "price": price, "pnl": pnl,
                            "reason": "SAFETY: assignment unwind",
                            "order_ref": fill.get("order_ref", ""),
                            "exec_ids": list(fill.get("exec_ids") or []),
                            "conids": list(fill.get("conids") or []),
                            "commission": fill.get("commission"),
                            "currency": fill.get("currency") or ""})
    remaining = sp.contracts - n
    if remaining <= 0:
        state.open_spreads = [s for s in state.open_spreads if s.key != sp.key]
    else:
        sp.contracts = remaining                   # the rest is a normal, still-paired spread
        sp.assigned_contracts = 0
        sp.assigned_stock_sold = False
        sp.assigned_auto = False
    return pnl
