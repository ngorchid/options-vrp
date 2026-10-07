"""Tests that every run path WITHOUT a daily report still emails its alerts (2026-10-07).

Pushbullet is not configured on the live machine, so push_if_alerts sends nothing there. The halt
paths (HALT_ALL, HALT_HARD), a failed IB connection and a crashed run never reach send_report, so
without risk_guard.email_if_alerts an open assigned position would alert NOWHERE. Uses the REAL
alert collector (logging is not stubbed) and a fake SMTP server.
scripts/mutate_alert_email.py seeds the faults.

Run: python scripts/test_alert_email.py
"""
from __future__ import annotations

import os
import smtplib
import sys
import tempfile
from datetime import datetime
from email import message_from_string
from email.header import decode_header, make_header
from pathlib import Path

import numpy as np

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


SENT: list[tuple[str, str]] = []
FAIL_SMTP = {"on": False}


class FakeSMTP:
    def __init__(self, *a, **k):
        if FAIL_SMTP["on"]:
            raise OSError("smtp down")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        pass

    def sendmail(self, frm, to, raw):
        m = message_from_string(raw)
        body = m.get_payload(decode=True)
        SENT.append((str(make_header(decode_header(m["Subject"]))),
                     body.decode("utf-8", "replace") if body else ""))


smtplib.SMTP_SSL = FakeSMTP
os.environ.update(EMAIL_USER="u@x", EMAIL_PASS="p", TO_EMAIL="t@x", BOOK_LABEL="LIVE")
os.environ.pop("PUSHBULLET_API_KEY", None)
TODAY = datetime.now().strftime("%Y-%m-%d")
runner.STATE_FILE = Path(tempfile.mkdtemp()) / "state.json"


def assigned_book():
    sp = OpenSpread(ticker="NKE", expiry="2026-11-20", short_strike=70.0, long_strike=67.5,
                    contracts=2, entry_credit=0.6, max_loss=1.9, entry_date="2026-10-01",
                    entry_spot=74.0, assigned_contracts=2,
                    assigned_date=str(np.busday_offset(np.datetime64(TODAY), -2, roll="backward")))
    OptionsState(open_spreads=[sp]).save(runner.STATE_FILE)


def reset():
    SENT.clear()
    runner.ALERTS.records.clear()


class NoConnect:
    def connect(self):
        return False


print("WITHOUT PUSHBULLET THE PUSH DOES NOTHING (why email is needed)")
check("push_alert returns False with no PUSHBULLET_API_KEY", rg.push_alert("t", "m") is False, "")

print("\nHALT_HARD — no connection, but the open assignment IS emailed")
assigned_book()
reset()
runner.halt_state = lambda root: (rg.HALT_HARD, "test")
runner.make_broker = lambda **kw: (_ for _ in ()).throw(AssertionError("no broker under HALT_HARD"))
sys.argv = ["run_options_paper.py", "--live", "--force"]
r = safe(runner.main)
check("one alert email is sent", r is None and len(SENT) == 1, str((r, [s for s, _ in SENT])))
subj = SENT[0][0] if SENT else ""
check("subject: level, LIVE label, and the assignment line (URGENT, day 3)",
      "Options VRP HALT_HARD LIVE" in subj and "ASSIGNED POSITION URGENT" in subj and "day 3" in subj, subj)
check("body lists every alert (the halt and the assignment)",
      bool(SENT) and "HALTED (hard)" in SENT[0][1] and "NKE" in SENT[0][1], SENT[0][1][:300] if SENT else "")

print("\nHALT_ALL — emailed whether or not IB connects")
reset()
runner.make_broker = lambda **kw: NoConnect()
safe(runner.run_safety_only, runner._cfg(None), 4001, 7)
check("connect failure under HALT_ALL -> email naming HALT_ALL and the assignment",
      len(SENT) == 1 and "Options VRP HALT_ALL" in SENT[0][0] and "ASSIGNED POSITION" in SENT[0][0],
      str([s for s, _ in SENT]))


class SafeBroker:
    def connect(self):
        return True

    def disconnect(self):
        pass

    def put_positions(self):
        return {("NKE", "20261120", 70.0): 0.0, ("NKE", "20261120", 67.5): 2.0}

    def stock_positions_detail(self):
        return {"NKE": (260.0, 64.0)}            # blended -> manual, nothing sold

    def marks(self):
        return ({("NKE", "20261120", 67.5): 0.6}, {"NKE": 65.0})


reset()
runner.make_broker = lambda **kw: SafeBroker()
safe(runner.run_safety_only, runner._cfg(None), 4001, 7)
check("a normal HALT_ALL safety pass with an open assignment -> email",
      len(SENT) == 1 and "HALT_ALL" in SENT[0][0] and "ASSIGNED" in SENT[0][0], str([s for s, _ in SENT]))

