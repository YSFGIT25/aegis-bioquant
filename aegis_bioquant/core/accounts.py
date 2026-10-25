"""
core/accounts.py
-----------------
Customer accounts and each customer's alert destinations.

STATUS: BUILT and tested (SQLite and Postgres). This is the data and
logic layer only. Nothing here is reachable by a customer yet — the
website (sign-up form, login session, settings page, the Telegram
webhook route) is the next piece and calls into these functions.

WHAT IT COVERS:
  - Accounts      create_user(), authenticate(). Passwords are hashed
                  with argon2id; the password itself is never stored.
  - Discord       set_discord_webhook(): the user pastes a webhook URL.
  - Telegram      create_telegram_link() -> the user opens the link and
                  presses Start -> Telegram tells us -> handle_telegram_update()
                  ties that chat to the user. "/stop" in the chat unlinks.
  - Delivery      get_active_destinations() is what telemetry/alerts.py
                  fans an alert out to; record_deliveries() logs the
                  result and switches off destinations that keep failing.

  - Email links   create_email_token() / peek_email_token() /
                  consume_email_token() back the "confirm your address"
                  and "reset your password" links; mark_email_verified()
                  and set_password() act on them. Sending the emails is
                  core/email.py; the pages are web/app.py.

WHAT IT DELIBERATELY DOES NOT DO YET:
  - No paywall. Every user with a destination gets alerts. The
    subscription columns exist on the user row but nothing reads them
    until payments are built.
  - This module does not itself require a verified email for anything.
    The website does: once email sending is configured, it only lets a
    verified account connect an alert channel (web/app.py). Enforcing it
    there, where channels are added, means the scanner needs no email
    configuration to respect it.

WHY DISCORD URLS ARE RESTRICTED: the server will later send HTTP
requests to whatever address is stored here. If any URL were accepted, a
user could point it at an internal address and make our server call it
for them. So only genuine Discord webhook URLs are accepted, and alert
delivery does not follow redirects.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional, Sequence

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from aegis_bioquant.config import settings
from aegis_bioquant.core.database import (
    AlertDeliveryRecord,
    AlertDestinationRecord,
    EmailTokenRecord,
    TelegramLinkTokenRecord,
    UserRecord,
    get_session_factory,
)

CHANNEL_TELEGRAM = "telegram"
CHANNEL_DISCORD = "discord"

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 200  # bounds the work a single hash request can demand
TELEGRAM_LINK_TTL_SECONDS = 15 * 60
PURPOSE_VERIFY = "verify"
PURPOSE_RESET = "reset"
VERIFY_TOKEN_TTL_SECONDS = 24 * 3600
RESET_TOKEN_TTL_SECONDS = 3600
# A destination that fails this many alerts in a row is switched off —
# typically a deleted Discord webhook or a user who blocked the bot.
MAX_CONSECUTIVE_FAILURES = 5

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DISCORD_WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+$"
)

_hasher = PasswordHasher()
# Verified against when the email doesn't exist, so a login attempt takes
# the same time whether or not the account exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class AccountError(ValueError):
    """Raised for input a user can fix. `code` is stable for the website
    to map to a message: INVALID_EMAIL | WEAK_PASSWORD | EMAIL_TAKEN |
    INVALID_DISCORD_WEBHOOK | TELEGRAM_NOT_CONFIGURED."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str) -> None:
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise AccountError(
            "WEAK_PASSWORD",
            f"Password must be {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters.",
        )


async def create_user(email: str, password: str) -> UserRecord:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise AccountError("INVALID_EMAIL", "That doesn't look like an email address.")
    validate_password(password)

    # Hashing is deliberately slow (that is what makes stolen hashes hard
    # to crack), so it runs in a worker thread rather than stalling every
    # other request the web server is handling.
    user = UserRecord(
        email=email,
        password_hash=await asyncio.to_thread(_hasher.hash, password),
        created_at=datetime.now(timezone.utc),
        subscription_status="none",
    )
    factory = get_session_factory()
    async with factory() as session:
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:  # unique email — also wins a sign-up race
            await session.rollback()
            raise AccountError("EMAIL_TAKEN", "An account with that email already exists.") from None
    return user


async def authenticate(email: str, password: str) -> Optional[UserRecord]:
    """Returns the user if the email and password match, otherwise None —
    deliberately without saying which of the two was wrong."""
    factory = get_session_factory()
    async with factory() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.email == normalize_email(email))
        )
        try:
            await asyncio.to_thread(
                _hasher.verify, user.password_hash if user else _DUMMY_HASH, password
            )
        except (VerificationError, InvalidHashError):
            return None
        if user is None:
            return None
        if _hasher.check_needs_rehash(user.password_hash):
            user.password_hash = await asyncio.to_thread(_hasher.hash, password)
            await session.commit()
        return user


async def get_user(user_id: int) -> Optional[UserRecord]:
    factory = get_session_factory()
    async with factory() as session:
        return await session.get(UserRecord, user_id)


async def get_user_by_email(email: str) -> Optional[UserRecord]:
    factory = get_session_factory()
    async with factory() as session:
        return await session.scalar(
            select(UserRecord).where(UserRecord.email == normalize_email(email))
        )


