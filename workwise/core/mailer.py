"""Pluggable outgoing email. Routes depend on get_email_sender so tests and deployments can swap it."""
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from workwise.core.security import auth_settings

logger = logging.getLogger("workwise.email")


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    """Development only: writes the whole message, including reset links, to the log."""

    def send(self, to: str, subject: str, body: str) -> None:
        logger.warning("EMAIL (console backend, development only)\nTo: %s\nSubject: %s\n\n%s", to, subject, body)


class SmtpEmailSender:
    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = auth_settings.email_from, to, subject
        message.set_content(body)
        with smtplib.SMTP(auth_settings.smtp_host, auth_settings.smtp_port, timeout=10) as smtp:
            if auth_settings.smtp_starttls:
                smtp.starttls()
            if auth_settings.smtp_user and auth_settings.smtp_password:
                smtp.login(auth_settings.smtp_user, auth_settings.smtp_password.get_secret_value())
            smtp.send_message(message)


def get_email_sender() -> EmailSender | None:
    """None means email is not configured."""
    if auth_settings.email_backend == "console":
        return ConsoleEmailSender()
    if auth_settings.email_backend == "smtp" and auth_settings.smtp_host:
        return SmtpEmailSender()
    return None


def deliver(sender: EmailSender, to: str, subject: str, body: str) -> None:
    """Background delivery; failures are logged without the message body."""
    try:
        sender.send(to, subject, body)
    except Exception:  # noqa: BLE001 - delivery must never crash the worker or leak details
        logger.error("Email delivery failed (%s)", sender.__class__.__name__)
