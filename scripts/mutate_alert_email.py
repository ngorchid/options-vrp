"""Mutation-test `scripts/test_alert_email.py`: alert emails on every path without a daily report (options-vrp).

Seeds real faults into a TEMP COPY of the repo (the real files are never edited) and demands the
suite catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_alert_email.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

MUTATIONS = [
    ('risk_guard.py',
     '    if not recs:\n        return False\n    pick',
     '    if True:\n        return False\n    pick',
     'email_if_alerts never sends'),
    ('risk_guard.py',
     '    pick = next((m for _, m in recs if prefer and prefer in m), None) or \\\n',
     '    pick = None or \\\n',
     'the preferred (assignment) line is not put in the subject'),
    ('risk_guard.py',
     '    except Exception as e:  # noqa: BLE001 -- an alert failure must never break the run',
     '    except KeyError as e:  # noqa: BLE001 -- an alert failure must never break the run',
     'an SMTP failure crashes the run'),
    ('scripts/run_options_paper.py',
     '        email_if_alerts(ALERTS, "Options VRP HALT_HARD", datetime.now().strftime("%Y-%m-%d"),\n                        prefer="ASSIGNED")\n',
     '',
     'HALT_HARD sends no email'),
    ('scripts/run_options_paper.py',
     '        escalate_assignments(state, today, "HALT_ALL")\n        email_if_alerts(ALERTS, "Options VRP HALT_ALL", today, prefer="ASSIGNED")\n        push_if_alerts(ALERTS, "Options VRP")\n        return []',
     '        escalate_assignments(state, today, "HALT_ALL")\n        push_if_alerts(ALERTS, "Options VRP")\n        return []',
     'HALT_ALL connect failure sends no email'),
    ('scripts/run_options_paper.py',
     '        email_if_alerts(ALERTS, "Options VRP HALT_ALL", today, prefer="ASSIGNED")\n        push_if_alerts(ALERTS, "Options VRP")\n    finally:',
     '        push_if_alerts(ALERTS, "Options VRP")\n    finally:',
     'the HALT_ALL safety pass sends no email'),
    ('scripts/run_options_paper.py',
     '        escalate_assignments(OptionsState.load(STATE_FILE), today)\n        email_if_alerts(ALERTS, "Options VRP", today, prefer="ASSIGNED")\n        return',
     '        email_if_alerts(ALERTS, "Options VRP", today, prefer="ASSIGNED")\n        return',
     'a failed connection does not mention the open assignment'),
    ('scripts/run_options_paper.py',
     '        email_if_alerts(ALERTS, "Options VRP", today, prefer="ASSIGNED")\n        return',
     '        return',
     'a failed connection sends no email'),
    ('scripts/run_options_paper.py',
     '        email_if_alerts(ALERTS, "Options VRP CRASHED", today, prefer="ASSIGNED")\n        raise',
     '        raise',
     'a crashed run sends no email'),
    ('scripts/run_options_paper.py',
     '        email_if_alerts(ALERTS, "Options VRP CRASHED", today, prefer="ASSIGNED")\n        raise',
     '        email_if_alerts(ALERTS, "Options VRP CRASHED", today, prefer="ASSIGNED")',
     'a crash is swallowed instead of propagating'),
]

if __name__ == "__main__":
    sys.exit(run("test_alert_email.py", MUTATIONS))
