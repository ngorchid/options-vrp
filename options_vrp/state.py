"""Persistent state for the options paper book — open spreads + realized-P&L ledger (JSON).

One machine owns state.json (like the sibling systems). Open spreads carry everything needed
to mark and manage them (strikes, entry credit, contracts); closing one books realized P&L.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path


@dataclass
class OpenSpread:
    ticker: str
    expiry: str            # 'YYYY-MM-DD'
    short_strike: float
    long_strike: float
    contracts: int
    entry_credit: float    # per share, net (positive = received)
    max_loss: float        # per share
    entry_date: str
    entry_spot: float
    # Highest mark seen while open, per share. Lets ONE book answer the stop A/B: with the
    # stop OFF, peak_value >= stop_mult * entry_credit identifies exactly the trades a stop
    # WOULD have cut, and the realised P&L shows what they went on to do. With the stop ON
    # the counterfactual is unobservable, because the stop truncates the path.
    peak_value: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.ticker}_{self.expiry}_{self.short_strike:g}_{self.long_strike:g}"


@dataclass
class OptionsState:
    inception_date: str | None = None
    realized_pnl: float = 0.0
    open_spreads: list = field(default_factory=list)   # list[OpenSpread]
    trade_log: list = field(default_factory=list)
    nav_history: list = field(default_factory=list)    # [(date, total_pnl)]

    # --- persistence ---
    @classmethod
    def load(cls, path: Path) -> "OptionsState":
        if not Path(path).exists():
            return cls()
        d = json.loads(Path(path).read_text())
        d["open_spreads"] = [OpenSpread(**s) for s in d.get("open_spreads", [])]
        return cls(**d)

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        Path(path).write_text(json.dumps(d, indent=2, default=str))

    def ensure_inception(self, today: str) -> None:
        if not self.inception_date:
            self.inception_date = today

    # --- mutations ---
    def has(self, key: str) -> bool:
        return any(s.key == key for s in self.open_spreads)

    def record_open(self, sp: OpenSpread, fill_credit: float, today: str) -> None:
        sp.entry_credit = fill_credit
        self.open_spreads.append(sp)
        self.trade_log.append({"date": today, "action": "OPEN", "key": sp.key,
                               "contracts": sp.contracts, "credit": fill_credit})

    def record_close(self, sp: OpenSpread, close_value: float, today: str, reason: str) -> float:
        """close_value = per-share debit paid to close. Books realized P&L, removes the spread."""
        pnl = (sp.entry_credit - close_value) * 100 * sp.contracts
        self.realized_pnl += pnl
        self.open_spreads = [s for s in self.open_spreads if s.key != sp.key]
        peak = max(sp.peak_value, close_value)
        self.trade_log.append({"date": today, "action": "CLOSE", "key": sp.key,
                               "close_value": close_value, "pnl": pnl, "reason": reason,
                               "entry_credit": sp.entry_credit, "peak_value": peak,
                               "peak_mult": (peak / sp.entry_credit) if sp.entry_credit else None,
                               "contracts": sp.contracts})
        return pnl

    def stop_counterfactual(self, stop_mult: float = 2.0) -> dict:
        """Would the 2x stop have helped? Readable only when the stop is DISABLED.

        Splits closed trades into those whose mark ever reached stop_mult x credit ("would
        have been stopped") and those that did not, and reports what actually happened to
        each group. `recovered` is the count that touched the threshold and still finished
        profitable — those are precisely the trades a stop destroys.
        """
        rows = [t for t in self.trade_log
                if t.get("action") == "CLOSE" and t.get("peak_mult") is not None]
        if not rows:
            return {"n": 0}
        hit = [t for t in rows if t["peak_mult"] >= stop_mult]
        miss = [t for t in rows if t["peak_mult"] < stop_mult]
        recovered = [t for t in hit if t["pnl"] > 0]
        # what the stop would have booked instead: -(stop_mult-1) x credit x 100 x contracts
        stopped_pnl = sum(-(stop_mult - 1.0) * t["entry_credit"] * 100 * t.get("contracts", 1)
                          for t in hit)
        return {
            "n": len(rows),
            "n_would_stop": len(hit),
            "n_recovered": len(recovered),
            "actual_pnl_of_stopped_group": sum(t["pnl"] for t in hit),
            "stop_would_have_booked": stopped_pnl,
            "edge_of_no_stop": sum(t["pnl"] for t in hit) - stopped_pnl,
            "pnl_untouched_group": sum(t["pnl"] for t in miss),
        }

    def record_snapshot(self, today: str, total_pnl: float) -> None:
        self.nav_history.append((today, total_pnl))
