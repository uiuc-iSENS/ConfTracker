"""Outbound mail, shared by the health report and the reminder sender.

The lab machine sits on a campus IP, so the university outbound relay accepts
its mail unauthenticated -- no credentials to store, no MTA to run. Off campus
(or if the relay policy changes), set CONFTRACKER_SMTP_USER and
CONFTRACKER_SMTP_PASSWORD and this authenticates instead.
"""

import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger(__name__)


def send(
    subject: str,
    body: str,
    to: str | None = None,
    reply_to: str | None = None,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"ConfTracker <{config.REPORT_FROM}>"
    msg["To"] = to or config.REPORT_TO
    # Mail sent from the campus relay has to carry an @illinois.edu sender for
    # SPF to pass, but replies ("unsubscribe") need to reach the signup
    # mailbox, which is somewhere else entirely. When we send as the group
    # Gmail the two are the same address and the header is just noise.
    if reply_to and reply_to.lower() != config.REPORT_FROM.lower():
        msg["Reply-To"] = reply_to
    # Keeps us out of vacation autoresponders -- and, since scraper.inbox
    # ignores auto-submitted mail, out of a loop with our own replies.
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60) as s:
        s.ehlo()
        try:
            s.starttls()
            s.ehlo()
        except (smtplib.SMTPException, OSError) as e:
            log.warning("STARTTLS unavailable, sending over plain SMTP: %s", e)
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)
