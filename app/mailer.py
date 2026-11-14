"""Outgoing email over SMTP. Configured by MAIL_* environment variables;
with no settings, mail_enabled() is False and email verification is off."""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

log = logging.getLogger(__name__)


def mail_enabled():
    return all(os.environ.get(k) for k in ("MAIL_SERVER", "MAIL_USERNAME", "MAIL_PASSWORD"))


def send_email(to, subject, text, html=None):
    """Returns True if the server accepted the message. Never raises."""
    if not mail_enabled():
        return False
    host = os.environ["MAIL_SERVER"]
    port = int(os.environ.get("MAIL_PORT", "465"))
    use_ssl = os.environ.get("MAIL_USE_SSL", "true").lower() == "true"
    sender = os.environ.get("MAIL_FROM") or os.environ["MAIL_USERNAME"]
    domain = parseaddr(sender)[1].split("@")[-1] or "localhost"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)       # missing Date/Message-ID
    msg["Message-ID"] = make_msgid(domain=domain)  # headers push mail to spam
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(os.environ["MAIL_USERNAME"], os.environ["MAIL_PASSWORD"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(os.environ["MAIL_USERNAME"], os.environ["MAIL_PASSWORD"])
                s.send_message(msg)
        return True
    except Exception:
        log.exception("Could not send email to %s", to)
        return False