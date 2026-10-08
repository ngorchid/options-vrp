"""Tests for scripts/book_hand_unwind.py: append-only booking of a hand unwind (decision #18).

Covers the P&L rules, every refusal, preview vs --apply, that applying only APPENDS (every earlier
byte of the ledger's content is kept), and the full blended path: the sleeve records a MANUAL
assignment, the owner unwinds by hand, the next run retires the spread, the tool books it.
scripts/mutate_book_hand_unwind.py seeds the faults.

Run: python scripts/test_book_hand_unwind.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import book_hand_unwind as bh  # noqa: E402
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


KEY = "IWM_2026-11-20_263_256"
C, KS, KL = 0.74, 263.0, 256.0


def ledger(retire="ASSIGNED_CLOSED_OUTSIDE", n=2, extra=None, open_spreads=None) -> dict:
    log = [{"date": "2026-10-01", "action": "OPEN", "key": KEY, "contracts": n, "credit": C,
            "exec_ids": ["o.01.01", "o.02.01", "o.03.01"]},
           {"date": "2026-10-07", "action": "ASSIGNED", "key": KEY, "contracts": n, "strike": KS,
            "shares": 100 * n, "exact": False}]
    log += extra or []
    if retire:
        log.append({"date": "2026-10-09", "action": retire, "key": KEY, "contracts": n})
    return {"inception_date": "2026-10-04", "realized_pnl": -12.5, "open_spreads": open_spreads or [],
            "trade_log": log, "nav_history": [["2026-10-08", -12.5]], "pending_orders": []}


def refused(st, **kw) -> str:
    args = dict(key=KEY, leg="stock", side="SELL", qty=200, price=254.1, date="2026-10-09",
                exec_ids=["h.01.01"])
    args.update(kw)
    try:
        bh.plan(st, **args)
        return ""
    except bh.Refused as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 -- a crash is a FAILED check here, not a test crash
        return f"crashed: {type(e).__name__}: {e}"


# =============================================================================================
print("P&L RULES (as the sleeve books its own unwind)")
row = bh.plan(ledger(), KEY, "stock", "SELL", 200, 254.10, "2026-10-09", ["h1"])
check("stock SELL 200 @ 254.10: (c + P - Ks) x 200", abs(row["pnl"] - (C + 254.10 - KS) * 200) < 1e-6, str(row))
row = bh.plan(ledger(), KEY, "long", "SELL", 2, 3.45, "2026-10-09", ["h2"])
check("long SELL 2 @ 3.45: p x 100 x 2, at the long strike", abs(row["pnl"] - 690.0) < 1e-6
      and row["strike"] == KL, str(row))
row = bh.plan(ledger("CLOSED_OUTSIDE"), KEY, "short", "BUY", 2, 5.00, "2026-10-09", ["h3"])
check("short BUY 2 @ 5.00 (closed by hand): (c - p) x 100 x 2, at the short strike",
      abs(row["pnl"] - (C - 5.0) * 200) < 1e-6 and row["strike"] == KS, str(row))
check("the row says what, when, the P&L, the ids and who booked it",
      row["action"] == "HAND_UNWIND" and row["date"] == "2026-10-09" and row["exec_ids"] == ["h3"]
      and row["booked_by"] == "book_hand_unwind.py" and row["retired_by"] == "CLOSED_OUTSIDE", str(row))

# =============================================================================================
print("\nREFUSALS")
check("a spread still OPEN in the sleeve (would be counted twice)",
      "still OPEN" in refused(ledger(open_spreads=[{"key": KEY}])), "")
check("no retirement row (the sleeve has not retired it)", "no retirement row" in refused(ledger(retire=None)), "")
check("no OPEN row for the key", "no OPEN row" in refused(ledger(), key="XLE_2026-11-20_59_57"), "")
check("more shares than delivered (250 for 2 contracts)", "exceed" in refused(ledger(), qty=250), "")
sold = [{"date": "2026-10-08", "action": "ASSIGNED_STOCK_SOLD", "key": KEY, "shares": 120.0}]
check("shares the sleeve already sold count against the limit (120 sold -> 80 left; 100 refused)",
      "exceed" in refused(ledger(extra=sold), qty=100) and refused(ledger(extra=sold), qty=80) == "", "")
prior = [{"date": "2026-10-09", "action": "HAND_UNWIND", "key": KEY, "leg": "stock", "qty": 150.0,
          "exec_ids": ["h.0"]}]
st2 = ledger()
st2["trade_log"].append(prior[0])
check("earlier hand bookings count too (150 booked -> 60 refused, 50 fine)",
      "exceed" in refused(st2, qty=60, exec_ids=["h.9"]) and refused(st2, qty=50, exec_ids=["h.9"]) == "", "")
check("more long puts than the spread had", "exceed" in refused(ledger(), leg="long", qty=3, price=3.4), "")
check("an execution id already in the ledger", "already in the ledger" in refused(ledger(), exec_ids=["o.02.01"]), "")
check("no exec ids without --no-exec-ids", "--no-exec-ids" in refused(ledger(), exec_ids=[]), "")
check("...but allowed when asked for explicitly",
      refused(ledger(), exec_ids=[], allow_no_ids=True) == "", "")
check("a short buyback on an ASSIGNED spread (its short leg was assigned)",
      "short-put buyback" in refused(ledger(), leg="short", side="BUY", qty=1, price=5.0), "")
check("the wrong side for the leg (stock BUY)", "not a BUY" in refused(ledger(), side="BUY"), "")
check("zero quantity", "quantity" in refused(ledger(), qty=0), "")

# =============================================================================================
print("\nPREVIEW vs --apply, AND APPEND-ONLY")
tmp = Path(tempfile.mkdtemp())
path = tmp / "state.json"
path.write_text(json.dumps(ledger(), indent=2))
before = json.loads(path.read_text())
argv = ["--key", KEY, "--leg", "stock", "--side", "SELL", "--qty", "200", "--price", "254.10",
        "--date", "2026-10-09", "--exec-ids", "h.01.01", "--order-ref", "options-vrp:manual-20261009",
        "--state", str(path)]
with redirect_stdout(io.StringIO()) as out:
    rc = bh.main(argv)
check("preview (no --apply): exit 0, says PREVIEW, the file is untouched",
      rc == 0 and "PREVIEW" in out.getvalue() and json.loads(path.read_text()) == before, out.getvalue()[-200:])
with redirect_stdout(io.StringIO()) as out:
    rc = bh.main(argv + ["--apply"])
after = json.loads(path.read_text())
check("--apply: exit 0 and a backup of the previous file exists",
      rc == 0 and len(list(tmp.glob("state.json.bak-*"))) == 1, out.getvalue()[-200:])
check("APPEND-ONLY: every earlier row and field is unchanged; one row added; realised += its P&L",
      after["trade_log"][:-1] == before["trade_log"]
      and {k: v for k, v in after.items() if k not in ("trade_log", "realized_pnl")}
      == {k: v for k, v in before.items() if k not in ("trade_log", "realized_pnl")}
      and abs(after["realized_pnl"] - (before["realized_pnl"] + after["trade_log"][-1]["pnl"])) < 1e-9
      and after["trade_log"][-1]["action"] == "HAND_UNWIND", "")
with redirect_stdout(io.StringIO()) as out:
    rc = bh.main(argv + ["--apply"])
check("running the same booking twice is REFUSED (its exec id is now in the ledger), exit 2",
      rc == 2 and "already in the ledger" in out.getvalue() and json.loads(path.read_text()) == after,
      out.getvalue()[-200:])
with redirect_stdout(io.StringIO()) as out:
    rc = bh.main(argv[:-2] + ["--state", str(tmp / "missing.json")])
check("no ledger at the path -> REFUSED, exit 2", rc == 2 and "no ledger" in out.getvalue(), "")

# =============================================================================================
print("\nTHE FULL BLENDED PATH (real handle_assignments, then the tool)")
runner.logging.error = lambda *a, **k: None
runner.logging.warning = lambda *a, **k: None
E = "20261120"


class FB:
    def __init__(self, puts, stocks):
        self.puts, self.stocks = puts, stocks

    def put_positions(self):
        return self.puts

    def stock_positions_detail(self):
        return self.stocks

    def marks(self):
        return ({("IWM", E, KL): 3.5}, {"IWM": 254.0})


st = OptionsState(open_spreads=[OpenSpread("IWM", "2026-11-20", KS, KL, 2, C, 6.26, "2026-10-01", 276.0)])
st.trade_log.append({"date": "2026-10-01", "action": "OPEN", "key": KEY, "contracts": 2, "credit": C,
                     "exec_ids": ["o.02.01", "o.03.01"]})
runner.handle_assignments(FB({("IWM", E, KS): 0.0, ("IWM", E, KL): 2.0},
                             {"IWM": (260.0, (60 * 150 + 200 * KS) / 260)}), st, "2026-10-07")
check("day 1: recorded as a MANUAL assignment, nothing sold", st.open_spreads
      and st.open_spreads[0].assigned_contracts == 2 and not st.open_spreads[0].assigned_auto, "")
runner.handle_assignments(FB({("IWM", E, KS): 0.0, ("IWM", E, KL): 0.0}, {"IWM": (60.0, 150.0)}),
                          st, "2026-10-09")              # owner sold 200 sh + 2 puts by hand
check("after the hand unwind the next run retires it (ASSIGNED_CLOSED_OUTSIDE), P&L not booked",
      st.open_spreads == [] and st.trade_log[-1]["action"] == "ASSIGNED_CLOSED_OUTSIDE"
      and st.realized_pnl == 0.0, str(st.trade_log[-1]))
p2 = tmp / "blended.json"
st.save(p2)
for a in (["--leg", "stock", "--side", "SELL", "--qty", "200", "--price", "254.10", "--exec-ids", "m.1"],
          ["--leg", "long", "--side", "SELL", "--qty", "2", "--price", "3.45", "--exec-ids", "m.2"]):
    with redirect_stdout(io.StringIO()):
        bh.main(["--key", KEY, "--date", "2026-10-09", "--state", str(p2), "--apply", *a])
final = OptionsState.load(p2)
want = (C + 254.10 - KS) * 200 + 3.45 * 200
check("the tool books both trades: realised = (c + S - Ks) x 200 + Pl x 200, the same as an automatic "
      "unwind would have booked", abs(final.realized_pnl - want) < 1e-6
      and [t["action"] for t in final.trade_log[-2:]] == ["HAND_UNWIND", "HAND_UNWIND"],
      f"{final.realized_pnl} vs {want}")
check("the booked ledger still loads in the sleeve (state round-trip)", isinstance(final, OptionsState), "")

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for x in _fails:
        print("   " + x)
    sys.exit(1)
print(f"all {_ran} hand-unwind booking checks behaved as expected")
