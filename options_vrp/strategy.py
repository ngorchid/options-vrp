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
    # 0 = NO STOP (default since 2026-08-08). The long wing already caps the loss, and a stop
    # only converts recoverable losers into realised ones. Confirmed on real OPRA option prices
    # (SPX 2013-2026, algo_trading/scripts/spx_vrp_lab.py): monotone across settings —
    # no-stop +$73/trade Sharpe +0.27, 3x +12/+0.05, 2x -44/-0.19, 1.5x -95/-0.47 — and robust
    # to the cost assumption since cost is constant across arms. Counterfactual: 23% of trades
    # touch 2x credit and 31% of THOSE still finish profitable; not stopping is worth +$8,468
    # over 137 trades. Set >0 to re-enable.
    stop_mult: float = 0.0
    # EXECUTION COST GUARD. Refuse a spread whose quoted round-trip cost exceeds this fraction
    # of the credit. Break-even from the SPX backtest is ~31% of credit, so 0.25 leaves margin.
    # Measured 2026-08-08 (weekend, provisional): SPX ~15% of credit, but PFE 62% / SBUX 65% /
    # CAT 59% -- single-name option spreads run ~4x the index (14.6-16.1% of premium vs 3.8%),
    # so most single names may not clear costs at all. A guard is preferred over pruning the
    # basket because it ADAPTS: a name untradeable in a calm week can be fine when premiums are
    # fat and the spread is proportionally smaller.
    max_cost_frac: float = 0.25
    # Per-CONTRACT commission, one side. IB direct ~USD 0.65 (Fixed); LYNX charges USD 3.50 for
    # US equity/index options, 5.4x more. A vertical is FOUR contract-sides per round trip, so
    # this is $2.60 at IB vs $14.00 at LYNX -- and because it is per contract it does NOT
    # amortise with size. Against PFE's ~$3 credit per contract that is 142% (IB) or 492%
    # (LYNX) before a single cent of spread. Default assumes the planned move to IB direct.
    commission_per_contract: float = 0.65
    option_multiplier: float = 100.0
    # What to do when no live quote is available (no IB market-data sub / Error 162). True =
    # skip the trade. A missed trade costs nothing; a bad fill costs money.
    skip_if_no_quote: bool = True
    # LIQUIDITY SCREEN on strike selection. A strike with no open interest gets no competitive
    # quote: nobody holds it, so no market maker has inventory to hedge or reason to post tight.
    # Measured on EEM 2026-08-10 -- the 1-sigma strike quoted 0.30/0.90 (104% of credit, REJECTED)
    # while the strike one increment up quoted 0.55/0.65 (21%, PASSES) on the IDENTICAL 0.600 mid.
    # Screening on OI rather than volume because volume is a same-day flow that reads 0 on liquid
    # strikes early in the session, while OI is a stock. See `oi_threshold` for why both bounds.
    min_open_interest: int = 50
    oi_pctile: float = 0.25
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


def oi_threshold(puts: pd.DataFrame, min_oi: int, pctile: float) -> float:
    """Open-interest floor for this chain: an absolute minimum AND a share of the name's own book.

    Both, because either alone fails somewhere. A flat number sensible for SPY (OI in the
    thousands) is meaningless for a thinly-optioned name; a pure percentile always admits its own
    bottom strikes no matter how dead the whole chain is. Taking the max means a name with no
    real open interest anywhere simply produces no trade — the correct outcome, and consistent
    with `skip_if_no_quote`: a missed trade costs nothing, a bad fill costs money.
    """
    # Test the COLUMN first: pd.to_numeric(None) returns a scalar NaN, not None, so an
    # `is None` check after the conversion silently never fires.
    if puts is None or "openInterest" not in getattr(puts, "columns", ()):
        return 0.0                                   # screen disabled — provider gave us nothing
    oi = pd.to_numeric(puts["openInterest"], errors="coerce")
    if not oi.notna().any():
        return 0.0
    q = float(oi[oi > 0].quantile(pctile)) if (oi > 0).any() else 0.0
    return max(float(min_oi), q if q == q else 0.0)


