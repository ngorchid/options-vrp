"""Book a hand unwind into the options-vrp ledger: APPEND-ONLY (2026-10-08, decision #18).

When a position is unwound by hand (the blended assignment runbook, or a spread closed outside the
sleeve), the sleeve retires it at its next run (ASSIGNED_CLOSED_OUTSIDE / CLOSED_OUTSIDE) but books
no P&L. This tool records what was traded, when, and the P&L -- one HAND_UNWIND row per trade -- so
the ledger's realised P&L matches IB again. With IB's execution ids on the row, the nightly two-way
check matches it like any other fill; it does not silence anything.

It never edits or removes a row. It refuses: a spread still open in the sleeve (it would be counted
twice), a spread with no retirement row, an execution id already booked, and more shares/contracts
than the retired spread can account for. Preview by default; --apply writes (after a backup).

  python scripts/book_hand_unwind.py --key IWM_2026-11-20_263_256 --leg stock --side SELL ^
      --qty 200 --price 254.10 --date 2026-10-09 --exec-ids 0001f4e8.6720a1b2.01.01 ^
      --order-ref options-vrp:manual-20261009 --commission -1.00             (preview)
  ... --apply                                                               (write)

Legs: stock SELL (delivered shares), long SELL (the long put), short BUY (buy back the short put).
P&L per trade, as the sleeve books its own unwind (credit c, strikes Ks > Kl):
  stock SELL q @ P : (c + P - Ks) x q          long SELL k @ p : p x 100 x k
  short BUY  k @ p : (c - p) x 100 x k
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "results" / "paper" / "state.json"
MULT = 100
RETIRED = ("ASSIGNED_CLOSED_OUTSIDE", "CLOSED_OUTSIDE")
LEGS = {"stock": "SELL", "long": "SELL", "short": "BUY"}


class Refused(Exception):
    pass


def plan(state: dict, key: str, leg: str, side: str, qty: float, price: float, date: str,
         exec_ids: list[str], order_ref: str = "", commission: float | None = None,
         note: str = "", allow_no_ids: bool = False, today: str = "") -> dict:
    """The HAND_UNWIND row to append, or Refused with the reason. Pure: reads `state` only."""
    if leg not in LEGS:
        raise Refused(f"unknown leg {leg!r} (stock / long / short)")
    if side != LEGS[leg]:
        raise Refused(f"a {leg} hand unwind is a {LEGS[leg]}, not a {side}")
    if qty <= 0 or price < 0:
        raise Refused("quantity must be > 0 and price >= 0")
    if not exec_ids and not allow_no_ids:
        raise Refused("no --exec-ids: without IB's execution ids the nightly check cannot match this "
                      "trade and will keep alerting; pass --no-exec-ids to book it anyway")
    try:
        tk, _exp, ks, kl = key.rsplit("_", 3)
        ks, kl = float(ks), float(kl)
    except ValueError:
        raise Refused(f"{key!r} is not a spread key (TICKER_YYYY-MM-DD_SHORT_LONG)") from None
    if any(s.get("key") == key for s in state.get("open_spreads", [])):
        raise Refused(f"{key} is still OPEN in the sleeve: booking now would count it twice. Finish "
                      f"the unwind; the next run retires it, then book")
    log = state.get("trade_log", [])
    opens = [i for i, t in enumerate(log) if t.get("action") == "OPEN" and t.get("key") == key]
    if not opens:
        raise Refused(f"no OPEN row for {key} in the ledger")
    o = opens[-1]
    credit = float(log[o].get("credit") or 0.0)
    ret = [t for t in log[o + 1:] if t.get("action") in RETIRED and t.get("key") == key]
    if not ret:
        raise Refused(f"{key} has no retirement row ({' / '.join(RETIRED)}) after its OPEN: only a "
                      f"position the sleeve has retired can be booked here")
    seen = {i for t in log for i in (t.get("exec_ids") or [])}
    dup = [i for i in exec_ids if i in seen]
    if dup:
        raise Refused(f"execution id(s) already in the ledger: {', '.join(dup)}")
    r = ret[-1]
    n = int(r.get("contracts") or 0)
    since = log[o + 1:]
    booked = sum(float(t.get("qty") or 0) for t in since
                 if t.get("action") == "HAND_UNWIND" and t.get("key") == key and t.get("leg") == leg)
    if leg == "stock":
        sold = sum(float(t.get("shares") or 0) for t in since
                   if t.get("action") == "ASSIGNED_STOCK_SOLD" and t.get("key") == key)
        limit = MULT * n - sold
        what = "shares"
    else:
        if leg == "short" and r.get("action") != "CLOSED_OUTSIDE":
            raise Refused("a short-put buyback only applies to a spread retired as CLOSED_OUTSIDE (the "
                          "short leg of an assigned spread was assigned, not bought back)")
        limit = n - (sum(float(t.get("contracts") or 0) for t in since
                         if t.get("action") == "ASSIGNED_LONG_SOLD" and t.get("key") == key)
                     if leg == "long" else 0)
        what = "contracts"
    if booked + qty > limit + 1e-9:
        raise Refused(f"{qty:g} {what} would exceed what {key} can account for: {limit:g} in total, "
                      f"{booked:g} already booked by hand")
    if leg == "stock":
        pnl = (credit + price - ks) * qty
    elif leg == "long":
        pnl = price * MULT * qty
    else:
        pnl = (credit - price) * MULT * qty
    return {"date": date, "action": "HAND_UNWIND", "key": key, "leg": leg, "side": side,
            "qty": float(qty), "strike": None if leg == "stock" else (ks if leg == "short" else kl),
            "price": float(price), "pnl": round(pnl, 6), "exec_ids": list(exec_ids),
            "order_ref": order_ref, "commission": commission, "currency": "USD",
            "retired_by": r.get("action"), "note": note,
            "booked_on": today or datetime.now().strftime("%Y-%m-%d"),
            "booked_by": "book_hand_unwind.py"}


def apply(path: Path, row: dict) -> Path:
    """Append `row` and add its P&L. Backs the file up first; returns the backup path."""
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    state = json.loads(path.read_text())
    state.setdefault("trade_log", []).append(row)
    state["realized_pnl"] = float(state.get("realized_pnl") or 0.0) + float(row["pnl"])
    path.write_text(json.dumps(state, indent=2, default=str))
    return backup


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True)
    ap.add_argument("--leg", required=True, choices=sorted(LEGS))
    ap.add_argument("--side", required=True, choices=["BUY", "SELL"])
    ap.add_argument("--qty", required=True, type=float)
    ap.add_argument("--price", required=True, type=float)
    ap.add_argument("--date", required=True, help="trade date, YYYY-MM-DD")
    ap.add_argument("--exec-ids", default="", help="IB execution ids, comma-separated")
    ap.add_argument("--no-exec-ids", action="store_true")
    ap.add_argument("--order-ref", default="")
    ap.add_argument("--commission", type=float, default=None)
    ap.add_argument("--note", default="")
    ap.add_argument("--state", default=str(STATE_FILE))
    ap.add_argument("--apply", action="store_true", help="write the row (default: preview only)")
    a = ap.parse_args(argv)
    path = Path(a.state)
    if not path.exists():
        print(f"REFUSED: no ledger at {path}")
        return 2
    try:
        row = plan(json.loads(path.read_text()), a.key, a.leg, a.side, a.qty, a.price, a.date,
                   [x.strip() for x in a.exec_ids.split(",") if x.strip()], a.order_ref,
                   a.commission, a.note, a.no_exec_ids)
    except Refused as e:
        print(f"REFUSED: {e}")
        return 2
    print(json.dumps(row, indent=2))
    print(f"P&L {row['pnl']:+,.2f} USD (commission recorded separately: {row['commission']})")
    if not a.apply:
        print("PREVIEW only — nothing written. Re-run with --apply to append this row.")
        return 0
    backup = apply(path, row)
    print(f"APPENDED to {path} (backup: {backup.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
