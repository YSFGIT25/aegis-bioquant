"""
core/outcome_review.py
-----------------------
Confirming FDA outcomes from Telegram, and the announcement time each one
needs.

SWITCHED OFF FOR THE STUDY SINCE 2026-10-25: outcomes are now recorded
automatically when the SEC filing and the FDA's own records agree
(core/auto_confirm.py). While settings.OUTCOMES_AUTOMATIC is on, the
buttons and /announced below refuse, and no buttons are sent. The SEC
acceptance-time lookup is still used, by auto_confirm.

BEFORE THAT, THE REVIEWER DECIDED. The pre-registered method was that an outcome
counts only once the reviewer has read the filing and confirmed it. This
module makes that a tap instead of a website visit: the daily status
message (telemetry/status.py) shows each outcome the finder has suggested
with the sentence it was read from and the filing link, and two buttons.
Only the operator's own Telegram account (settings.STATUS_TELEGRAM_CHAT_ID)
can use them; Telegram's webhook secret is checked by the web route before
anything here runs.

THE ANNOUNCEMENT TIME. The analysis's primary score is the last one stored
at least 24 hours before the decision's FIRST public announcement: the
earlier of the company's press release and its SEC filing. The SEC's
acceptance time for the filing is looked up automatically (EDGAR's
-index-headers.html, in US Eastern time) and offered with the outcome. A
press release can come out before the filing, so the button says "no
earlier press release"; if there was one, the reviewer sends

    /announced TICKER YYYY-MM-DD HH:MM [link]      (time in UTC)

Times are stored on the outcome row (announcement_source REVIEWER once
the reviewer has accepted or entered them), copied into each weekly OSF
snapshot, and written into analysis/announcements.csv by
analysis/export_announcements.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db

SEC_SOURCE, REVIEWER_SOURCE = "SEC_ACCEPTANCE", "REVIEWER"
_CODES = {"APPROVED": "A", "CRL": "C", "DELAYED": "D", "WITHDRAWN": "W", "INVALID": "I"}
_ARCHIVE_RE = re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/(\d+)/(\d{18})/")
_ACCEPTED_RE = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})")
_EASTERN = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------
# The SEC acceptance time
# --------------------------------------------------------------------------

def index_headers_url(filing_url: str) -> Optional[str]:
    """The EDGAR header file for the filing a document belongs to, or None
    if the address is not an EDGAR archive document."""
    m = _ARCHIVE_RE.match(filing_url or "")
    if not m:
        return None
    cik, acc = m.groups()
    dashed = f"{acc[:10]}-{acc[10:12]}-{acc[12:]}"
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{dashed}-index-headers.html"


def parse_acceptance(header_text: str) -> Optional[datetime]:
    """EDGAR's ACCEPTANCE-DATETIME (US Eastern, YYYYMMDDHHMMSS) in UTC."""
    m = _ACCEPTED_RE.search(header_text)
    if not m:
        return None
    local = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=_EASTERN)
    return local.astimezone(timezone.utc)


async def sec_acceptance_time(client: httpx.AsyncClient, filing_url: str) -> Optional[datetime]:
    url = index_headers_url(filing_url)
    if url is None:
        return None
    from aegis_bioquant.ingestion.fda_scraper import _get_with_retry

    try:
        resp = await _get_with_retry(client, url)
    except httpx.HTTPError:
        return None
    return parse_acceptance(resp.text) if resp.status_code == 200 else None


async def fill_acceptance_times() -> int:
    """Looks up the SEC acceptance time for every SUGGESTED or CONFIRMED
    outcome that has a filing link and no announcement time yet. Returns
    how many were filled. Never overwrites a time already stored."""
    from aegis_bioquant.ingestion.fda_scraper import _build_user_agent

    filled = 0
    factory = db.get_session_factory()
    async with factory() as session:
        rows = list(await session.scalars(select(db.CatalystOutcomeRecord).where(
            db.CatalystOutcomeRecord.status.in_(("SUGGESTED", "CONFIRMED")),
            db.CatalystOutcomeRecord.outcome.in_(("APPROVED", "CRL")),
            db.CatalystOutcomeRecord.announced_at.is_(None),
            db.CatalystOutcomeRecord.evidence_url.is_not(None),
        )))
        if not rows:
            return 0
        async with httpx.AsyncClient(headers={"User-Agent": _build_user_agent()}, timeout=30.0) as client:
            for row in rows:
                when = await sec_acceptance_time(client, row.evidence_url)
                if when is not None:
                    row.announced_at, row.announcement_url, row.announcement_source = when, row.evidence_url, SEC_SOURCE
                    filled += 1
        await session.commit()
    return filled


