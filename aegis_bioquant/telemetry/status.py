"""
telemetry/status.py
--------------------
The operator's daily scan report: one short Telegram message after every
scheduled scan, saying whether it worked.

WHY: a scan that fails, an API account that runs out of credit (seen
live: 24 evidence reviews failed in a row) or a catalyst source that goes
dark otherwise shows up only as a gap in the record, found weeks later.
For a study whose evidence is one stored score per day, that gap is the
costly part. The message also lists FDA decisions the outcome finder has
suggested and that are waiting for the reviewer to confirm, with the
reminder to record the announcement time the analysis needs.

Sent with the bot token (settings.TELEGRAM_BOT_TOKEN) to
settings.STATUS_TELEGRAM_CHAT_ID only, never to customers. Off when either
is unset. Failures are swallowed: a report must never break the scheduler.
"""

from __future__ import annotations

import re
from typing import Optional

import httpx

from aegis_bioquant.config import settings

_COUNTS = (
    ("scored", re.compile(r"^Scored: (\d+)")),
    ("errors", re.compile(r"^Errors: (\d+)")),
    ("catalysts", re.compile(r"^Total catalysts scanned: (\d+)")),
)
_REVIEWS = re.compile(r"^Claude evidence reviews: (\d+) written, (\d+) with nothing to review, (\d+) failed")
_MAX_ERROR_LINES = 6


def summarize(lines: list[str]) -> dict:
    """The numbers that matter from a scan's printed report."""
    found: dict = {"market_closed": False, "error_lines": [], "rules_changed": False}
    in_errors = False
    for raw in lines:
        line = raw.rstrip()
        for key, pattern in _COUNTS:
            m = pattern.match(line)
            if m:
                found[key] = int(m.group(1))
        m = _REVIEWS.match(line)
        if m:
            found["reviews"] = tuple(int(x) for x in m.groups())
        if line.startswith("US MARKET CLOSED"):
            found["market_closed"] = True
        if line.startswith("-- Errors --"):
            in_errors = True
            continue
        if in_errors and line.startswith("  "):
            found["error_lines"].append(line.strip())
            if line.strip().startswith("RULES:"):
                found["rules_changed"] = True
        elif in_errors and line and not line.startswith("  "):
            in_errors = False
    return found


def format_report(ok: bool, retried: bool, summary: dict, awaiting: list[str], snapshot: Optional[str]) -> str:
    head = "FDAOdds scan OK" if ok else "FDAOdds scan FAILED"
    if retried:
        head += " (after one retry)" if ok else " (also on retry; next try at the next scheduled scan)"
    lines = [head]
    if summary.get("market_closed"):
        lines.append("Market closed: catalyst list refreshed, nothing scored (normal on weekends).")
    elif "scored" in summary:
        lines.append(f"Scored {summary['scored']} upcoming decision(s) of {summary.get('catalysts', '?')} catalysts found.")
    if "reviews" in summary:
        written, empty, failed = summary["reviews"]
        lines.append(f"Evidence reviews: {written} written, {failed} failed.")
    if summary.get("rules_changed"):
        lines.append("WARNING: the scoring rules changed without being logged. Scores now carry a new fingerprint.")
    errors = summary.get("error_lines", [])
    if errors:
        lines.append(f"Problems ({summary.get('errors', len(errors))}):")
        lines += [f"- {e[:160]}" for e in errors[:_MAX_ERROR_LINES]]
        if len(errors) > _MAX_ERROR_LINES:
            lines.append(f"- ...and {len(errors) - _MAX_ERROR_LINES} more in the logs")
    if snapshot:
        lines.append(f"Weekly record snapshot: {snapshot}")
    if awaiting:
        lines.append("Waiting for you (each comes as its own message below, with buttons):")
        lines += [f"- {a}" for a in awaiting]
    return "\n".join(lines)


async def awaiting_confirmation() -> list[str]:
    """Outcome suggestions not yet confirmed, as one line each."""
    from aegis_bioquant.core.database import dispose_db, get_outcome_rows

    try:
        rows = await get_outcome_rows()
    finally:
        await dispose_db()
    waiting = sorted((r for r in rows.values() if r.status == "SUGGESTED"), key=lambda r: r.catalyst_date)
    return [f"{r.ticker} {r.catalyst_date}: {r.outcome}" + (f" ({r.evidence_url})" if r.evidence_url else "")
            for r in waiting]


async def send(text: str, buttons: Optional[list[list[dict]]] = None) -> bool:
    """`buttons`: rows of Telegram inline-keyboard buttons
    ({"text", "callback_data"}), answered by the web app's webhook
    (core/outcome_review.handle_button)."""
    if not (settings.TELEGRAM_BOT_TOKEN and settings.STATUS_TELEGRAM_CHAT_ID):
        return False
    url = f"{settings.TELEGRAM_API_BASE_URL}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {"chat_id": settings.STATUS_TELEGRAM_CHAT_ID, "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=body)
        return resp.is_success
    except httpx.HTTPError:
        return False


async def send_report(ok: bool, retried: bool, lines: list[str], snapshot: Optional[str]) -> bool:
    """The daily report, then one message with buttons per outcome that
    needs the reviewer (core/outcome_review.py). SEC acceptance times are
    looked up first, so each message can show one."""
    from aegis_bioquant.core import outcome_review
    from aegis_bioquant.core.database import dispose_db

    pending: list = []
    try:
        try:
            await outcome_review.fill_acceptance_times()
        except Exception:  # noqa: BLE001 — the messages still go out, without a time
            pass
        pending = await outcome_review.pending_reviews()
        awaiting = [p.text.splitlines()[0] for p in pending]
    except Exception as exc:  # noqa: BLE001
        awaiting = [f"(could not read pending outcomes: {type(exc).__name__})"]
    finally:
        await dispose_db()
    sent = await send(format_report(ok, retried, summarize(lines), awaiting, snapshot))
    for item in pending:
        await send(item.text, item.buttons)
    return sent
