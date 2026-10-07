"""Mutation-test `scripts/test_sizing_identity.py`: the move of the live budget onto config/capital_bases.json.

Seeds real faults into a TEMP COPY of the repo (the real files are never edited) and demands the
suite catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_sizing_identity.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

MUTATIONS = [
    ('risk_guard.py',
     '        doc = float(entry[key])',
     '        doc = float(env_val or 100000)',
     'the documented value is ignored'),
    ('risk_guard.py',
     '        logging.warning("sizing: %s env override %g DIFFERS',
     '        logging.info("sizing: %s env override %g DIFFERS',
     'a diverging env override is silent'),
    ('risk_guard.py',
     '                    (ALLOCATIONS[sleeve].fraction * NOMINAL_NAV if key == "budget" else 1.0))',
     '                    (0.0 if key == "budget" else 1.0))',
     'a missing config falls back to a ZERO budget'),
    ('risk_guard.py',
     '    if alloc is not None and budget > alloc.fraction * nav + 1e-6:',
     '    if False:',
     'the ALLOCATIONS ceiling is never checked'),
    ('risk_guard.py',
     '    logging.info("%s", line)\n    alloc = ALLOCATIONS.get(sleeve)',
     '    alloc = ALLOCATIONS.get(sleeve)',
     'the startup line is not logged'),
    ('config/capital_bases.json',
     '  "options-vrp": {\n    "amount": 50000,\n    "budget": 50000,',
     '  "options-vrp": {\n    "amount": 50000,\n    "budget": 55000,',
     'options-vrp budget changed in the config'),
    ('risk_guard.py',
     '    "options-vrp": Allocation(1.00,',
     '    "options-vrp": Allocation(0.50,',
     'options-vrp ceiling lowered below its budget'),
    ('scripts/test_sizing_identity.py',
     'def same(a: str, b: str, rel: float = 1e-9) -> bool:',
     'def same(a: str, b: str, rel: float = 1e-3) -> bool:',
     'tolerance loosened to 1e-3 (would hide real changes)'),
    ('scripts/test_sizing_identity.py',
     '        if isinstance(x, str) and isinstance(y, str) and x[:1] in "{[" and y[:1] in "{[":',
     '        if False:',
     'nested JSON (trend targets) compared as raw text'),
    ('scripts/test_sizing_identity.py',
     '            return abs(x - y) <= rel * max(1.0, abs(x), abs(y))',
     '            return True',
     'floats never compared'),
]

if __name__ == "__main__":
    sys.exit(run("test_sizing_identity.py", MUTATIONS))
