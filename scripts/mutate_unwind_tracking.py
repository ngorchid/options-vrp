"""Mutation-test `scripts/test_unwind_tracking.py`: tracked assignment-unwind orders (decision #15).

Seeds faults into a TEMP COPY of the repo (the real files are never edited) and demands the suite
catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_unwind_tracking.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

R = "scripts/run_options_paper.py"
A = "options_vrp/assignment.py"

MUTATIONS = [
    (R, '        if getattr(sp, "unwind_order", None):\n            # Decision #15',
     '        if False:\n            # Decision #15', 'a tracked order is never resolved (its fills never booked)'),
    (R, '                    pend["booked"], qty)\n    return False',
     '                    pend["booked"], qty)\n    return True', 'a second order is placed while the first still works'),
    (R, '                      leg, pend.get("permId"))\n        return False',
     '                      leg, pend.get("permId"))\n        return True',
     'an order IB cannot show is assumed dead at once (could sell twice)'),
    (R, '        if age >= 2:', '        if age >= 0:', 'an invisible order is declared expired the next day'),
    (R, '    filled = max(by_ib, by_pos, booked)', '    filled = max(by_ib, booked)',
     'the position change is ignored when IB gives no count'),
    (R, '            src = "MARK (IB fill price not available)"', '            src = "IB average fill"',
     'a mark-priced booking is labelled as a real fill'),
    (R, '        if price is None:\n            logging.error("ASSIGNMENT UNWIND %s: tracked',
     '        if price is None:\n            price = 0.0\n        if False:\n            logging.error("ASSIGNMENT UNWIND %s: tracked',
     'a fill with no price is booked at zero'),
    (R, '        pend["booked"] = booked + new\n', '',
     'booked quantity not remembered (the same fill booked again)'),
    (R, '                _track_unwind_order(sp, "stock", f, need, held, today)\n',
     '                pass\n', 'an unfilled share sale is not tracked'),
    (R, '            _track_unwind_order(sp, "put", f2, n, long_held, today)\n',
     '            pass\n', 'an unfilled long-put sale is not tracked'),
    (A, '    q = left if shares is None else min(float(shares), left)', '    q = left',
     'a partial share fill books all the shares'),
    (A, '    k = n if contracts is None else max(0, min(int(contracts), n))', '    k = n',
     'a partial put fill books all the puts'),
    (A, '            left = mult * n_a - float(getattr(sp, "assigned_shares_sold", 0.0) or 0.0)',
     '            left = mult * n_a', 'sold shares are valued again (double counted)'),
    (A, '        working = (f"; unwind order working at IB (permId {order.get(\'permId\')})" if order else "")',
     '        working = ""', 'the escalation hides the working order'),
]

if __name__ == "__main__":
    sys.exit(run("test_unwind_tracking.py", MUTATIONS))
