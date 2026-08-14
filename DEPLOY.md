# Options VRP — deployment

Systematic variance-risk-premium harvester: defined-risk **put credit spreads** on a small
liquid basket, gated to only sell rich vol in calm markets. **Daily** rebalance (manage + enter)
and **daily** P&L email. Runs on the **same IB paper account** as the other two systems but
**clientId 7** (magic formula = 5, trend overlay = 6) so all three connect at once. Signals and
chains come from yfinance; IB is used only to trade and to mark positions.

## The strategy in one paragraph
Sell the ~16Δ put / buy the ~10Δ put (defined-risk wing), 30–45 DTE, on the richest-VRP names,
**only** when the regime gate is open (`VIX/VIX3M < 1.00` = contango) and a name's `ATM IV − RV20`
clears `VRP_MIN` (default 2 vol pts). Each position is sized so its max loss ≤ `RISK_PER_TRADE`
(3%) of `BUDGET`, up to 6 concurrent positions. Every weekday: **manage** open spreads first
(close at 50% of max profit / 2× stop / 21-DTE time-stop), then **open** new ones if the gate is
open and there's room.

## Windows setup

```bat
cd C:\trading
git clone <bitbucket-remote>/options-vrp.git
cd options-vrp
python -m venv .venv && call .venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env & notepad .env
```

Fill `.env`: `IB_PORT` (paper Gateway port), `IB_CLIENT_ID=7`, `BUDGET=100000`, `REGIME_THR=1.00`,
`VRP_MIN=0.02`, and `EMAIL_USER/PASS/TO_EMAIL` (Gmail App Password).

## IB Gateway
Same paper account/Gateway as the other systems. Two prerequisites:
- **Options trading must be ENABLED** on the paper account (not covered by the stock/futures
  permissions — enable it in the account's trading-permissions settings).
- **Deactivate API order precautions** (else orders hold at PendingSubmit — same gotcha as the
  other two systems).

Error 162 ("market data from a different IP") is harmless — signals come from yfinance and marks
come from IB's portfolio feed, so no market-data subscription is needed.

## One-time SHAKEOUT (do this before scheduling — important)
Options combos are broker-specific, so confirm the order semantics live **once**, exactly like the
futures-order shakeout:

```bat
python scripts\run_options_paper.py --selftest        # offline: management-rule check
python scripts\run_options_paper.py                    # offline: regime + VRP + target book
python scripts\run_options_paper.py --live             # Gateway up, DURING US market hours (RTH)
```
After the `--live` run opens a position, **check in TWS that it is a short put SPREAD** — two legs
(short higher strike, long lower strike), defined risk, not a naked or inverted position. If the
combo direction is wrong, flip the parent action in `broker.py` `_combo_order` and retest. Do this
with the book small (it sizes to a few contracts) so a mistake is cheap.

## Schedule
Run **daily on weekdays, during US market hours (RTH)** so entries fill the same session and
management can act same-day. ~15:30 ET is a good slot — late enough for stable chains,
before the close.

⚠ `/ST` uses the BOX's LOCAL clock, and the box is on CET — so this must be **21:30**, not
15:30. It read 15:30 until 2026-08-14, which is 09:30 ET: the market OPEN, the widest
spreads and least stable chains of the day. Since the cost guard keys entirely off spread
width, that systematically inflated measured cost-of-credit and would have skipped trades
that should pass.

Running LAST is also deliberate: margin in the shared account is first-come-first-served,
and this is the lowest-Sharpe sleeve (0.52 vs magic-formula 0.96, trend 0.74).

```bat
schtasks /Create /TN "OptionsVRPPaper" ^
  /TR "C:\trading\options-vrp\scripts\run_options_paper.py --live" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 21:30 /F
```
(Wrap in a .bat that activates the venv, like the other systems, if preferred.)

## Notes
- **State** (`results\paper\state.json`) is the **source of truth for open spreads** — it holds
  each spread's strikes, contracts, and **entry credit** (needed to compute P&L and manage). Unlike
  the futures book, this can't be fully reconstructed from IB alone, so **back it up**. Lives on
  this box, not in git.
- yfinance option mids are unreliable off-hours; the **real credit comes from the IB fill**, which
  the runner records. The dry-run credit figures are illustrative only.
- Very high-IV names (e.g. AMD at ~80% IV) can have spreads too wide in dollars to fit one contract
  on a $100k book — they'll simply be skipped that day. Expected behaviour with the current sizing.
- Start conservative (the defaults already are: ≤3% risk/position, ≤6 positions). Widen only once
  the live shakeout and a few weeks of forward runs look clean.
