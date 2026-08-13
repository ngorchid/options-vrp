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
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from options_vrp import OptionsConfig, target_book  # noqa: E402
from options_vrp.strategy import (  # noqa: E402
    _nearest_delta_strike, cost_ok, earnings_cutoff, manage_action, oi_threshold,
    pick_expiry)
from options_vrp.state import OpenSpread, OptionsState  # noqa: E402
from risk_guard import (RiskLimits, check_order, effective_budget,  # noqa: E402
                        install_alert_collector, missed_runs, push_if_alerts,
                        reconcile, halt_state, HALT_ALL, HALT_NEW,
                        circuit_breaker, peak_equity, margin_check, MarginLimits)

load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(message)s")
# WARNING+ collected for the daily email. Matters most HERE: SKIP_IF_NO_QUOTE=1 can silently halt
# ALL trading, and an off-hours chain makes VRP negative for every name — both look like a normal
# quiet day in the report unless the warning is delivered.
ALERTS = install_alert_collector()
STATE_FILE = ROOT / "results" / "paper" / "state.json"


def _cfg(state: OptionsState | None = None) -> OptionsConfig:
    """BUDGET is the BASE capital; sizing compounds off this strategy's OWN realised P&L.

    So names come online by themselves as the account grows (a name is tradeable only when its
    max loss per contract fits risk_per_trade x budget) and position size shrinks after losses,
    with no manual edit and no restart.

    Deliberately NOT IB NetLiquidation: three strategies share one account, so NetLiq would have
    each of them sizing as though it owned the whole thing. Realised-only for now, which lags
    gains and therefore UNDER-sizes after a good run -- the right way to be wrong.
    """
    base = float(os.getenv("BUDGET", "100000"))
    budget = base
    if state is not None:
        budget, note = effective_budget(base, state.realized_pnl,
                                        step=float(os.getenv("BUDGET_STEP", "0.10")))
        logging.info("budget: %s", note)
    return OptionsConfig(
        budget=budget,
        regime_thr=float(os.getenv("REGIME_THR", "1.00")),
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
        _mu = broker.margin_usage()
        _mlvl, _mscale, _mwhy = margin_check(*(_mu if _mu else (float("nan"), 0.0)),
                                             limits=MarginLimits())
        if _mwhy:
            (logging.error if _mlvl in ("derisk", "halt") else logging.warning)(
                "margin: %s", _mwhy)
        if _mscale <= 0 and _mlvl != "unknown":
            cfg.max_positions = 0
        elif 0 < _mscale < 1.0:
            cfg.risk_per_trade *= _mscale

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
                    logging.warning("close for %s NOT filled (status=%s) — left open to retry",
                                    sp.key, fill["status"])
                    orders.append({**fill, "pnl": None, "reason": f"{action} (unfilled)"})

        # 2) OPEN new spreads if the gate is open and we have room
        res = target_book(cfg)
        if res.regime_open:
            room = cfg.max_positions - len(state.open_spreads)
            # One open position per ticker — never stack a 2nd spread on a name we already hold
            # (avoids concentrating idiosyncratic risk on one underlying).
            open_tickers = {sp.ticker for sp in state.open_spreads}
            for s in res.targets:
                if room <= 0:
                    break
                if s.ticker in open_tickers:
                    continue
                sp = OpenSpread(s.ticker, s.expiry, s.short_strike, s.long_strike, s.contracts,
                                s.credit, s.max_loss, today, s.spot)
                if state.has(sp.key):
                    continue
                # EXECUTION COST GUARD — check the live COMBO quote before committing.
                # Single-name option spreads run ~4x the index (measured 2026-08-08), which can
                # eat 60% of the credit against a ~31% break-even. Reject rather than fill.
                bid, ask = broker.quote_spread(sp)
                ok, ratio, br = cost_ok(bid, ask, cfg.max_cost_frac,
                                        cfg.commission_per_contract, cfg.option_multiplier)
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
                    logging.warning("RISK REJECT %s", chk.reason)
                    rejected.append({"ticker": sp.ticker, "reason": chk.reason,
                                     "bid": bid, "ask": ask, "ratio": ratio})
                    continue
                fill = broker.open_spread(sp)
                if fill["status"] in ("Filled", "Submitted", "PreSubmitted"):
                    credit = fill["net_price"] if fill["net_price"] is not None else s.credit
                    state.record_open(sp, credit, today)
                    orders.append(fill); open_tickers.add(s.ticker); room -= 1

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
    _halt, _hwhy = halt_state(ROOT)
    if _halt == HALT_ALL:
        logging.error("HALTED (all): %s — exiting without trading. NOTE: profit targets and the "
                      "21-DTE time stop did NOT run.", _hwhy)
        push_if_alerts(ALERTS, "Options VRP")
        return
    if _halt == HALT_NEW:
        logging.warning("HALTED (new risk): %s — managing open spreads only", _hwhy)
        cfg.max_positions = 0

    # CIRCUIT BREAKER on this sleeve's own equity (base + realised P&L). nav_history here is a
    # list of (date, total_pnl) TUPLES, not dicts — peak_equity handles both. derisk halves
    # risk_per_trade; reduce_only/halt stop opening while management (profit target, 21-DTE time
    # stop) keeps running, because leaving short options unmanaged into expiry is worse than the
    # drawdown that triggered it. Never auto-flattens.
    _state0 = OptionsState.load(STATE_FILE)
    _base = float(os.getenv("BUDGET", "100000"))
    _peak = peak_equity(_state0.nav_history, _base)
    _blvl, _bscale, _bwhy = circuit_breaker(_base + _state0.realized_pnl, _peak)
    if _bwhy:
        (logging.error if _blvl == "halt" else logging.warning)("circuit breaker: %s", _bwhy)
    if _bscale <= 0:
        cfg.max_positions = 0
    elif _bscale < 1.0:
        cfg.risk_per_trade *= _bscale

    if args.selftest:
        selftest(cfg); return
    if not args.live:
        dry_run(cfg); return
    if datetime.now().weekday() >= 5 and not args.force:
        logging.info("Weekend — skipping."); return
    run_live(cfg, args.port, args.client_id)


if __name__ == "__main__":
    main()
