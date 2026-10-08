"""Mutation-test `scripts/test_e2e_assignment.py`: the WIRING of the assignment fix in a live pass.

Each fault breaks how the pieces connect inside run_live / the report -- the pieces themselves are
covered by test_assignment.py. Seeds the faults into a TEMP COPY of the repo (the real files are
never edited) and demands the end-to-end suite catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_e2e_assignment.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from _mutate_repo_core import run

RUNNER = "scripts/run_options_paper.py"
REPORT = "options_vrp/email_report.py"
_src = (Path(__file__).resolve().parents[1] / RUNNER).read_text(encoding="utf-8")

# "handle_assignments runs AFTER management": cut it from step 0b, paste it just before the breaker.
_a = "        orders.extend(handle_assignments(broker, state, today))\n"
_b = "        # CIRCUIT BREAKER — here, AFTER management"
_block = _src[_src.index(_a):_src.index(_b)]
_late = _block.replace(_a, "", 1) + _a + "\n"

INTACT_ONLY = ("sum((sp.entry_credit - values[sp.key]) * 100 * sp.contracts "
               "for sp in state.open_spreads if sp.key in values)")

MUTATIONS = [
    (RUNNER, '            if getattr(sp, "assigned_contracts", 0) or getattr(sp, "assign_suspected", 0):\n                continue',
     '            if getattr(sp, "assigned_contracts", 0):\n                continue',
     '#17: the manage loop combo-closes a SUSPECTED (broken) pair'),
    (RUNNER, '                        or int(getattr(sp, "assign_suspected", 0) or 0))',
     '                        or 0)', '#17: the reconcile expects a suspected short leg (daily false alarm)'),
    (REPORT, '        if n_s and not n_a:', '        if False:', '#17: the report shows a suspected spread as normal'),
    (RUNNER, _block, _late,
     'handle_assignments runs AFTER management instead of before'),
    (RUNNER, '        orders.extend(handle_assignments(broker, state, today))\n',
     '        handle_assignments(broker, state, today)\n',
     'unwind result not carried into the report'),
    (RUNNER, '        orders.extend(handle_assignments(broker, state, today))\n        escalate_assignments(state, today)\n',
     '        orders.extend(handle_assignments(broker, state, today))\n',
     'no escalating alert in a normal live run'),
    (RUNNER, '        _unreal = book_unrealized(state, values, *broker.marks())',
     f'        _unreal = {INTACT_ONLY}',
     'circuit breaker skips book_unrealized (assigned spreads invisible to it)'),
    (RUNNER, '        unreal = book_unrealized(state, values, *_marks)',
     f'        unreal = {INTACT_ONLY}',
     'NAV snapshot skips book_unrealized (assigned spreads invisible to it)'),
    (RUNNER, '                    alerts=ALERTS, unreal=unreal, marks=_marks)',
     '                    alerts=ALERTS)',
     'report total and rows ignore the book_unrealized value'),
    (RUNNER, '            actions.append({**f, "key": sp.key, "ticker": sp.ticker})',
     '            actions.append({**f, "key": sp.key})',
     'the share sale reaches the report without its ticker'),
    (RUNNER, '            if f.get("status") != "Filled" or f.get("price") is None:',
     '            if f.get("status") not in ("Filled", "Submitted"):',
     'a partly filled share sale is treated as done (long puts sold, shares booked)'),
    (REPORT, '            unreal = unrealized(sp, *marks) if marks else None',
     '            unreal = None',
     'report rows leave an ASSIGNED spread unvalued'),
    (REPORT, '        return "<h3>Open assignments</h3><p>none</p>"',
     '        return ""',
     'no explicit empty state in the Open assignments block'),
    (REPORT, '    {_open_assignments(state, today)}\n',
     '\n',
     'Open assignments block not rendered at all'),
    (REPORT, '        if str(o.get("label", "")).startswith("SAFETY"):     # an assignment-unwind order',
     '        if False:',
     'unwind orders rendered without quantity / price / SAFETY label'),
]

if __name__ == "__main__":
    sys.exit(run("test_e2e_assignment.py", MUTATIONS))
