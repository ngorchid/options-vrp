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


def _stop_ab(state, stop_mult: float = 2.0) -> str:
    """The stop A/B, readable from ONE book: with the stop disabled, a spread whose mark ever
    touched stop_mult x credit is one the stop WOULD have cut — and its realised P&L shows
    what that would have cost or saved."""
    try:
        r = state.stop_counterfactual(stop_mult)
    except Exception:  # noqa: BLE001 - reporting must never break the run
        return ""
    if not r.get("n"):
        return ""
    if not r["n_would_stop"]:
        return (f"<p style='font-size:13px;color:#475569'>Stop A/B: none of {r['n']} closed "
                f"spreads ever reached {stop_mult:g}x credit — no evidence either way yet.</p>")
    edge = r["edge_of_no_stop"]
    verdict = ("NOT having the stop was better" if edge > 0 else
               "the stop would have been better")
    return (f"<p style='font-size:13px;color:#475569'>"
            f"<b>Stop A/B</b> ({r['n']} closed): <b>{r['n_would_stop']}</b> spreads touched "
            f"{stop_mult:g}x credit, of which <b>{r['n_recovered']}</b> still finished "
            f"profitable. That group actually returned <b>${r['actual_pnl_of_stopped_group']:+,.0f}</b>; "
            f"a {stop_mult:g}x stop would have booked <b>${r['stop_would_have_booked']:+,.0f}</b> "
            f"&rarr; <b>${edge:+,.0f}</b>, i.e. {verdict}. "
            f"<i>Only meaningful with stop_mult&lt;=0 (stop disabled) — otherwise the stop "
            f"truncates the path and the counterfactual is unobservable.</i></p>")


def _close_tally(trade_log) -> str:
    """Lifetime count of WHY spreads were closed — the stop-vs-no-stop A/B at a glance."""
    reasons = [t.get("reason") for t in trade_log if t.get("action") == "CLOSE"]
    if not reasons:
        return "no closes yet"
    from collections import Counter
    c = Counter(reasons)
    return "  ".join(f"{r}: {c[r]}" for r in ("profit", "stop", "time") if c.get(r)) or "—"


def send_report(state, values, orders, regime_ratio, gate_open, today, dry_run=False,
                alerts=None) -> None:
    unreal = sum((sp.entry_credit - values[sp.key]) * 100 * sp.contracts
                 for sp in state.open_spreads if sp.key in values)
    total = state.realized_pnl + unreal
    gate = "OPEN (contango)" if gate_open else "SHUT (backwardation)"
    _alert_html = alerts.html() if (alerts is not None and getattr(alerts, "records", None)) else ""
    _mark = (f"[{alerts.worst} x{len(alerts.records)}] "
             if alerts is not None and getattr(alerts, "worst", None) else "")
    html = f"""<html><body style="font-family:sans-serif">{_alert_html}
    <h2>Options VRP paper — {today}</h2>
    <p>Regime VIX/VIX3M = {regime_ratio:.3f} → gate <b>{gate}</b></p>
    <p>Realized ${state.realized_pnl:,.0f} + Unrealized ${unreal:,.0f} = <b>${total:,.0f}</b>
       since {state.inception_date}</p>
    <p>Closes since inception (why) — <b>{_close_tally(state.trade_log)}</b></p>
    {_stop_ab(state)}
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
    msg["Subject"] = _mark + f"Options VRP paper — {today}  (${total:,.0f})"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw); s.sendmail(user, [to], msg.as_string())
        logging.info("email sent to %s", to)
    except Exception as e:  # noqa: BLE001
        logging.error("email failed: %s", e)