async def set_password(user_id: int, password: str) -> None:
    """Replaces the user's password. Ending their existing login sessions
    is the caller's job (web/sessions.py owns sessions)."""
    validate_password(password)
    new_hash = await asyncio.to_thread(_hasher.hash, password)
    factory = get_session_factory()
    async with factory() as session:
        user = await session.get(UserRecord, user_id)
        if user is not None:
            user.password_hash = new_hash
            await session.commit()


async def mark_email_verified(user_id: int) -> None:
    factory = get_session_factory()
    async with factory() as session:
        user = await session.get(UserRecord, user_id)
        if user is not None and user.email_verified_at is None:
            user.email_verified_at = datetime.now(timezone.utc)
            await session.commit()


# --------------------------------------------------------------------------
# Emailed links (verify address / reset password)
# --------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_email_token(user_id: int, purpose: str) -> str:
    """
    Returns a fresh single-use token to put in an emailed link. Issuing a
    new one cancels the user's earlier tokens of the same purpose, so
    only the most recent email's link works.
    """
    ttl = VERIFY_TOKEN_TTL_SECONDS if purpose == PURPOSE_VERIFY else RESET_TOKEN_TTL_SECONDS
    token = secrets.token_urlsafe(32)
    now = time.time()
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            delete(EmailTokenRecord).where(
                ((EmailTokenRecord.user_id == user_id) & (EmailTokenRecord.purpose == purpose))
                | (EmailTokenRecord.expires_at <= now)
            )
        )
        session.add(
            EmailTokenRecord(
                token_hash=_token_hash(token), user_id=user_id, purpose=purpose, expires_at=now + ttl
            )
        )
        await session.commit()
    return token


async def peek_email_token(token: str, purpose: str) -> Optional[int]:
    """The user a still-valid token belongs to, WITHOUT using it up — for
    showing the reset form before the new password is submitted."""
    if not token:
        return None
    factory = get_session_factory()
    async with factory() as session:
        return await session.scalar(
            select(EmailTokenRecord.user_id).where(
                EmailTokenRecord.token_hash == _token_hash(token),
                EmailTokenRecord.purpose == purpose,
                EmailTokenRecord.expires_at > time.time(),
            )
        )


async def consume_email_token(token: str, purpose: str) -> Optional[int]:
    """Uses the token up and returns its user, or None if it is unknown,
    expired, for a different purpose, or already used."""
    if not token:
        return None
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            delete(EmailTokenRecord)
            .where(
                EmailTokenRecord.token_hash == _token_hash(token),
                EmailTokenRecord.purpose == purpose,
                EmailTokenRecord.expires_at > time.time(),
            )
            .returning(EmailTokenRecord.user_id)
        )
        user_id = result.scalar()
        await session.commit()
    return user_id


# --------------------------------------------------------------------------
# Destinations
# --------------------------------------------------------------------------

async def _set_destination(user_id: int, channel: str, address: str) -> None:
    """Creates or replaces the user's destination for one channel, and
    re-enables it — saving a new address is how a switched-off
    destination is brought back."""
    factory = get_session_factory()
    async with factory() as session:
        existing = await session.scalar(
            select(AlertDestinationRecord).where(
                AlertDestinationRecord.user_id == user_id,
                AlertDestinationRecord.channel == channel,
            )
        )
        if existing:
            existing.address = address
            existing.enabled = True
            existing.consecutive_failures = 0
        else:
            session.add(
                AlertDestinationRecord(
                    user_id=user_id,
                    channel=channel,
                    address=address,
                    enabled=True,
                    consecutive_failures=0,
                    created_at=datetime.now(timezone.utc),
                )
            )
        await session.commit()


async def set_discord_webhook(user_id: int, url: str) -> None:
    url = url.strip().split("?", 1)[0]
    if not _DISCORD_WEBHOOK_RE.match(url):
        raise AccountError(
            "INVALID_DISCORD_WEBHOOK",
            "That isn't a Discord webhook URL. It should start with "
            "https://discord.com/api/webhooks/",
        )
    await _set_destination(user_id, CHANNEL_DISCORD, url)


async def remove_destination(user_id: int, channel: str) -> bool:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            delete(AlertDestinationRecord).where(
                AlertDestinationRecord.user_id == user_id,
                AlertDestinationRecord.channel == channel,
            )
        )
        await session.commit()
        return result.rowcount > 0


