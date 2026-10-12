"""
web/sessions.py
----------------
Login sessions and failed-login throttling for the hosted site.

A session is a random token held in the browser's cookie. The database
stores only the token's SHA-256, so a copy of the sessions table is not
a set of working logins. Each session also carries a second random
value, the CSRF token, which every form on a logged-in page must send
back — it proves the request came from one of our own pages.

Throttling is per email address: after MAX_LOGIN_FAILURES wrong
passwords inside LOGIN_WINDOW_SECONDS, further attempts for that email
are refused until the window passes, right password or not. It limits
password guessing against one account; it does not stop someone trying
one password against many accounts (that needs per-IP limits, which
depend on how the host forwards client addresses — not built).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import delete, select

from aegis_bioquant.core.accounts import normalize_email
from aegis_bioquant.core.database import (
    LoginThrottleRecord,
    SessionRecord,
    UserRecord,
    get_session_factory,
)

SESSION_TTL_SECONDS = 30 * 24 * 3600
MAX_LOGIN_FAILURES = 8
LOGIN_WINDOW_SECONDS = 15 * 60


@dataclass
class Session:
    user_id: int
    email: str
    csrf_token: str
    email_verified: bool


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(user_id: int) -> str:
    """Starts a session and returns the token to put in the cookie."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    factory = get_session_factory()
    async with factory() as db:
        await db.execute(delete(SessionRecord).where(SessionRecord.expires_at <= now))
        db.add(
            SessionRecord(
                token_hash=_hash(token),
                user_id=user_id,
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now + SESSION_TTL_SECONDS,
            )
        )
        await db.commit()
    return token


async def get_session(token: Optional[str]) -> Optional[Session]:
    if not token:
        return None
    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(
                select(SessionRecord, UserRecord)
                .join(UserRecord, UserRecord.id == SessionRecord.user_id)
                .where(
                    SessionRecord.token_hash == _hash(token),
                    SessionRecord.expires_at > time.time(),
                )
            )
        ).first()
    if row is None:
        return None
    session, user = row
    return Session(
        user_id=user.id,
        email=user.email,
        csrf_token=session.csrf_token,
        email_verified=user.email_verified_at is not None,
    )


async def delete_session(token: Optional[str]) -> None:
    if not token:
        return
    factory = get_session_factory()
    async with factory() as db:
        await db.execute(delete(SessionRecord).where(SessionRecord.token_hash == _hash(token)))
        await db.commit()


async def delete_sessions_for_user(user_id: int) -> None:
    """Logs the user out everywhere — used after a password reset, so
    whoever knew the old password doesn't keep a working login."""
    factory = get_session_factory()
    async with factory() as db:
        await db.execute(delete(SessionRecord).where(SessionRecord.user_id == user_id))
        await db.commit()


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

async def allow_action(key: str, limit: int, window_seconds: float) -> bool:
    """
    Counts one attempt against `key` and returns whether it is within
    `limit` per `window_seconds`. Used to cap how many emails one account
    or address can trigger — without it, the "forgot password" form would
    let anyone make this site send mail to any address as often as they
    liked. Shares the login-throttle table; keys are prefixed to keep
    them apart from plain email keys.
    """
    now = time.time()
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(LoginThrottleRecord, key)
        if row is None:
            db.add(LoginThrottleRecord(key=key, failures=1, window_start=now))
            allowed = True
        elif now - row.window_start > window_seconds:
            row.failures, row.window_start = 1, now
            allowed = True
        else:
            row.failures += 1
            allowed = row.failures <= limit
        await db.commit()
    return allowed


# --------------------------------------------------------------------------
# Failed-login throttling
# --------------------------------------------------------------------------

async def login_allowed(email: str) -> bool:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(LoginThrottleRecord, normalize_email(email))
    if row is None or time.time() - row.window_start > LOGIN_WINDOW_SECONDS:
        return True
    return row.failures < MAX_LOGIN_FAILURES


async def record_login_failure(email: str) -> None:
    key, now = normalize_email(email), time.time()
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(LoginThrottleRecord, key)
        if row is None:
            db.add(LoginThrottleRecord(key=key, failures=1, window_start=now))
        elif now - row.window_start > LOGIN_WINDOW_SECONDS:
            row.failures, row.window_start = 1, now
        else:
            row.failures += 1
        await db.commit()


async def clear_login_failures(email: str) -> None:
    factory = get_session_factory()
    async with factory() as db:
        await db.execute(delete(LoginThrottleRecord).where(LoginThrottleRecord.key == normalize_email(email)))
        await db.commit()
