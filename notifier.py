"""
Email notifier — sends scan results to the configured address via Gmail SMTP.
Credentials loaded from env vars (GitHub Secrets in production, .env locally).
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime


GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "")
TO_EMAIL   = os.getenv("NOTIFY_EMAIL", "rfgenius95@gmail.com")


def _build_email(subject: str, body: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(body, "html"))
    return msg


def send(subject: str, body_html: str):
    if not GMAIL_USER or not GMAIL_PASS:
        print("  [email] GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping email.")
        return
    try:
        msg = _build_email(subject, body_html)
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())
        print(f"  [email] Sent to {TO_EMAIL}")
    except Exception as e:
        print(f"  [email] Failed: {e}")


# ── Email builders ─────────────────────────────────────────────────────────────

def send_no_setup(spy_regime: str, posture: str, rotation: list):
    subject = f"AI Trader - No Setup Today ({datetime.now().strftime('%b %d')})"
    rows = "".join(
        f"<tr><td>{r['sector']}</td><td>{r['ticker']}</td>"
        f"<td>{r['ret_20d']:+.1f}%</td><td>{r['obv']}</td></tr>"
        for r in rotation[:6]
    )
    body = f"""
    <h2 style='color:#333'>Swing Trading Scanner</h2>
    <p><b>Date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</p>
    <p><b>SPY Regime:</b> {spy_regime} &nbsp;|&nbsp; <b>Market Posture:</b> {posture}</p>
    <h3 style='color:#555'>No setups today — stay in cash.</h3>
    <h4>Top Sector Rotation</h4>
    <table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:13px'>
      <tr style='background:#eee'><th>Sector</th><th>ETF</th><th>20d Ret</th><th>OBV</th></tr>
      {rows}
    </table>
    <p style='color:#888;font-size:11px'>AI Trader — paper account</p>
    """
    send(subject, body)


def send_signals(signals: list, spy_regime: str, posture: str, account_value: float):
    count   = len(signals)
    subject = f"AI Trader - {count} Signal(s) Found! ({datetime.now().strftime('%b %d')})"

    cards = ""
    for s in signals:
        sig    = s.get("signal", "BUY")
        color  = "#27ae60" if sig == "BUY" else "#e74c3c"
        status = s.get("order_status", "")
        cards += f"""
        <div style='border:1px solid #ddd;border-radius:6px;padding:12px;margin-bottom:12px'>
          <h3 style='margin:0;color:{color}'>{s['symbol']} — {sig} (Strategy {s.get('strategy','')})</h3>
          <p style='margin:4px 0;color:#555'>Regime: {s['regime']} &nbsp;|&nbsp; RS: {s['rs']} &nbsp;|&nbsp; OBV: {s['obv']}</p>
          <table style='font-size:13px'>
            <tr><td><b>Entry</b></td><td>${s['entry']:.2f}</td>
                <td><b>Stop</b></td><td>${s['stop']:.2f}</td>
                <td><b>Target (2R)</b></td><td>${s['2R']:.2f}</td></tr>
            <tr><td><b>Shares</b></td><td>{s['shares']}</td>
                <td><b>Notional</b></td><td>${s['notional']:,.2f}</td>
                <td><b>Risk</b></td><td>${s['risk_$']:.2f}</td></tr>
          </table>
          <p style='margin:6px 0;color:#333'><b>Signal:</b> {s['reason']}</p>
          <p style='margin:4px 0;color:#777'><b>Sentiment:</b> {s['sentiment']} &nbsp;|&nbsp; <b>Order Flow:</b> {s['of_score']:+d}/+3</p>
          {f"<p style='color:{color}'><b>Order:</b> {status}</p>" if status else ""}
        </div>
        """

    body = f"""
    <h2 style='color:#333'>Swing Trading Scanner</h2>
    <p><b>Date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</p>
    <p><b>SPY Regime:</b> {spy_regime} &nbsp;|&nbsp; <b>Market Posture:</b> {posture}</p>
    <p><b>Account Value:</b> ${account_value:,.2f}</p>
    <h3 style='color:#27ae60'>{count} Setup(s) Found — Orders Placed on Paper Account</h3>
    {cards}
    <p style='color:#888;font-size:11px'>AI Trader — paper account. Not financial advice.</p>
    """
    send(subject, body)