print("\nNORMAL RUN THAT NEVER REACHES ITS REPORT")
reset()
runner.make_broker = lambda **kw: NoConnect()
safe(runner.run_live, runner._cfg(None), 4001, 7)
check("IB connect failure in a normal run -> email, with the open assignment escalated",
      len(SENT) == 1 and "IB connect failed" in SENT[0][1] and "ASSIGNED POSITION" in SENT[0][0],
      str([s for s, _ in SENT]))


class Crashing(SafeBroker):
    def margin_cushion(self):
        raise RuntimeError("feed exploded")


reset()
runner.make_broker = lambda **kw: Crashing()
r = safe(runner.run_live, runner._cfg(None), 4001, 7)
check("a crashed run -> email marked CRASHED, and the crash still propagates",
      len(SENT) == 1 and "Options VRP CRASHED" in SENT[0][0] and isinstance(r, str)
      and "feed exploded" in r, str(([s for s, _ in SENT], r)))

print("\nemail_if_alerts NEVER BREAKS A RUN")
reset()
check("no alerts -> nothing sent", rg.email_if_alerts(runner.ALERTS, "X") is False and SENT == [], "")
runner.logging.error("something")
FAIL_SMTP["on"] = True
check("an SMTP failure returns False, never raises", safe(rg.email_if_alerts, runner.ALERTS, "X") is False, "")
FAIL_SMTP["on"] = False
for k in ("EMAIL_USER",):
    os.environ.pop(k)
check("EMAIL_* unset -> False, never raises", safe(rg.email_if_alerts, runner.ALERTS, "X") is False, "")
os.environ["EMAIL_USER"] = "u@x"
runner.logging.warning("first warning")
runner.logging.error("ASSIGNED POSITION OPEN — day 1: XLE ...")
rg.email_if_alerts(runner.ALERTS, "X", prefer="ASSIGNED")
check("`prefer` puts the matching line in the subject even if it is not the first record",
      bool(SENT) and "ASSIGNED POSITION OPEN" in SENT[-1][0] and "[ERROR x3]" in SENT[-1][0],
      SENT[-1][0] if SENT else "")


print("\nNO PHONE PUSH FROM A TEST RUN (the dev box's .env has a real key)")
import types as _types  # noqa: E402
_PUSHED: list[str] = []
_fake_pb = _types.ModuleType("pushbullet")
_fake_pb.Pushbullet = lambda key: _types.SimpleNamespace(push_note=lambda t, m: _PUSHED.append(t))
sys.modules["pushbullet"] = _fake_pb
import __main__ as _main  # noqa: E402
_argv0 = _main.__file__
_main.__file__ = "scripts/test_alert_email.py"
check("under a test script, push_alert with a key sends NOTHING",
      rg.push_alert("t", "m", api_key="k") is False and _PUSHED == [], str(_PUSHED))
_main.__file__ = "scripts/mutate_alert_email.py"
check("...nor under a mutation runner", rg.push_alert("t", "m", api_key="k") is False and _PUSHED == [],
      str(_PUSHED))
_main.__file__ = "scripts/run_live_entry.py"
check("a live entry point with a key DOES push (the guard is name-based, not a blanket off)",
      rg.push_alert("t", "m", api_key="k") is True and _PUSHED == ["t"], str(_PUSHED))
_main.__file__ = _argv0

print("\nNO REAL ALERT EMAIL FROM A TEST RUN")
_MAILED: list[str] = []


class _RealLooking:                       # stands in for smtplib.SMTP_SSL: same module name
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        pass

    def sendmail(self, frm, to, raw):
        _MAILED.append(raw)


_RealLooking.__module__ = "smtplib"
_saved_smtp = smtplib.SMTP_SSL
smtplib.SMTP_SSL = _RealLooking
os.environ.update(EMAIL_USER="u@x", EMAIL_PASS="p", TO_EMAIL="t@x")
_c = type("C", (), {"records": [("ERROR", "HALTED (hard): x")], "worst": "ERROR"})()
_main.__file__ = "scripts/test_alert_email.py"
check("under a test script, with the REAL smtplib class, email_if_alerts sends nothing",
      rg.email_if_alerts(_c, "T") is False and _MAILED == [], str(len(_MAILED)))
_main.__file__ = "scripts/run_live_entry.py"
sys.argv = ["run_options_paper.py", "--live"]
_main.__file__ = _argv0
check("a test that rewrites sys.argv to drive main() is STILL treated as a test (no real mail)",
      rg.email_if_alerts(_c, "T") is False and _MAILED == [], str(len(_MAILED)))
_main.__file__ = "scripts/run_live_entry.py"
check("a live entry point sends it (the guard is name-based)", rg.email_if_alerts(_c, "T") is True
      and len(_MAILED) == 1, str(len(_MAILED)))
_main.__file__ = _argv0
smtplib.SMTP_SSL = _saved_smtp

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} alert-email checks behaved as expected")
