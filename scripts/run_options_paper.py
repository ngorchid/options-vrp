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
    _nearest_delta_strike, cost_ok, manage_action, oi_threshold)
from options_vrp.state import OpenSpread, OptionsState  # noqa: E402

load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(message)s")
STATE_FILE = ROOT / "results" / "paper" / "state.json"


def _cfg() -> OptionsConfig:
    return OptionsConfig(
        budget=float(os.getenv("BUDGET", "100000")),
        regime_thr=float(os.getenv("REGIME_THR", "1.00")),
        vrp_min=float(os.getenv("VRP_MIN", "0.02")),
        stop_mult=float(os.getenv("STOP_MULT", "0")),
        max_cost_frac=float(os.getenv("MAX_COST_FRAC", "0.25")),
        commission_per_contract=float(os.getenv("COMMISSION_PER_CONTRACT", "0.65")),
        min_open_interest=int(os.getenv("MIN_OPEN_INTEREST", "50")),
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
                close_val = fill["net_price"] if fill["net_price"] is not None else cv
                pnl = state.record_close(sp, close_val, today, action)
                orders.append({**fill, "pnl": pnl, "reason": action})

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
        send_report(state, values, orders, res.regime_ratio, res.regime_open, today)
    finally:
        broker.disconnect()
    if rejected:
        logging.info("cost guard rejected %d candidate(s): %s", len(rejected),
                     ", ".join(f"{r['ticker']}({r['reason']})" for r in rejected))
    logging.info("Done %s: %d actions, %d open spreads.", today, len(orders), len(state.open_spreads))

    from options_vrp.strategy import cost_ok
    print("\nSELF-TEST — execution cost guard (threshold 25% of credit):")
    for lab, b, a in (("SPX-like  16.50 / 17.00", 16.50, 17.00),
                      ("marginal   1.00 /  1.30", 1.00, 1.30),
                      ("PFE-like   0.03 /  0.05", 0.03, 0.05),
                      ("no quote available     ", None, None)):
        ok, ratio = cost_ok(b, a, 0.25)
        rs = "n/a" if ratio is None else f"{ratio:.0%}"
        print(f"  {lab}  cost/credit {rs:>5s}  -> {'TRADE' if ok else 'SKIP'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--force", action="store_true", help="run on weekends too")
    ap.add_argument("--port", type=int, default=int(os.getenv("IB_PORT", "7497")))
    ap.add_argument("--client-id", type=int, default=int(os.getenv("IB_CLIENT_ID", "7")))
    args = ap.parse_args()
    cfg = _cfg()

    if args.selftest:
        selftest(cfg); return
    if not args.live:
        dry_run(cfg); return
    if datetime.now().weekday() >= 5 and not args.force:
        logging.info("Weekend — skipping."); return
    run_live(cfg, args.port, args.client_id)


if __name__ == "__main__":
    main()
