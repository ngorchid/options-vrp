"""IB options bridge (ib_insync) — put credit spreads as atomic combo (BAG) orders.

Mirrors the trend-overlay FuturesBroker: connect (clientId 7), dry_run mode, and marks read
from IB's portfolio feed so no market-data subscription is required. Verticals go as a two-leg
BAG (SELL short put / BUY long put) so both legs fill together — no legging risk.

NB — like the futures broker, the exact combo action semantics must be confirmed in a live
paper SHAKEOUT (place 1 contract, verify the resulting position is a short put SPREAD). Options
combos are broker-specific; this uses the standard ib_insync convention (explicit per-leg
actions, parent BUY to open / SELL to close).
"""
from __future__ import annotations

import logging

import pandas as pd

from .state import OpenSpread


def _ib_expiry(expiry: str) -> str:
    """'YYYY-MM-DD' -> 'YYYYMMDD' for IB lastTradeDateOrContractMonth."""
    return pd.Timestamp(expiry).strftime("%Y%m%d")


class OptionsBroker:
    def __init__(self, host="127.0.0.1", port=7497, client_id=7, dry_run=False):
        self.host, self.port, self.client_id, self.dry_run = host, port, client_id, dry_run
        self.ib = None

    def connect(self, timeout: int = 15) -> bool:
        from ib_insync import IB
        self.ib = IB()
        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=timeout)
            logging.info("IB options connected (clientId %s, port %s)", self.client_id, self.port)
            return True
        except Exception as e:  # noqa: BLE001
            logging.error("IB connect failed: %s", e)
            return False

    def margin_cushion(self) -> tuple[float, float] | None:
        """(excess liquidity, net liquidation) for the WHOLE account, or None.

        Account-wide on purpose: several strategies share this account, so the constraint
        genuinely is shared.

        ⚠ ExcessLiquidity, NOT MaintMarginReq (changed 2026-08-14). Maintenance margin on long
        stock is 25% of position value regardless of leverage, so a fully-invested unborrowed
        equity book read as 25% "used" and tripped the ceiling in normal operation. Excess
        liquidity is what an actual liquidation is measured against and is leverage-aware:
        it goes to zero only when the account is genuinely near forced liquidation.

        Returns None on any failure — the caller treats that as "unknown" and logs it, rather
        than assuming healthy.

        The dry-run / not-connected guard is NOT optional: without it this raises on every dry
        run, the exception is caught, and a WARNING is logged — which the alert collector then
        puts in the email subject and pushes to your phone. An alert channel that cries wolf on
        every offline run is worse than none, because you learn to ignore it.
        """
        if self.dry_run or self.ib is None:
            return None
        try:
            rows = {r.tag: r for r in self.ib.accountSummary()}
            xl = rows.get("ExcessLiquidity") or rows.get("FullExcessLiquidity")
            nl = rows.get("NetLiquidation")
            if not xl or not nl:
                return None
            return float(xl.value), float(nl.value)
        except Exception as e:  # noqa: BLE001
            logging.warning("margin_cushion failed: %s", e)
            return None

    def disconnect(self):
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()

    # --- contracts ---
    def _leg(self, ticker: str, expiry: str, strike: float, right: str = "P"):
        from ib_insync import Option
        o = Option(ticker, _ib_expiry(expiry), strike, right, "SMART", currency="USD")
        q = self.ib.qualifyContracts(o)
        return q[0] if q else None

    def _bag(self, ticker: str, short_leg, long_leg):
        """BAG combo: SELL the short put, BUY the long put (the credit spread as held)."""
        from ib_insync import Contract, ComboLeg
        bag = Contract(secType="BAG", symbol=ticker, exchange="SMART", currency="USD")
        bag.comboLegs = [
            ComboLeg(conId=short_leg.conId, ratio=1, action="SELL", exchange="SMART"),
            ComboLeg(conId=long_leg.conId, ratio=1, action="BUY", exchange="SMART"),
        ]
        return bag

    # --- orders ---
    def _combo_order(self, sp: OpenSpread, opening: bool, wait: float) -> dict:
        """BUY the bag to open (receive credit), SELL to close (pay debit). Returns a fill dict:
        {key, action, contracts, net_price, status}. net_price is the combo avg fill (per share)."""
        action = "BUY" if opening else "SELL"
        base = {"key": sp.key, "ticker": sp.ticker, "action": "OPEN" if opening else "CLOSE",
                "contracts": sp.contracts}
        if self.dry_run:
            logging.info("[DRY RUN] %s %dx %s %s %g/%gp", "OPEN" if opening else "CLOSE",
                         sp.contracts, sp.ticker, sp.expiry, sp.short_strike, sp.long_strike)
            return {**base, "net_price": None, "status": "dryrun"}
        from ib_insync import MarketOrder
        short_leg = self._leg(sp.ticker, sp.expiry, sp.short_strike)
        long_leg = self._leg(sp.ticker, sp.expiry, sp.long_strike)
        if not short_leg or not long_leg:
            logging.warning("could not qualify legs for %s — skipped", sp.key)
            return {**base, "net_price": None, "status": "qualify_failed"}
        bag = self._bag(sp.ticker, short_leg, long_leg)
        order = MarketOrder(action, sp.contracts)
        order.tif = "DAY"
        trade = self.ib.placeOrder(bag, order)
        # Poll up to `wait` seconds, returning as soon as the order reaches a terminal state.
        # A single fixed sleep too often read the status while still PreSubmitted, so the email
        # and state showed an unfilled order that had actually filled a second later (and, for a
        # close, that gap is what left a spread open in state while IB had closed it). Polling
        # reflects the real fill without slowing liquid names.
        waited = 0.0
        while waited < wait:
            self.ib.sleep(1.0)
            waited += 1.0
            if trade.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break
        st = trade.orderStatus.status
        fp = trade.orderStatus.avgFillPrice or None
        logging.info("%s %dx %s %g/%gp -> %s%s", "OPEN" if opening else "CLOSE",
                     sp.contracts, sp.ticker, sp.short_strike, sp.long_strike, st,
                     f" @ {fp}" if fp else "")
        return {**base, "net_price": abs(float(fp)) if fp else None, "status": st}

    def quote_spread(self, sp: OpenSpread, wait: float = 3.0) -> tuple[float | None, float | None]:
        """Live BID/ASK for the vertical as a COMBO.

        The combo is quoted far tighter than the two legs summed, so this is the number that
        matters. NB it needs an IB market-data subscription — this system otherwise uses
        yfinance for marks precisely to avoid one — so (None, None) is an expected outcome and
        the caller must handle it.
        """
        if self.dry_run:
            return None, None
        try:
            short_leg = self._leg(sp.ticker, sp.expiry, sp.short_strike)
            long_leg = self._leg(sp.ticker, sp.expiry, sp.long_strike)
            if not short_leg or not long_leg:
                return None, None
            bag = self._bag(sp.ticker, short_leg, long_leg)
            tk = self.ib.reqMktData(bag, "", False, False)
            self.ib.sleep(wait)
            bid = float(tk.bid) if tk.bid is not None and tk.bid == tk.bid else None
            ask = float(tk.ask) if tk.ask is not None and tk.ask == tk.ask else None
            try:
                self.ib.cancelMktData(bag)
            except Exception:  # noqa: BLE001
                pass
            return bid, ask
        except Exception as exc:  # noqa: BLE001 - a quote failure must never break the run
            logging.warning("no combo quote for %s: %r", sp.key, exc)
            return None, None

    # 45s, not 20s: IB caps combo MARKET orders at a regulatory limit, so they fill like limit
    # orders — in pieces across venues as the market comes to the cap. On 2026-08-21 an 8-lot SBUX
    # close completed at ~20.2s, a hair past the old 20s poll, so it logged "not filled" and was
    # never booked while IB had closed it -> a PHANTOM. The poll still returns the instant the order
    # fills, so fast names are unaffected; this only widens the window for slow capped combos.
    def open_spread(self, sp: OpenSpread, wait: float = 45.0) -> dict:
        return self._combo_order(sp, opening=True, wait=wait)

    def close_spread(self, sp: OpenSpread, wait: float = 45.0) -> dict:
        return self._combo_order(sp, opening=False, wait=wait)

    # --- marks (from portfolio feed; no market-data sub needed) ---
    def put_positions(self) -> dict[tuple, float] | None:
        """{(symbol, expiry, strike) -> signed contracts} for SHORT-PUT-bearing legs at IB.

        Filtered to OPT/P deliberately: this account is shared with the equity and futures
        strategies, and an unfiltered read would report their positions as orphans. Returns None
        (not {}) when unavailable, so the caller can tell "nothing held" from "could not check" —
        an empty dict would make every state position look like a phantom.
        """
        if self.dry_run or self.ib is None:
            return None
        try:
            out: dict[tuple, float] = {}
            for it in self.ib.portfolio():
                c = it.contract
                if c.secType == "OPT" and c.right == "P" and it.position:
                    k = (c.symbol, c.lastTradeDateOrContractMonth, float(c.strike))
                    out[k] = out.get(k, 0.0) + float(it.position)
            return out
        except Exception as e:  # noqa: BLE001
            logging.warning("put_positions failed: %s", e)
            return None

    def spread_values(self, open_spreads: list[OpenSpread]) -> dict[str, float]:
        """{spread.key -> current per-share debit to close} = short_put_mark − long_put_mark.
        Empty for dry-run or legs not found in the portfolio."""
        if self.dry_run or self.ib is None:
            return {}
        marks: dict[tuple, float] = {}
        for it in self.ib.portfolio():
            c = it.contract
            if c.secType == "OPT" and c.right == "P":
                marks[(c.symbol, c.lastTradeDateOrContractMonth, float(c.strike))] = it.marketPrice
        out = {}
        for sp in open_spreads:
            e = _ib_expiry(sp.expiry)
            s = marks.get((sp.ticker, e, sp.short_strike))
            l = marks.get((sp.ticker, e, sp.long_strike))
            if s is not None and l is not None:
                out[sp.key] = float(s - l)
        return out
