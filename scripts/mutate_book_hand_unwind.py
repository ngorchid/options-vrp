"""Mutation-test `scripts/test_book_hand_unwind.py`: the append-only hand-unwind booking tool (#18).

Seeds faults into a TEMP COPY of the repo (the real files are never edited) and demands the suite
catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_book_hand_unwind.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

T = "scripts/book_hand_unwind.py"

MUTATIONS = [
    (T, '    if any(s.get("key") == key for s in state.get("open_spreads", [])):',
     '    if False:', 'a spread still open in the sleeve can be booked (double count)'),
    (T, '    if not ret:\n        raise Refused', '    if False:\n        raise Refused',
     'a spread the sleeve never retired can be booked'),
    (T, '    if dup:\n', '    if False:\n', 'an execution id can be booked twice'),
    (T, '    if not exec_ids and not allow_no_ids:', '    if False:',
     'a booking without exec ids passes silently'),
    (T, '    if booked + qty > limit + 1e-9:', '    if qty > limit + 1e-9:',
     'earlier hand bookings are not counted against the limit'),
    (T, '        limit = MULT * n - sold', '        limit = MULT * n',
     "shares the sleeve already sold are not counted against the limit"),
    (T, '        pnl = (credit + price - ks) * qty', '        pnl = (price - ks) * qty',
     'the stock P&L forgets the credit'),
    (T, '        pnl = (credit - price) * MULT * qty', '        pnl = (price - credit) * MULT * qty',
     'the short buyback P&L has the wrong sign'),
    (T, '        pnl = price * MULT * qty', '        pnl = price * qty', 'the long-put P&L forgets the multiplier'),
    (T, '        if leg == "short" and r.get("action") != "CLOSED_OUTSIDE":', '        if False:',
     'a short buyback is accepted on an assigned spread'),
    (T, '    if side != LEGS[leg]:', '    if False:', 'a leg booked on the wrong side'),
    (T, '    if not a.apply:\n', '    if False:\n', 'preview writes the ledger'),
    (T, '    state.setdefault("trade_log", []).append(row)', '    state["trade_log"] = [row]',
     'applying replaces the ledger instead of appending'),
    (T, '    state["realized_pnl"] = float(state.get("realized_pnl") or 0.0) + float(row["pnl"])',
     '    state["realized_pnl"] = float(row["pnl"])', 'applying overwrites realised P&L'),
    (T, '    shutil.copy2(path, backup)\n', '', 'no backup before writing'),
]

if __name__ == "__main__":
    sys.exit(run("test_book_hand_unwind.py", MUTATIONS))
