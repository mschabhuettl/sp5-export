"""E-Mail-Dienst für OpenSchichtplaner5.

Versendet HTML-Benachrichtigungs-Mails via SMTP. Konfiguration über
Umgebungsvariablen (siehe .env.example):

    SP5_SMTP_HOST      – SMTP-Server-Hostname (Default: leer = deaktiviert)
    SP5_SMTP_PORT      – SMTP-Port (Default: 587)
    SP5_SMTP_USER      – SMTP-Benutzername
    SP5_SMTP_PASSWORD  – SMTP-Passwort
    SP5_SMTP_FROM      – Absenderadresse (Default: SP5_SMTP_USER)
    SP5_SMTP_TLS       – "true" für STARTTLS (Default), "ssl" für implizites SSL
    SP5_SMTP_ENABLED   – "true" / "false" (Default: "true", wenn Host gesetzt)
    SP5_APP_URL        – Basis-URL für Links in Mails (z. B. https://sp5.example.com)
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger("sp5.email")


# ── Configuration ─────────────────────────────────────────────────────────────


class EmailConfig:
    """Nur-Lese-Momentaufnahme der SMTP-Einstellungen aus der Umgebung."""

    def __init__(self) -> None:
        self.host: str = os.environ.get("SP5_SMTP_HOST", "").strip()
        self.port: int = int(os.environ.get("SP5_SMTP_PORT", "587"))
        self.user: str = os.environ.get("SP5_SMTP_USER", "").strip()
        self.password: str = os.environ.get("SP5_SMTP_PASSWORD", "")
        self.from_addr: str = (
            os.environ.get("SP5_SMTP_FROM", "").strip() or self.user
        )
        self.tls_mode: str = os.environ.get("SP5_SMTP_TLS", "true").lower()
        self.app_url: str = os.environ.get("SP5_APP_URL", "http://localhost:8000").rstrip("/")

        _enabled_raw = os.environ.get("SP5_SMTP_ENABLED", "").lower()
        if _enabled_raw in ("true", "1", "yes"):
            self.enabled = True
        elif _enabled_raw in ("false", "0", "no"):
            self.enabled = False
        else:
            # auto: aktiviert, wenn ein Host gesetzt ist
            self.enabled = bool(self.host)

    @property
    def is_configured(self) -> bool:
        """Liefert True, wenn genug Konfiguration für einen Sendeversuch da ist."""
        return bool(self.host) and self.enabled

    def to_safe_dict(self) -> dict[str, Any]:
        """Liefert die Konfiguration ohne Passwort (für die Admin-Anzeige)."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "from_addr": self.from_addr,
            "tls_mode": self.tls_mode,
            "enabled": self.enabled,
            "is_configured": self.is_configured,
            "app_url": self.app_url,
        }


def get_config() -> EmailConfig:
    """Return a fresh EmailConfig (re-reads env each time)."""
    return EmailConfig()


