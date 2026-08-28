"""Strategy layer: turn signals + chains into a target book of put credit spreads.

Structure (from the design work): sell the ~16-delta (≈1σ) put, buy a further-OTM put for
defined risk; 30-45 DTE; only when the regime gate is open, only on names with positive VRP,
ranked by VRP (richest first). Sizing caps each position's max loss at `risk_per_trade` of
budget. Pure/offline (no IB) so the dry-run book is fully inspectable before any live order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data, signal
from .greeks import put_delta

# risk_guard.py lives at the REPO ROOT, not in this package. The runner puts ROOT on sys.path.
try:
    from risk_guard import RiskLimits, chain_sane, data_fresh, stale_columns
except ImportError:  # guard unavailable -> keep trading, just unscreened
    RiskLimits = None
    chain_sane = None
    data_fresh = None
    stale_columns = None

# Liquid, well-optioned basket spread across sectors (diversified 2026-07-29 from all-mega-cap-
# tech, which meant a tech vol-spike hit every single name at once — and left the whole pool
# barren when tech vol was under-priced). Index anchors + 2 tech + health/energy/consumer/
# industrials/rates. The daily VRP filter picks which are genuinely rich each day; the basket's
# job is a diverse, liquid POOL where *something* is usually rich + uncorrelated. See
# scripts/screen_basket.py for the VRP-richness + correlation-to-tech screen behind these picks.
# ETFs and indices: no earnings, ever. Held explicitly so that a FAILED lookup (which also
# returns None) cannot be mistaken for "no earnings" on a single name, nor a provider outage
# wrongly halt the index sleeve. Any ticker NOT listed here is treated as an earnings name.
NO_EARNINGS_TICKERS = frozenset({
    "SPY", "QQQ", "IWM", "XLV", "XLE", "TLT", "GLD", "SLV", "HYG", "LQD", "EEM", "EFA",
    "XLF", "XLP", "XLY", "XLI", "XLK", "XLU", "XLB", "XLC", "XLRE", "DIA", "VTI", "IVV",
    "SPX", "XSP", "VIX",
})

# Factor group per name, for the selection-time concentration cap. The basket was CONSTRUCTED
# for sector diversity (2026-07-29, after an all-tech basket meant one vol spike hit every name
# at once) — but nothing enforced diversity in the SELECTION, and the VRP filter actively works
# against it: it ranks on IV-RV, and richness clusters BY SECTOR because sector vol is bid
# together. Measured 2026-08-10, the book held XOM and XLE simultaneously — and XLE HOLDS XOM,
# so that is one position taken twice. Same selection pathology as the filter steering into the
# widest spreads and the pre-earnings names.
SECTOR = {
    "SPY": "index", "QQQ": "index", "IWM": "index",
    "NVDA": "tech", "AAPL": "tech",
    "XLV": "health", "PFE": "health",
    "XOM": "energy", "XLE": "energy",
    "SBUX": "consumer", "MCD": "consumer",
    "DE": "industrial", "CAT": "industrial",
}

# ETF / constituent overlap. A sector CAP does not catch this: XOM and XLE are both "energy",
# so a cap of 2 admits both — yet XOM is ~22% of XLE, so that is ONE bet sized twice while
# counting as two diversified positions. Holding a sector ETF and its major constituent at the
# same time is a levered single-name bet wearing a diversification label.
# SPY is deliberately NOT listed: it holds everything, but each single name is only ~2-7% of it,
# so the overlap is immaterial and blocking it would cost the most liquid name in the basket.
# ⚠ STATIC — and measured 2026-08-14 to be BOTH incomplete and mis-targeted. It is retained
# only as a fallback when the price panel is unavailable; `correlated_pairs()` below computes the
# real thing each run. Two problems with the static approach:
#   * IT GOES STALE. Sector-ETF compositions drift, constituents are reclassified, and the basket
#     changes — a snapshot silently stops catching real overlaps and nothing complains.
#   * HOLDINGS OVERLAP IS THE WRONG MEASURE ANYWAY. What matters for risk is whether two
#     positions LOSE TOGETHER, and that is correlation, not weight. Measured over 2025-01..
#     2026-08: the pairs this map blocks run 0.56-0.90 (AAPL/QQQ is only 0.56 despite AAPL being
#     ~9% of QQQ), while the THREE HIGHEST pairs in the whole basket are index-vs-index and are
#     NOT in this map at all — QQQ/SPY 0.95, IWM/SPY 0.86, IWM/QQQ 0.80. QQQ+SPY is a more
#     duplicative pair than XOM+XLE, and the sector cap of 2 happily admits it.
OVERLAP = {
    "XLE": {"XOM"}, "XOM": {"XLE"},
    "XLV": {"PFE"}, "PFE": {"XLV"},
    "QQQ": {"AAPL", "NVDA"}, "AAPL": {"QQQ"}, "NVDA": {"QQQ"},
}

DEFAULT_BASKET = [
    "SPY", "QQQ", "IWM",          # index anchors (reliable, thin premium)
    "NVDA", "AAPL",               # tech (trimmed from 5 — rich only when tech vol is bid)
    "XLV", "PFE",                 # healthcare (XLV = health ETF, liquid + sizeable; LLY dropped —
                                  #   at ~$1200/share its spread is too wide to size on this book)
    "XOM", "XLE",                 # energy (rich VRP, negative corr to tech)
    "SBUX", "MCD",                # consumer (SBUX richest in screen; MCD defensive, neg corr)
    "DE", "CAT",                  # industrials
    # TLT REMOVED 2026-08-14 — structurally untradeable, not merely expensive. It trades ~$82 on
    # a $1 strike grid at ~10.5% IV (the lowest in the basket, being a bond ETF), so the 16d and
    # 10d legs land on ADJACENT strikes: a $1-wide spread yielding $10-13 of credit. The $2.60
    # round-trip commission is then 20-26% of that BEFORE any bid-ask, and even the tightest
    # possible penny-wide market adds another 8-10% -- over the 25% guard on every plausible
    # combination. Unlike CAT (which fails on SPREAD and clears on a fatter-premium day), this is
    # a FIXED cost that no execution improves; only TLT's own vol roughly doubling would rescue
    # it (~30% IV gives a 2-wide spread, $26 credit, 9.9%). Rates exposure is forfeited; nothing
    # else in the basket replaces that factor.
]


@dataclass
class OptionsConfig:
    basket: list[str] = field(default_factory=lambda: list(DEFAULT_BASKET))
    regime_thr: float = 1.00          # sell only when VIX/VIX3M < this (contango)
    vrp_min: float = 0.02             # require ATM IV − RV20 above this (vol points): skip thin premium
    short_delta: float = 0.16         # short put ≈ 1σ
    long_delta: float = 0.10          # long put (defined-risk wing); nearer = narrower spread
    dte_min: int = 30
    # 45 -> 50 on 2026-08-28. Single names and sector ETFs carry usable open interest only on
    # the MONTHLY (3rd-Friday) expiries; the weeklies between them are near-dead (XOM measured
    # that day: 92,654 contracts of put OI on the 21-DTE monthly vs 799 on the 35-DTE weekly
    # the 30-45 window forced it to select -- only 5 strikes cleared the OI floor, so
    # build_spread returned None and EVERY single name failed while SPY/QQQ/NVDA traded).
    # Monthlies are 28-35 days apart and the window is narrower than that gap, so it can never
    # hold two and there are structural blackouts. Over 2020-2026 business days a monthly sits
    # inside 30-45 on only 55% of days (dark runs mean 9.6d, max 13d); 30-50 raises that to 69%.
    # ⚠ NOT backtested. The OPRA walk-forward that validated 30-45 ran on SPX, which has deep
    # weeklies, so it is blind to this failure mode entirely -- it cannot justify 45 OR 50. 50
    # is still ordinary short-premium territory and the change is one character to revert.
    dte_max: int = 50
    rate: float = 0.04                # risk-free for delta calc
    budget: float = 75_000.0   # risk base, not cash; see BASE_BUDGET in run_options_paper
    risk_per_trade: float = 0.03      # max loss per spread position ≤ 3% of budget (fits high-IV names)
    max_positions: int = 6
    # Max simultaneous positions sharing a factor group (see SECTOR). Binds well before
    # max_positions does: the VRP filter typically yields only 3-5 candidates, so without this
    # a single rich sector can take most of the book. 2 leaves room for a genuine pair trade
    # while stopping XOM+XLE (where one ETF literally holds the other single name).
    max_per_sector: int = 2
    # Daily-return correlation above which two names are treated as ONE position. 0.80 blocks the
    # index anchors from pairing (SPY/QQQ 0.95, IWM/SPY 0.86, IWM/QQQ 0.80) as well as XLE/XOM
    # (0.90) — all of which are the same bet twice. Deliberately does NOT block SBUX/MCD or
    # DE/CAT, which are genuinely distinct names that happen to share a sector.
    corr_overlap_thr: float = 0.80
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
    # EARNINGS FILTER. A 30-45d hold on a single name straddles an earnings print roughly half the
    # time, and the VRP filter actively STEERS INTO it: VRP = ATM_IV - RV20, and ahead of earnings
    # IV rises for the event while RV20 (trailing) does not, so the score is mechanically inflated
    # exactly where the binary risk sits. Measured on 8,600 simulated 39-day holds over the 8
    # single names 2005-2026: earnings-spanning holds carry E[loss] 0.091 of width vs 0.050, and
    # P(max loss) 7.3% vs 3.8% -- about DOUBLE both, with 2/3 of breaches going straight to max
    # loss (a gap signature, not drift). The market pays only ~+14% more credit for it (NVDA
    # ladder), against ~+52% needed to break even on the best available estimate.
    # NOT SETTLED -- the verdict flips if the market widens strikes 25-30% for the event and the
    # measurement is one name. Default ON regardless, because a skipped trade costs nothing and,
    # decisively, the walk-forward that validated 16d/10d + VRP>2 ran on SPX -- AN INDEX WITH NO
    # EARNINGS -- so event-spanning single-name trades are untested by anything we have.
    earnings_filter: bool = True
    earnings_buffer_days: int = 2      # companies confirm late and move dates; clear the event early
    # `next_earnings` returns None both for a genuine ETF and for a failed lookup. True = treat
    # unknown as PENDING (skip), matching skip_if_no_quote. Known ETFs are exempted by
    # NO_EARNINGS_TICKERS so a provider outage cannot silently halt the index sleeve.
    skip_if_earnings_unknown: bool = True
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
    quote_width: float = 0.0   # combo bid/ask width per share (short leg + long leg) = cost to cross both legs


def pick_expiry(expiries: tuple[str, ...], today: pd.Timestamp,
                dte_min: int, dte_max: int,
                before: pd.Timestamp | None = None) -> tuple[str, int] | None:
    """Choose the expiry whose DTE lands in [min,max], nearest the window midpoint.

    `before` (an earnings date, already buffered) restricts candidates to expiries that settle
    BEFORE it. Selecting the expiry is strictly better than opening a spread and closing it the
    day before the event: the event premium does NOT decay ahead of the event, so an early close
    sells inflated vol and buys it back still inflated, capturing only ordinary theta while paying
    a full round trip. Measured on the real 2026-08-10 combos, closing early nets NEGATIVE for any
    event inside ~day 20 (SBUX 20.0% cost) or ~day 26 (CAT 27.7%) of a 39-day hold — and beyond
    that the 50% profit target would have closed the position anyway. Choosing the expiry instead
    keeps the full premium, the profit target, and zero event exposure.

    Returns None when no expiry both fits the window and clears the event, which is the correct
    outcome: the name is simply not tradeable this cycle.
    """
    mid = (dte_min + dte_max) / 2
    best = None
    for e in expiries:
        if before is not None and pd.Timestamp(e) >= before:
            continue
        dte = (pd.Timestamp(e) - today).days
        if dte_min <= dte <= dte_max:
            if best is None or abs(dte - mid) < abs(best[1] - mid):
                best = (e, dte)
    return best


def correlated_pairs(prices: pd.DataFrame, threshold: float = 0.80,
                     window: int = 252) -> dict[str, set[str]]:
    """{ticker -> set of tickers too correlated with it to hold simultaneously}.

    Computed FRESH each run from the price panel the strategy already loads, so it cannot go
    stale the way a hard-coded holdings map does — ETF compositions drift, names get
    reclassified, and the basket itself changes.

    Correlation rather than holdings weight, because the question is whether two positions LOSE
    TOGETHER, which is what concentration costs you. Measured 2026-08-14 the two disagree
    sharply: AAPL is ~9% of QQQ yet correlates only 0.56, while SPY/QQQ correlate 0.95 with
    neither holding the other in any meaningful sense — they are simply the same bet.

    Returns an empty map when there is too little history, so the caller falls back to OVERLAP
    rather than silently dropping the check.
    """
    if prices is None or len(prices) < max(window // 4, 40):
        return {}
    r = prices.tail(window).pct_change(fill_method=None).dropna(how="all")
    if len(r) < 30:
        return {}
    c = r.corr()
    out: dict[str, set[str]] = {}
    for a in c.columns:
        peers = {b for b in c.columns
                 if b != a and pd.notna(c.loc[a, b]) and c.loc[a, b] >= threshold}
        if peers:
            out[a] = peers
    return out


def earnings_cutoff(ticker: str, cfg: OptionsConfig,
                    lookup=None) -> tuple[pd.Timestamp | None, str]:
    """(cutoff, note): expiries must settle strictly BEFORE `cutoff`. None = unrestricted.

    Three branches, and the frequencies matter for how restrictive this is. A name reports about
    every 91 days, so over a 30-45 DTE window: the event is beyond the window ~51% of the time
    (no action), inside the window ~16% (shift the expiry), and nearer than dte_min ~33% (skip).
    That is far milder than a blanket "skip if earnings within 45 days", which would sideline the
    name ~49% of the time.
    """
    if not cfg.earnings_filter or ticker in NO_EARNINGS_TICKERS:
        return None, ""
    fn = lookup or data.next_earnings
    d = fn(ticker)
    if d is None:
        # Unknown, not absent -- the ticker is not a known ETF, so the lookup failed or the
        # company has not confirmed. Fail safe.
        # Timestamp.MIN, not MAX. The cutoff means "expiries must settle BEFORE this", so MAX
        # would let EVERY expiry through -- unguarded, the exact opposite of failing safe.
        # MIN excludes them all, so pick_expiry returns None and the name is skipped.
        return (pd.Timestamp.min, "earnings date unknown - skipped") if cfg.skip_if_earnings_unknown \
            else (None, "earnings date unknown - traded anyway")
    return d - pd.Timedelta(days=cfg.earnings_buffer_days), f"earnings {d.date()}"


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
                          min_oi: int = 0, oi_pctile: float = 0.0,
                          below: float | None = None) -> tuple[float, float, float, float] | None:
    """(strike, delta, mid_price, bid_ask_width) of the OTM put whose |delta| is closest to `target`.

    `below` restricts candidates to strikes strictly under it — used to force the long leg at
    least one increment beneath the short leg (see `build_spread`).

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
        if below is not None and K >= below:
            continue
        if floor > 0:
            oi = pd.to_numeric(row.get("openInterest"), errors="coerce")
            if oi != oi or oi < floor:               # NaN or below the floor -> not a real line
                continue
        d = put_delta(spot, K, T, r, iv)
        rows.append((K, d, (bid + ask) / 2, float(ask - bid)))    # carry the leg's bid/ask width
    if not rows:
        return None
    return min(rows, key=lambda x: abs(abs(x[1]) - target))


