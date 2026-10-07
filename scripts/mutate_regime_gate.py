"""Mutation-test `scripts/test_regime_gate.py`: the regime-gate decision as code.

Seeds real faults into a TEMP COPY of the repo (the real files are never edited) and demands the
suite catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_regime_gate.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

MUTATIONS = [
    # --- the decision itself ---
    ('options_vrp/strategy.py', 'REGIME_THR_DEFAULT = REGIME_GATE_OFF          # global: OFF',
     'REGIME_THR_DEFAULT = 1.00          # global: OFF', 'global gate switched back ON'),
    ('options_vrp/strategy.py', 'REGIME_GATE_OFF = 99.0', 'REGIME_GATE_OFF = 2.0',
     '"off" threshold a stressed ratio can reach'),
    ('options_vrp/strategy.py', 'GATED_NAMES: dict[str, float] = {"USO": 1.00}',
     'GATED_NAMES: dict[str, float] = {}', 'USO no longer gated'),
    ('options_vrp/strategy.py', 'GATED_NAMES: dict[str, float] = {"USO": 1.00}',
     'GATED_NAMES: dict[str, float] = {"USO": 1.00, "XLE": 1.00}', 'an extra name gated'),
    ('options_vrp/strategy.py', 'GATED_NAMES: dict[str, float] = {"USO": 1.00}',
     'GATED_NAMES: dict[str, float] = {"USO": 1.50}', "USO's threshold changed"),
    # --- the dataclass carries it ---
    ('options_vrp/strategy.py', '    regime_thr: float = REGIME_THR_DEFAULT\n',
     '    regime_thr: float = 1.00\n', 'dataclass back to the stale gate-ON default'),
    ('options_vrp/strategy.py',
     '    regime_thr_by_name: dict[str, float] = field(default_factory=lambda: dict(GATED_NAMES))',
     '    regime_thr_by_name: dict[str, float] = field(default_factory=dict)',
     'dataclass drops the per-name gates'),
    ('options_vrp/strategy.py',
     '    regime_thr_by_name: dict[str, float] = field(default_factory=lambda: dict(GATED_NAMES))',
     '    regime_thr_by_name: dict[str, float] = field(default_factory=lambda: GATED_NAMES)',
     'configs share (and can edit) the decision dict'),
    ('options_vrp/strategy.py', '        return self.regime_thr_by_name.get(ticker, self.regime_thr)',
     '        return self.regime_thr', 'per-name thresholds ignored'),
    # --- the runner applies it, and warns on overrides ---
    ('scripts/run_options_paper.py', '    thr, by_name = REGIME_THR_DEFAULT, dict(GATED_NAMES)',
     '    thr, by_name = 1.00, {"USO": 1.00}', 'runner hard-codes its own (stale) defaults'),
    ('scripts/run_options_paper.py', '            thr = float(env_thr)',
     '            thr = REGIME_THR_DEFAULT', 'env REGIME_THR override ignored'),
    ('scripts/run_options_paper.py', '        by_name = _parse_gated(env_names)',
     '        pass', 'env REGIME_THR_BY_NAME override ignored'),
    ('scripts/run_options_paper.py', '    if thr != REGIME_THR_DEFAULT:\n        logging.warning(',
     '    if False:\n        logging.warning(', 'REGIME_THR override no longer warned'),
    ('scripts/run_options_paper.py', '    if by_name != GATED_NAMES:\n        logging.warning(',
     '    if False:\n        logging.warning(', 'REGIME_THR_BY_NAME override no longer warned'),
    ('scripts/run_options_paper.py', '        except ValueError:\n            logging.warning("REGIME_THR=%r',
     '        except TypeError:\n            logging.warning("REGIME_THR=%r',
     'a malformed REGIME_THR crashes the run'),
    ('scripts/run_options_paper.py', '        regime_thr=regime_thr,                 # the decision',
     '        regime_thr=1.00,                 # the decision', '_cfg ignores the decision'),
    # --- the template cannot pin it ---
    ('.env.example', '# REGIME_THR=99\n', 'REGIME_THR=99\n', '.env.example pins REGIME_THR again'),
]

if __name__ == "__main__":
    sys.exit(run("test_regime_gate.py", MUTATIONS))
