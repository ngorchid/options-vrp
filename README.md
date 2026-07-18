# Options VRP paper system

A systematic **variance-risk-premium harvester**: sells defined-risk put credit spreads on a
small liquid basket, but only when the vol is genuinely rich and the market is calm. Third IB
paper stream alongside `magic-formula` and `trend-overlay` (own clientId **7**).

## The edge (validated on free index data)
- **Regime gate** — sell only when `VIX/VIX3M < 1.00` (contango). Stand down in backwardation,
  where short-vol blows up. This is the survival mechanism: on the index proxy it turned a −51%
  drawdown into ~−5% and dodged 2008/2018/2020/2022.
- **VRP filter** — per name, require `ATM IV − RV20 > 2 vol points`: only sell where you're
  genuinely overpaid (not the thin index premium that historically earned nothing).
- **Structure** — short ~16Δ put, long ~10Δ (defined-risk wing), 30–45 DTE, richest-VRP names
  first, sized so each position risks ≤ 3% of budget.
- **Management** — close at 50% of max profit / 2× stop / 21-DTE time-stop.

Forward paper-testing both validates real execution AND records live chains/IVs, building the
single-name IV-Rank history we chose not to buy.

## Run
```
python scripts/run_options_paper.py             # offline dry-run: regime + VRP + target book
python scripts/run_options_paper.py --selftest   # offline: management-rule check
python scripts/run_options_paper.py --live        # connect IB (clientId 7): manage + open + email
```

## Live shakeout (do once, like the trend-overlay futures test)
1. Confirm **options trading is enabled** on the paper account, API precautions off.
2. `pip install -r requirements.txt`, copy `.env.example` → `.env`, fill it in.
3. Run the paper Gateway (clientId 7). Do a **1-contract** `--live` during RTH and **verify in
   TWS that the resulting position is a short put SPREAD** (both legs, defined risk) — the combo
   action/leg semantics in `broker.py` need this one-time confirmation.
4. Once verified, schedule `--live` daily on weekdays.

## Notes
- yfinance for signal/chains, IB for fills (no market-data subscription needed; marks come from
  IB's portfolio feed). yfinance option mids are unreliable off-hours — real credit comes from
  the IB fill.
- `results/paper/state.json` is the source of truth and lives on one machine (not in git).
