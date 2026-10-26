"""
web/visits.py
--------------
A visitor count of the site's own, with no cookies and no outside
tracker (the owner asked for real numbers on 2026-10-26; the request
logs could only give rough ones).

HOW A VISIT IS COUNTED: every page loads /static/v.js, a few lines that
send the page's path and the address the visitor came from to POST /v.
Most bots and scanners never run a page's script, so they never report a
visit: the logs had shown about 2,600 bot or probe requests against some
30 real visitors in the site's first two weeks. Visits by the site's
administrators are not counted, and neither are user agents that say
they are bots.

WHAT IS STORED, per page view: the day, the page, the website the
visitor came from (its host name only, e.g. "t.co"), any utm_source on
the link, whether the visitor was logged in, and a visitor code. The
code is a keyed hash of the visitor's IP address and browser, under a
key that changes every day and is deleted the next day: it tells two
visits on the same day apart from one visitor's two visits, and after
that day it can't be linked back to an address or to the same visitor
on another day. No IP address, full URL or cookie is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Boolean, Date, DateTime, Integer, String, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from aegis_bioquant.core.database import Base, UserRecord, get_session_factory

# The pages a visit is counted on. Anything else reported is ignored.
PAGES = ("/", "/signup", "/login", "/dashboard", "/settings", "/forgot")
_BOT_UA = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|monitor|uptime|curl|python|httpx|wget|java/|go-http",
                     re.I)
_SOURCE_RE = re.compile(r"[^a-z0-9._-]")
MAX_BODY = 2048

SCRIPT = (
    "(function(){try{var d=JSON.stringify({p:location.pathname,r:document.referrer||'',q:location.search});"
    "if(navigator.sendBeacon){navigator.sendBeacon('/v',new Blob([d],{type:'text/plain'}));}"
    "else{fetch('/v',{method:'POST',body:d,keepalive:true});}}catch(e){}})();"
)


class VisitRecord(Base):
    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, index=True)              # UTC
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visitor: Mapped[str] = mapped_column(String)                     # daily keyed hash, see module docstring
    path: Mapped[str] = mapped_column(String)
    referrer: Mapped[str] = mapped_column(String)                    # host name, "direct" or "internal"
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)   # utm_source, if the link had one
    member: Mapped[bool] = mapped_column(Boolean)                    # logged in


class VisitKeyRecord(Base):
    """Today's key for the visitor code. Older keys are deleted."""

    __tablename__ = "visit_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, unique=True)
    key: Mapped[str] = mapped_column(String)


async def _key_for(day: date) -> str:
    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(select(VisitKeyRecord).where(VisitKeyRecord.day == day))
        if row is not None:
            return row.key
        await session.execute(delete(VisitKeyRecord).where(VisitKeyRecord.day < day))
        session.add(VisitKeyRecord(day=day, key=secrets.token_hex(32)))
        try:
            await session.commit()
        except IntegrityError:   # another request made today's key first
            await session.rollback()
        row = await session.scalar(select(VisitKeyRecord).where(VisitKeyRecord.day == day))
        return row.key


def referrer_label(referrer: str, own_host: str) -> str:
    host = (urlsplit(referrer).hostname or "").lower() if referrer else ""
    if not host:
        return "direct"
    if host == own_host.lower() or host.removeprefix("www.") == own_host.lower().removeprefix("www."):
        return "internal"
    return host.removeprefix("www.")[:100]


def parse_report(body: bytes) -> Optional[tuple[str, str, Optional[str]]]:
    """(page, referrer, utm_source) from the script's report, or None if
    it isn't one worth counting."""
    if not body or len(body) > MAX_BODY:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    path = data.get("p")
    if path not in PAGES:
        return None
    referrer = data.get("r") if isinstance(data.get("r"), str) else ""
    query = data.get("q") if isinstance(data.get("q"), str) else ""
    source = (parse_qs(query.lstrip("?")).get("utm_source") or [""])[0]
    source = _SOURCE_RE.sub("", source.lower())[:40] or None
    return path, referrer[:500], source


async def record(body: bytes, *, ip: str, user_agent: str, own_host: str, member: bool, admin: bool,
                 now: Optional[datetime] = None) -> bool:
    """Stores one page view. Returns whether it was counted."""
    report = parse_report(body)
    if report is None or admin or not user_agent or _BOT_UA.search(user_agent):
        return False
    path, referrer, source = report
    now = now or datetime.now(timezone.utc)
    key = await _key_for(now.date())
    visitor = hmac.new(key.encode(), f"{ip}|{user_agent}".encode(), hashlib.sha256).hexdigest()[:20]
    factory = get_session_factory()
    async with factory() as session:
        session.add(VisitRecord(day=now.date(), at=now, visitor=visitor, path=path,
                                referrer=referrer_label(referrer, own_host), source=source, member=member))
        await session.commit()
    return True


async def summary(today: date, days: int = 30) -> dict:
    """What the private Visitors page shows: per day, and the top places
    visitors came from, over the last `days` days."""
    start = today - timedelta(days=days - 1)
    factory = get_session_factory()
    async with factory() as session:
        rows = list(await session.scalars(select(VisitRecord).where(VisitRecord.day >= start)))
        users = list(await session.scalars(select(UserRecord.created_at)))
        first = await session.scalar(select(VisitRecord.day).order_by(VisitRecord.day).limit(1))
    signups = Counter(u.date() for u in users if u and u.date() >= start)
    per_day: dict[date, dict] = defaultdict(lambda: {"visitors": set(), "views": 0})
    came_from: dict[str, set] = defaultdict(set)
    sources: dict[str, set] = defaultdict(set)
    pages: dict[str, set] = defaultdict(set)
    for r in rows:
        per_day[r.day]["visitors"].add(r.visitor)
        per_day[r.day]["views"] += 1
        who = (r.day, r.visitor)
        if r.referrer != "internal":
            came_from[r.referrer].add(who)
        if r.source:
            sources[r.source].add(who)
        pages[r.path].add(who)
    table = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        entry = per_day.get(day)
        table.append({"day": day, "visitors": len(entry["visitors"]) if entry else 0,
                      "views": entry["views"] if entry else 0, "signups": signups.get(day, 0)})

    def top(groups: dict[str, set]) -> list[tuple[str, int]]:
        return sorted(((name, len(who)) for name, who in groups.items()), key=lambda x: (-x[1], x[0]))[:12]

    return {
        "days": table,
        "visitors": sum(d["visitors"] for d in table),
        "views": sum(d["views"] for d in table),
        "signups": sum(d["signups"] for d in table),
        "came_from": top(came_from), "sources": top(sources), "pages": top(pages),
        "counting_since": first, "period": days,
    }
