# Runbook: an assigned put on a name magic-formula also holds (blended assignment)

Derived from what the code does today (pinned by `scripts/test_assignment_edges.py`, section 5).
Anything marked **check in IBKR** could not be verified from the code.

## 1. How you find out

The sleeve cannot prove which shares are its own, so it **does not sell anything**. It tells you:

- **Same run** (sleeve email, alert at ERROR): `ASSIGNED <key>: n of N short <K>P gone and Q <T> held
  (avg X) vs 100n expected at K — probably assigned ... NOT unwound automatically` and
  `MANUAL unwind needed — see DEPLOY.md`.
- **Every run after:** `ASSIGNED <key> (since <date>) still needs a MANUAL unwind`, plus
  `ASSIGNED POSITION OPEN / ESCALATION / URGENT — day d` (URGENT from business day 3). The same
  line appears in the sleeve report's **Open assignments** block, in the daily book summary (red
  block, subject starts `⚠ ASSIGNED`), and in the alert email if the run is halted or fails.
- Side effects you will also see: magic-formula's reconcile warns that IB holds more shares than
  its ledger; the IB-records email attributes the delivered shares to `conflict` (both sleeves own
  the name).

The same MANUAL path applies when the share count is more than 100 × n, when the cost basis is
more than about $0.01/share off the strike (e.g. fees in the average cost), and when two spreads on
the same name are assigned together.

**Meanwhile the sleeve** values the position as 100 × n shares at the stock mark plus the n long
puts (and the original credit), counts it in its circuit breaker and NAV snapshot, keeps it out of
normal management, and keeps the long puts on as protection. Nothing is sold.

## 2. Verify in IBKR first (before any order)

1. **Options:** the short leg at strike K is down by n contracts; the **n long puts** at the lower
   strike are still held.
2. **Stock:** total shares of the name and the average cost (TWS Portfolio).
3. **Who holds what:** magic-formula's shares = its ledger (`results\paper\state.json` of
   algo_trading, or "Names held by both sleeves" in the book summary). options-vrp's shares =
   100 × n. **Total at IB must equal magic + 100 × n.** If it does not, stop and find out why first.
4. The assignment itself: Activity / Trades shows the assignment (OptionEAE "Assignment") on the
   expected date. **Check in IBKR.**

## 3. Manual unwind options

| Option | How | For | Against |
|---|---|---|---|
| **A. Sell 100 × n shares, then the n long puts** (what the automatic unwind does) | During regular hours: SELL exactly 100 × n shares (market or marketable limit). Only once filled: SELL the n long puts. | Removes the large stock exposure first; mirrors the tested path | Pays the bid/ask on the put; IB's default tax-lot method may close magic's lots instead of the delivered lot — choose the lot if it matters (**check in IBKR**: lot selection / Tax Optimizer) |
| **B. Exercise the n long puts** | Exercise request for n puts before IB's cutoff (**check in IBKR**) | Sells 100 × n shares at the long strike; better than A when the put is deep in the money and its bid is below intrinsic | Settles overnight; exercise fee; one more day before it shows as done |

Not an option: leaving it to expiry. The shares tie up roughly 25% of their value in maintenance
margin, and if the put expires out of the money the shares stay with no hedge.

## 4. What not to do

- Do not sell **all** shares of the name: part of them are magic-formula's.
- Do not sell the long puts **before** the shares: that leaves unhedged stock.
- Do not buy back the short put: it no longer exists (that would open a new long put).
- Do not edit `state.json` by hand: the sleeve retires the spread by itself (step 5).
- Do not set `HALT_HARD` while this is open unless you are watching the account.
- Avoid untagged orders: put `options-vrp:manual-YYYYMMDD` in the order's **Order Ref** field
  (**check in IBKR**: TWS order ticket). Tagged, the trades are attributed to options-vrp;
  untagged, they land in `conflict` and alert.

## 5. Afterwards — keep attribution correct

1. **Next options-vrp run:** it sees the long puts gone and retires the spread with
   `ASSIGNED_CLOSED_OUTSIDE`. The alerts stop. **The hand unwind's P&L is NOT booked in the
   options-vrp ledger** (the code has no step for it); it appears in the book summary's
   *unattributed* line of the NAV bridge.
2. If you sold the shares but **kept** the puts, the sleeve still values the shares and keeps
   alerting: sell the puts too.
3. **Record** in your notes: date, shares sold and price, puts sold/exercised and price,
   commissions, IB execution ids, and the Order Ref used. That is what explains the unattributed
   amount and the next audit email.
4. **IB-records emails:** a tagged manual trade shows as `flex_tagged_unbooked` (it is in no
   ledger); an untagged one as `flex_untagged` / `conflict`. **This repeats in every nightly email**:
   the check re-reads all trades since 2026-10-06 and has no way to acknowledge a known hand trade
   yet (open proposal). Your note from step 3 is what tells these lines apart from a real fault.
5. Check that magic-formula's reconcile no longer warns (IB shares = its ledger again).
