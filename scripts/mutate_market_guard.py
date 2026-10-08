"""Mutation-test `scripts/test_market_guard.py`: the ET-anchored run time and the market-open guard.

Seeds faults into a TEMP COPY of the repo (the real files are never edited) and demands the suite
catches every one. Engine: _mutate_repo_core.py.

Run: python scripts/mutate_market_guard.py
"""
from __future__ import annotations

import sys

from _mutate_repo_core import run

MH = "options_vrp/market_hours.py"
RUNNER = "scripts/run_options_paper.py"

MUTATIONS = [
    (MH, '    t = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)',
     '    t = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_localize(None).tz_localize("Europe/Zurich")',
     'the slot is read off the box\'s local (CET) clock again'),
    (MH, '    return abs((t - target).total_seconds()) <= tolerance_min * 60',
     '    return abs((t - target).total_seconds()) <= 90 * 60',
     'slot tolerance so wide that both local starts run'),
    (MH, '        if start <= t < end:', '        if start <= t <= end:',
     'the close (16:00) counted as open'),
    (MH, '            if part.split(":")[0] == today:\n                seen_today = True',
     '            if part.split(":")[0] == today:\n                return True',
     'a CLOSED day (holiday) read as open'),
    (MH, '    return False if seen_today else None', '    return False',
     'a day IB does not list is "closed" instead of unknown (clock fallback skipped)'),
    (MH, '    return 9 * 60 + 30 <= minutes < 16 * 60', '    return 9 * 60 <= minutes < 17 * 60',
     'clock fallback with the wrong session'),
    (MH, '        if r is not None:\n            return r, f"IB trading hours',
     '        if True:\n            return bool(r), f"IB trading hours',
     'an unknown IB answer is taken as closed instead of falling back to the clock'),
    (RUNNER, '        if not in_et_slot(args.et_slot):', '        if False:',
     '--et-slot ignored: both scheduled starts run'),
    (RUNNER, '        if res.regime_open and _mkt_ok:', '        if res.regime_open:',
     'a closed market no longer blocks new spreads'),
    (RUNNER, '        if not _mkt_ok:\n            logging.warning("MARKET CLOSED',
     '        if False:\n            logging.warning("MARKET CLOSED',
     'a closed market blocks silently (no warning)'),
    (RUNNER, '''        _mkt_ok, _mkt_how = market_open(broker)
        if not _mkt_ok:''', '''        _mkt_ok, _mkt_how = market_open(broker)
        if not _mkt_ok:
            return''', 'a closed market stops the whole run (would skip the report and reconcile)'),
]

if __name__ == "__main__":
    sys.exit(run("test_market_guard.py", MUTATIONS))
