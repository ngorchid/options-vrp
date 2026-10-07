"""Mutation-test `scripts/test_assignment_edges.py`: the detection edge cases pinned as they are today.

Seeds faults into a TEMP COPY of the repo (the real files are never edited) and demands the suite
catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_assignment_edges.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

ASG = "options_vrp/assignment.py"
RUNNER = "scripts/run_options_paper.py"

MUTATIONS = [
    (ASG, '                 and abs(float(avg) - float(sp.short_strike)) < 0.01)',
     '                 and abs(float(avg) - float(sp.short_strike)) < 0.02)',
     'cost-basis tolerance widened to $0.02 (fees on 100 shares pass as exact)'),
    (ASG, '                 and abs(float(avg) - float(sp.short_strike)) < 0.01)',
     '                 and abs(float(avg) - float(sp.short_strike)) < 0.005)',
     'cost-basis tolerance narrowed to $0.005 (a $1 fee on 200 shares goes manual)'),
    (ASG, '        exact = (abs(qty - need) < 1e-9 and avg is not None',
     '        exact = (qty >= need and avg is not None',
     'more shares than 100 x n still counted as exact'),
    (ASG, '        if getattr(sp, "assigned_contracts", 0):\n            continue                                   # already recorded',
     '        if False:\n            continue                                   # already recorded',
     'a spread with an unwind in progress is detected again'),
    (ASG, '        elif qty >= need:',
     '        elif qty > need:',
     'exactly-100xn-but-wrong-price falls into the "fewer shares" message'),
    (RUNNER, '        if a.stock_qty >= a.shares_expected:\n            asg.mark_assigned(state, a, today)',
     '        if True:\n            asg.mark_assigned(state, a, today)',
     'a short leg gone with too few shares is recorded as assigned'),
    (RUNNER, '        if not sp.assigned_auto:',
     '        if False:',
     'a MANUAL (blended / two-spread) assignment is sold automatically'),
    (RUNNER, '        if long_held is not None and long_held <= 0:',
     '        if long_held is not None and long_held < 0:',
     'long puts gone at IB no longer retire the spread'),
]

if __name__ == "__main__":
    sys.exit(run("test_assignment_edges.py", MUTATIONS))
