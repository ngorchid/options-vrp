"""Tests for the execution-id backfill: options-vrp writing the real IB execution ids that the
nightly two-way check found in Flex into its OWN tag-only trade_log rows (pending self-heal),
keeping the superseded state. Bookkeeping only. scripts/mutate_exec_backfill.py seeds the faults.

Run: python scripts/test_exec_backfill.py
"""
from __future__ import annotations

import csv
import inspect
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from options_vrp.audit import apply_exec_backfill, read_backfill  # noqa: E402
from options_vrp.state import OptionsState  # noqa: E402
import run_options_paper as runner  # noqa: E402

REF = "options-vrp:20261007-213001"
KEY = "IWM_2026-11-20_263_256"
XLE = "XLE_2026-11-20_90_85"
_fails: list[str] = []
_ran = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _ran
    _ran += 1
    if not cond:
        _fails.append(f"{label}  | {detail}")
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + ("" if cond else f"   <- {detail}"))


def state() -> OptionsState:
    st = OptionsState()
    st.trade_log = [
        {"date": "2026-10-07", "action": "CLOSE", "key": KEY, "order_ref": REF, "exec_ids": []},
        {"date": "2026-10-07", "action": "OPEN", "key": KEY, "order_ref": REF,
         "exec_ids": ["bag", "s", "l"]},                                  # booked WITH ids
        {"date": "2026-10-08", "action": "CLOSE", "key": KEY, "order_ref": REF, "exec_ids": []},
        {"date": "2026-10-07", "action": "CLOSE", "key": KEY, "order_ref": "", "exec_ids": []},
        {"date": "2026-10-07", "action": "OPEN", "key": KEY, "order_ref": REF, "exec_ids": []},
        {"date": "2026-10-07", "action": "CLOSE", "key": XLE, "order_ref": REF, "exec_ids": []},
    ]
    return st


ROWS = [{"sleeve": "options-vrp", "date": "20261007", "action": "CLOSE", "key": KEY,
         "order_ref": REF, "exec_ids": "h1;h2"},
        {"sleeve": "options-vrp", "date": "20261007", "action": "OPEN", "key": KEY,
         "order_ref": REF, "exec_ids": "zz1;zz2"},
        {"sleeve": "options-vrp", "date": "20261007", "action": "CLOSE", "key": XLE,
         "order_ref": REF, "exec_ids": "x1;x2"},
        # never produced by the records job, but a row must not be linked on an EMPTY tag
        {"sleeve": "options-vrp", "date": "20261007", "action": "CLOSE", "key": KEY,
         "order_ref": "", "exec_ids": "bad1"}]

print("APPLY")
st = state()
n = apply_exec_backfill(st, ROWS, "2026-10-09")
r0 = st.trade_log[0]
check("a tag-only row gets the real execution ids", r0["exec_ids"] == ["h1", "h2"], str(r0))
check("...the superseded state is kept on the row",
      r0.get("superseded") == {"exec_ids": [], "linked_by": "tag only"}, str(r0))
check("...and the source is recorded", r0.get("exec_ids_source") == "flex backfill 2026-10-09", str(r0))
check("a row booked WITH ids at fill time is never overwritten",
      st.trade_log[1]["exec_ids"] == ["bag", "s", "l"] and "superseded" not in st.trade_log[1],
      str(st.trade_log[1]))
check("a row on another date is not touched (identity = date + action + key + tag)",
      st.trade_log[2]["exec_ids"] == [], str(st.trade_log[2]))
check("a row without a tag is not touched", st.trade_log[3]["exec_ids"] == [], str(st.trade_log[3]))
check("identity includes the ACTION: the same day's tag-only OPEN gets the OPEN's ids",
      st.trade_log[4]["exec_ids"] == ["zz1", "zz2"], str(st.trade_log[4]))
check("identity includes the KEY: another spread of the same run gets its own ids",
      st.trade_log[5]["exec_ids"] == ["x1", "x2"], str(st.trade_log[5]))
check("the count reports exactly the rows updated", n == 3, str(n))
check("re-applying is a no-op (converged)", apply_exec_backfill(st, ROWS, "2026-10-10") == 0
      and st.trade_log[0].get("exec_ids_source") == "flex backfill 2026-10-09", str(st.trade_log[0]))

print("\nREADING THE RECORDS JOB'S FILE")
tmp = Path(tempfile.mkdtemp())
f = tmp / "tables" / "exec_backfill.csv"
f.parent.mkdir(parents=True)
with open(f, "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["sleeve", "date", "action", "key", "order_ref", "exec_ids"])
    w.writeheader()
    w.writerows(ROWS + [{"sleeve": "trend-overlay", "date": "20261007", "action": "",
                         "key": "", "order_ref": "trend-overlay:x", "exec_ids": "t1"}])
check("only this sleeve's rows are read", len(read_backfill(f)) == len(ROWS), str(read_backfill(f)))
check("a missing file is simply nothing to do", read_backfill(tmp / "nope.csv") == [], "")

print("\nRUNNER WIRING")
runner.RECORDS_DIR = tmp
st = state()
check("backfill_exec_ids applies the file to the sleeve's own state",
      runner.backfill_exec_ids(st, "2026-10-09") == 3 and st.trade_log[0]["exec_ids"] == ["h1", "h2"],
      str(st.trade_log[0]))
f.unlink()
f.mkdir()                        # unreadable: a directory where the file should be -> OSError
try:
    _n = runner.backfill_exec_ids(state(), "2026-10-09")
except Exception as e:  # noqa: BLE001 -- a crash is a FAILED check here, not a test crash
    _n = f"crashed: {type(e).__name__}: {e}"
check("an unreadable file never stops the run (0 rows, no exception)", _n == 0, str(_n))
src = inspect.getsource(runner.run_live)
check("run_live applies the backfill right after loading state",
      "state = OptionsState.load(STATE_FILE); state.ensure_inception(today)\n"
      "    backfill_exec_ids(state, today)" in src, "")

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} exec-backfill checks behaved as expected")
