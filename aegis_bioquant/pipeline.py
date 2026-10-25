"""
pipeline.py
------------
System orchestrator (Main Engine).

WHAT IT DOES: run_discovery_scan() is the daily scan. It finds every
catalyst, refreshes the evidence reviews and the daily option readings,
scores every upcoming PDUFA date (and AdCom with a listed sponsor) in the
next EVENT_MOVE_LOOKAHEAD_DAYS through run_candidate_pipeline(), stores
each score through core/database.py under a per-ticker lockout
(core/cache.py), alerts on in-window signals (telemetry/alerts.py) and
tracks outcomes. Its own docstring gives the details.

TRIALS: a candidate is scored on a clinical trial only when the trial was
matched with high confidence (resolution_tier STUDY_TRIAL_NAME/ACRONYM)
or confirmed by an administrator (CONFIRMED). Otherwise it is still
scored, on the evidence review or the base rate, and an in-window one is
listed as needing a person to confirm its trial. AdCom meetings whose page
names no listed company are reported, not scored.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from aegis_bioquant.config import constants as c
from aegis_bioquant.config import rules
from aegis_bioquant.config import settings
from aegis_bioquant.core.cache import acquire_lock, pipeline_lock_key, release_lock
from aegis_bioquant.core.database import (
    dispose_db,
    get_catalysts_needing_outcome,
    get_iv_history,
    get_iv_snapshot,
    get_outcome_rows,
    get_outcomes_needing_price,
    get_trial_overrides,
    get_llm_reviews,
    save_llm_review,
    touch_llm_review,
    init_db,
    save_event_move_snapshot,
    record_outcome_check,
    set_trial_override,
    save_iv_snapshot,
    save_outcome_price,
    save_prediction,
    save_screened_candidate,
    upsert_catalyst_events,
)
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment, score
from aegis_bioquant.engines.clinical_nlp.llm_review import LLMError, has_new_filings, review_catalyst
from aegis_bioquant.engines.options_quant.metrics import (
    atm_implied_volatility,
    atm_straddle_implied_move,
    event_implied_move,
    iv_rank_from_history,
    market_implied_approval_probability,
    options_cost_estimate,
    realized_volatility_range,
)
from aegis_bioquant.ingestion.clinical_trials_api import StudyProtocol, fetch_study_protocol
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent, ingest_catalysts
from aegis_bioquant.ingestion.market_data_feed import (
    MarketSnapshot,
    fetch_daily_closes,
    fetch_iv_reference,
    fetch_market_snapshot,
    fetch_quote,
    front_month_liquidity,
    reaction_closes,
    us_market_open,
)
from aegis_bioquant.ingestion.outcome_finder import find_outcome_evidence
from aegis_bioquant.logic.phase_a import format_market_cap, market_cap_ok, phase_a_failures
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import ExecutionPlan, build_execution_plan
from aegis_bioquant.telemetry.alerts import (
    AlertResult,
    ExitNotice,
    notify_due_exits,
    notify_if_actionable,
)

# Only these resolution_tier values are trusted enough to auto-run the
# full pipeline unattended — see module docstring and
# fda_scraper.CatalystEvent.resolution_tier for why NDA_FOR/INN_SUFFIX
# (medium/low confidence) are excluded by default.
HIGH_CONFIDENCE_RESOLUTION_TIERS = ("STUDY_TRIAL_NAME", "ACRONYM", "CONFIRMED")
# "CONFIRMED" is not something the text matching can produce: it is set
# when an administrator has picked the trial on the website's review
# page (core/database.TrialOverrideRecord).
IV_RANK_MIN_READINGS = c.IV_RANK_MIN_READINGS


@dataclass
class PipelineResult:
    candidate: Candidate
    fundamental_score: FundamentalScore
    market_snapshot: MarketSnapshot
    execution_plan: ExecutionPlan
    iv_readings: int = 0  # daily IV readings on record for this ticker (IV Rank needs IV_RANK_MIN_READINGS)


class PhaseAEviction(Exception):
    """Raised by run_candidate_pipeline() when the manual's Phase A
    filters remove a candidate. Not an error — the scan records it and
    reports why."""

    def __init__(self, ticker: str, reasons: list[str], spot_price: Optional[float],
                 open_interest: int, daily_volume: int):
        super().__init__(f"{ticker} screened out: {'; '.join(reasons)}")
        self.ticker = ticker
        self.reasons = reasons
        self.spot_price = spot_price
        self.open_interest = open_interest
        self.daily_volume = daily_volume


@dataclass
class EvictedCandidate:
    event: CatalystEvent
    reasons: list[str]
    spot_price: Optional[float]
    open_interest: int
    daily_volume: int


async def run_candidate_pipeline(
    ticker: str,
    nct_id: Optional[str],
    catalyst_type: str,
    catalyst_date: date,
    total_cash: float = 100_000.0,
    iv_rank: Optional[float] = None,
    enforce_phase_a: Optional[bool] = None,
    iv_rank_estimated: bool = False,
    claude_review=None,
) -> PipelineResult:
    """
    Runs one ticker/catalyst/trial triple through the full system:
    Phase A filters -> Engine 1 (classifier.py) + Engine 2 (metrics.py)
    -> matrix_matcher.py -> risk_manager.py.

    PHASE A FILTERS (logic/phase_a.py; on unless
    settings.ENFORCE_PHASE_A_FILTERS is off or enforce_phase_a=False): a
    candidate that fails is not returned — PhaseAEviction is raised with
    the reasons.
      - Wrong kind of company (worth more than the size ceiling, or a
        share price under the floor): evicted immediately, before any
        clinical data is fetched.
      - Options too thin (open interest / volume): NOT evicted on the
        spot. The Long-Term Equity Accumulation track exists precisely
        for a strong drug whose options are untradeable, so the clinical
        score is computed first; the candidate is evicted only if it
        then fails to qualify for that stock-only track. Evicting on
        thin options outright would make that track unreachable.

    iv_rank is supplied by the caller (the scan computes it from stored
    history) so this function stays free of database access.

    nct_id=None means no trial has been confidently matched. The candidate
    is still scored: its clinical score is the base rate for a drug the
    FDA has agreed to review, marked evidence_basis="BASELINE". Before
    this, such a candidate was not scored at all and showed as a blank
    row waiting on a person.
    """
    if enforce_phase_a is None:
        enforce_phase_a = settings.ENFORCE_PHASE_A_FILTERS

    snapshot: MarketSnapshot = await fetch_market_snapshot(ticker, catalyst_date)
    liquidity = front_month_liquidity(snapshot)
    spot = snapshot.quote.last_price
    open_interest = liquidity.total_open_interest if liquidity else 0
    volume = liquidity.total_volume if liquidity else 0

    market_cap = snapshot.quote.market_cap
    company_failures, liquidity_failures = phase_a_failures(spot, open_interest, volume, market_cap)
    if enforce_phase_a and company_failures:
        raise PhaseAEviction(ticker, company_failures + liquidity_failures, spot, open_interest, volume)

    if nct_id:
        protocol: StudyProtocol = await fetch_study_protocol(nct_id)
        fundamental = score(protocol)
        evidence_basis = "TRIAL_NO_RESULTS" if fundamental.data_completeness == "PARTIAL_NO_RESULTS" else "TRIAL_RESULTS"
    else:
        fundamental = FundamentalScore(
            nct_id="UNMATCHED",
            base_rate=c.FUNDAMENTAL_BASE_RATE_PRIOR,
            p_fundamental=c.FUNDAMENTAL_BASE_RATE_PRIOR,
            data_completeness="BASELINE_NO_TRIAL",
        )
        evidence_basis = "BASELINE"

    # Claude's reading of the evidence, where there is one, is the clinical
    # score the rules act on (see engines/clinical_nlp/llm_review.py). The
    # point system's number is kept beside it for comparison.
    point_score: Optional[float] = None
    if claude_review is not None and claude_review.score is not None:
        point_score = fundamental.p_fundamental
        claude_p = claude_review.score / 100
        fundamental = FundamentalScore(
            nct_id=fundamental.nct_id,
            base_rate=c.FUNDAMENTAL_BASE_RATE_PRIOR,
            adjustments=[ScoreAdjustment(
                "Evidence review", round(claude_p - c.FUNDAMENTAL_BASE_RATE_PRIOR, 4),
                claude_review.summary or "see the dashboard for its reasons",
            )],
            p_fundamental=claude_p,
            data_completeness="EVIDENCE_REVIEW",  # goes out in alert payloads: no vendor name
        )
        evidence_basis = "CLAUDE_THIN" if claude_review.evidence_quality == "THIN" else "CLAUDE"

    # The expiry AFTER the catalyst is the one whose price actually
    # reflects the event's volatility (a pre-catalyst expiry excludes it
    # entirely) — see this module's note in metrics.py's docstring on
    # why that's the one Engine 2 should read from.
    primary_chain = snapshot.expiry_after_catalyst
    p_market: Optional[float] = None
    p_market_confidence: Optional[str] = None
    if primary_chain is not None:
        prob = market_implied_approval_probability(primary_chain, snapshot.quote.last_price)
        if prob is not None:
            p_market_confidence = prob.confidence
            # POLICY: a non-GOOD confidence means the specific near-the-money
            # strikes used to derive this number were too thin/wide-spread to
            # trust, REGARDLESS of whether the chain's aggregate OI/volume
            # passed Phase A's liquidity gate — verified live on CYTK: front-
            # month OI=4512, volume=2002 (both pass Phase A) but the ATM
            # strikes were still ~100% bid/ask spread. Null out p_market
            # rather than feed a number into evaluate_candidate() the system
            # itself has already flagged as unreliable — p_market_confidence
            # stays on the Candidate so this isn't silently lost, just not
            # acted on.
            if prob.confidence == "GOOD":
                p_market = prob.probability_above

    event_move = event_implied_move(
        snapshot.expiry_before_catalyst, snapshot.expiry_after_catalyst, spot,
        catalyst_date, snapshot.fetched_at.date(),
    )

    candidate = Candidate(
        ticker=ticker,
        catalyst_type=catalyst_type,
        catalyst_date=catalyst_date,
        spot_price=spot,
        open_interest=open_interest,
        daily_options_volume=volume,
        p_fundamental=fundamental.p_fundamental,
        p_market=p_market,
        iv_rank=iv_rank,  # true IV Rank, or the price-history estimate until its history exists
        iv_rank_estimated=iv_rank_estimated and iv_rank is not None,
        evidence_basis=evidence_basis,
        point_score=point_score,
        p_market_confidence=p_market_confidence,
        market_cap=market_cap,
        event_implied_move=event_move.move if event_move else None,
        event_move_confidence=event_move.confidence if event_move else None,
    )
    evaluate_candidate(candidate)

    if (
        enforce_phase_a
        and liquidity_failures
        and candidate.arbitrage_signal != c.SIGNAL_LONG_TERM_EQUITY_ACCUMULATION
    ):
        raise PhaseAEviction(
            ticker,
            liquidity_failures + [
                f"its evidence score of {fundamental.p_fundamental * 100:.0f} is below the "
                f"{c.EQUITY_ACCUMULATION_P_FUNDAMENTAL_MIN * 100:.0f} needed to be covered on its shares alone"
            ],
            spot, open_interest, volume,
        )

    plan = build_execution_plan(candidate, total_cash=total_cash)

    return PipelineResult(
        candidate=candidate,
        fundamental_score=fundamental,
        market_snapshot=snapshot,
        execution_plan=plan,
    )


@dataclass
class DiscoveryScanResult:
    scanned_at: datetime
    total_catalysts_found: int
    pipeline_results: list[PipelineResult] = field(default_factory=list)
    needs_review_pdufa: list[CatalystEvent] = field(default_factory=list)  # in-window, not auto-run
    adcom_in_window: list[CatalystEvent] = field(default_factory=list)     # in-window, no resolver at all
    skipped_locked: list[CatalystEvent] = field(default_factory=list)      # auto-runnable, held by another run
    alerts: list[AlertResult] = field(default_factory=list)                # Phase E, one per candidate run
    evicted: list[EvictedCandidate] = field(default_factory=list)          # removed by Phase A filters
    exit_notices: list[ExitNotice] = field(default_factory=list)           # Phase D exit reminders sent this scan
    iv_snapshots: tuple[int, int] = (0, 0)                                 # (saved, tickers attempted)
    event_moves: tuple[int, int] = (0, 0)                                  # (saved, catalysts attempted)
    llm_reviews: tuple[int, int, int] = (0, 0, 0)                          # (reviewed, nothing to review, failed)
    outcomes: tuple[int, int, int] = (0, 0, 0)                             # (catalysts checked, decisions suggested, price reactions filled)
    phase3_watch: int = 0                                                  # Phase 3 readouts on the watch list
    market_closed: bool = False                                            # scan ran outside US market hours
    deferred: int = 0                                                      # candidates not scored because of that
    too_large: dict[str, float] = field(default_factory=dict)              # {ticker: company value} dropped by the size ceiling
    upcoming_pdufa: int = 0                                                # PDUFA dates still ahead
    errors: list[tuple[str, str]] = field(default_factory=list)           # (ticker, error message)


async def drop_oversized_companies(
    events: list[CatalystEvent], today: date
) -> tuple[list[CatalystEvent], dict[str, float]]:
    """
    Removes every catalyst belonging to a company worth more than
    constants.MARKET_CAP_MAX. Returns (events kept, {ticker: company
    value} for the companies removed).

    Done for the whole catalyst list, not only for candidates about to be
    scored, so that the watch list, the entry-window list and the daily
    IV readings are all about in-scope companies — the Phase 3 list in
    particular is otherwise dominated by the largest pharmaceutical
    companies. One price lookup per company with a current or upcoming
    catalyst; past events are left alone (they are only a record).

    A company whose value can't be read is KEPT (logic/phase_a.py,
    "missing data is not a failure"), as is an event with no ticker.
    """
    current = today.replace(day=1)  # Phase 3 dates can be month-only: "this month" is still current
    tickers = sorted({e.ticker for e in events if e.ticker and e.start_date >= current})
    too_large: dict[str, float] = {}
    for ticker in tickers:
        try:
            quote = await fetch_quote(ticker)
        except Exception:  # noqa: BLE001 — unknown value: keep the company
            continue
        finally:
            await asyncio.sleep(0.15)
        if not market_cap_ok(quote.market_cap):
            too_large[ticker] = quote.market_cap
    return [e for e in events if e.ticker not in too_large], too_large


def _drug_key(review) -> str:
    return "".join(ch for ch in (getattr(review, "drug", None) or "").lower() if ch.isalnum())


async def carry_over_confirmed_trials(events: list[CatalystEvent]) -> list[tuple[str, date, date]]:
    """
    When the FDA moves a decision, the same decision shows up under a new
    date, and the trial a reviewer confirmed is still filed under the old
    one. This copies the confirmation across, so the moved decision is
    not scored on a guess (or the baseline) until someone notices.

    A confirmation is carried from an old date to a new one for the same
    company and event type only when the two are known to be the same
    decision:
      - the evidence review of the old date says it moved to the new date
        (within MOVED_DATE_TOLERANCE_DAYS), or
      - the old date is recorded as DELAYED, the new date is later, and
        the evidence reviews of both name the same drug.
    A company can have two drugs in front of the FDA, so a shared ticker
    alone is never enough.

    Updates `events` in place and returns (ticker, old date, new date)
    for each one carried. Requires init_db().
    """
    overrides = await get_trial_overrides()
    if not overrides:
        return []
    reviews = await get_llm_reviews()
    outcomes = await get_outcome_rows()
    carried: list[tuple[str, date, date]] = []
    for event in events:
        key = (event.ticker, event.event_type, event.start_date)
        if not event.ticker or key in overrides:
            continue
        new_drug = _drug_key(reviews.get(key))
        for (ticker, event_type, old_date), old in sorted(overrides.items(), key=lambda kv: kv[0][2], reverse=True):
            if (ticker, event_type) != (event.ticker, event.event_type):
                continue
            old_review = reviews.get((ticker, event_type, old_date))
            moved_to = getattr(old_review, "date_moved_to", None)
            named = moved_to is not None and abs((moved_to - event.start_date).days) <= c.MOVED_DATE_TOLERANCE_DAYS
            outcome = outcomes.get((ticker, event_type, old_date))
            delayed = outcome is not None and outcome.outcome == "DELAYED" and outcome.status in ("SUGGESTED", "CONFIRMED")
            same_drug = bool(new_drug) and new_drug == _drug_key(old_review)
            if named or (delayed and same_drug and event.start_date > old_date):
                await set_trial_override(
                    event.ticker, event.event_type, event.start_date, old.nct_id, old.trial_title,
                    confirmed_by=old.confirmed_by,
                )
                event.resolved_nct_id, event.resolution_tier = old.nct_id, "CONFIRMED"
                carried.append((event.ticker, old_date, event.start_date))
                break
    return carried


# Why the last update_llm_reviews() run's failures happened, in order.
# Messages are LLMError text or an exception class name: safe to log.
LLM_REVIEW_FAILURES: list[str] = []


async def update_llm_reviews(events: list[CatalystEvent], today: date) -> tuple[int, int, int]:
    """
    Has Claude read the company's filings for each upcoming PDUFA
    catalyst that has no review newer than LLM_REVIEW_MAX_AGE_DAYS (see
    engines/clinical_nlp/llm_review.py). Returns (reviewed, nothing to
    review, failed). Does nothing without an API key.

    A review that is due is only redone if the company has filed
    something mentioning the drug since it was written; otherwise it is
    marked as checked and kept (most weeks nothing has changed, and a
    re-read would cost money to say the same thing). Thin reviews are
    always redone: something may have been published since.

    Soonest decisions first, at most LLM_REVIEW_MAX_PER_SCAN a scan. One
    at a time: the SEC rate-limits, and there is no hurry. A failure is
    counted and retried next scan; it never stops the others. Does not
    depend on market hours.
    """
    if not settings.ANTHROPIC_API_KEY:
        return 0, 0, 0
    from aegis_bioquant.ingestion.fda_scraper import _build_user_agent

    existing = await get_llm_reviews()
    overrides = await get_trial_overrides()
    fresh_after = datetime.now(timezone.utc) - timedelta(days=c.LLM_REVIEW_MAX_AGE_DAYS)
    horizon = today + timedelta(days=c.EVENT_MOVE_LOOKAHEAD_DAYS)

    due = []
    for event in sorted(events, key=lambda e: e.start_date):
        if event.event_type != "PDUFA" or not event.ticker or not event.cik or not (today < event.start_date <= horizon):
            continue
        row = existing.get((event.ticker, event.event_type, event.start_date))
        if row is not None:
            reviewed = row.reviewed_at if row.reviewed_at.tzinfo else row.reviewed_at.replace(tzinfo=timezone.utc)
            if reviewed > fresh_after:
                continue
        due.append(event)

    reviewed = empty = failed = 0
    LLM_REVIEW_FAILURES.clear()
    if not due:
        return 0, 0, 0
    async with httpx.AsyncClient(headers={"User-Agent": _build_user_agent()}, timeout=30.0) as sec, \
            httpx.AsyncClient(timeout=300.0) as llm:  # a review with web search can take minutes
        for event in due[: c.LLM_REVIEW_MAX_PER_SCAN]:
            row = existing.get((event.ticker, event.event_type, event.start_date))
            if row is not None and row.status == "REVIEWED" and row.drug and row.evidence_quality != "THIN":
                written = (row.reviewed_at if row.reviewed_at.tzinfo else row.reviewed_at.replace(tzinfo=timezone.utc)).date()
                if not await has_new_filings(sec, event.cik, row.drug, written, today):
                    await touch_llm_review(event.ticker, event.event_type, event.start_date)
                    continue
            override = overrides.get((event.ticker, event.event_type, event.start_date))
            nct_id = override.nct_id if override else (
                event.resolved_nct_id if event.resolution_tier in HIGH_CONFIDENCE_RESOLUTION_TIERS else None
            )
            try:
                review = await review_catalyst(
                    sec, llm, company=event.company_name or event.ticker, cik=event.cik,
                    catalyst_date=event.start_date, source_url=event.source_url, today=today, nct_id=nct_id,
                )
                await save_llm_review(event.ticker, event.event_type, event.start_date, review, settings.LLM_REVIEW_MODEL)
            except (LLMError, httpx.HTTPError, ValueError) as exc:
                failed += 1
                LLM_REVIEW_FAILURES.append(str(exc) if isinstance(exc, LLMError) else type(exc).__name__)
                # The same failure three times running is the account or the
                # service, not the catalyst (seen live: credit ran out and 24
                # reviews failed one after another). Stop and say why.
                if len(LLM_REVIEW_FAILURES) >= 3 and len(set(LLM_REVIEW_FAILURES[-3:])) == 1:
                    break
                continue
            if review is None:
                empty += 1
            else:
                reviewed += 1
                if review.date_moved_to is not None:
                    # The date on record is no longer the FDA's date. Put it in
                    # front of a reviewer as a suggested DELAYED outcome (a
                    # person confirms; see update_outcomes) — the dashboard
                    # warns on the row in the meantime.
                    try:
                        await record_outcome_check(
                            event.ticker, event.event_type, event.start_date, "DELAYED", today,
                            evidence_url=(review.sources[0] if review.sources else None),
                            evidence_text=f"Evidence review: the FDA decision date has moved to {review.date_moved_to.isoformat()}. "
                                          f"{review.summary}"[:500],
                        )
                    except Exception:  # noqa: BLE001 — the review itself is saved; the warning still shows
                        pass
    return reviewed, empty, failed


async def update_outcomes(today: date) -> tuple[int, int, int]:
    """
    The daily outcome-tracking step. Returns (catalysts checked for a
    decision, decisions suggested, price reactions filled in).

    1. For catalysts at or near their date with no confirmed outcome,
       looks in the company's SEC filings for a statement of the FDA's
       decision (ingestion/outcome_finder.py) and stores what it finds as
       a SUGGESTION for an administrator to confirm. A catalyst already
       holding a suggestion is left alone until someone acts on it; one
       with nothing found is looked up again every OUTCOME_RECHECK_DAYS.
       At most OUTCOME_MAX_CHECKS_PER_SCAN per scan, most recent dates
       first, so a backlog is worked through over a few days instead of
       in one burst of requests to the SEC.
    2. For CONFIRMED approvals and rejections, fills in the stock's
       closing price either side of the decision once both exist.

    Does not depend on market hours: filings and historical closing
    prices are available at any time.
    """
    from aegis_bioquant.ingestion.fda_scraper import _build_user_agent

    needing = await get_catalysts_needing_outcome(today, c.OUTCOME_LOOKBACK_DAYS, c.OUTCOME_GIVE_UP_DAYS)
    existing = await get_outcome_rows()
    recheck_before = datetime.now(timezone.utc) - timedelta(days=c.OUTCOME_RECHECK_DAYS)

    due = []
    for event in needing:
        row = existing.get((event.ticker, event.event_type, event.start_date))
        if not event.cik:
            continue  # no SEC company number to search by
        if row is not None and row.status == "SUGGESTED":
            continue  # waiting on a person
        if row is not None and row.checked_at is not None:
            checked = row.checked_at if row.checked_at.tzinfo else row.checked_at.replace(tzinfo=timezone.utc)
            if checked > recheck_before:
                continue
        due.append(event)

    checked = suggested = 0
    if due:
        async with httpx.AsyncClient(headers={"User-Agent": _build_user_agent()}, timeout=30.0) as client:
            for event in due[: c.OUTCOME_MAX_CHECKS_PER_SCAN]:
                try:
                    evidence = await find_outcome_evidence(client, event.cik, event.start_date, today)
                except Exception:  # noqa: BLE001 — not recorded as checked, so it is retried next scan
                    continue
                checked += 1
                if evidence is None:
                    await record_outcome_check(event.ticker, event.event_type, event.start_date)
                else:
                    suggested += 1
                    await record_outcome_check(
                        event.ticker, event.event_type, event.start_date, evidence.outcome,
                        evidence.decision_date, evidence.filing_url, evidence.sentence,
                    )
                await asyncio.sleep(0.3)

    priced = 0
    for outcome in await get_outcomes_needing_price():
        if outcome.decision_date >= today:
            continue  # the day after hasn't happened yet
        try:
            closes = await fetch_daily_closes(
                outcome.ticker, outcome.decision_date - timedelta(days=10), outcome.decision_date + timedelta(days=10)
            )
        except Exception:  # noqa: BLE001 — retried next scan
            continue
        pair = reaction_closes(closes, outcome.decision_date)
        if pair is not None:
            await save_outcome_price(outcome.id, pair[0], pair[1])
            priced += 1
        await asyncio.sleep(0.2)

    return checked, suggested, priced


async def record_iv_snapshots(events: list[CatalystEvent], as_of: date) -> tuple[int, int]:
    """
    Stores today's implied-volatility reading for every ticker with an
    upcoming PDUFA date or advisory committee meeting — not just the
    ones in their entry window. (Not the Phase 3 watch list: that is a
    few hundred more tickers for names that are never scored.) A
    ticker enters its window 45 days out; if readings only started then,
    IV Rank would never have history to rank against. Returns (readings
    saved, tickers attempted). A ticker with no usable options quote
    simply gets no reading that day.

    One ticker at a time with a short pause: this is a few dozen requests
    to a free data source, and there is no hurry.
    """
    tickers = sorted({
        e.ticker for e in events
        if e.event_type in ("PDUFA", "ADCOM") and e.ticker and e.start_date >= as_of
    })
    saved = 0
    for ticker in tickers:
        try:
            reference = await fetch_iv_reference(ticker, c.IV_REFERENCE_MIN_DAYS_TO_EXPIRY)
            if reference is not None:
                quote, chain = reference
                iv = atm_implied_volatility(chain, quote.last_price)
                if iv is not None:
                    hv_low = hv_high = None
                    try:  # a year of closes, for the options-cost estimate
                        closes = await fetch_daily_closes(ticker, as_of - timedelta(days=370), as_of)
                        span = realized_volatility_range([price for _, price in closes])
                        if span is not None:
                            hv_low, hv_high = span
                    except Exception:  # noqa: BLE001 — the reading itself is still worth saving
                        pass
                    await save_iv_snapshot(ticker, as_of, iv, chain.expiration, quote.last_price, hv_low, hv_high)
                    saved += 1
        except Exception:  # noqa: BLE001 — one ticker's bad data must not stop the rest
            pass
        await asyncio.sleep(0.3)
    return saved, len(tickers)


async def record_event_moves(events: list[CatalystEvent], as_of: date) -> tuple[int, int]:
    """
    Stores today's event-implied move (metrics.event_implied_move) for
    every catalyst with a ticker in the next EVENT_MOVE_LOOKAHEAD_DAYS —
    all of them, not just those in their entry window, so each catalyst
    builds a day-by-day series as its date approaches. Returns (readings
    saved, catalysts attempted). A catalyst whose options have no live
    quotes on both sides of the date simply gets no reading that day.
    """
    horizon = as_of + timedelta(days=c.EVENT_MOVE_LOOKAHEAD_DAYS)
    targets = sorted(
        {(e.ticker, e.event_type, e.start_date) for e in events
         if e.event_type in ("PDUFA", "ADCOM") and e.ticker and as_of < e.start_date <= horizon}
    )
    saved = 0
    for ticker, event_type, catalyst_date in targets:
        try:
            snapshot = await fetch_market_snapshot(ticker, catalyst_date)
            spot = snapshot.quote.last_price
            reading = event_implied_move(
                snapshot.expiry_before_catalyst, snapshot.expiry_after_catalyst, spot, catalyst_date, as_of
            )
            if reading is not None:
                await save_event_move_snapshot(
                    ticker, event_type, catalyst_date, as_of, reading.move, reading.baseline_iv,
                    reading.post_event_iv, reading.event_share, reading.confidence, spot,
                )
                saved += 1
        except Exception:  # noqa: BLE001 — one catalyst's bad data must not stop the rest
            pass
        await asyncio.sleep(0.3)
    return saved, len(targets)


async def _iv_rank_for(ticker: str, as_of: date) -> tuple[Optional[float], int, bool]:
    """
    (rank, number of readings on record, is it the estimate?).

    True IV Rank once the stock has IV_RANK_MIN_READINGS daily readings.
    Before that, metrics.options_cost_estimate from today's reading and
    the stock's past year of price swings, flagged as an estimate. None
    if today has no reading at all — a stale value is never ranked as
    current.
    """
    history = await get_iv_history(ticker, as_of - timedelta(days=c.IV_HISTORY_LOOKBACK_DAYS))
    if not history:
        return None, 0, False
    last_day, last_iv = history[-1]
    if last_day != as_of:
        return None, len(history), False
    rank = iv_rank_from_history(last_iv, [iv for _, iv in history])
    if rank is not None:
        return rank, len(history), False
    today = await get_iv_snapshot(ticker, as_of)
    estimate = options_cost_estimate(last_iv, today.hv_low, today.hv_high) if today else None
    return estimate, len(history), estimate is not None


async def run_discovery_scan(
    total_cash: float = 100_000.0,
    high_confidence_tiers: tuple[str, ...] = HIGH_CONFIDENCE_RESOLUTION_TIERS,
    persist: bool = True,
    use_lockout: bool = True,
    notify: bool = True,
) -> DiscoveryScanResult:
    """
    Phase A, automated: scans every live catalyst, filters to ones
    actually inside their entry window right now, and auto-runs the full
    pipeline on PDUFA candidates that resolved to an NCT with high
    confidence. See module docstring for exactly what's excluded and why
    — nothing here is silently skipped, it's sorted into a reported
    bucket (needs_review_pdufa / adcom_in_window / errors).

    WHAT GETS SCORED: every upcoming PDUFA date (and advisory meeting
    with a listed sponsor) in the next EVENT_MOVE_LOOKAHEAD_DAYS, whether
    or not it is in its entry window and whether or not a trial has been
    confirmed for it. Scoring only in-window, trial-confirmed candidates
    — the first design — left the product saying nothing about all but
    one or two companies. What the window still decides is who is
    ALERTED: only an in-window signal goes out.

    Runs candidates SEQUENTIALLY, not concurrently — deliberate, not an
    oversight: each candidate already fires several concurrent requests
    internally (CT.gov + yfinance), and this hits free public APIs
    (SEC EDGAR, CT.gov) that ask for restrained request patterns. With
    the small number of candidates any given scan actually finds, the
    wall-clock cost of sequencing is negligible next to being a
    considerate API citizen.

    With persist=True (default) every catalyst seen and every prediction
    made is saved via core/database.py — the historical record calibration
    tracking depends on. A database failure is reported in `errors` under
    the "DB" label rather than raised: losing a save must not cost the
    scan its results.

    With use_lockout=True (default) each candidate is run under a
    per-ticker lockout (core/cache.py), so two scans overlapping in time
    don't both process the same ticker. A ticker another run currently
    holds is not waited for — it goes into `skipped_locked` and is
    reported. If the lockout itself can't be reached, the candidate runs
    unlocked and that is reported under "DB": the worst case of running
    unlocked is a duplicate prediction row, which is cheaper than
    silently skipping a live candidate.

    With notify=True (default) each result goes to Phase E
    (telemetry/alerts.py), which alerts only on an actionable signal,
    only to channels configured in settings, and at most once per
    cooldown. What happened is recorded in `alerts`; a channel that
    failed is also listed in `errors` under "ALERT". The same switch
    sends Phase D exit reminders for earlier alerts whose exit date has
    arrived (`exit_notices`).

    With persist=True the scan also takes the daily implied-volatility
    reading for every ticker with an upcoming PDUFA date
    (record_iv_snapshots) — the history IV Rank is computed from.

    A catalyst whose trial an administrator has confirmed on the
    website's review page is scored on that trial, whatever the text
    matching guessed (needs the database, so not with all three of
    persist / use_lockout / notify switched off).

    Outside US market hours (settings.REQUIRE_MARKET_HOURS) the scan
    refreshes the catalyst list and sends due exit reminders, but scores
    nothing and stores no option reading: `market_closed` is set and
    `deferred` counts the candidates left for the next scan.

    Companies worth more than the size ceiling are dropped from the
    whole catalyst list up front (drop_oversized_companies) and listed
    in `too_large`.

    Candidates removed by the manual's Phase A price / liquidity filters
    are listed in `evicted` with the reasons, and saved so the dashboard
    can show them.
    """
    scanned_at = datetime.now(timezone.utc)
    ingest_problems: list[str] = []
    all_events = await ingest_catalysts(problems=ingest_problems)

    too_large: dict[str, float] = {}
    if settings.ENFORCE_PHASE_A_FILTERS:
        all_events, too_large = await drop_oversized_companies(all_events, scanned_at.date())

    results: list[PipelineResult] = []
    # "INGEST" errors mean a catalyst source was unreachable or partly
    # unreachable — the counts below are then an undercount, not a quiet day.
    errors: list[tuple[str, str]] = [("INGEST", problem) for problem in ingest_problems]
    if rules.unlogged_change():
        errors.append(("RULES", f"the scoring rules changed without being logged (now {rules.fingerprint()}): "
                                "see config/rules.py"))

    if persist or use_lockout or notify:
        try:
            await init_db()
            # Apply confirmed trials before anything is saved or sorted
            # into buckets, so a confirmed catalyst is stored with, and
            # scored on, the trial an administrator chose.
            await carry_over_confirmed_trials(all_events)   # a moved date keeps its confirmed trial
            overrides = await get_trial_overrides()
            for event in all_events:
                confirmed = overrides.get((event.ticker, event.event_type, event.start_date))
                if confirmed is not None:
                    event.resolved_nct_id = confirmed.nct_id
                    event.resolution_tier = "CONFIRMED"
            if persist:
                await upsert_catalyst_events(all_events)
        except Exception as exc:  # noqa: BLE001 — see docstring
            errors.append(("DB", f"database setup / catalyst event save failed: {exc}"))

    pdufa_in_window = [
        e for e in all_events if e.event_type == "PDUFA" and e.horizon_status == "PDUFA_ENTRY_WINDOW"
    ]
    adcoms = [e for e in all_events if e.event_type == "ADCOM" and e.horizon_status == "ADCOM_ENTRY_WINDOW"]
    # An AdCom whose meeting page named a listed company is handled like a
    # PDUFA event from here on; one that didn't stays in its own bucket.
    adcom_with_ticker = [e for e in adcoms if e.ticker]
    adcom_in_window = [e for e in adcoms if not e.ticker]
    in_window = pdufa_in_window + adcom_with_ticker

    def _trial_for(e: CatalystEvent) -> Optional[str]:
        """The trial to score on: only one matched with confidence or
        confirmed by a reviewer. Anything looser is not used — the
        candidate is scored on the baseline instead (see
        run_candidate_pipeline)."""
        return e.resolved_nct_id if (e.resolved_nct_id and e.resolution_tier in high_confidence_tiers) else None

    needs_review = [e for e in in_window if e.ticker and _trial_for(e) is None]
    phase3_watch = [e for e in all_events if e.event_type == "PHASE_3_READOUT"]

    # EVERY upcoming catalyst with a ticker is scored each scan, in or out
    # of its entry window, so the dashboard has something to say about
    # each one. Only those IN the window can raise an alert (below).
    today_ = scanned_at.date()
    horizon = today_ + timedelta(days=c.EVENT_MOVE_LOOKAHEAD_DAYS)
    in_window_ids = {id(e) for e in in_window}
    auto_runnable = sorted(
        (e for e in all_events
         if e.event_type in ("PDUFA", "ADCOM") and e.ticker and today_ < e.start_date <= horizon),
        key=lambda e: (id(e) not in in_window_ids, e.start_date, e.ticker),
    )

    skipped_locked: list[CatalystEvent] = []
    alerts: list[AlertResult] = []
    evicted: list[EvictedCandidate] = []
    exit_notices: list[ExitNotice] = []
    today = scanned_at.date()

    # Options data is only real while the market is open — see
    # us_market_open(). Off-hours, the catalyst calendar above is still
    # refreshed, but nothing is scored and no reading is stored.
    market_closed = settings.REQUIRE_MARKET_HOURS and not us_market_open(scanned_at)
    deferred = len(auto_runnable) if market_closed else 0
    if market_closed:
        auto_runnable = []

    iv_snapshots = (0, 0)
    event_moves = (0, 0)
    if persist and not market_closed:
        try:
            iv_snapshots = await record_iv_snapshots(all_events, today)
        except Exception as exc:  # noqa: BLE001 — IV history is a nice-to-have for this scan
            errors.append(("DB", f"IV readings not recorded: {type(exc).__name__}"))
        try:
            event_moves = await record_event_moves(all_events, today)
        except Exception as exc:  # noqa: BLE001 — same
            errors.append(("DB", f"event-move readings not recorded: {type(exc).__name__}"))

    # Claude's evidence reviews are refreshed BEFORE scoring, because the
    # scores below are built on them. A catalyst whose review fails or
    # finds nothing is scored by the point system instead.
    llm_reviews = (0, 0, 0)
    claude_reviews: dict = {}
    if persist:
        try:
            llm_reviews = await update_llm_reviews(all_events, today)
        except Exception as exc:  # noqa: BLE001 — the scan still scores, on the point system
            errors.append(("LLM", f"evidence reviews failed: {type(exc).__name__}"))
        if llm_reviews[2]:
            errors.append(("LLM", f"{llm_reviews[2]} evidence review(s) failed, retried next scan. "
                                  f"Last reason: {LLM_REVIEW_FAILURES[-1] if LLM_REVIEW_FAILURES else 'unknown'}"))
        try:
            # Again, now that today's reviews exist: one may have just said a
            # date moved, or named the drug behind a date that is new today.
            await carry_over_confirmed_trials(all_events)
        except Exception as exc:  # noqa: BLE001 — those catalysts are scored as before
            errors.append(("DB", f"confirmed trials not carried to moved dates: {type(exc).__name__}"))
        try:
            claude_reviews = {k: r for k, r in (await get_llm_reviews()).items() if r.status == "REVIEWED"}
        except Exception as exc:  # noqa: BLE001 — same
            errors.append(("DB", f"evidence reviews not read: {type(exc).__name__}"))

    for event in auto_runnable:
        lock_key = pipeline_lock_key(event.ticker)
        lock_token: Optional[str] = None
        if use_lockout:
            try:
                lock_token = await acquire_lock(lock_key)
            except Exception as exc:  # noqa: BLE001 — see docstring
                errors.append(("DB", f"{event.ticker} lockout unavailable, ran unlocked: {exc}"))
            else:
                if lock_token is None:
                    skipped_locked.append(event)
                    continue

        try:
            iv_rank, iv_readings, iv_estimated = None, 0, False
            if persist:
                try:
                    iv_rank, iv_readings, iv_estimated = await _iv_rank_for(event.ticker, today)
                except Exception:  # noqa: BLE001 — score without IV Rank rather than not at all
                    pass

            try:
                result = await run_candidate_pipeline(
                    ticker=event.ticker,
                    nct_id=_trial_for(event),
                    catalyst_type=event.event_type,
                    catalyst_date=event.start_date,
                    total_cash=total_cash,
                    iv_rank=iv_rank,
                    iv_rank_estimated=iv_estimated,
                    claude_review=claude_reviews.get((event.ticker, event.event_type, event.start_date)),
                )
                result.iv_readings = iv_readings
                results.append(result)
            except PhaseAEviction as out:
                evicted.append(
                    EvictedCandidate(event, out.reasons, out.spot_price, out.open_interest, out.daily_volume)
                )
                if persist:
                    try:
                        await save_screened_candidate(
                            event.ticker, event.event_type, event.start_date, out.spot_price,
                            out.open_interest, out.daily_volume, out.reasons, scanned_at=scanned_at,
                        )
                    except Exception as exc:  # noqa: BLE001 — see docstring
                        errors.append(("DB", f"{event.ticker} screen-out not saved: {exc}"))
                continue
            except Exception as exc:  # noqa: BLE001 — one bad candidate must not kill the whole scan
                errors.append((event.ticker or "UNKNOWN", str(exc)))
                continue

            if persist:
                try:
                    await save_prediction(result, scanned_at=scanned_at)
                except Exception as exc:  # noqa: BLE001 — see docstring
                    errors.append(("DB", f"{event.ticker} prediction not saved: {exc}"))

            # A signal is only ACTED on inside the entry window. Outside it
            # the score is saved and shown ("setting up"), but nobody is
            # alerted to trade something that isn't in its window yet.
            if notify and id(event) in in_window_ids:
                try:
                    alert = await notify_if_actionable(result, as_of=scanned_at.date())
                except Exception as exc:  # noqa: BLE001 — an alerting bug must not kill the scan
                    # Class name only: httpx exception text can contain a
                    # webhook URL / bot token (see telemetry/alerts.py).
                    errors.append(("ALERT", f"{event.ticker} alert not attempted: {type(exc).__name__}"))
                else:
                    alerts.append(alert)
                    # Operator channels are named one by one; customer
                    # deliveries are only counted — there can be many, and
                    # a report shouldn't list customers.
                    for outcome in alert.outcomes:
                        if not outcome.ok and outcome.destination_id is None:
                            errors.append(("ALERT", f"{event.ticker} {outcome.channel}: {outcome.detail}"))
                    customer_failures = sum(
                        1 for o in alert.outcomes if not o.ok and o.destination_id is not None
                    )
                    if customer_failures:
                        errors.append(("ALERT", f"{event.ticker} {customer_failures} customer delivery(ies) failed"))
                    for warning in alert.warnings:
                        errors.append(("ALERT", f"{event.ticker} {warning}"))
        finally:
            if lock_token is not None:
                try:
                    await release_lock(lock_key, lock_token)
                except Exception as exc:  # noqa: BLE001 — the TTL will clear it
                    errors.append(("DB", f"{event.ticker} lockout not released: {exc}"))

    outcomes = (0, 0, 0)
    if persist:
        try:
            outcomes = await update_outcomes(today)
        except Exception as exc:  # noqa: BLE001 — outcome tracking must not cost the scan its results
            errors.append(("DB", f"outcome tracking failed: {type(exc).__name__}"))

    if notify:
        try:
            exit_notices = await notify_due_exits(as_of=today)
        except Exception as exc:  # noqa: BLE001 — as with alerts: class name only
            errors.append(("ALERT", f"exit reminders not attempted: {type(exc).__name__}"))
        for notice in exit_notices:
            for outcome in notice.outcomes:
                if not outcome.ok and outcome.destination_id is None:
                    errors.append(("ALERT", f"{notice.ticker} exit reminder {outcome.channel}: {outcome.detail}"))

    return DiscoveryScanResult(
        scanned_at=scanned_at,
        total_catalysts_found=len(all_events),
        pipeline_results=results,
        needs_review_pdufa=needs_review,
        adcom_in_window=adcom_in_window,
        skipped_locked=skipped_locked,
        alerts=alerts,
        evicted=evicted,
        exit_notices=exit_notices,
        iv_snapshots=iv_snapshots,
        event_moves=event_moves,
        llm_reviews=llm_reviews,
        outcomes=outcomes,
        phase3_watch=len(phase3_watch),
        market_closed=market_closed,
        deferred=deferred,
        too_large=too_large,
        upcoming_pdufa=sum(1 for e in all_events if e.event_type == "PDUFA" and e.start_date >= today),
        errors=errors,
    )


def _format_discovery_report(scan: DiscoveryScanResult) -> str:
    lines = [
        f"=== Discovery Scan @ {scan.scanned_at.isoformat()} ===",
    ]
    if scan.market_closed:
        lines.append(
            "US MARKET CLOSED: catalyst list refreshed only. Scoring and option readings need live "
            f"quotes and wait for the next scan in market hours ({scan.deferred} candidate(s) waiting)."
        )
    lines += [
        f"Companies dropped as too large (over {format_market_cap(c.MARKET_CAP_MAX)}): {len(scan.too_large)}"
        + (f" - {', '.join(sorted(scan.too_large, key=scan.too_large.get, reverse=True))}" if scan.too_large else ""),
        f"Total catalysts scanned: {scan.total_catalysts_found}",
        f"  of which PDUFA dates still ahead: {scan.upcoming_pdufa}",
        f"  of which Phase 3 readouts on the watch list (never scored): {scan.phase3_watch}",
        f"Scored: {len(scan.pipeline_results)} upcoming catalyst(s)",
        f"Screened out by Phase A filters: {len(scan.evicted)}",
        f"In-window but needs manual review: {len(scan.needs_review_pdufa)}",
        f"AdCom in-window with no listed sponsor found: {len(scan.adcom_in_window)}",
        f"Skipped, locked by another run: {len(scan.skipped_locked)}",
        f"Alerts sent: {sum(1 for a in scan.alerts if a.status in ('SENT', 'PARTIAL'))}",
        f"Exit reminders sent: {sum(1 for n in scan.exit_notices if n.status in ('SENT', 'PARTIAL'))}",
        f"IV readings recorded: {scan.iv_snapshots[0]} of {scan.iv_snapshots[1]} tracked tickers",
        f"Event-move readings recorded: {scan.event_moves[0]} of {scan.event_moves[1]} upcoming catalysts",
        f"Claude evidence reviews: {scan.llm_reviews[0]} written, {scan.llm_reviews[1]} with nothing to review, "
        f"{scan.llm_reviews[2]} failed" + ("" if settings.ANTHROPIC_API_KEY else " (off: no API key)"),
        f"Outcomes: {scan.outcomes[0]} catalyst(s) checked for an FDA decision, {scan.outcomes[1]} suggested "
        f"for confirmation, {scan.outcomes[2]} price reaction(s) recorded",
        f"Errors: {len(scan.errors)}",
        "",
    ]

    for result in scan.pipeline_results:
        lines.append(_format_report(result))
        lines.append("")

    if scan.evicted:
        lines.append("-- Screened out by Phase A filters --")
        for out in scan.evicted:
            lines.append(f"  {out.event.ticker} | {out.event.start_date} | {'; '.join(out.reasons)}")
        lines.append("")

    if scan.exit_notices:
        lines.append("-- Phase D: Exit reminders --")
        for n in scan.exit_notices:
            lines.append(f"  {n.ticker} | {n.label} due {n.trigger_date} | {n.status}")
        lines.append("")

    if scan.needs_review_pdufa:
        lines.append("-- In-window candidates needing manual NCT lookup --")
        for e in scan.needs_review_pdufa:
            lines.append(
                f"  {e.ticker or '??????'} | {e.event_type} {e.start_date} | "
                f"candidate={e.drug_or_trial_candidate!r} tier={e.resolution_tier} | {e.source_url}"
            )
        lines.append("")

    if scan.adcom_in_window:
        lines.append("-- AdCom meetings in-window with no listed sponsor on the meeting page --")
        for e in scan.adcom_in_window:
            lines.append(f"  {e.start_date} | {e.committee} | {e.title[:80]} | {e.source_url}")
        lines.append("")

    if scan.skipped_locked:
        lines.append("-- Skipped: pipeline lockout held by another run --")
        for e in scan.skipped_locked:
            lines.append(f"  {e.ticker} | {e.start_date} | {e.resolved_nct_id}")
        lines.append("")

    if scan.alerts:
        lines.append("-- Phase E: Alerts --")
        for a in scan.alerts:
            parts = [f"{o.channel}={o.detail}" for o in a.outcomes if o.destination_id is None]
            customers = [o for o in a.outcomes if o.destination_id is not None]
            if customers:
                parts.append(f"customers: {sum(1 for o in customers if o.ok)} of {len(customers)} delivered")
            lines.append(f"  {a.ticker} | {a.signal} | {a.status}" + (f" | {', '.join(parts)}" if parts else ""))
        lines.append("")

    if scan.errors:
        lines.append("-- Errors --")
        for ticker, msg in scan.errors:
            lines.append(f"  {ticker}: {msg}")

    return "\n".join(lines)


def _format_report(result: PipelineResult) -> str:
    c, fs, snap, plan = (
        result.candidate,
        result.fundamental_score,
        result.market_snapshot,
        result.execution_plan,
    )
    lines = [
        f"=== {c.ticker} | {c.catalyst_type} {c.catalyst_date} ===",
        "",
        "-- Engine 1: Fundamental --",
        fs.explain(),
        "",
        "-- Engine 2: Market --",
        f"  Spot: {c.spot_price} | Company value: {format_market_cap(c.market_cap)}",
        f"  Chance stock ends above today's price: {c.p_market} (confidence: {c.p_market_confidence})"
        " - recorded only, no rule reads it",
        f"  Event-implied move: "
        + (f"{c.event_implied_move:.1%} ({c.event_move_confidence})" if c.event_implied_move is not None else "not available"),
        f"  Evidence basis: {c.evidence_basis}"
        + (f" (registry point system said {c.point_score:.2f})" if c.point_score is not None else ""),
        f"  IV_Rank{' (ESTIMATE from price history)' if c.iv_rank_estimated else ''}: {c.iv_rank} "
        f"({result.iv_readings} daily reading(s) on record; "
        f"{IV_RANK_MIN_READINGS} needed)",
        f"  Front-month OI: {c.open_interest} | Volume: {c.daily_options_volume}",
        "",
        "-- Phase C: Arbitrage Matrix --",
        f"  Signal: {c.arbitrage_signal}",
        f"  Why: {c.signal_notes}",
        "",
        "-- Phase D: Execution Plan --",
        f"  Hold state: {plan.hold_state} | use_options: {plan.use_options}",
        f"  Allocation: {plan.allocation_pct:.2%} (${plan.allocation_dollars:,.2f})",
        f"  Trailing stop: {plan.trailing_stop_pct}",
    ]
    for trigger in plan.exit_triggers:
        lines.append(f"    exit[{trigger.label}] active={trigger.active} date={trigger.trigger_date}")
    lines.append(f"  Notes: {plan.notes}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    async def _main() -> bool:
        try:
            scan = await run_discovery_scan()
            print(_format_discovery_report(scan))
            return not any(label == "INGEST" for label, _ in scan.errors)
        finally:
            await dispose_db()

    # Exit non-zero when a catalyst source failed, so a scheduled run that
    # couldn't see its inputs shows up as a failed run on the host instead
    # of a clean-looking empty report.
    sys.exit(0 if asyncio.run(_main()) else 1)