def build_spread(ticker: str, puts: pd.DataFrame, spot: float, expiry: str, dte: int,
                 iv: float, rv: float, cfg: OptionsConfig) -> SpreadTarget | None:
    """Build the 16d/10d put credit spread, or None if this chain cannot support one.

    The legs are picked SEQUENTIALLY, not independently: the long leg is chosen only from strikes
    strictly BELOW the short. Choosing both by nearest-delta and rejecting the collision afterwards
    silently dropped names whose strike grid is coarse relative to their vol — SBUX 2026-08-10 put
    both the 16d and 10d legs on strike 95, so a genuinely rich-VRP name (+6.9%) never traded at
    all. Stepping down one increment always yields a real spread where one exists.

    Consequence to be aware of: when the grid is that coarse the long leg lands further OTM than
    the 10d target, so the spread is WIDER than intended, max loss per contract is larger and the
    position sizes to fewer contracts. That is the correct trade-off — a slightly wider spread is
    a real position, a collapsed one is nothing — but it is why width is not a constant here.
    """
    T = dte / 365.0
    short = _nearest_delta_strike(puts, spot, T, cfg.rate, cfg.short_delta,
                                  cfg.min_open_interest, cfg.oi_pctile)
    if short is None:
        return None
    long_ = _nearest_delta_strike(puts, spot, T, cfg.rate, cfg.long_delta,
                                  cfg.min_open_interest, cfg.oi_pctile, below=short[0])
    if long_ is None:
        return None
    credit = short[2] - long_[2]
    width = short[0] - long_[0]
    max_loss = width - credit
    if credit <= 0 or max_loss <= 0:
        return None
    quote_width = float(short[3] + long_[3])   # combo bid/ask width per share = cost to cross both legs
    # max(..., 0): floor division on a negative budget returns a NEGATIVE contract count
    # (-30 // 1100 == -1), i.e. an order to SELL -1 contracts. `allocated_budget` cannot currently
    # go negative, but the sizer must not depend on that -- same defect class as the magic-formula
    # negative-share bug found 2026-08-14.
    contracts = max(int((cfg.risk_per_trade * cfg.budget) // (max_loss * 100)), 0)
    return SpreadTarget(ticker, expiry, dte, spot, iv, rv, signal.vrp(iv, rv),
                        short[0], long_[0], short[1], long_[1], credit, width, max_loss, contracts,
                        quote_width)


@dataclass
class BookResult:
    regime_ratio: float
    vix: float
    regime_open: bool
    diagnostics: list[dict]        # per-name signal snapshot (for the dry-run table + IV recorder)
    targets: list[SpreadTarget]    # selected spreads (empty if gate shut)
    # {ticker -> peers too correlated to hold alongside}, computed FRESH from this run's price
    # panel. Empty when there is too little history, in which case the caller falls back to the
    # static OVERLAP map rather than dropping the check.
    corr_overlap: dict = field(default_factory=dict)


def target_book(cfg: OptionsConfig, today: pd.Timestamp | None = None) -> BookResult:
    today = today or pd.Timestamp.today().normalize()
    ratio, vix, _ = data.regime()
    open_ = signal.regime_open(ratio, cfg.regime_thr)

    prices = data.price_history(cfg.basket)
    # STALENESS on the price panel. RV20 comes from these prices, so a frozen feed yields a
    # stale realised vol and therefore a wrong VRP for every name — plausible numbers, wrong
    # decisions. Distinct from chain_sane, which screens the OPTION side.
    if data_fresh is not None and RiskLimits is not None:
        _f = data_fresh(prices.index, today, RiskLimits(budget=cfg.budget))
        if not _f:
            logging.error("data staleness: %s — no book built today", _f.reason)
            return BookResult(ratio, vix, False, [{"ticker": "-", "spot": float("nan"),
                                                   "rv": float("nan"), "iv": float("nan"),
                                                   "vrp": float("nan"), "expiry": None,
                                                   "dte": None, "note": _f.reason}], [])
    # PER-NAME STALENESS. `data_fresh` above inspects only the panel INDEX, so one basket
    # member whose feed dies or freezes leaves the panel current while that name's RV20
    # collapses toward zero — which INFLATES its VRP (VRP = IV - RV) and makes it look like the
    # richest name in the book. The cost guard cannot save us: it screens the quote, not the
    # signal, so a bad VRP produces a well-executed trade on a false premise. Skipped, not
    # halted: one dead ticker should not stop the other twelve.
    stale_px: dict[str, int] = {}
    if stale_columns is not None and RiskLimits is not None:
        stale_px, _sc = stale_columns(prices[[c for c in prices.columns if c in cfg.basket]],
                                      today, RiskLimits(budget=cfg.budget))
        if stale_px:
            logging.error("per-name staleness: %s — those names are SKIPPED today", _sc.reason)

    diags: list[dict] = []
    candidates: list[SpreadTarget] = []
    bad_chains: list[str] = []
    checked_chains = 0
    for tk_name in cfg.basket:
        if tk_name not in prices.columns:
            continue
        if tk_name in stale_px:
            diags.append({"ticker": tk_name, "spot": float("nan"), "rv": float("nan"),
                          "iv": float("nan"), "vrp": float("nan"), "expiry": None, "dte": None,
                          "note": f"stale price ({stale_px[tk_name]}d since it last moved)"})
            continue
        spot = float(prices[tk_name].iloc[-1])
        rv = signal.realized_vol(prices[tk_name])
        rec = {"ticker": tk_name, "spot": spot, "rv": rv, "iv": float("nan"),
               "vrp": float("nan"), "expiry": None, "dte": None, "note": ""}
        try:
            tk, expiries = data.option_expiries(tk_name)
            cutoff, enote = earnings_cutoff(tk_name, cfg)
            pick = pick_expiry(expiries, today, cfg.dte_min, cfg.dte_max, before=cutoff)
            if pick is None:
                rec["note"] = (f"no expiry clears {enote}" if cutoff is not None
                               else "no expiry in DTE window")
                diags.append(rec); continue
            if enote:
                rec["note"] = enote        # kept when a trade IS made, so the log shows the date
            expiry, dte = pick
            checked_chains += 1
            pdf = data.puts(tk, expiry)
            iv = signal.atm_iv(pdf, spot)
            rec.update(iv=iv, vrp=signal.vrp(iv, rv), expiry=expiry, dte=dte)
            # CHAIN SANITY. Outside US hours the provider returns bid=0/ask=0/OI=0 and a CONSTANT
            # junk IV (SPY 38d read 1.56% on 2026-08-11 against 12.2% in hours). VRP = IV - RV is
            # then hugely negative for EVERY name, so the book reports "gate open but nothing
            # passed the VRP filters" -- indistinguishable from a genuinely quiet day. Computing a
            # VRP from that number is the failure; refuse to, and say so.
            if chain_sane is not None:
                cs = chain_sane(tk_name, iv, pdf.get("bid"), RiskLimits(budget=cfg.budget))
                if not cs:
                    rec["note"] = cs.reason
                    bad_chains.append(tk_name)
                    diags.append(rec)
                    continue
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

    # SYSTEMIC vs per-name. One junk chain is a bad ticker; EVERY chain junk is a broken feed or
    # an off-hours run, and that distinction is the whole point -- otherwise "no name passed the
    # filters" reads as a quiet market when the data is simply unusable.
    if checked_chains and len(bad_chains) == checked_chains:
        logging.error("ALL %d option chains failed sanity (%s) — data source unusable, NOT a "
                      "quiet market. Check the run time is inside US market hours.",
                      checked_chains, ", ".join(bad_chains[:6]))
    elif bad_chains:
        logging.warning("chain sanity rejected %d of %d: %s",
                        len(bad_chains), checked_chains, ", ".join(bad_chains))

    candidates.sort(key=lambda s: s.vrp, reverse=True)
    return BookResult(ratio, vix, open_, diags, candidates[: cfg.max_positions],
                      correlated_pairs(prices, cfg.corr_overlap_thr))


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
    # NaN/inf must return ratio None, not a NaN ratio. Every NaN comparison below is False, so a
    # NaN quote slipped past `mid <= 0` and `hi <= lo` and produced ratio=NaN. `ratio <= max_frac`
    # is then False so it did skip -- but the CALLER branches on `ratio is None` to distinguish
    # "no quote" from "quoted and too expensive", so a NaN was reported as `cost nan%` and did not
    # honour skip_if_no_quote. Unusable input must look unusable.
    if not (np.isfinite(bid) and np.isfinite(ask)):
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
