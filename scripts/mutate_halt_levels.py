"""Mutation-test `scripts/test_halt_levels.py`: the kill-switch levels, safety-only path and escalation (options-vrp).

Seeds real faults into a TEMP COPY of the repo (the real files are never edited) and demands the
suite catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_halt_levels.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

MUTATIONS = [
    ('risk_guard.py',
     '    for name, mode in ((r / "HALT_HARD", HALT_HARD), (r / "HALT_ALL", HALT_ALL), (r / "HALT", HALT_NEW)):',
     '    for name, mode in ((r / "HALT_ALL", HALT_ALL), (r / "HALT", HALT_NEW)):',
     'the HALT_HARD file is not recognised'),
    ('risk_guard.py',
     '    for name, mode in ((r / "HALT_HARD", HALT_HARD), (r / "HALT_ALL", HALT_ALL), (r / "HALT", HALT_NEW)):',
     '    for name, mode in ((r / "HALT_ALL", HALT_ALL), (r / "HALT_HARD", HALT_HARD), (r / "HALT", HALT_NEW)):',
     'HALT_ALL takes precedence over HALT_HARD'),
    ('risk_guard.py',
     '    if env in ("hard", "3"):\n        return HALT_HARD, "TRADING_HALT=hard"\n',
     '',
     'TRADING_HALT=hard not recognised'),
    ('scripts/run_options_paper.py',
     '    if _halt == HALT_HARD:\n        logging.error("HALTED (hard)',
     '    if False:\n        logging.error("HALTED (hard)',
     'HALT_HARD connects and trades'),
    ('scripts/run_options_paper.py',
     '        escalate_assignments(OptionsState.load(STATE_FILE), datetime.now().strftime("%Y-%m-%d"),\n                             "HALT_HARD")',
     '        pass',
     'HALT_HARD hides the open assignment'),
    ('scripts/run_options_paper.py',
     '    if _halt == HALT_ALL:\n        run_safety_only(cfg, args.port, args.client_id)\n        return\n',
     '',
     'HALT_ALL runs the full live run'),
    ('scripts/run_options_paper.py',
     '        acts = handle_assignments(broker, state, today)\n        escalate_assignments(state, today, "HALT_ALL")',
     '        acts = handle_assignments(broker, state, today)\n        broker.close_spread(state.open_spreads[0])\n        escalate_assignments(state, today, "HALT_ALL")',
     'HALT_ALL also manages/closes a normal spread'),
    ('scripts/run_options_paper.py',
     '        escalate_assignments(state, today, "HALT_ALL")\n        state.save(STATE_FILE)',
     '        state.save(STATE_FILE)',
     'HALT_ALL hides the open assignment'),
    ('scripts/run_options_paper.py',
     '        state.save(STATE_FILE)\n        logging.info("HALT_ALL safety pass done',
     '        logging.info("HALT_ALL safety pass done',
     'the safety pass does not save state'),
    ('scripts/run_options_paper.py',
     '        escalate_assignments(state, today)\n',
     '',
     'a normal run never escalates'),
    ('options_vrp/assignment.py',
     '        sev = "URGENT" if days >= 3 else ("ESCALATION" if days == 2 else "OPEN")\n        will',
     '        sev = "OPEN"\n        will',
     'the alert never escalates'),
    ('options_vrp/assignment.py',
     '        since = getattr(sp, "assigned_date", "") or today\n        days = int(np.busday_count(since, today)) + 1 if since <= today else 1',
     '        since = getattr(sp, "assigned_date", "") or today\n        days = int(np.busday_count(since, today)) if since <= today else 1',
     'the day count is off by one'),
    ('scripts/run_options_paper.py',
     '    budget, bsrc = documented_sizing(ROOT, "options-vrp")',
     '    budget, bsrc = float(os.getenv("BUDGET", "75000")), "env"',
     'budget read from env only'),
]

if __name__ == "__main__":
    sys.exit(run("test_halt_levels.py", MUTATIONS))