# --------------------------------------------------------------------------
# What the reviewer is shown
# --------------------------------------------------------------------------

@dataclass
class Pending:
    text: str
    buttons: list[list[dict]]


def _when(moment: Optional[datetime]) -> str:
    if moment is None:
        return "not found"
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return f"{moment:%Y-%m-%d %H:%M} UTC"


async def pending_reviews() -> list[Pending]:
    """One message per outcome needing the reviewer: suggestions to
    confirm, and confirmed decisions whose announcement time hasn't been
    accepted yet."""
    factory = db.get_session_factory()
    async with factory() as session:
        rows = list(await session.scalars(select(db.CatalystOutcomeRecord).where(
            db.CatalystOutcomeRecord.status.in_(("SUGGESTED", "CONFIRMED"))
        ).order_by(db.CatalystOutcomeRecord.catalyst_date)))
    out = []
    for r in rows:
        code = _CODES.get(r.outcome or "", "?")
        if r.status == "SUGGESTED":
            lines = [f"{r.ticker}, FDA date {r.catalyst_date}: suggested {r.outcome} (decided {r.decision_date})",
                     f"Read from: \"{(r.evidence_text or '')[:300]}\""]
            if r.evidence_url:
                lines.append(r.evidence_url)
            buttons = []
            if r.outcome in ("APPROVED", "CRL"):
                lines.append(f"SEC filing accepted: {_when(r.announced_at)}")
                label = f"Confirm {r.outcome}" + (", no earlier press release" if r.announced_at else "")
            else:
                label = f"Confirm {r.outcome}"
            buttons.append([{"text": label, "callback_data": f"ok:{r.id}:{code}"}])
            buttons.append([{"text": "Not right (fix on the website)", "callback_data": f"no:{r.id}:{code}"}])
            lines.append("Read the filing first: it must be about this drug and this application.")
            out.append(Pending("\n".join(lines), buttons))
        elif r.outcome in ("APPROVED", "CRL") and r.announcement_source != REVIEWER_SOURCE:
            lines = [f"{r.ticker}, FDA date {r.catalyst_date}: CONFIRMED {r.outcome}, announcement time not checked yet",
                     f"SEC filing accepted: {_when(r.announced_at)}"]
            if r.announcement_url or r.evidence_url:
                lines.append(r.announcement_url or r.evidence_url)
            lines.append(f"If a press release came out earlier, send: /announced {r.ticker} YYYY-MM-DD HH:MM (UTC)")
            buttons = [[{"text": "Use the SEC time, no earlier press release", "callback_data": f"at:{r.id}:{code}"}]] \
                if r.announced_at else []
            out.append(Pending("\n".join(lines), buttons))
    return out


# --------------------------------------------------------------------------
# Acting on the reviewer's taps and commands (called by the web route)
# --------------------------------------------------------------------------

def is_operator(chat_or_user_id) -> bool:
    return bool(settings.STATUS_TELEGRAM_CHAT_ID) and str(chat_or_user_id) == str(settings.STATUS_TELEGRAM_CHAT_ID)


async def _reviewer_user_id() -> Optional[int]:
    from aegis_bioquant.core.accounts import get_user_by_email

    for email in sorted(settings.ADMIN_EMAILS):
        user = await get_user_by_email(email)
        if user is not None:
            return user.id
    return None


AUTOMATIC_REPLY = ("Outcomes are recorded automatically now, when the SEC filing and the FDA's records agree. "
                   "Nothing to confirm.")


