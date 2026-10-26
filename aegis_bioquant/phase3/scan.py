"""
phase3/scan.py
---------------
One daily run of the Phase 3 forecasts (see this package's __init__ for
what it does and why it is separate). Called by entrypoint.after_scan
when settings.PHASE3_FORECASTS is on, or by hand:

    python -m aegis_bioquant.phase3              # a full run, as the scanner does
    python -m aegis_bioquant.phase3 --limit 3    # at most 3 reviews (a cost check)

A trial is reviewed when it has no review yet, when its review is
P3_REVIEW_RECHECK_DAYS old and the company has filed about the drug
since, or when the review is P3_REVIEW_MAX_AGE_DAYS old. At most
P3_MAX_REVIEWS_PER_SCAN reviews run per scan, soonest trials first, so a
first run over many trials is spread over several days.

Every trial that passes the price screen and has a review gets one score
row for the day. A trial whose results are already public gets none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from aegis_bioquant.engines.clinical_nlp.llm_review import LLMError, has_new_filings
from aegis_bioquant.ingestion.market_data_feed import fetch_quote
from aegis_bioquant.phase3 import constants as k
from aegis_bioquant.phase3 import review as R
from aegis_bioquant.phase3 import rules, store

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


@dataclass
class Phase3Report:
    trials: int = 0
    scored: int = 0
    reviewed: int = 0
    already_reported: int = 0
    under_price: int = 0
    no_price: int = 0
    waiting_for_review: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        text = (f"phase 3 (rules v{rules.RULES_VERSION}, {rules.fingerprint()}): {self.trials} trials, {self.scored} scored, "
                f"{self.reviewed} reviewed (~${self.cost_usd:.2f}), {self.already_reported} already reported, "
                f"{self.under_price} under ${k.P3_MIN_PRICE:.0f}, {self.waiting_for_review} waiting for a review")
        if self.no_price:
            text += f", {self.no_price} without a price"
        if self.errors:
            text += f"; {len(self.errors)} error(s): " + "; ".join(self.errors[:3])
        return text


async def cik_by_ticker(sec: httpx.AsyncClient) -> dict[str, str]:
    """The SEC's ticker list, for the company filings each review reads.
    Empty if it can't be fetched: the reviews then rely on the web."""
    try:
        resp = await sec.get(COMPANY_TICKERS_URL)
        resp.raise_for_status()
        return {str(r.get("ticker", "")).upper(): str(r.get("cik_str")) for r in resp.json().values() if r.get("cik_str")}
    except (httpx.HTTPError, ValueError, AttributeError):
        return {}


def review_due(row: Optional[store.Phase3ReviewRecord], now: datetime) -> str:
    """'new', 'old' (redo it), 'check' (redo only if something was filed) or ''."""
    if row is None:
        return "new"
    if row.status == R.STATUS_ALREADY_REPORTED:
        return ""
    reviewed = row.reviewed_at if row.reviewed_at.tzinfo else row.reviewed_at.replace(tzinfo=timezone.utc)
    age = now - reviewed
    if age >= timedelta(days=k.P3_REVIEW_MAX_AGE_DAYS):
        return "old"
    if age >= timedelta(days=k.P3_REVIEW_RECHECK_DAYS):
        return "check"
    return ""


async def run(today: Optional[date] = None, *, max_reviews: int = k.P3_MAX_REVIEWS_PER_SCAN,
              now: Optional[datetime] = None) -> Phase3Report:
    from aegis_bioquant.ingestion.fda_scraper import _build_user_agent

    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    report = Phase3Report()
    await store.init_tables()
    events = await store.trials_to_forecast(today)
    reviews = await store.get_reviews()
    report.trials = len(events)

    async with httpx.AsyncClient(headers={"User-Agent": _build_user_agent()}, timeout=30.0) as sec, \
            httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0)) as llm, \
            httpx.AsyncClient(timeout=30.0) as http:
        ciks = await cik_by_ticker(sec)
        for event in events:
            try:
                price = (await fetch_quote(event.ticker)).last_price
            except Exception:  # noqa: BLE001 — the quote source raises many kinds; a missing price only skips the day
                report.no_price += 1
                continue
            if price < k.P3_MIN_PRICE:
                report.under_price += 1
                continue

            row = reviews.get(event.resolved_nct_id)
            due = review_due(row, now)
            if due == "check" and row is not None and row.drug:
                cik = ciks.get(event.ticker.upper())
                if cik and not await has_new_filings(sec, cik, row.drug, row.reviewed_at.date(), today):
                    await store.touch_review(event.resolved_nct_id, now)
                    due = ""
            if due and report.reviewed < max_reviews:
                try:
                    result = await R.review_trial(
                        sec, llm, http, ticker=event.ticker, company=event.company_name or event.ticker,
                        cik=ciks.get(event.ticker.upper()), nct_id=event.resolved_nct_id,
                        title=event.title, completion=event.start_date, today=today,
                    )
                except (LLMError, httpx.HTTPError) as exc:
                    report.errors.append(f"{event.ticker} {event.resolved_nct_id}: {exc if isinstance(exc, LLMError) else type(exc).__name__}")
                    result = None
                if result is not None:
                    row = await store.save_review(event, result, now)
                    reviews[event.resolved_nct_id] = row
                    report.reviewed += 1
                    report.cost_usd += result.cost_usd

            if row is None:
                report.waiting_for_review += 1
            elif row.status == R.STATUS_ALREADY_REPORTED:
                report.already_reported += 1
            else:
                await store.save_score(event, row, price, now)
                report.scored += 1
    return report
