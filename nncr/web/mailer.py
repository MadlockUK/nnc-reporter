"""Sending the app's emails: invitations, password resets, security notices.

SMTP settings come from the environment (SMTP_HOST, SMTP_PORT, SMTP_USER,
SMTP_PASSWORD, SMTP_FROM, SMTP_SECURITY=starttls|ssl|none). Any provider works -
Microsoft 365, Gmail/Workspace with an app password, Brevo, Mailgun, SES.

With no SMTP configured nothing is sent: the message is written to the server
log instead (so a first admin can still be set up), and the admin pages say so.
"""
import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import settings

log = logging.getLogger("nnc.mail")


def _send_now(to: str, subject: str, text: str) -> bool:
    s = settings.get()
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(s.smtp_from.split("@")[-1].strip(">") or None))
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    try:
        ctx = ssl.create_default_context()
        if s.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port or 465, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port or 587, timeout=20)
            if s.smtp_security != "none":
                server.starttls(context=ctx)
        with server:
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
        return True
    except Exception as e:                                   # noqa: BLE001
        log.error("could not send email to %s (%s): %s", to, subject, e)
        return False


def send(to: str, subject: str, text: str, wait: bool = False) -> bool:
    """Send in the background so a slow mail server never slows a sign-in page
    (and so response times reveal nothing about whether an account exists)."""
    s = settings.get()
    if not s.mail_configured:
        log.warning("SMTP not configured - email to %s not sent.\n--- %s ---\n%s\n---",
                    to, subject, text)
        return False
    if wait:
        return _send_now(to, subject, text)
    threading.Thread(target=_send_now, args=(to, subject, text), daemon=True).start()
    return True


def link(path: str) -> str:
    base = settings.get().base_url or "http://127.0.0.1:%d" % settings.get().port
    return base + path


FOOTER = ("\n\n--\nNNC Reporter - reporting street issues to North Northamptonshire "
          "Council.\nIf you did not expect this email, you can ignore it, or tell "
          "whoever runs NNC Reporter for your group.")


def invite(to: str, name: str, inviter: str, url: str, days: int) -> bool:
    return send(to, "You're invited to NNC Reporter",
                f"Hello {name or ''},\n\n{inviter} has invited you to NNC Reporter, the "
                f"tool for reporting potholes, fly-tipping and other street issues to "
                f"North Northamptonshire Council.\n\nSet your password and sign in here "
                f"(the link works once, for {days} days):\n\n{url}\n" + FOOTER)


def reset(to: str, url: str, minutes: int) -> bool:
    return send(to, "Reset your NNC Reporter password",
                f"Someone - hopefully you - asked to reset the password for this "
                f"account.\n\nChoose a new password here (the link works once, for "
                f"{minutes} minutes):\n\n{url}\n\nIf it wasn't you, ignore this email: "
                f"your password has not changed." + FOOTER)


def notice(to: str, subject: str, text: str) -> bool:
    return send(to, subject, text + FOOTER)
