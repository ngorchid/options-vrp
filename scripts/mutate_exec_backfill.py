"""Mutation-test `scripts/test_exec_backfill.py`: the execution-id backfill into options-vrp's own ledger.

Seeds real faults into a TEMP COPY of the repo (the real files are never edited) and demands the
suite catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_exec_backfill.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

MUTATIONS = [
    ('options_vrp/audit.py',
     '        if t.get("exec_ids") or not t.get("order_ref"):',
     '        if not t.get("order_ref"):',
     'rows booked WITH ids get overwritten'),
    ('options_vrp/audit.py',
     '        if t.get("exec_ids") or not t.get("order_ref"):',
     '        if t.get("exec_ids"):',
     'a row is linked on an EMPTY tag'),
    ('options_vrp/audit.py',
     '        t["superseded"] = {"exec_ids": list(t.get("exec_ids") or []), "linked_by": "tag only"}\n',
     '',
     'the superseded state is not kept'),
    ('options_vrp/audit.py',
     '        t["exec_ids_source"] = f"flex backfill {today}"\n',
     '',
     'the backfill source is not recorded'),
    ('options_vrp/audit.py',
     '        k = ((t.get("date") or "").replace("-", ""), t.get("action", ""), t.get("key", ""),',
     '        k = ((t.get("date") or ""), t.get("action", ""), t.get("key", ""),',
     'ledger ISO date never matches Flex YYYYMMDD'),
    ('options_vrp/audit.py',
     '        k = ((t.get("date") or "").replace("-", ""), t.get("action", ""), t.get("key", ""),',
     '        k = ((t.get("date") or "").replace("-", ""), "CLOSE", t.get("key", ""),',
     'action left out of the identity'),
    ('options_vrp/audit.py',
     '        k = ((t.get("date") or "").replace("-", ""), t.get("action", ""), t.get("key", ""),',
     '        k = ((t.get("date") or "").replace("-", ""), t.get("action", ""), "IWM_2026-11-20_263_256",',
     'key left out of the identity'),
    ('options_vrp/audit.py',
     '        n += 1\n',
     '',
     'the count is wrong'),
    ('options_vrp/audit.py',
     '        return [r for r in csv.DictReader(f) if r.get("sleeve") == "options-vrp"]',
     '        return list(csv.DictReader(f))',
     "other sleeves' rows read"),
    ('scripts/run_options_paper.py',
     '    state = OptionsState.load(STATE_FILE); state.ensure_inception(today)\n    backfill_exec_ids(state, today)\n',
     '    state = OptionsState.load(STATE_FILE); state.ensure_inception(today)\n',
     'run_live never applies the backfill'),
    ('scripts/run_options_paper.py',
     '    except Exception as e:  # noqa: BLE001\n        logging.warning("exec-id backfill skipped: %s", e)',
     '    except ValueError as e:  # noqa: BLE001\n        logging.warning("exec-id backfill skipped: %s", e)',
     'an unreadable backfill file crashes the run'),
]

if __name__ == "__main__":
    sys.exit(run("test_exec_backfill.py", MUTATIONS))
