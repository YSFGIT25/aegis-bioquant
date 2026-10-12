"""
core/cache.py
--------------
Pipeline lockouts (per-ticker processing locks).

STATUS: PARTIAL — lockouts are built and tested. The manual's "task
queuing" half of this file is NOT built: nothing in the system queues
tasks yet, so there is nothing real to design it against.

ENVIRONMENT NOTE, same as core/database.py: the manual specifies Redis.
There is no Redis server in this dev environment to run or test against,
so this implements the same contract Redis lockouts give —
    acquire  = SET key token NX EX ttl   (only one holder, auto-expires)
    release  = delete only if the token still matches
— on top of the pipeline_locks table in the existing database instead.

WHAT THAT DOES AND DOESN'T GIVE YOU, stated plainly:
  - Real mutual exclusion across separate processes, as long as they
    share one database: acquisition is a primary-key INSERT, so the
    database itself guarantees a single winner (verified with concurrent
    processes racing for one key).
  - On the default SQLite file that means "processes on this machine".
    Pointed at a shared Postgres it holds across machines too.
  - It is slower than Redis (a disk transaction per acquire). Irrelevant
    at this system's scale — a scan locks a handful of tickers.
  - Expiry relies on each machine's wall clock, so across machines it is
    only as accurate as their clock sync.

WHY THE TTL EXISTS: if a run crashes (or the laptop dies) while holding
a lock, nothing ever releases it. The TTL makes the lock expire on its
own so the ticker isn't locked out forever. It must be comfortably
longer than one candidate's pipeline run — a lock that expires mid-run
can be taken by a second run while the first is still working.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from aegis_bioquant.config import settings
from aegis_bioquant.core.database import PipelineLockRecord, get_session_factory


def pipeline_lock_key(ticker: str) -> str:
    return f"pipeline:{ticker.upper()}"


async def acquire_lock(key: str, ttl_seconds: Optional[float] = None) -> Optional[str]:
    """
    Tries to take the lock. Returns an ownership token on success, or
    None if someone else holds an unexpired lock on this key. Never
    waits — a held lock means "skip this one", not "queue behind it".
    Requires init_db() to have been called.
    """
    ttl = settings.PIPELINE_LOCK_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    now = time.time()
    token = uuid.uuid4().hex

    factory = get_session_factory()
    async with factory() as session:
        # Clear an expired holder first, so a crashed run's lock is taken
        # over here rather than blocking forever.
        await session.execute(
            delete(PipelineLockRecord).where(
                PipelineLockRecord.key == key, PipelineLockRecord.expires_at <= now
            )
        )
        session.add(
            PipelineLockRecord(key=key, token=token, acquired_at=now, expires_at=now + ttl)
        )
        try:
            await session.commit()
        except IntegrityError:  # primary key taken — a live holder exists
            await session.rollback()
            return None
    return token


async def release_lock(key: str, token: str) -> bool:
    """
    Releases the lock only if `token` still owns it. Returns False if it
    doesn't — i.e. this holder's TTL ran out and someone else took over;
    their lock is left untouched.
    """
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            delete(PipelineLockRecord).where(
                PipelineLockRecord.key == key, PipelineLockRecord.token == token
            )
        )
        await session.commit()
        return result.rowcount == 1


async def is_locked(key: str) -> bool:
    factory = get_session_factory()
    async with factory() as session:
        holder = await session.scalar(
            select(PipelineLockRecord.key).where(
                PipelineLockRecord.key == key, PipelineLockRecord.expires_at > time.time()
            )
        )
        return holder is not None


@asynccontextmanager
async def pipeline_lockout(key: str, ttl_seconds: Optional[float] = None) -> AsyncIterator[bool]:
    """
    `async with pipeline_lockout(key) as acquired:` — yields whether the
    lock was obtained (the body must check it) and releases on exit,
    including when the body raises.
    """
    token = await acquire_lock(key, ttl_seconds)
    try:
        yield token is not None
    finally:
        if token is not None:
            await release_lock(key, token)
