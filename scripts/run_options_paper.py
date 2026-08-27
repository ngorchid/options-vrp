"""Options VRP paper runner.

  python scripts/run_options_paper.py             # offline dry-run: regime + VRP + target spreads
  python scripts/run_options_paper.py --selftest   # offline: exercise the management rules
  python scripts/run_options_paper.py --live        # connect IB (clientId 7): manage + open + email

Daily weekday design: (1) MANAGE open spreads — close on 50% profit / 2× stop / 21-DTE time-stop;
(2) if the regime gate is OPEN, open new put credit spreads on the richest-VRP names up to the
position cap; (3) mark the book, persist state, email. yfinance for signal/chains, IB for fills.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from options_vrp import OptionsConfig, target_book  # noqa: E402
from options_vrp.strategy import OVERLAP, SECTOR  # noqa: E402
from options_vrp.strategy import (  # noqa: E402
    _nearest_delta_strike, cost_ok, earnings_cutoff, manage_action, oi_threshold,
    pick_expiry)
from options_vrp.state import OpenSpread, OptionsState  # noqa: E402
from risk_guard import (NOMINAL_NAV, RiskLimits, allocated_budget,  # noqa: E402
                        code_version,
                        check_allocations, check_order,
                        install_alert_collector, missed_runs, push_if_alerts,
                        reconcile, halt_state, HALT_ALL, HALT_NEW,
                        circuit_breaker, peak_equity, liquidity_check, MarginLimits,
                        write_equity, book_drawdown, book_vol,
                        BreakerLevels, blended_vol)

load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(message)s")
# WARNING+ collected for the daily email. Matters most HERE: SKIP_IF_NO_QUOTE=1 can silently halt
# ALL trading, and an off-hours chain makes VRP negative for every name — both look like a normal
# quiet day in the report unless the warning is delivered.
ALERTS = install_alert_collector()
STATE_FILE = ROOT / "results" / "paper" / "state.json"

# Annualised vol prior for the circuit-breaker levels, from the SPX VRP backtest marked daily
# (algo_trading/scripts/breaker_calibration_lab.py, live spec): 6.3%.
#
# ⚠ CORRECTED 2026-08-13 from 12.5%, which was WRONG BY 2x. The backtest series has only ~64
# observations per CALENDAR year — the sleeve holds a position roughly a quarter of the time —
# and it was annualised with sqrt(252) as if it were daily. Correct factor is sqrt(64), so the
# vol was overstated by sqrt(252/64) ~ 2.0. The tell was that its worst drawdown came out at
# 0.91 sigma against 1.90 for trend and 1.19 for magic formula; at the corrected vol it is 1.80
# sigma, in line with the others.
#
# This affects the PRIOR only. The live nav_history records a snapshot every run, so it IS daily
# and `realised_vol`'s sqrt(252) is right for it — the estimate converges to the truth as history
# accumulates; only the cold-start number was wrong.
#
# ⚠ Measured on the SPX sleeve only; the live 14-name basket carries idiosyncratic single-name
# risk SPX does not, so its true vol is probably higher. Update if the basket, risk_per_trade or
# max_positions changes materially.
VOL_PRIOR = 0.063
# Breaker sigmas for THIS sleeve, deliberately wider than the 1.2/2.0/2.8 default (decision
# 2026-08-15, measured on the 13.2yr OPRA series: vol 6.26%, maxDD -11.27% = 1.80 sigma).
#
# THE MEASUREMENT: the breaker never improves this sleeve's drawdown AT ANY LEVEL -- maxDD stays
# -11.27% under every configuration tested -- because `derisk` only halves exposure, and halving
# once a drawdown is already 10% deep does not stop the remaining path to 11.27%. So every level
# that fires is pure cost: the default sigmas cost -0.06 Sharpe, and REMOVING the vol floor (the
# obvious "fix" for the floor overriding a legitimate 6.3% reading) makes it worse at -0.16,
# because it lowers derisk to 7.5% and fires more often. Raising the sigmas costs 0.00.
#
# WHY NOT DROP lo=0.10 INSTEAD: the floor exists to stop a degenerate vol ESTIMATE turning the
# breaker into a hair trigger, which is a real hazard for every sleeve. The problem here was
# never the floor itself, it was that 1.2 x 6.26% = 7.5% sits below it, so the level was being
# set by the floor rather than by this book's own risk. Setting sigmas that clear 1.80 sigma
# fixes the cause: derisk lands at 12.5%, above the floor, so the floor no longer binds and the
# level is a genuine multiple of measured vol again.
#
# A circuit breaker is an OPERATIONAL failsafe for "the model is broken", not a risk tool for
# normal losses -- normal losses are handled by SIZING. For a book whose worst 13-year drawdown
# is 1.80 sigma and slow, there is nothing for a drawdown trigger to catch, and it should sit
# beyond the historical range so it only speaks when something is genuinely wrong.
BREAKER_SIGMAS = (2.0, 2.6, 3.2)      # -> 12.5% / 16.3% / 20.0% at VOL_PRIOR
# BASE CAPITAL is no longer set here. It is this sleeve's share of live account NetLiquidation,
# from the validated table in risk_guard.ALLOCATIONS — see `allocated_budget`. The old hard-coded
# $75,000 was chosen from the capital sweep against a $50k account, which meant three sleeves
# holding $50k + $75k + $100k of independently-set budget on one $50k account: 74% of NAV in
# maintenance, tripping `no_new_risk` on an ordinary day with nothing able to see it.
#
# The sweep's finding still holds and is now reached automatically: peak margin is structurally
# max_positions x risk_per_trade = 6 x 3% = 18% of budget, and because the budget tracks NetLiq,
# this sleeve arrives at its measured $75-100k plateau as the account grows, with no edit.
# `BUDGET` in .env still overrides for a deliberate one-off, and says so in the log.


def _cfg(state: OptionsState | None = None) -> OptionsConfig:
    """BUDGET is the BASE capital; sizing compounds off this strategy's OWN realised P&L.

    So names come online by themselves as the account grows (a name is tradeable only when its
    max loss per contract fits risk_per_trade x budget) and position size shrinks after losses,
    with no manual edit and no restart.

    Deliberately NOT IB NetLiquidation: three strategies share one account, so NetLiq would have
    each of them sizing as though it owned the whole thing. Realised-only for now, which lags
    gains and therefore UNDER-sizes after a good run -- the right way to be wrong.
    """
    # Budget is this sleeve's SHARE of the live account, not a hand-set number. The fractions
    # live in one validated table (risk_guard.ALLOCATIONS) whose summed peak margin must leave
    # >= 40% cushion, so the three sleeves can no longer be resized independently into a
    # combination that does not fit -- which is what $50k + $75k + $100k of budget on a $50k
    # account was. BUDGET in .env still overrides, for a deliberate one-off.
    _alloc_ok = check_allocations()
    if not _alloc_ok:
        logging.error("ALLOCATION: %s", _alloc_ok.reason)
    _override = os.getenv("BUDGET")
    if _override:
        budget = float(_override)
        logging.info("budget: $%s from BUDGET override — allocation table BYPASSED",
                     f"{budget:,.0f}")
    else:
        budget, note = allocated_budget("options-vrp",
                                        getattr(state, "last_net_liq", 0.0) or None,
                                        float(os.getenv("NOMINAL_NAV", str(NOMINAL_NAV))),
                                        step=float(os.getenv("BUDGET_STEP", "0.10")))
        logging.info("budget: %s", note)
    return OptionsConfig(
        budget=budget,
        regime_thr=float(os.getenv("REGIME_THR", "99")),   # 99 = gate off; see .env.example
        vrp_min=float(os.getenv("VRP_MIN", "0.02")),
        stop_mult=float(os.getenv("STOP_MULT", "0")),
        max_cost_frac=float(os.getenv("MAX_COST_FRAC", "0.25")),
        commission_per_contract=float(os.getenv("COMMISSION_PER_CONTRACT", "0.65")),
        min_open_interest=int(os.getenv("MIN_OPEN_INTEREST", "50")),
        earnings_filter=os.getenv("EARNINGS_FILTER", "1") not in ("0", "false", "False"),
        earnings_buffer_days=int(os.getenv("EARNINGS_BUFFER_DAYS", "2")),
        oi_pctile=float(os.getenv("OI_PCTILE", "0.25")),
        skip_if_no_quote=os.getenv("SKIP_IF_NO_QUOTE", "1") not in ("0", "false", "False"))


# ---------- offline modes ----------
def dry_run(cfg: OptionsConfig) -> None:
    res = target_book(cfg)
    gate = "OPEN (contango)" if res.regime_open else "SHUT (backwardation)"
    print(f"\nREGIME  VIX/VIX3M = {res.regime_ratio:.3f}  (thr {cfg.regime_thr:.2f})  VIX {res.vix:.1f}  ->  gate {gate}")
    print(f"\n{'ticker':7s} {'spot':>9s} {'RV':>7s} {'IV':>7s} {'VRP':>7s} {'expiry':>11s} {'dte':>4s}  note")
    print("-" * 70)
    for d in res.diagnostics:
        f = lambda x: f"{x:.1%}" if x == x else "  —"  # noqa: E731
        v = f"{d['vrp']:+.1%}" if d['vrp'] == d['vrp'] else "  —"
        print(f"{d['ticker']:7s} {d['spot']:>9.2f} {f(d['rv']):>7s} {f(d['iv']):>7s} {v:>7s} "
              f"{(d['expiry'] or '—'):>11s} {str(d['dte'] or '—'):>4s}  {d['note']}")
    print(f"\nTARGET BOOK  budget ${cfg.budget:,.0f}  risk/trade {cfg.risk_per_trade:.0%}  max {cfg.max_positions}")
    if not res.regime_open:
        print("  (regime gate SHUT — no new premium sold)"); return
    if not res.targets:
        print("  (gate open but no name passed the VRP/structure filters today)"); return
    for s in res.targets:
        print(f"  {s.ticker:6s} {s.expiry} ({s.dte}d)  SELL {s.contracts}x {s.short_strike:g}/{s.long_strike:g}p  "
              f"Δ{abs(s.short_delta):.2f}/{abs(s.long_delta):.2f}  credit ${s.credit*100:,.0f}  "
              f"maxloss ${s.max_loss*100:,.0f}  VRP {s.vrp:+.1%}  risk ${s.max_loss*100*s.contracts:,.0f}")


def selftest(cfg: OptionsConfig) -> None:
    """Exercise the management rules on synthetic marks (no IB, no yfinance)."""
    sp = OpenSpread("IWM", "2026-08-21", 275, 268, 4, entry_credit=0.75, max_loss=6.25,
                    entry_date="2026-07-18", entry_spot=293.7)
    today = pd.Timestamp("2026-08-01")
    cases = [("50% profit  (buy back @ 0.37)", 0.37, 30),
             ("2x stop     (spread @ 1.55)", 1.55, 30),
             ("21-DTE time (@ 0.60, 20 dte)", 0.60, 20),
             ("hold        (@ 0.60, 30 dte)", 0.60, 30)]
    print("SELF-TEST — management decisions (entry credit 0.75):")
    for label, cur, dte in cases:
        print(f"  {label:34s} -> {manage_action(sp.entry_credit, cur, dte, cfg) or 'hold'}")

    # Execution cost guard. Break-even is ~31% of credit (SPX backtest); single-name spreads
    # measured at 59-65% on 2026-08-08, so this is the difference between trading and not.
    print(f"\nSELF-TEST — cost guard (threshold {cfg.max_cost_frac:.0%} of credit, "
          f"skip_if_no_quote={cfg.skip_if_no_quote}):")
    print(f"  (commission ${cfg.commission_per_contract:.2f}/contract x 4 sides = "
          f"${4*cfg.commission_per_contract:.2f} per round trip)")
    print(f"  {'combo quote':22s} {'credit$/ct':>11s} {'spread':>8s} {'comm':>7s} "
          f"{'TOTAL':>7s}  verdict")
    for label, bid, ask in (("SPX-like  16.20/17.10", 16.20, 17.10),
                            ("SPY-like   1.72/ 1.86", 1.72, 1.86),
                            ("CAT-like   4.40/ 5.90", 4.40, 5.90),
                            ("PFE-like   0.02/ 0.04", 0.023, 0.043),
                            ("no quote available   ", None, None)):
        ok, ratio, br = cost_ok(bid, ask, cfg.max_cost_frac,
                                cfg.commission_per_contract, cfg.option_multiplier)
        if ratio is None and not cfg.skip_if_no_quote:
            ok = True
        if ratio is None:
            print(f"  {label:22s} {'n/a':>11s} {'n/a':>8s} {'n/a':>7s} {'n/a':>7s}  "
                  f"{'TRADE' if ok else 'SKIP'}")
        else:
            print(f"  {label:22s} {br['credit']:>11,.0f} {br['spread']:>8.0%} "
                  f"{br['commission']:>7.0%} {ratio:>7.0%}  {'TRADE' if ok else 'SKIP'}")

    # Liquidity screen. REGRESSION CASE = the real EEM chain quoted in TWS on 2026-08-10: the
    # 1-sigma strike had NO open interest and quoted 0.30/0.90 (104% of credit -> rejected) while
    # BOTH neighbours quoted ~0.10 wide on essentially the same mid. Selecting on delta alone
    # picks the dead strike and loses the trade; the screen must pick a neighbour instead.
    print(f"\nSELF-TEST — liquidity screen (min OI {cfg.min_open_interest}, "
          f"pctile {cfg.oi_pctile:.0%} of the chain's own OI):")
    # IVs solved so strike 44 sits EXACTLY on the 16-delta target -- i.e. the dead strike is
    # precisely what delta-only selection would choose. Quotes are EEM's real ones. Note 44's
    # IV (0.224) is itself contaminated: it is inverted from the garbage 0.60 mid, which is the
    # compounding failure -- a bad quote yields a bad IV yields a bad delta.
    spot, T = 47.0, 39 / 365.0
    chain = pd.DataFrame({
        "strike":           [46.0,   45.0,   44.0,   43.0,   42.0],
        "bid":              [1.15,   0.55,   0.30,   0.43,   0.18],
        "ask":              [1.25,   0.65,   0.90,   0.54,   0.23],
        "lastPrice":        [1.20,   0.60,   0.60,   0.48,   0.20],
        "impliedVolatility": [0.1582, 0.1609, 0.2243, 0.2515, 0.2689],
        "openInterest":     [1400,   1100,      0,    900,    480],   # 44.0 is the dead strike
        "volume":           [  31,     12,      0,      8,      3]})
    floor = oi_threshold(chain, cfg.min_open_interest, cfg.oi_pctile)
    print(f"  OI floor for this chain = {floor:,.0f} contracts")
    for label, mn, pc in (("screen OFF (old behaviour)", 0, 0.0),
                          ("screen ON", cfg.min_open_interest, cfg.oi_pctile)):
        pick = _nearest_delta_strike(chain, spot, T, cfg.rate, 0.16, mn, pc)
        if pick is None:
            print(f"  {label:26s} -> no eligible strike (no trade)")
            continue
        K, d, mid = pick
        oi = int(chain.loc[chain["strike"] == K, "openInterest"].iloc[0])
        row = chain.loc[chain["strike"] == K].iloc[0]
        ok, ratio, _ = cost_ok(row["bid"], row["ask"], cfg.max_cost_frac,
                               cfg.commission_per_contract, cfg.option_multiplier)
        print(f"  {label:26s} -> strike {K:g}  delta {d:+.3f}  OI {oi:>5,}  "
              f"quote {row['bid']:.2f}/{row['ask']:.2f}  cost {ratio:>6.0%}  "
              f"{'TRADE' if ok else 'SKIP'}")

    # Leg collision. REGRESSION CASE = SBUX on 2026-08-10: a coarse strike grid put the 16d AND
    # 10d legs on the SAME strike (95), so picking both by nearest-delta independently and then
    # rejecting `long >= short` silently killed a genuinely rich-VRP name (+6.9%). The long leg
    # must be chosen from strikes strictly BELOW the short.
    print("\nSELF-TEST — leg collision (coarse strike grid, 16d and 10d both nearest 95):")
    coarse = pd.DataFrame({
        "strike":           [100.0,  95.0,  90.0,  85.0],
        "bid":              [ 1.90,  0.70,  0.28,  0.10],
        "ask":              [ 2.10,  0.82,  0.36,  0.16],
        "lastPrice":        [ 2.00,  0.76,  0.32,  0.13],
        # IVs solved so strike 95 is nearest to BOTH the 16d and the 10d target (delta -0.130,
        # equidistant at 0.030) -- which is what makes the two legs collide.
        "impliedVolatility": [0.2710, 0.2962, 0.2844, 0.2987],
        "openInterest":     [ 2200,  1800,  1500,  1100],
        "volume":           [   40,    25,    18,    11]})
    cspot, cT = 105.0, 39 / 365.0
    s = _nearest_delta_strike(coarse, cspot, cT, cfg.rate, cfg.short_delta,
                              cfg.min_open_interest, cfg.oi_pctile)
    li = _nearest_delta_strike(coarse, cspot, cT, cfg.rate, cfg.long_delta,
                               cfg.min_open_interest, cfg.oi_pctile)
    lb = _nearest_delta_strike(coarse, cspot, cT, cfg.rate, cfg.long_delta,
                               cfg.min_open_interest, cfg.oi_pctile, below=s[0])
    print(f"  short leg (16d)                     -> strike {s[0]:g}  delta {s[1]:+.3f}")
    print(f"  long leg, picked INDEPENDENTLY      -> strike {li[0]:g}  delta {li[1]:+.3f}"
          f"{'   COLLISION -> old code returned None (no trade)' if li[0] >= s[0] else ''}")
    print(f"  long leg, forced BELOW the short    -> strike {lb[0]:g}  delta {lb[1]:+.3f}"
          f"   width {s[0]-lb[0]:g}, credit ${(s[2]-lb[2])*100:,.0f}")

    # Earnings filter. Three branches, plus the fail-safe. The MIN/MAX sentinel is the subtle
    # part: the cutoff means "expiries must settle BEFORE this", so an unknown date must map to
    # Timestamp.MIN (excludes everything -> skip). Timestamp.MAX would admit every expiry, i.e.
    # trade UNGUARDED -- the opposite of failing safe.
    print(f"\nSELF-TEST — earnings filter (buffer {cfg.earnings_buffer_days}d, "
          f"dte {cfg.dte_min}-{cfg.dte_max}, skip_if_unknown={cfg.skip_if_earnings_unknown}):")
    exps = ("2026-08-14", "2026-08-21", "2026-08-28", "2026-09-04", "2026-09-11",
            "2026-09-18", "2026-09-25", "2026-10-16")
    tday = pd.Timestamp("2026-08-11")
    cases = [("beyond the window", pd.Timestamp("2026-10-29")),
             ("inside the window", pd.Timestamp("2026-09-20")),
             ("nearer than dte_min", pd.Timestamp("2026-08-20"))]
    base = pick_expiry(exps, tday, cfg.dte_min, cfg.dte_max)
    print(f"  {'(no filter)':24s} -> {base[0]} ({base[1]}d)")
    for label, d in cases:
        cut, _ = earnings_cutoff("XX", cfg, lookup=lambda _t, _d=d: _d)
        got = pick_expiry(exps, tday, cfg.dte_min, cfg.dte_max, before=cut)
        print(f"  earnings {str(d.date())} {label:22s} -> "
              f"{f'{got[0]} ({got[1]}d)' if got else 'SKIP — no expiry clears it'}")
    for tk, fn, lbl in [("SPY", lambda _t: None, "known ETF, exempt"),
                        ("MYSTERY", lambda _t: None, "unknown -> must SKIP")]:
        cut, note = earnings_cutoff(tk, cfg, lookup=fn)
        got = pick_expiry(exps, tday, cfg.dte_min, cfg.dte_max, before=cut)
        print(f"  {tk:8s} {lbl:29s} -> "
              f"{f'{got[0]} ({got[1]}d)' if got else 'SKIP'}   {note}")


def _resolve_pending(broker, state, today: str) -> None:
    """Book / reverse orders that hadn't reached a terminal state when a prior run's poll gave up.

    IB caps combo MARKET orders, so they can fill up to ~40s after placement -- sometimes after the
    run ended -- and the poll then logged "not filled" without booking. Here, at the START of the
    next run, each pending order is checked against the REAL IB position (source of truth) plus the
    order's own fill: a late CLOSE that IB now shows flat is booked; a late OPEN that IB shows held
    is booked; an OPEN that never filled is reversed. Runs BEFORE management, so a spread already
    closed at IB is removed before the manage loop could place a close on a position that is not
    there (which would OPEN a short spread). Only touches THIS strategy's own orders (matched by
    permId / its own tracked spread), so it cannot adopt or clobber another strategy's position on
    the shared account -- the reason the general reconcile stays report-only."""
    pend = getattr(state, "pending_orders", None)
    if not pend:
        return
    actual = broker.put_positions()
    if actual is None:
        logging.warning("pending: IB positions unavailable — %d order(s) left for next run", len(pend))
        return
    from options_vrp.broker import _ib_expiry
    from options_vrp.state import OpenSpread
    still: list = []
    for p in pend:
        sp = OpenSpread(**p["spread"])
        e = _ib_expiry(sp.expiry)
        held = (abs(actual.get((sp.ticker, e, float(sp.short_strike)), 0.0)) >= 1e-9 or
                abs(actual.get((sp.ticker, e, float(sp.long_strike)), 0.0)) >= 1e-9)
        info = broker.order_fill(p.get("permId"))                 # (status, avg_fill) or None
        age = (pd.Timestamp(today) - pd.Timestamp(p.get("placed_date", today))).days
        if p["action"] == "close":
            if held:
                logging.info("pending CLOSE %s still open at IB — resuming normal management", sp.key)
            elif state.has(sp.key):                               # legs flat at IB -> the close filled
                price = info[1] if (info and info[1]) else broker.spread_values([sp]).get(sp.key)
                if price is None:
                    price = sp.entry_credit                       # last resort: breakeven, flagged
                pnl = state.record_close(sp, float(price), today,
                    f"self-heal: CLOSE filled at IB after the poll returned; booked at "
                    f"{'fill' if (info and info[1]) else 'mark/breakeven'} {float(price):.4f}")
                logging.warning("self-heal: booked late CLOSE %s @ %.4f (pnl %.2f)",
                                sp.key, float(price), pnl)
        else:  # open
            if held:                                              # open filled
                if not state.has(sp.key):
                    credit = info[1] if (info and info[1]) else sp.entry_credit
                    state.record_open(sp, float(credit), today)
                    logging.warning("self-heal: booked late OPEN %s @ %.4f", sp.key, float(credit))
            elif age >= 1:                                        # never filled -> reverse optimistic booking
                if state.has(sp.key):
                    state.open_spreads = [s for s in state.open_spreads if s.key != sp.key]
                    logging.warning("self-heal: OPEN %s never filled at IB — removed optimistic booking",
                                    sp.key)
            else:
                still.append(p)                                   # same-day, may still fill: keep one run
    if len(still) != len(pend):
        logging.info("pending: resolved %d, %d still open", len(pend) - len(still), len(still))
    state.pending_orders = still


# ---------- live ----------
def run_live(cfg: OptionsConfig, port: int, client_id: int) -> None:
    from options_vrp.broker import OptionsBroker
    from options_vrp.email_report import send_report

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    broker = OptionsBroker(port=port, client_id=client_id, dry_run=False)
    if not broker.connect():
        logging.error("IB connect failed — aborting."); return
    state = OptionsState.load(STATE_FILE); state.ensure_inception(today)
    orders: list[dict] = []
    rejected: list[dict] = []
    try:
        # MARGIN CEILING — needs a live connection, so it sits after connect() rather than at
        # config time. Reading is ACCOUNT-WIDE: three strategies share the account, so this
        # sleeve can be blocked by the overlay's futures margin. Correct — the constraint really
        # is shared, and being blocked beats being liquidated. Only ever blocks NEW spreads;
        # management below runs regardless, since leaving short options unmanaged into expiry is
        # worse than the margin pressure that triggered it.
        _mu = broker.margin_cushion()
        _mlvl, _mscale, _mwhy = liquidity_check(*(_mu if _mu else (float("nan"), 0.0)),
                                                limits=MarginLimits())
        # Same call already returns NetLiq; store it so the NEXT run can size off the real
        # account instead of the nominal anchor.
        if _mu and _mu[1] and _mu[1] > 0:
            state.last_net_liq = float(_mu[1])
        if _mwhy:
            (logging.error if _mlvl in ("derisk", "halt") else logging.warning)(
                "margin: %s", _mwhy)
        if _mscale <= 0 and _mlvl != "unknown":
            cfg.max_positions = 0
        elif 0 < _mscale < 1.0:
            cfg.risk_per_trade *= _mscale

        # 0) RESOLVE PENDING — book/reverse any order that filled after a prior run's poll gave up,
        # BEFORE managing, so a spread already closed at IB is gone before the manage loop could try
        # to re-close a position that is not there. This is the self-heal for the combo-fill race.
        _resolve_pending(broker, state, today)

        # 1) MANAGE open spreads
        values = broker.spread_values(state.open_spreads)
        for sp in list(state.open_spreads):
            cv = values.get(sp.key)
            if cv is None:
                continue
            # Track the high-water mark BEFORE deciding, so the stop counterfactual is
            # recorded even on the run that closes the spread.
            sp.peak_value = max(getattr(sp, "peak_value", 0.0) or 0.0, cv)
            dte = (pd.Timestamp(sp.expiry) - pd.Timestamp(today)).days
            action = manage_action(sp.entry_credit, cv, dte, cfg)
            if action:
                fill = broker.close_spread(sp)
                # Only BOOK the close when it actually FILLED. A DAY order left at
                # PreSubmitted/PendingSubmit (e.g. submitted near the close) may never fill;
                # recording it would drop the spread from state while it stays open at IB —
                # the phantom-close bug (a pending SBUX close was booked as realized profit
                # on 2026-08-07, then never filled). Leave an unfilled close open so it is
                # retried / reconciled next run rather than booked off the mark.
                if fill["status"] == "Filled" and fill["net_price"] is not None:
                    pnl = state.record_close(sp, fill["net_price"], today, action)
                    orders.append({**fill, "pnl": pnl, "reason": action})
                else:
                    logging.warning("close for %s NOT filled (status=%s) — left open, pending self-heal",
                                    sp.key, fill["status"])
                    orders.append({**fill, "pnl": None, "reason": f"{action} (unfilled)"})
                    # Track it: if the capped combo fills after this poll, the next run books it
                    # from the real IB fill instead of leaving a phantom (the 2026-08-21 SBUX case).
                    state.pending_orders.append({"action": "close", "permId": fill.get("permId", 0),
                                                 "spread": asdict(sp), "placed_date": today})

        # CIRCUIT BREAKER — here, AFTER management, so it can see UNREALISED P&L from the live
        # marks just fetched. Realised-only was blind to exactly the drawdowns that matter: a
        # short-put book can be deep underwater with nothing booked until it closes. Placed
        # before the OPEN loop so it can only ever block NEW risk.
        _unreal = sum((sp.entry_credit - values[sp.key]) * 100 * sp.contracts
                      for sp in state.open_spreads if sp.key in values)
        # cfg.budget, NOT a re-read of the env: sizing now comes from the allocation table, and
        # a breaker measuring drawdown against a DIFFERENT base than the one being traded would
        # fire at the wrong level. Sizing and the breaker reading different numbers is
        # exactly the drift the single-constant rule was introduced to prevent.
        _base = cfg.budget
        _eq = _base + state.realized_pnl + _unreal
        _peak = max(peak_equity(state.nav_history, _base), _eq)
        write_equity(ROOT.parent, "options-vrp", _eq, _peak)
        # Vol-scaled to this sleeve's own equity curve (nav_history here is TUPLES).
        _lv = BreakerLevels.from_vol(blended_vol(state.nav_history, _base, VOL_PRIOR),
                                     sigmas=BREAKER_SIGMAS)
        _blvl, _bscale, _bwhy = circuit_breaker(_eq, _peak, _lv)
        if _bwhy:
            (logging.error if _blvl == "halt" else logging.warning)("circuit breaker: %s", _bwhy)
        # BOOK-level: three sleeves each down 20% all sit under their own 25% threshold while the
        # total is down 20%. Take the WORSE of own and book.
        _bdd, _beq, _bpk, _bnote = book_drawdown(ROOT.parent)
        if _bdd is not None:
            # Book levels scale to the BOOK's OWN vol, not a hard-coded tighter set. With one
            # strategy live the book curve IS that strategy's, so the levels come out identical
            # and this adds nothing — correct, since there is no diversification to reward.
            # Skipped entirely until the book has enough history to estimate its vol.
            _bvol = book_vol(ROOT.parent)
            _lvl2, _sc2, _why2 = (circuit_breaker(_beq, _bpk, BreakerLevels.from_vol(_bvol))
                                  if _bvol else ('ok', 1.0, ''))
            if _why2:
                logging.warning("BOOK circuit breaker: %s | %s", _why2, _bnote)
            _bscale = min(_bscale, _sc2)
        if _bscale <= 0:
            cfg.max_positions = 0
        elif _bscale < 1.0:
            cfg.risk_per_trade *= _bscale

        # 2) OPEN new spreads if the gate is open and we have room
        res = target_book(cfg)
        # Logged in the LIVE path, not just dry_run: with the gate disabled via .env there is
        # otherwise no way to confirm from a live run which state it is actually in, and a
        # Windows .env still carrying REGIME_THR=1.00 would silently keep it on.
        logging.info("regime: VIX/VIX3M %.3f vs thr %.2f -> gate %s", res.regime_ratio,
                     cfg.regime_thr,
                     "DISABLED (thr>=99)" if cfg.regime_thr >= 99 else
                     ("OPEN" if res.regime_open else "SHUT — no new premium sold"))
        if res.regime_open:
            room = cfg.max_positions - len(state.open_spreads)
            # One open position per ticker — never stack a 2nd spread on a name we already hold
            # (avoids concentrating idiosyncratic risk on one underlying).
            open_tickers = {sp.ticker for sp in state.open_spreads}
            # Factor-group counts across ALREADY-OPEN spreads too, not just today's picks —
            # otherwise the cap resets every run and the book concentrates over successive days.
            from collections import Counter
            sect_count = Counter(SECTOR.get(sp.ticker, sp.ticker) for sp in state.open_spreads)
            _ov_map = res.corr_overlap or OVERLAP

            # Quotes are cached: the cheapest-of-group pass below needs the live combo quote to
            # compare members, and the main loop needs the same quote to run the cost guard.
            # Quoting twice would both waste calls and risk the two decisions disagreeing.
            _qcache: dict[str, tuple] = {}

            def _spread_of(t):
                return OpenSpread(t.ticker, t.expiry, t.short_strike, t.long_strike, t.contracts,
                                  t.credit, t.max_loss, today, t.spot)

            def _quote_of(t):
                if t.ticker not in _qcache:
                    bid, ask = broker.quote_spread(_spread_of(t))
                    ok, ratio, br = cost_ok(bid, ask, cfg.max_cost_frac,
                                            cfg.commission_per_contract, cfg.option_multiplier)
                    _qcache[t.ticker] = (bid, ask, ok, ratio, br)
                return _qcache[t.ticker]

            # CHEAPEST-OF-GROUP. Members of an overlap group are the same bet by construction, so
            # the only thing separating them is execution cost — yet VRP rank decides which one
            # claims the slot and blocks the rest. In simulation that left SPY, the cheapest name
            # in the basket at 1.0% of credit, with FEWER fills than QQQ (1.9%) and IWM (5.4%).
            # Sorting the members of each clashing group by measured cost is worth ~+0.29 capital
            # Sharpe (scripts/vrp_basket_mc.py, algo_trading).
            #
            # This REORDERS rather than drops: the overlap check in the loop below still does the
            # blocking, so if the cheapest member is rejected for some unrelated reason (risk
            # check, no quote) the next-cheapest is still reachable. Ordering ACROSS groups is
            # untouched and stays on VRP rank — cost only breaks ties between interchangeable
            # names, it does not override the edge signal.
            _targets = list(res.targets)
            _pos = {t.ticker: i for i, t in enumerate(_targets)}
            _seen: set[str] = set()
            for _t in list(_targets):
                if _t.ticker in _seen:
                    continue
                grp = [x for x in _targets
                       if x.ticker == _t.ticker or x.ticker in _ov_map.get(_t.ticker, set())]
                _seen.update(x.ticker for x in grp)
                if len(grp) < 2:
                    continue
                slots = sorted(_pos[x.ticker] for x in grp)
                # Unquotable members sort last so a missing quote never wins a slot on a 0 cost.
                ranked = sorted(grp, key=lambda x: (_quote_of(x)[3] is None,
                                                    _quote_of(x)[3] or 0.0))
                if [x.ticker for x in ranked] != [_targets[i].ticker for i in slots]:
                    logging.info("GROUP %s — cheapest first: %s", ", ".join(sorted(
                        x.ticker for x in grp)), " < ".join(
                        f"{x.ticker} {_quote_of(x)[3]:.0%}" if _quote_of(x)[3] is not None
                        else f"{x.ticker} n/a" for x in ranked))
                for slot, x in zip(slots, ranked):
                    _targets[slot] = x

            for s in _targets:
                if room <= 0:
                    break
                if s.ticker in open_tickers:
                    continue
                # ETF/constituent overlap first: a sector cap does NOT catch it, since the two
                # share a sector and so sit within the cap, yet one holds the other.
                # Prefer the FRESH correlation map; fall back to the static holdings map only
                # when there was too little history to compute one.
                _clash = _ov_map.get(s.ticker, set()) & open_tickers
                if _clash:
                    logging.info("SKIP %s — too correlated with an open position (%s)%s",
                                 s.ticker, ", ".join(sorted(_clash)),
                                 "" if res.corr_overlap else " [static fallback]")
                    continue
                _sect = SECTOR.get(s.ticker, s.ticker)
                if sect_count[_sect] >= cfg.max_per_sector:
                    logging.info("SKIP %s — already %d position(s) in %s (cap %d)",
                                 s.ticker, sect_count[_sect], _sect, cfg.max_per_sector)
                    continue
                sp = OpenSpread(s.ticker, s.expiry, s.short_strike, s.long_strike, s.contracts,
                                s.credit, s.max_loss, today, s.spot)
                if state.has(sp.key):
                    continue
                # EXECUTION COST GUARD — check the live COMBO quote before committing.
                # Single-name option spreads run ~4x the index (measured 2026-08-08), which can
                # eat 60% of the credit against a ~31% break-even. Reject rather than fill.
                bid, ask, ok, ratio, br = _quote_of(s)
                if not ok:
                    if ratio is None:
                        msg = "no live quote"
                        if not cfg.skip_if_no_quote:
                            logging.warning("%s: %s — trading anyway (SKIP_IF_NO_QUOTE=0)",
                                            sp.ticker, msg)
                            ok = True
                    else:
                        msg = (f"cost {ratio:.0%} of credit > {cfg.max_cost_frac:.0%} "
                               f"(spread {br['spread']:.0%} + comm {br['commission']:.0%}, "
                               f"credit ${br['credit']:,.0f}/ct)")
                    if not ok:
                        logging.info("SKIP %s — %s", sp.ticker, msg)
                        rejected.append({"ticker": sp.ticker, "reason": msg,
                                         "bid": bid, "ask": ask, "ratio": ratio})
                        continue
                # INDEPENDENT PRE-TRADE GUARD — re-derives the order from the budget rather
                # than trusting `s.contracts`, because a guard that reuses the strategy's own
                # sizing cannot catch a sizing bug. Notional here is MAX LOSS (what a defined-risk
                # spread can actually cost), not the credit.
                lim = RiskLimits.for_options(cfg.budget)
                chk = check_order(sp.ticker, "SELL", sp.contracts, sp.max_loss,
                                  cfg.option_multiplier, lim,
                                  gross_notional=sum(x.contracts * x.max_loss *
                                                     cfg.option_multiplier
                                                     for x in state.open_spreads))
                if not chk:
                    # Deferrable = spread too big for the CURRENT budget: expected, self-heals as
                    # the book grows, so log at INFO not a daily WARNING alert. Genuine rejects stay
                    # WARNING. (A reducing order is never rejected here — see check_order.)
                    (logging.info if chk.deferrable else logging.warning)(
                        "%s: %s", "RISK DEFER" if chk.deferrable else "RISK REJECT", chk.reason)
                    rejected.append({"ticker": sp.ticker, "reason": chk.reason,
                                     "bid": bid, "ask": ask, "ratio": ratio})
                    continue
                fill = broker.open_spread(sp)
                if fill["status"] in ("Filled", "Submitted", "PreSubmitted"):
                    credit = fill["net_price"] if fill["net_price"] is not None else s.credit
                    state.record_open(sp, credit, today)
                    orders.append(fill); open_tickers.add(s.ticker); sect_count[_sect] += 1; room -= 1
                    if fill["status"] != "Filled":
                        # Booked optimistically off a non-terminal status. Track it so the next run
                        # confirms it actually filled at IB, and reverses the booking if it did not.
                        state.pending_orders.append({"action": "open", "permId": fill.get("permId", 0),
                                                     "spread": asdict(sp), "placed_date": today})

        # 3) mark, persist, email
        values = broker.spread_values(state.open_spreads)
        unreal = sum((sp.entry_credit - values[sp.key]) * 100 * sp.contracts
                     for sp in state.open_spreads if sp.key in values)
        state.record_snapshot(today, state.realized_pnl + unreal)
        state.save(STATE_FILE)
        # RECONCILE state against IB. Motivated by the 2026-08-07 phantom close: a pending
        # close was booked as filled, so state dropped a spread that stayed open at IB with
        # nothing left to manage it. Each spread is two legs; compare per LEG, since a partial
        # fill can leave one leg on. Report only — never auto-correct a shared account.
        actual = broker.put_positions()
        if actual is not None:
            from options_vrp.broker import _ib_expiry
            exp: dict[tuple, float] = {}
            for sp in state.open_spreads:
                e = _ib_expiry(sp.expiry)
                exp[(sp.ticker, e, float(sp.short_strike))] = -float(sp.contracts)
                exp[(sp.ticker, e, float(sp.long_strike))] = float(sp.contracts)
            keyed_exp = {f"{k[0]} {k[1]} {k[2]:g}P": v for k, v in exp.items()}
            keyed_act = {f"{k[0]} {k[1]} {k[2]:g}P": v for k, v in actual.items()}
            _d, _rnote = reconcile(keyed_exp, keyed_act, label="options")
            if _rnote:
                logging.warning("%s", _rnote)
        else:
            logging.warning("reconcile: IB positions unavailable — state NOT verified this run")

        _m, _l, _note = missed_runs(state.nav_history, today)
        if _note:
            logging.warning("heartbeat: %s", _note)
        send_report(state, values, orders, res.regime_ratio, res.regime_open, today,
                    alerts=ALERTS)
        push_if_alerts(ALERTS, "Options VRP")
    finally:
        broker.disconnect()
    if rejected:
        logging.info("cost guard rejected %d candidate(s): %s", len(rejected),
                     ", ".join(f"{r['ticker']}({r['reason']})" for r in rejected))
    logging.info("Done %s: %d actions, %d open spreads.", today, len(orders), len(state.open_spreads))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--force", action="store_true", help="run on weekends too")
    ap.add_argument("--port", type=int, default=int(os.getenv("IB_PORT", "7497")))
    ap.add_argument("--client-id", type=int, default=int(os.getenv("IB_CLIENT_ID", "7")))
    args = ap.parse_args()
    cfg = _cfg(OptionsState.load(STATE_FILE))

    # KILL SWITCH. HALT_ALL exits before connecting. HALT keeps MANAGEMENT running — profit
    # targets and the 21-DTE time stop still fire — but opens nothing new. Blocking management
    # would leave short options running into expiry unmanaged, which is worse than whatever
    # prompted the halt.
    # Report WHICH COMMIT is running before anything else. Placed above the kill switch so it
    # is recorded even on a halted run: "the box is running week-old code" is exactly the kind of
    # thing you want to learn from a halted day's log, not discover a month later.
    code_version(ROOT)
    _halt, _hwhy = halt_state(ROOT)
    if _halt == HALT_ALL:
        logging.error("HALTED (all): %s — exiting without trading. NOTE: profit targets and the "
                      "21-DTE time stop did NOT run.", _hwhy)
        push_if_alerts(ALERTS, "Options VRP")
        return
    if _halt == HALT_NEW:
        logging.warning("HALTED (new risk): %s — managing open spreads only", _hwhy)
        cfg.max_positions = 0



    if args.selftest:
        selftest(cfg); return
    if not args.live:
        dry_run(cfg); return
    if datetime.now().weekday() >= 5 and not args.force:
        logging.info("Weekend — skipping."); return
    run_live(cfg, args.port, args.client_id)


if __name__ == "__main__":
    main()
