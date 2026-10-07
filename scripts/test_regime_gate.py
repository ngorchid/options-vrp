"""REGIME GATE (2026-10-07): the owner's decision is CODE, and nothing silently overrides it.

Decision (options_vrp/strategy.py): the VIX/VIX3M gate is OFF for the basket (2026-08-28) and ON
only for names whose stress shows in the term structure before the damage — USO (2026-09-08).
Until 2026-10-07 it lived only in os.getenv() defaults, the dataclass defaulted to the old
gate-ON 1.00, and a stale REGIME_THR=1.00 in the live .env silently gated EVERY name.

scripts/mutate_regime_gate.py seeds the faults. Run: python scripts/test_regime_gate.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from options_vrp import signal  # noqa: E402
from options_vrp import strategy as S  # noqa: E402
import run_options_paper as runner  # noqa: E402  (loads the repo .env, if any — cleared below)

_fails: list[str] = []
_ran = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _ran
    _ran += 1
    if not cond:
        _fails.append(f"{label}  | {detail}")
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + ("" if cond else f"   <- {detail}"))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.msgs: list[str] = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def settings(env: dict[str, str]) -> tuple[float, dict, list[str]]:
    """_regime_settings() under exactly `env` for the two REGIME_* keys."""
    for k in ("REGIME_THR", "REGIME_THR_BY_NAME"):
        os.environ.pop(k, None)
    os.environ.update(env)
    cap = _Capture()
    logging.getLogger().addHandler(cap)
    try:
        thr, names = runner._regime_settings()
    finally:
        logging.getLogger().removeHandler(cap)
        for k in env:
            os.environ.pop(k, None)
    return thr, names, cap.msgs


print("THE DECISION, AS CODE")
check("global default is OFF (a threshold the ratio can never reach)",
      S.REGIME_THR_DEFAULT == S.REGIME_GATE_OFF and S.REGIME_GATE_OFF >= 99, str(S.REGIME_THR_DEFAULT))
check("per-name gate: exactly USO at 1.00", S.GATED_NAMES == {"USO": 1.00}, str(S.GATED_NAMES))
cfg = S.OptionsConfig()
check("OptionsConfig() defaults ARE the decision (no stale gate-ON 1.00)",
      cfg.regime_thr == S.REGIME_THR_DEFAULT and cfg.regime_thr_by_name == S.GATED_NAMES,
      f"{cfg.regime_thr} {cfg.regime_thr_by_name}")
check("gated_names() is exactly ['USO']", cfg.gated_names() == ["USO"], str(cfg.gated_names()))
cfg.regime_thr_by_name["SPY"] = 1.0
check("a config's per-name dict is its own copy (editing one never changes the decision)",
      S.GATED_NAMES == {"USO": 1.00} and S.OptionsConfig().regime_thr_by_name == {"USO": 1.00},
      str(S.GATED_NAMES))

print("\nTHE DECISION, IN EFFECT")
cfg = S.OptionsConfig()
back, cont = 1.20, 0.85              # VIX/VIX3M in backwardation (stress) and contango (calm)
open_in_stress = [t for t in cfg.basket if signal.regime_open(back, cfg.thr_for(t))]
check("in backwardation USO is blocked ...", "USO" not in open_in_stress, str(open_in_stress))
check("... and every other basket name still trades",
      sorted(open_in_stress) == sorted(t for t in cfg.basket if t != "USO"), str(open_in_stress))
check("in contango every name trades, USO included",
      all(signal.regime_open(cont, cfg.thr_for(t)) for t in cfg.basket), "")
check("the global gate never closes, even in deep backwardation",
      signal.regime_open(5.0, cfg.regime_thr), "")

print("\nTHE RUNNER")
thr, names, warns = settings({})
check("no env -> the documented decision", thr == S.REGIME_THR_DEFAULT and names == S.GATED_NAMES,
      f"{thr} {names}")
check("...and no override warning", not warns, str(warns))
thr, names, warns = settings({"REGIME_THR": "1.00"})
check("env REGIME_THR=1.00 is honoured (a deliberate override still works)", thr == 1.00, str(thr))
check("...but WARNED about, saying it now gates every name",
      any("REGIME_THR=1.00" in w and "EVERY name" in w for w in warns), str(warns))
thr, names, warns = settings({"REGIME_THR_BY_NAME": ""})
check("env REGIME_THR_BY_NAME='' gates nothing, as documented", names == {}, str(names))
check("...and is WARNED about", any("REGIME_THR_BY_NAME" in w for w in warns), str(warns))
thr, names, warns = settings({"REGIME_THR": "abc"})
check("a malformed REGIME_THR falls back to the decision instead of crashing",
      thr == S.REGIME_THR_DEFAULT and any("not a number" in w for w in warns), f"{thr} {warns}")
for k in ("REGIME_THR", "REGIME_THR_BY_NAME"):
    os.environ.pop(k, None)
logging.disable(logging.WARNING)          # _cfg logs sizing notes; irrelevant here
try:
    c = runner._cfg(None)
finally:
    logging.disable(logging.NOTSET)
check("_cfg() (what run_live uses) carries the decision",
      c.regime_thr == S.REGIME_THR_DEFAULT and c.regime_thr_by_name == S.GATED_NAMES,
      f"{c.regime_thr} {c.regime_thr_by_name}")

print("\nTHE TEMPLATE")
lines = [l.strip() for l in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()]
check(".env.example does not SET REGIME_THR or REGIME_THR_BY_NAME (a copy must not pin them)",
      not any(l.startswith(("REGIME_THR=", "REGIME_THR_BY_NAME=")) for l in lines),
      str([l for l in lines if l.startswith("REGIME")]))

print("\n" + "=" * 88)
if _fails:
    print(f"{len(_fails)} FAILURE(S) of {_ran}:")
    for f in _fails:
        print("   " + f)
    sys.exit(1)
print(f"all {_ran} regime-gate checks behaved as expected")
