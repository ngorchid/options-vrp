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
from options_vrp.strategy import manage_action  # noqa: E402
from options_vrp.state import OpenSpread, OptionsState  # noqa: E402

load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(message)s")
STATE_FILE = ROOT / "results" / "paper" / "state.json"


def _cfg() -> OptionsConfig:
    return OptionsConfig(
        budget=float(os.getenv("BUDGET", "100000")),
        regime_thr=float(os.getenv("REGIME_THR", "1.00")),
        vrp_min=float(os.getenv("VRP_MIN", "0.02")))


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
    try:
        # 1) MANAGE open spreads
        values = broker.spread_values(state.open_spreads)
        for sp in list(state.open_spreads):
            cv = values.get(sp.key)
            if cv is None:
                continue
            dte = (pd.Timestamp(sp.expiry) - pd.Timestamp(today)).days
            action = manage_action(sp.entry_credit, cv, dte, cfg)
            if action:
                fill = broker.close_spread(sp)
                close_val = fill["net_price"] if fill["net_price"] is not None else cv
                pnl = state.record_close(sp, close_val, today, action)
                orders.append({**fill, "pnl": pnl})

        # 2) OPEN new spreads if the gate is open and we have room
        res = target_book(cfg)
        if res.regime_open:
            room = cfg.max_positions - len(state.open_spreads)
            for s in res.targets:
                if room <= 0:
                    break
                sp = OpenSpread(s.ticker, s.expiry, s.short_strike, s.long_strike, s.contracts,
                                s.credit, s.max_loss, today, s.spot)
                if state.has(sp.key):
                    continue
                fill = broker.open_spread(sp)
                if fill["status"] in ("Filled", "Submitted", "PreSubmitted"):
                    credit = fill["net_price"] if fill["net_price"] is not None else s.credit
                    state.record_open(sp, credit, today)
                    orders.append(fill); room -= 1

        # 3) mark, persist, email
        values = broker.spread_values(state.open_spreads)
        unreal = sum((sp.entry_credit - values[sp.key]) * 100 * sp.contracts
                     for sp in state.open_spreads if sp.key in values)
        state.record_snapshot(today, state.realized_pnl + unreal)
        state.save(STATE_FILE)
        send_report(state, values, orders, res.regime_ratio, res.regime_open, today)
    finally:
        broker.disconnect()
    logging.info("Done %s: %d actions, %d open spreads.", today, len(orders), len(state.open_spreads))


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
