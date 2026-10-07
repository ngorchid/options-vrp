"""Audit-trail helpers for options-vrp. Bookkeeping and alerts only: nothing here trades, sizes or
decides an entry or exit.

  apply_exec_backfill   writes the real IB execution ids, found by the nightly two-way check in
                        IB's Flex records, into trade_log rows the pending self-heal booked with a
                        tag but no ids (the API returns only the current day's executions, so a
                        late fill booked on the next run cannot carry them). The superseded state
                        of each row is kept on the row. Applied by THIS sleeve to its OWN ledger:
                        the records job never writes a ledger.
"""
from __future__ import annotations

import csv
from pathlib import Path


def read_backfill(path: Path) -> list[dict]:
    """Rows of the records job's tables/exec_backfill.csv for this sleeve; [] if absent."""
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("sleeve") == "options-vrp"]


def apply_exec_backfill(state, rows: list[dict], today: str) -> int:
    """Fill in execution ids on tag-only trade_log rows. Returns the number of rows updated.

    A row matches on (date, action, key, order_ref) -- the ledger's own identity for the fill --
    and is updated only while its exec_ids are still EMPTY, so re-applying is a no-op and a row
    booked with ids at fill time is never overwritten."""
    by_key = {(r.get("date", ""), r.get("action", ""), r.get("key", ""), r.get("order_ref", "")):
              [i for i in (r.get("exec_ids") or "").split(";") if i] for r in rows}
    n = 0
    for t in state.trade_log:
        if t.get("exec_ids") or not t.get("order_ref"):
            continue
        k = ((t.get("date") or "").replace("-", ""), t.get("action", ""), t.get("key", ""),
             t.get("order_ref", ""))
        ids = by_key.get(k)
        if not ids:
            continue
        t["superseded"] = {"exec_ids": list(t.get("exec_ids") or []), "linked_by": "tag only"}
        t["exec_ids"] = ids
        t["exec_ids_source"] = f"flex backfill {today}"
        n += 1
    return n