async def handle_button(data: str) -> str:
    """Acts on one button's callback data; returns the reply text."""
    if settings.OUTCOMES_AUTOMATIC:
        return AUTOMATIC_REPLY
    try:
        action, row_id, code = data.split(":")
        row_id = int(row_id)
    except ValueError:
        return "That button is not recognised."
    factory = db.get_session_factory()
    async with factory() as session:
        row = await session.get(db.CatalystOutcomeRecord, row_id)
    if row is None or _CODES.get(row.outcome or "") != code:
        return "That suggestion has changed since this message was sent. Check the website's outcomes page."

    if action == "no":
        return (f"Left unconfirmed: {row.ticker} {row.catalyst_date}. Record the right outcome on the website "
                f"(Review > Outcomes).")
    if action == "ok":
        if row.status == "CONFIRMED":
            return f"Already confirmed: {row.ticker} {row.catalyst_date} {row.outcome}."
        if row.status != "SUGGESTED" or row.decision_date is None:
            return "That suggestion can't be confirmed from here. Use the website's outcomes page."
        await db.confirm_outcome(row.ticker, row.event_type, row.catalyst_date, row.outcome, row.decision_date,
                                 confirmed_by=await _reviewer_user_id())
        reply = f"Confirmed: {row.ticker} {row.catalyst_date} {row.outcome}, decided {row.decision_date}."
        if row.outcome in ("APPROVED", "CRL") and row.announced_at is not None:
            await set_announcement(row.id, row.announced_at, row.announcement_url or row.evidence_url)
            reply += f" Announcement time recorded: {_when(row.announced_at)} (SEC acceptance)."
        elif row.outcome in ("APPROVED", "CRL"):
            reply += (f" No SEC time was found: send /announced {row.ticker} YYYY-MM-DD HH:MM (UTC) "
                      f"with the press release or filing time.")
        return reply
    if action == "at":
        if row.announced_at is None:
            return "No SEC time is stored for that decision."
        await set_announcement(row.id, row.announced_at, row.announcement_url or row.evidence_url)
        return f"Announcement time recorded: {row.ticker} {_when(row.announced_at)}."
    return "That button is not recognised."


async def set_announcement(row_id: int, when: datetime, url: Optional[str]) -> None:
    factory = db.get_session_factory()
    async with factory() as session:
        row = await session.get(db.CatalystOutcomeRecord, row_id)
        if row is not None:
            row.announced_at = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            row.announcement_url, row.announcement_source = url, REVIEWER_SOURCE
            await session.commit()


_ANNOUNCED_RE = re.compile(r"^/announced(?:@\w+)?\s+([A-Za-z.]{1,6})\s+(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})(?:\s*(?:UTC|Z))?(?:\s+(https://\S+))?\s*$", re.I)


async def handle_announced(text: str) -> str:
    """/announced TICKER YYYY-MM-DD HH:MM [link] — the reviewer's own time,
    in UTC, for that ticker's most recent APPROVED/CRL outcome."""
    if settings.OUTCOMES_AUTOMATIC:
        return AUTOMATIC_REPLY
    m = _ANNOUNCED_RE.match(text.strip())
    if not m:
        return "Format: /announced TICKER YYYY-MM-DD HH:MM [link], with the time in UTC. Example: /announced IONS 2026-10-26 20:01"
    ticker, day, clock, url = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
    try:
        when = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return "That date or time is not valid."
    factory = db.get_session_factory()
    async with factory() as session:
        row = await session.scalar(select(db.CatalystOutcomeRecord).where(
            db.CatalystOutcomeRecord.ticker == ticker,
            db.CatalystOutcomeRecord.status.in_(("SUGGESTED", "CONFIRMED")),
            db.CatalystOutcomeRecord.outcome.in_(("APPROVED", "CRL")),
        ).order_by(db.CatalystOutcomeRecord.catalyst_date.desc()))
    if row is None:
        return f"No APPROVED or CRL outcome is on record for {ticker}."
    await set_announcement(row.id, when, url or row.announcement_url or row.evidence_url)
    note = "" if row.status == "CONFIRMED" else " The outcome itself is still waiting for you to confirm."
    return f"Announcement time recorded: {ticker} (FDA date {row.catalyst_date}) {_when(when)}.{note}"
