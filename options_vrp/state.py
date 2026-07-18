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
        self.trade_log.append({"date": today, "action": "CLOSE", "key": sp.key,
                               "close_value": close_value, "pnl": pnl, "reason": reason})
        return pnl

    def record_snapshot(self, today: str, total_pnl: float) -> None:
        self.nav_history.append((today, total_pnl))
