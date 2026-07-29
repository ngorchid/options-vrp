"""Daily HTML email — open spreads (with mark-to-market), today's trades, and P&L since
inception. SMTP via env (EMAIL_USER / EMAIL_PASS / TO_EMAIL), gmail SSL — same as siblings.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _rows(open_spreads, values) -> str:
    if not open_spreads:
        return "<tr><td colspan=6>— no open spreads —</td></tr>"
    out = []
    for sp in open_spreads:
        cv = values.get(sp.key)
        unreal = (sp.entry_credit - cv) * 100 * sp.contracts if cv is not None else None
        out.append(
            f"<tr><td>{sp.ticker}</td><td>{sp.expiry}</td>"
            f"<td>{sp.short_strike:g}/{sp.long_strike:g}p ×{sp.contracts}</td>"
            f"<td>${sp.entry_credit*100:,.0f}</td>"
            f"<td>{'—' if cv is None else f'${cv*100:,.0f}'}</td>"
            f"<td>{'—' if unreal is None else f'${unreal:,.0f}'}</td></tr>")
    return "\n".join(out)


def _trades(orders) -> str:
    if not orders:
        return "<tr><td colspan=5>— none —</td></tr>"
    rows = []
    for o in orders:
        pnl = "" if o.get("pnl") is None else f"${o['pnl']:,.0f}"
        rows.append(f"<tr><td>{o['action']}</td><td>{o.get('ticker','')}</td>"
                    f"<td>{o['key']}</td><td>{o.get('reason','')}</td>"
                    f"<td>{o.get('status','')} {pnl}</td></tr>")
    return "\n".join(rows)


def _close_tally(trade_log) -> str:
    """Lifetime count of WHY spreads were closed — the stop-vs-no-stop A/B at a glance."""
    reasons = [t.get("reason") for t in trade_log if t.get("action") == "CLOSE"]
    if not reasons:
        return "no closes yet"
    from collections import Counter
    c = Counter(reasons)
    return "  ".join(f"{r}: {c[r]}" for r in ("profit", "stop", "time") if c.get(r)) or "—"


def send_report(state, values, orders, regime_ratio, gate_open, today, dry_run=False) -> None:
    unreal = sum((sp.entry_credit - values[sp.key]) * 100 * sp.contracts
                 for sp in state.open_spreads if sp.key in values)
    total = state.realized_pnl + unreal
    gate = "OPEN (contango)" if gate_open else "SHUT (backwardation)"
    html = f"""<html><body style="font-family:sans-serif">
    <h2>Options VRP paper — {today}</h2>
    <p>Regime VIX/VIX3M = {regime_ratio:.3f} → gate <b>{gate}</b></p>
    <p>Realized ${state.realized_pnl:,.0f} + Unrealized ${unreal:,.0f} = <b>${total:,.0f}</b>
       since {state.inception_date}</p>
    <p>Closes since inception (why) — <b>{_close_tally(state.trade_log)}</b></p>
    <h3>Open spreads</h3><table border=1 cellpadding=4>
    <tr><th>ticker</th><th>expiry</th><th>spread</th><th>credit</th><th>mark</th><th>unreal P&L</th></tr>
    {_rows(state.open_spreads, values)}</table>
    <h3>Today's trades</h3><table border=1 cellpadding=4>
    <tr><th>action</th><th>ticker</th><th>spread</th><th>reason</th><th>status / P&L</th></tr>
    {_trades(orders)}</table>
    </body></html>"""

    user, pw, to = os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"), os.getenv("TO_EMAIL")
    if dry_run or not (user and pw and to):
        logging.info("email skipped (dry_run or EMAIL_* unset)"); return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Options VRP paper — {today}  (${total:,.0f})"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw); s.sendmail(user, [to], msg.as_string())
        logging.info("email sent to %s", to)
    except Exception as e:  # noqa: BLE001
        logging.error("email failed: %s", e)
