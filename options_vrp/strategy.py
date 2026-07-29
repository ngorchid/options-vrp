"""Strategy layer: turn signals + chains into a target book of put credit spreads.

Structure (from the design work): sell the ~16-delta (≈1σ) put, buy a further-OTM put for
defined risk; 30-45 DTE; only when the regime gate is open, only on names with positive VRP,
ranked by VRP (richest first). Sizing caps each position's max loss at `risk_per_trade` of
budget. Pure/offline (no IB) so the dry-run book is fully inspectable before any live order.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import data, signal
from .greeks import put_delta

# Liquid, well-optioned basket spread across sectors (diversified 2026-07-29 from all-mega-cap-
# tech, which meant a tech vol-spike hit every single name at once — and left the whole pool
# barren when tech vol was under-priced). Index anchors + 2 tech + health/energy/consumer/
# industrials/rates. The daily VRP filter picks which are genuinely rich each day; the basket's
# job is a diverse, liquid POOL where *something* is usually rich + uncorrelated. See
# scripts/screen_basket.py for the VRP-richness + correlation-to-tech screen behind these picks.
DEFAULT_BASKET = [
    "SPY", "QQQ", "IWM",          # index anchors (reliable, thin premium)
    "NVDA", "AAPL",               # tech (trimmed from 5 — rich only when tech vol is bid)
    "XLV", "PFE",                 # healthcare (XLV = health ETF, liquid + sizeable; LLY dropped —
                                  #   at ~$1200/share its spread is too wide to size on this book)
    "XOM", "XLE",                 # energy (rich VRP, negative corr to tech)
    "SBUX", "MCD",                # consumer (SBUX richest in screen; MCD defensive, neg corr)
    "DE", "CAT",                  # industrials
    "TLT",                        # rates (different factor, uncorrelated)
]


@dataclass
class OptionsConfig:
    basket: list[str] = field(default_factory=lambda: list(DEFAULT_BASKET))
    regime_thr: float = 1.00          # sell only when VIX/VIX3M < this (contango)
    vrp_min: float = 0.02             # require ATM IV − RV20 above this (vol points): skip thin premium
    short_delta: float = 0.16         # short put ≈ 1σ
    long_delta: float = 0.10          # long put (defined-risk wing); nearer = narrower spread
    dte_min: int = 30
    dte_max: int = 45
    rate: float = 0.04                # risk-free for delta calc
    budget: float = 100_000.0
    risk_per_trade: float = 0.03      # max loss per spread position ≤ 3% of budget (fits high-IV names)
    max_positions: int = 6
    # Mechanical management (from the design work) — reduces the end-of-life gamma tail.
    profit_target: float = 0.50       # close when the spread can be bought back for ≤ 50% of entry credit
    stop_mult: float = 2.0            # close when the spread has doubled (loss ≈ 1× credit)
    time_stop_dte: int = 21           # close on/under this DTE regardless


@dataclass
class SpreadTarget:
    ticker: str
    expiry: str
    dte: int
    spot: float
    iv: float
    rv: float
    vrp: float
    short_strike: float
    long_strike: float
    short_delta: float
    long_delta: float
    credit: float          # net credit per share
    width: float           # strike distance per share
    max_loss: float        # (width − credit) per share
    contracts: int         # number of spreads


def pick_expiry(expiries: tuple[str, ...], today: pd.Timestamp,
                dte_min: int, dte_max: int) -> tuple[str, int] | None:
    """Choose the expiry whose DTE lands in [min,max], nearest the window midpoint."""
    mid = (dte_min + dte_max) / 2
    best = None
    for e in expiries:
        dte = (pd.Timestamp(e) - today).days
        if dte_min <= dte <= dte_max:
            if best is None or abs(dte - mid) < abs(best[1] - mid):
                best = (e, dte)
    return best


def _nearest_delta_strike(puts: pd.DataFrame, spot: float, T: float, r: float,
                          target: float) -> tuple[float, float, float] | None:
    """(strike, delta, mid_price) of the OTM put whose |delta| is closest to `target`."""
    rows = []
    for _, row in puts.iterrows():
        K, bid, ask, iv = row["strike"], row["bid"], row["ask"], row["impliedVolatility"]
        if K >= spot or bid <= 0 or ask <= 0 or not (0.01 < iv < 5):
            continue
        d = put_delta(spot, K, T, r, iv)
        rows.append((K, d, (bid + ask) / 2))
    if not rows:
        return None
    return min(rows, key=lambda x: abs(abs(x[1]) - target))


def build_spread(ticker: str, puts: pd.DataFrame, spot: float, expiry: str, dte: int,
                 iv: float, rv: float, cfg: OptionsConfig) -> SpreadTarget | None:
    T = dte / 365.0
    short = _nearest_delta_strike(puts, spot, T, cfg.rate, cfg.short_delta)
    long_ = _nearest_delta_strike(puts, spot, T, cfg.rate, cfg.long_delta)
    if short is None or long_ is None or long_[0] >= short[0]:
        return None
    credit = short[2] - long_[2]
    width = short[0] - long_[0]
    max_loss = width - credit
    if credit <= 0 or max_loss <= 0:
        return None
    contracts = int((cfg.risk_per_trade * cfg.budget) // (max_loss * 100))
    return SpreadTarget(ticker, expiry, dte, spot, iv, rv, signal.vrp(iv, rv),
                        short[0], long_[0], short[1], long_[1], credit, width, max_loss, contracts)


@dataclass
class BookResult:
    regime_ratio: float
    vix: float
    regime_open: bool
    diagnostics: list[dict]        # per-name signal snapshot (for the dry-run table + IV recorder)
    targets: list[SpreadTarget]    # selected spreads (empty if gate shut)


def target_book(cfg: OptionsConfig, today: pd.Timestamp | None = None) -> BookResult:
    today = today or pd.Timestamp.today().normalize()
    ratio, vix, _ = data.regime()
    open_ = signal.regime_open(ratio, cfg.regime_thr)

    prices = data.price_history(cfg.basket)
    diags: list[dict] = []
    candidates: list[SpreadTarget] = []
    for tk_name in cfg.basket:
        if tk_name not in prices.columns:
            continue
        spot = float(prices[tk_name].iloc[-1])
        rv = signal.realized_vol(prices[tk_name])
        rec = {"ticker": tk_name, "spot": spot, "rv": rv, "iv": float("nan"),
               "vrp": float("nan"), "expiry": None, "dte": None, "note": ""}
        try:
            tk, expiries = data.option_expiries(tk_name)
            pick = pick_expiry(expiries, today, cfg.dte_min, cfg.dte_max)
            if pick is None:
                rec["note"] = "no expiry in DTE window"; diags.append(rec); continue
            expiry, dte = pick
            pdf = data.puts(tk, expiry)
            iv = signal.atm_iv(pdf, spot)
            rec.update(iv=iv, vrp=signal.vrp(iv, rv), expiry=expiry, dte=dte)
            if open_ and signal.vrp(iv, rv) > cfg.vrp_min:
                sp = build_spread(tk_name, pdf, spot, expiry, dte, iv, rv, cfg)
                if sp and sp.contracts > 0:
                    candidates.append(sp)
                elif sp:
                    rec["note"] = "sized to 0 (max-loss > risk budget)"
                else:
                    rec["note"] = "no valid spread (strikes/credit)"
            elif open_:
                rec["note"] = f"VRP {signal.vrp(iv, rv):+.1%} ≤ min"
        except Exception as e:  # noqa: BLE001
            rec["note"] = f"chain error: {type(e).__name__}"
        diags.append(rec)

    candidates.sort(key=lambda s: s.vrp, reverse=True)
    return BookResult(ratio, vix, open_, diags, candidates[: cfg.max_positions])


def manage_action(entry_credit: float, current_value: float, dte: int,
                  cfg: OptionsConfig) -> str | None:
    """Decide whether to close an open spread. `current_value` = per-share debit to buy it
    back now (from IB marks). Returns 'profit' | 'stop' | 'time' | None. Pure/offline-testable."""
    if current_value <= cfg.profit_target * entry_credit:
        return "profit"
    if current_value >= cfg.stop_mult * entry_credit:
        return "stop"
    if dte <= cfg.time_stop_dte:
        return "time"
    return None