# ── HTML template ─────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         margin: 0; padding: 0; background: #f5f7fa; color: #1e293b; }}
  .wrap {{ max-width: 560px; margin: 24px auto; background: #fff;
           border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .header {{ background: #3b82f6; color: #fff; padding: 20px 24px;
             border-radius: 8px 8px 0 0; font-size: 18px; font-weight: 600; }}
  .body {{ padding: 24px; line-height: 1.6; }}
  .body h2 {{ margin: 0 0 8px; font-size: 16px; color: #1e293b; }}
  .body p {{ margin: 0 0 16px; }}
  .cta {{ display: inline-block; background: #3b82f6; color: #fff !important;
          text-decoration: none; padding: 10px 20px; border-radius: 6px;
          font-weight: 500; }}
  .footer {{ padding: 16px 24px; font-size: 12px; color: #94a3b8;
             border-top: 1px solid #e2e8f0; text-align: center; }}
</style></head>
<body>
<div class="wrap">
  <div class="header">📋 OpenSchichtplaner5</div>
  <div class="body">
    <h2>{title}</h2>
    <p>{message}</p>
    {cta_html}
  </div>
  <div class="footer">
    Diese E-Mail wurde automatisch von OpenSchichtplaner5 versendet.<br>
    {app_url}
  </div>
</div>
</body></html>
"""


def _safe_href(link: str, app_url: str) -> str | None:
    """Liefert einen escapeten, schema-sicheren href für *link*, sonst None.

    Relative Links werden gegen *app_url* absolut gemacht. Absolute Links sind
    nur mit http/https-Schema erlaubt, damit keine ``javascript:``/``data:``-
    URIs in den CTA-Button gelangen.
    """
    href = link if link.startswith("http") else f"{app_url}{link}"
    scheme = href.split(":", 1)[0].lower() if ":" in href else ""
    # Relativ (ohne Schema) oder explizit http(s) sind erlaubt; alles andere wird abgewiesen.
    if scheme and scheme not in ("http", "https"):
        return None
    return html.escape(href, quote=True)


def _render_html(
    title: str,
    message: str,
    link: str | None = None,
    app_url: str = "",
) -> str:
    cta_html = ""
    if link:
        href = _safe_href(link, app_url)
        if href is not None:
            cta_html = f'<p><a class="cta" href="{href}">Jetzt ansehen →</a></p>'
    # Nutzer-beeinflussten Inhalt vor dem Einsetzen ins HTML-Template escapen
    # (verhindert gespeicherte HTML-/Script-Injection in Benachrichtigungs-Mails).
    # Die Zeilenumbruch→<br>-Umwandlung passiert NACH dem Escapen, damit die
    # eingefügten <br>-Tags überleben.
    safe_message = html.escape(message).replace("\n", "<br>")
    return _HTML_TEMPLATE.format(
        title=html.escape(title),
        message=safe_message,
        cta_html=cta_html,
        app_url=html.escape(app_url),
    )


def _render_plain(title: str, message: str, link: str | None = None, app_url: str = "") -> str:
    parts = [title, "", message]
    if link:
        href = link if link.startswith("http") else f"{app_url}{link}"
        parts += ["", f"Link: {href}"]
    parts += ["", "---", f"OpenSchichtplaner5 — {app_url}"]
    return "\n".join(parts)


# ── Sending ───────────────────────────────────────────────────────────────────


def send_email(
    *,
    to: str,
    subject: str,
    title: str,
    message: str,
    link: str | None = None,
    config: EmailConfig | None = None,
) -> bool:
    """Send a single email. Returns True on success, False on failure.

    This is a **blocking** call (network I/O).  Use ``send_email_async``
    for fire-and-forget from request handlers.
    """
    cfg = config or get_config()
    if not cfg.is_configured:
        logger.debug("Email not configured – skipping send to %s", to)
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = cfg.from_addr
    msg["To"] = to
    msg["Subject"] = subject

    plain = _render_plain(title, message, link, cfg.app_url)
    html = _render_html(title, message, link, cfg.app_url)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        if cfg.tls_mode == "ssl":
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=15) as srv:
                if cfg.user:
                    srv.login(cfg.user, cfg.password)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=15) as srv:
                if cfg.tls_mode in ("true", "starttls", "1", "yes"):
                    srv.starttls()
                if cfg.user:
                    srv.login(cfg.user, cfg.password)
                srv.send_message(msg)
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to)
        return False


def send_email_async(
    *,
    to: str,
    subject: str,
    title: str,
    message: str,
    link: str | None = None,
) -> None:
    """Fire-and-forget-Mailversand in einem Hintergrund-Thread."""
    t = threading.Thread(
        target=send_email,
        kwargs=dict(to=to, subject=subject, title=title, message=message, link=link),
        daemon=True,
    )
    t.start()


# ── Notification-Email bridge ─────────────────────────────────────────────────

# Notification type → email subject prefix
_SUBJECT_MAP: dict[str, str] = {
    "shift_change": "Schichtänderung",
    "swap_request": "Tauschanfrage",
    "swap_approved": "Tausch genehmigt",
    "swap_rejected": "Tausch abgelehnt",
    "vacation_request": "Urlaubsantrag",
    "vacation_approved": "Urlaub genehmigt",
    "vacation_rejected": "Urlaub abgelehnt",
    "absence_status": "Urlaubsantrag-Status",
    "absence": "Abwesenheit",
    "general": "Benachrichtigung",
    "info": "Information",
    "warning": "Warnung",
}


def send_notification_email(
    *,
    notification_type: str,
    title: str,
    message: str,
    recipient_email: str | None,
    link: str | None = None,
) -> None:
    """Sendet die Mail zu einer Benachrichtigung, wenn konfiguriert und Adresse vorhanden.

    Aufgerufen aus create_notification(). Nicht-blockierend (Hintergrund-Thread).
    """
    if not recipient_email:
        return
    cfg = get_config()
    if not cfg.is_configured:
        return
    prefix = _SUBJECT_MAP.get(notification_type, "Benachrichtigung")
    subject = f"[SP5] {prefix}: {title}"
    send_email_async(to=recipient_email, subject=subject, title=title, message=message, link=link)
