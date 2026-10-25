"""
core/email.py
--------------
Sending account emails (address verification, password reset).

STATUS: BUILT, tested against a local stand-in for the email service.
NOT yet sent through the real service: no API key or sending address is
configured, so the first real email is still an unverified step.

PROVIDER: Resend's HTTP API (POST /emails with a bearer key). An HTTP
API rather than SMTP on purpose — hosts commonly block outbound SMTP
ports, and an HTTPS request works anywhere the rest of this system does.
Another provider with a similar API would be a change to send_email()
only.

THREE SETTINGS, ALL REQUIRED (settings.py): RESEND_API_KEY, EMAIL_FROM
and PUBLIC_BASE_URL. Until all three are set, email_enabled() is False
and the site behaves as it did before email existed: no verification is
demanded and password reset says it isn't available.

WHY LINKS USE PUBLIC_BASE_URL: the address in an emailed link must come
from configuration, never from the request that triggered the email. A
request's Host header is chosen by whoever sends the request — building
a reset link from it would let an attacker have a victim's genuine
reset email point at the attacker's own site.
"""

from __future__ import annotations

import html
from urllib.parse import quote

import httpx

from aegis_bioquant.config import settings

_SEND_TIMEOUT_SECONDS = 15.0


def email_enabled() -> bool:
    return bool(settings.RESEND_API_KEY and settings.EMAIL_FROM and settings.PUBLIC_BASE_URL)


def _link(path: str, token: str) -> str:
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{path}?token={quote(token, safe='')}"


async def send_email(to: str, subject: str, text: str, html_body: str) -> bool:
    """Sends one email. Returns False on any failure — the caller decides
    what that means for the user; nothing is raised and nothing about the
    failure (which could include the API key in a URL or header echo) is
    passed back."""
    if not email_enabled():
        return False
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{settings.EMAIL_API_BASE_URL.rstrip('/')}/emails",
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": settings.EMAIL_FROM,
                    "to": [to],
                    "subject": subject,
                    "text": text,
                    "html": html_body,
                },
            )
    except Exception:  # noqa: BLE001 — see docstring
        return False
    return response.is_success


def _html(paragraphs: list[str], link: str, link_text: str, footer: str) -> str:
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return (
        '<div style="font-family:system-ui,Segoe UI,sans-serif;font-size:15px;line-height:1.5;color:#171b21">'
        f"{body}"
        f'<p><a href="{html.escape(link, quote=True)}">{html.escape(link_text)}</a></p>'
        f'<p style="color:#5d6673;font-size:13px">{html.escape(footer)}</p>'
        "</div>"
    )


async def send_verification_email(to: str, token: str) -> bool:
    link = _link("/verify", token)
    intro = "Confirm this email address to finish setting up your FDAOdds account."
    footer = "This link works for 24 hours. If you didn't create an account, you can ignore this email."
    return await send_email(
        to,
        "Confirm your email for FDAOdds",
        f"{intro}\n\n{link}\n\n{footer}\n",
        _html([intro], link, "Confirm email address", footer),
    )


async def send_password_reset_email(to: str, token: str) -> bool:
    link = _link("/reset", token)
    intro = "Someone asked to reset the password for your FDAOdds account. Use this link to choose a new one."
    footer = (
        "This link works for 1 hour and only once. If you didn't ask for it, you can ignore "
        "this email; your password has not been changed."
    )
    return await send_email(
        to,
        "Reset your FDAOdds password",
        f"{intro}\n\n{link}\n\n{footer}\n",
        _html([intro], link, "Choose a new password", footer),
    )