def _nearest_delta_strike(puts: pd.DataFrame, spot: float, T: float, r: float, target: float,
                          min_oi: int = 0, oi_pctile: float = 0.0) -> tuple[float, float, float] | None:
    """(strike, delta, mid_price) of the OTM put whose |delta| is closest to `target`.

    Liquidity-screened. Selecting on delta ALONE lands on dead strikes, and the damage compounds:
    the delta is computed from the provider's `impliedVolatility`, which on a no-open-interest
    strike is inverted from a garbage mid — so a bad quote yields a bad IV yields a bad delta, and
    an illiquid line can masquerade as the 16-delta strike. The cost guard cannot repair this
    because it runs AFTER selection: it vetoes the whole trade rather than re-picking the strike.
    Screening here instead means we trade a neighbouring strike at nearly identical risk (adjacent
    strikes differ by ~1-3 delta points) rather than not trading at all.
    """
    floor = oi_threshold(puts, min_oi, oi_pctile) if min_oi or oi_pctile else 0.0
    rows = []
    for _, row in puts.iterrows():
        K, bid, ask, iv = row["strike"], row["bid"], row["ask"], row["impliedVolatility"]
        if K >= spot or bid <= 0 or ask <= 0 or not (0.01 < iv < 5):
            continue
        if floor > 0:
            oi = pd.to_numeric(row.get("openInterest"), errors="coerce")
            if oi != oi or oi < floor:               # NaN or below the floor -> not a real line
                continue
        d = put_delta(spot, K, T, r, iv)
        rows.append((K, d, (bid + ask) / 2))
    if not rows:
        return None
    return min(rows, key=lambda x: abs(abs(x[1]) - target))


def build_spread(ticker: str, puts: pd.DataFrame, spot: float, expiry: str, dte: int,
                 iv: float, rv: float, cfg: OptionsConfig) -> SpreadTarget | None:
    T = dte / 365.0
    short = _nearest_delta_strike(puts, spot, T, cfg.rate, cfg.short_delta,
                                  cfg.min_open_interest, cfg.oi_pctile)
    long_ = _nearest_delta_strike(puts, spot, T, cfg.rate, cfg.long_delta,
                                  cfg.min_open_interest, cfg.oi_pctile)
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


def cost_ok(bid: float | None, ask: float | None, max_frac: float,
            commission_per_contract: float = 0.65,
            multiplier: float = 100.0) -> tuple[bool, float | None, dict]:
    """Is the TOTAL round-trip cost acceptable relative to the credit?

    Two components, and they fail in different ways:

      SPREAD      you SELL the combo to open (hitting the bid) and BUY it back to close
                  (paying the ask), so the round trip costs one FULL combo width vs mid.
      COMMISSION  a vertical is FOUR contract-sides per round trip (2 legs x open + close).
                  This is PER CONTRACT, so it does not shrink with size.

        cost/credit = (ask-bid)/mid  +  4*commission / (mid*multiplier)

    Contract count cancels from every term, so the ratio is size-independent. Returns
    (ok, ratio, breakdown); ratio is None when the quote is unusable.
    """
    empty = {"spread": None, "commission": None, "credit": None}
    if bid is None or ask is None:
        return False, None, empty
    try:
        bid, ask = abs(float(bid)), abs(float(ask))
    except (TypeError, ValueError):
        return False, None, empty
    lo, hi = min(bid, ask), max(bid, ask)
    mid = (lo + hi) / 2.0
    if mid <= 0 or hi <= lo:
        return False, None, empty
    credit = mid * multiplier
    spread_cost = (hi - lo) * multiplier
    comm_cost = 4.0 * commission_per_contract
    ratio = (spread_cost + comm_cost) / credit
    return ratio <= max_frac, ratio, {"spread": spread_cost / credit,
                                      "commission": comm_cost / credit,
                                      "credit": credit}


def manage_action(entry_credit: float, current_value: float, dte: int,
                  cfg: OptionsConfig) -> str | None:
    """Decide whether to close an open spread. `current_value` = per-share debit to buy it
    back now (from IB marks). Returns 'profit' | 'stop' | 'time' | None. Pure/offline-testable."""
    if current_value <= cfg.profit_target * entry_credit:
        return "profit"
    # Stop is debatable for DEFINED-RISK spreads: the long wing already caps max loss, and
    # short-premium losers often recover by expiry, so a tight stop realizes losses that would
    # have reverted — CONFIRMED on real option prices 2026-08-08, so the stop is now OFF by
    # default. stop_mult<=0 disables it (rely on the wing + the 21-DTE time-stop).
    if cfg.stop_mult > 0 and current_value >= cfg.stop_mult * entry_credit:
        return "stop"
    if dte <= cfg.time_stop_dte:
        return "time"
    return None