async def get_destinations(user_id: int) -> list[AlertDestinationRecord]:
    """All of one user's destinations, including switched-off ones — for
    their settings page."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(AlertDestinationRecord)
            .where(AlertDestinationRecord.user_id == user_id)
            .order_by(AlertDestinationRecord.channel)
        )
        return list(rows)


# --------------------------------------------------------------------------
# Telegram linking
# --------------------------------------------------------------------------

async def create_telegram_link(user_id: int) -> str:
    """
    Returns the link the user opens to connect Telegram. It carries a
    single-use code valid for TELEGRAM_LINK_TTL_SECONDS; pressing Start
    sends that code to the bot, which is how handle_telegram_update()
    learns which chat belongs to which account.
    """
    if not settings.TELEGRAM_BOT_USERNAME:
        raise AccountError("TELEGRAM_NOT_CONFIGURED", "Telegram alerts aren't available yet.")

    token = secrets.token_urlsafe(24)  # 32 chars of [A-Za-z0-9_-]: valid as a Telegram start parameter
    now = time.time()
    factory = get_session_factory()
    async with factory() as session:
        # One live code per user; also clears out everyone's expired ones.
        await session.execute(
            delete(TelegramLinkTokenRecord).where(
                (TelegramLinkTokenRecord.user_id == user_id) | (TelegramLinkTokenRecord.expires_at <= now)
            )
        )
        session.add(
            TelegramLinkTokenRecord(token=token, user_id=user_id, expires_at=now + TELEGRAM_LINK_TTL_SECONDS)
        )
        await session.commit()
    return f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={token}"


@dataclass
class TelegramReply:
    chat_id: str
    text: str


async def handle_telegram_update(update: dict) -> Optional[TelegramReply]:
    """
    Processes one update from Telegram (the JSON Telegram posts to a
    bot's webhook). Returns the message to send back to that chat, or
    None if the update is nothing this bot acts on. Does not send
    anything itself, and trusts that `update` really came from Telegram —
    the webhook route that calls this must check Telegram's secret-token
    header first.

    Handles:  /start <code>  -> link this chat to the code's account
              /stop          -> stop sending alerts to this chat
    """
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return None
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")
    if chat_id is None or not isinstance(text, str):
        return None
    chat_id = str(chat_id)

    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower() if parts else ""  # "/start@BotName" in groups

    if command == "/stop":
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                delete(AlertDestinationRecord).where(
                    AlertDestinationRecord.channel == CHANNEL_TELEGRAM,
                    AlertDestinationRecord.address == chat_id,
                )
            )
            await session.commit()
        if result.rowcount:
            return TelegramReply(chat_id, "Alerts to this chat are off. You can reconnect any time from Alert settings on the website.")
        return TelegramReply(chat_id, "This chat isn't connected to an account.")

    if command != "/start":
        return None

    not_linked = TelegramReply(
        chat_id,
        "That link has expired or was already used. Open Alert settings on the website and press Connect Telegram again.",
    )
    if len(parts) < 2:
        return TelegramReply(chat_id, "To get alerts here, sign in on the FDAOdds website, open Alert settings and press Connect Telegram.")

    factory = get_session_factory()
    async with factory() as session:
        # Delete-then-check makes the code single-use even if Telegram
        # delivers the same update twice.
        result = await session.execute(
            delete(TelegramLinkTokenRecord)
            .where(
                TelegramLinkTokenRecord.token == parts[1].strip(),
                TelegramLinkTokenRecord.expires_at > time.time(),
            )
            .returning(TelegramLinkTokenRecord.user_id)
        )
        user_id = result.scalar()
        await session.commit()
    if user_id is None:
        return not_linked

    await _set_destination(user_id, CHANNEL_TELEGRAM, chat_id)
    return TelegramReply(chat_id, "Connected. FDAOdds alerts will arrive in this chat. Send /stop to turn them off.")


# --------------------------------------------------------------------------
# Alert delivery support (used by telemetry/alerts.py)
# --------------------------------------------------------------------------

@dataclass
class Destination:
    id: int
    user_id: int
    channel: str
    address: str


async def get_active_destinations() -> list[Destination]:
    """Every destination an alert should be fanned out to right now."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(AlertDestinationRecord)
            .where(AlertDestinationRecord.enabled.is_(True))
            .order_by(AlertDestinationRecord.id)
        )
        return [Destination(r.id, r.user_id, r.channel, r.address) for r in rows]


async def record_deliveries(
    outcomes: Sequence,  # telemetry.alerts.DispatchOutcome with destination_id set
    ticker: str,
    arbitrage_signal: str,
    catalyst_date: date,
) -> int:
    """
    Logs each per-user delivery attempt and updates each destination's
    failure streak: reset on success, +1 on failure, switched off at
    MAX_CONSECUTIVE_FAILURES. Returns how many destinations this call
    switched off.
    """
    if not outcomes:
        return 0
    now = datetime.now(timezone.utc)
    disabled = 0
    factory = get_session_factory()
    async with factory() as session:
        for o in outcomes:
            session.add(
                AlertDeliveryRecord(
                    user_id=o.user_id,
                    channel=o.channel,
                    ticker=ticker,
                    arbitrage_signal=arbitrage_signal,
                    catalyst_date=catalyst_date,
                    ok=o.ok,
                    detail=o.detail,
                    sent_at=now,
                )
            )
            dest = await session.get(AlertDestinationRecord, o.destination_id)
            if dest is None:  # removed while the alert was going out
                continue
            if o.ok:
                dest.consecutive_failures = 0
            else:
                dest.consecutive_failures += 1
                if dest.enabled and dest.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    dest.enabled = False
                    disabled += 1
        await session.commit()
    return disabled
