"""
core/database.py
-----------------
Async database engine + ORM models for FDAOdds.

TWO DATABASES, ONE CODE PATH: settings.DATABASE_URL defaults to a local
SQLite file (zero setup, used for local runs and tests). The hosted
scanner sets it to a Postgres URL instead — the PostgreSQL + asyncpg
setup the manual specifies. This module only uses portable SQLAlchemy
ORM calls, no database-specific SQL, and every function here has been
run against both: SQLite locally, and a real PostgreSQL 18 server on
Railway (2026-10-06). Timestamp columns are timezone-aware for that
reason — Postgres rejects the UTC-aware datetimes this code writes if
the column is a plain timestamp.

NO REAL MIGRATIONS YET: init_db() creates missing tables, and adds the
few nullable columns listed in _ADDED_COLUMNS to databases created
before those columns existed. Any other change to an existing table
needs a migration tool (Alembic), which is not set up.

WHY PERSISTENCE MATTERS MOST HERE: the single highest-value use of a
database for this system isn't caching API responses — it's building
the historical record needed for CALIBRATION TRACKING, flagged as the
project's top priority before any engine code was written. Every
prediction (candidate scored, signal assigned, plan built) gets saved
with its full input breakdown. Once a catalyst date passes, the outcome
is recorded in CatalystOutcomeRecord (suggested by
ingestion/outcome_finder.py, confirmed by an administrator), and the
system can check: does a 0.83
P_Fundamental actually resolve true 83% of the time? That question is
unanswerable without this table existing first.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import AsyncIterator, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    delete,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, mapped_column
from sqlalchemy.pool import NullPool

from aegis_bioquant.config import rules, settings


class Base(DeclarativeBase):
    pass


class CatalystEventRecord(Base):
    """Mirrors ingestion/fda_scraper.py's CatalystEvent — every catalyst
    this system has ever seen, upserted on each scan so history survives
    even after an event ages out of its entry window."""

    __tablename__ = "catalyst_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_url: Mapped[str] = mapped_column(String, unique=True, index=True)  # natural key

    event_type: Mapped[str] = mapped_column(String)
    ticker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    company_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cik: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    title: Mapped[str] = mapped_column(String)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    center: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    committee: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    raw_source: Mapped[str] = mapped_column(String)
    horizon_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    drug_or_trial_candidate: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_nct_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolution_tier: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PredictionRecord(Base):
    """
    One row per full pipeline run on a candidate (pipeline.PipelineResult).
    Outcomes are recorded once per catalyst in CatalystOutcomeRecord; the
    three outcome columns at the end of this table are never written (see
    the SUPERSEDED note there).
    """

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    ticker: Mapped[str] = mapped_column(String, index=True)
    nct_id: Mapped[str] = mapped_column(String)
    catalyst_type: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date, index=True)

    # Engine 1
    p_fundamental: Mapped[float] = mapped_column(Float)
    fundamental_breakdown: Mapped[dict] = mapped_column(JSON)  # list of {label, delta, reason}
    fundamental_base_rate: Mapped[float] = mapped_column(Float)

    # Engine 2
    spot_price: Mapped[float] = mapped_column(Float)
    p_market: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_market_confidence: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    iv_rank: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Added after the table existed on the hosted database — see _ADDED_COLUMNS.
    event_implied_move: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    signal_notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    iv_rank_estimated: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)  # iv_rank is the price-history estimate
    evidence_basis: Mapped[Optional[str]] = mapped_column(String, nullable=True)      # CLAUDE | CLAUDE_THIN | TRIAL_RESULTS | TRIAL_NO_RESULTS | BASELINE
    point_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # the registry point system's score when Claude's drove
    # Which rules made this row (config/rules.py). Empty on rows from
    # before 2026-10-12, when the rules were not yet numbered.
    rules_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rules_fingerprint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    open_interest: Mapped[int] = mapped_column(Integer)
    daily_volume: Mapped[int] = mapped_column(Integer)

    # Phase C/D
    divergence_delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    arbitrage_signal: Mapped[str] = mapped_column(String)
    hold_state: Mapped[str] = mapped_column(String)
    use_options: Mapped[bool] = mapped_column(Integer)  # SQLite has no native bool
    allocation_pct: Mapped[float] = mapped_column(Float)
    allocation_dollars: Mapped[float] = mapped_column(Float)
    trailing_stop_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # SUPERSEDED: outcomes are recorded once per catalyst in
    # CatalystOutcomeRecord, not on each prediction row. These three
    # columns are never written; they stay only because removing a column
    # from the hosted table needs a migration tool this project lacks.
    actual_fda_outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # "APPROVED" | "CRL" | None
    actual_price_change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome_recorded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertRecord(Base):
    """One row per Phase E alert received by the dashboard's webhook
    (telemetry/dashboard.py). `payload` is the full telemetry
    AlertPayload exactly as it arrived; the other columns are copies of
    a few of its fields so alerts can be listed without unpacking JSON."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    arbitrage_signal: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class UserRecord(Base):
    """One customer account — see core/accounts.py, which owns the logic.
    The two billing columns are unused until payments are built; they are
    here now because there is no migration tool yet (see module docstring)
    and adding columns to a live table later would need one."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)  # stored lower-cased
    password_hash: Mapped[str] = mapped_column(String)                   # argon2id, never the password
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subscription_status: Mapped[str] = mapped_column(String, default="none")

    # None until the user has proved they can read mail at `email`.
    # Added after the table already existed on the hosted database — see
    # _ADDED_COLUMNS below.
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailTokenRecord(Base):
    """A single-use link sent by email — "verify" (confirm the address)
    or "reset" (set a new password). See core/accounts.py. Only the
    token's SHA-256 is stored, as with sessions: a copy of this table
    is not a set of working reset links."""

    __tablename__ = "email_tokens"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String)  # "verify" | "reset"
    expires_at: Mapped[float] = mapped_column(Float)  # epoch seconds


class AlertDestinationRecord(Base):
    """Where one user wants alerts sent: at most one row per user per
    channel. `address` is a Telegram chat id or a Discord webhook URL —
    the webhook URL is a credential (anyone holding it can post to that
    channel) and is stored as-is, not encrypted."""

    __tablename__ = "alert_destinations"
    __table_args__ = (UniqueConstraint("user_id", "channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String)  # "telegram" | "discord"
    address: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TelegramLinkTokenRecord(Base):
    """A short-lived, single-use code that ties "whoever presses Start in
    Telegram" to a logged-in user — see core/accounts.py."""

    __tablename__ = "telegram_link_tokens"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[float] = mapped_column(Float)  # epoch seconds, as in PipelineLockRecord


class SessionRecord(Base):
    """A logged-in browser — see web/sessions.py. The cookie holds a
    random token; only its SHA-256 is stored, so reading this table does
    not let anyone log in as a user."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String)
    expires_at: Mapped[float] = mapped_column(Float)  # epoch seconds


class LoginThrottleRecord(Base):
    """Failed-login counter per email — see web/sessions.py."""

    __tablename__ = "login_throttles"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[float] = mapped_column(Float)  # epoch seconds


class AlertDeliveryRecord(Base):
    """One row per attempt to deliver an alert to a user's destination —
    the record of who was sent what, and what failed."""

    __tablename__ = "alert_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String)
    ticker: Mapped[str] = mapped_column(String)
    arbitrage_signal: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date)
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(String)  # "HTTP 200", "HTTP 404", "ConnectError"
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ScreenedCandidateRecord(Base):
    """A candidate the scan found in its entry window but removed under
    the manual's Phase A price / liquidity filters, with the reasons —
    kept so the dashboard can say WHY a ticker wasn't scored instead of
    it silently vanishing."""

    __tablename__ = "screened_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    catalyst_type: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date)
    spot_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    open_interest: Mapped[int] = mapped_column(Integer)
    daily_volume: Mapped[int] = mapped_column(Integer)
    reasons: Mapped[list] = mapped_column(JSON)  # list of plain-language strings
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class IVSnapshotRecord(Base):
    """One implied-volatility reading per ticker per day. This table IS
    the system's IV history: there is no free source for a year of past
    implied volatility, so the scan records today's value every day and
    IV Rank is computed from whatever has accumulated here."""

    __tablename__ = "iv_snapshots"
    __table_args__ = (UniqueConstraint("ticker", "as_of"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    as_of: Mapped[date] = mapped_column(Date)
    atm_iv: Mapped[float] = mapped_column(Float)         # annualised, e.g. 0.85 = 85%
    expiration: Mapped[date] = mapped_column(Date)       # the expiry it was read from
    spot_price: Mapped[float] = mapped_column(Float)
    # Lowest / highest 30-day volatility the stock actually showed over the
    # past year, as of this day — what metrics.options_cost_estimate ranks
    # today's reading against until real IV history is long enough.
    hv_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hv_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class EventMoveSnapshotRecord(Base):
    """One event-implied-move reading per catalyst per day (see
    metrics.event_implied_move). Recorded for every upcoming catalyst,
    not only those in their entry window, so that over time each
    catalyst has a series from far out to close in — the data needed to
    see whether, and by how much, the market's pricing of an event
    builds as its date approaches."""

    __tablename__ = "event_move_snapshots"
    __table_args__ = (UniqueConstraint("ticker", "catalyst_date", "as_of"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    event_move: Mapped[float] = mapped_column(Float)
    baseline_iv: Mapped[float] = mapped_column(Float)
    post_event_iv: Mapped[float] = mapped_column(Float)
    event_share: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String)
    spot_price: Mapped[float] = mapped_column(Float)


class ExitNoticeRecord(Base):
    """Marks an exit reminder as sent, so it is sent once. Permanent on
    purpose (unlike the alert cooldown, which expires): an exit date
    passes only once."""

    __tablename__ = "exit_notices"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LLMReviewRecord(Base):
    """Claude's reading of the company's own filings about one catalyst's
    drug (engines/clinical_nlp/llm_review.py): one row per catalyst,
    replaced when it is re-read. Since 2026-10-09 its score is the
    evidence score the signal rules act on (pipeline.run_candidate_pipeline);
    the point-system score is kept beside it for comparison."""

    __tablename__ = "llm_reviews"
    __table_args__ = (UniqueConstraint("ticker", "event_type", "catalyst_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model: Mapped[str] = mapped_column(String)
    # score is None when the filings held nothing to review ("NO_EVIDENCE"):
    # the row then only records that it was looked at, so it isn't retried daily.
    status: Mapped[str] = mapped_column(String)          # REVIEWED | NO_EVIDENCE
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    primary_endpoint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    evidence_quality: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    drug: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    indication: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    application: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    positives: Mapped[list] = mapped_column(JSON)
    risks: Mapped[list] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON)
    date_moved_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)   # the review found the FDA date has moved to this


class LLMReviewHistoryRecord(Base):
    """Every evidence review ever written, never changed or removed.
    LLMReviewRecord holds only the newest review of a catalyst; this
    table keeps each one as it was on the day, so the reasoning behind an
    old score can still be read after the catalyst is reviewed again.
    Same columns as LLMReviewRecord, plus the rules that produced it
    (config/rules.py; empty for reviews written before the rules were
    numbered)."""

    __tablename__ = "llm_review_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    model: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    primary_endpoint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    evidence_quality: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    drug: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    indication: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    application: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    positives: Mapped[list] = mapped_column(JSON)
    risks: Mapped[list] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON)
    date_moved_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    rules_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rules_fingerprint: Mapped[Optional[str]] = mapped_column(String, nullable=True)


_REVIEW_FIELDS = (
    "reviewed_at", "model", "status", "score", "primary_endpoint", "evidence_quality", "drug", "indication",
    "application", "summary", "positives", "risks", "sources", "date_moved_to",
)


class CatalystOutcomeRecord(Base):
    """
    What actually happened at a catalyst: the FDA's decision and the
    stock's reaction. One row per catalyst (ticker + event type + date),
    not per prediction — a catalyst is scored many times but decided once.

    `status` is the row's standing:
      PENDING    looked for a decision in the company's filings, none found yet
      SUGGESTED  a filing appears to state one (outcome / evidence_* filled in)
                 — a guess by text matching, NOT yet a fact
      CONFIRMED  an administrator has confirmed the outcome
    Only CONFIRMED rows count anywhere: the track record, calibration,
    and the price-reaction lookup all ignore the other two.

    `outcome`: APPROVED | CRL (rejected) | DELAYED (the FDA moved the
    date) | WITHDRAWN | INVALID (it was never a real decision date —
    a misread filing or a superseded date).

    The price columns are the closing prices on the last trading day
    before the decision date and the first trading day after it. Using
    the days either side captures the reaction whether the news came out
    before the open or after the close.
    """

    __tablename__ = "catalyst_outcomes"
    __table_args__ = (UniqueConstraint("ticker", "event_type", "catalyst_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date, index=True)

    status: Mapped[str] = mapped_column(String, index=True)
    outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    decision_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    evidence_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    evidence_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    confirmed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # users.id
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    price_before: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_after: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # e.g. 0.42 = +42%


class TrialOverrideRecord(Base):
    """An administrator's decision about which clinical trial a catalyst
    is really about. The scan's own trial matching reads free text and is
    often unsure ("needs manual review"); a row here overrides it, and
    the scan then scores that catalyst on the confirmed trial.

    Keyed by ticker + event type + catalyst date rather than by the
    filing it came from, because the filing behind an event changes as
    newer ones are published. If the catalyst date itself moves, the
    scan copies the override to the new date when it can tell the two
    are the same decision (pipeline.carry_over_confirmed_trials);
    otherwise the event goes back to review."""

    __tablename__ = "trial_overrides"
    __table_args__ = (UniqueConstraint("ticker", "event_type", "catalyst_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    catalyst_date: Mapped[date] = mapped_column(Date)
    nct_id: Mapped[str] = mapped_column(String)
    trial_title: Mapped[str] = mapped_column(String)
    confirmed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # users.id
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PipelineLockRecord(Base):
    """One row per currently-held pipeline lockout — see core/cache.py,
    which owns all the logic. Timestamps are epoch seconds (floats), not
    DateTime, so expiry comparisons behave identically on SQLite and
    Postgres with no timezone handling involved."""

    __tablename__ = "pipeline_locks"

    key: Mapped[str] = mapped_column(String, primary_key=True)  # uniqueness IS the lock
    token: Mapped[str] = mapped_column(String)                  # proves ownership on release
    acquired_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)


# --------------------------------------------------------------------------
# Engine / session plumbing
# --------------------------------------------------------------------------

_engine = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        # aiosqlite runs each connection on its own NON-daemon worker
        # thread. With SQLAlchemy's default pool, a connection (and its
        # thread) stays open after the last session closes, so the Python
        # process never exits — observed for real: a finished test run sat
        # idle ~30 min until the OS killed it. NullPool closes the
        # connection as each session ends, so a caller that forgets
        # dispose_db() still exits cleanly. Postgres keeps its normal pool.
        kwargs = {"poolclass": NullPool} if settings.DATABASE_URL.startswith("sqlite") else {}
        _engine = create_async_engine(settings.DATABASE_URL, echo=False, **kwargs)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_db() -> None:
    """Closes all pooled connections. Call once at shutdown (required for a
    pooled Postgres engine; harmless for SQLite). The engine is rebuilt
    lazily if anything uses the database again afterwards."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()  # ensures _session_factory is initialized
    assert _session_factory is not None
    return _session_factory


# Columns added to a table AFTER it already existed on a real database.
# create_all() only creates missing tables — it never alters one — so each
# column listed here is added by hand at start-up if it isn't there yet.
# This covers exactly one kind of change: adding a nullable column. A
# rename, a type change, a new constraint or a data backfill still needs
# a real migration tool (Alembic), which this project doesn't have yet.
_ADDED_COLUMNS = (
    ("users", "email_verified_at", "TIMESTAMP WITH TIME ZONE"),
    ("predictions", "event_implied_move", "FLOAT"),
    ("predictions", "signal_notes", "VARCHAR"),
    ("predictions", "iv_rank_estimated", "BOOLEAN"),
    ("predictions", "evidence_basis", "VARCHAR"),
    ("predictions", "point_score", "FLOAT"),
    ("predictions", "rules_version", "INTEGER"),
    ("predictions", "rules_fingerprint", "VARCHAR"),
    ("llm_reviews", "date_moved_to", "DATE"),
    ("iv_snapshots", "hv_low", "FLOAT"),
    ("iv_snapshots", "hv_high", "FLOAT"),
)


def _add_missing_columns(sync_conn) -> None:
    if sync_conn.dialect.name == "postgresql":
        # IF NOT EXISTS: the website and the scanner can start at the same
        # moment and both get here; the second must not fail.
        for table, column, ddl in _ADDED_COLUMNS:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))
        return
    inspector = inspect(sync_conn)  # SQLite has no ADD COLUMN IF NOT EXISTS
    for table, column, ddl in _ADDED_COLUMNS:
        if column not in {col["name"] for col in inspector.get_columns(table)}:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def init_db() -> None:
    """Creates all tables if they don't already exist, and adds any
    column in _ADDED_COLUMNS that an older database is missing. Safe to
    call every startup."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def reset_db() -> None:
    """Drops and recreates all tables — for tests only, never call this in production use."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


# --------------------------------------------------------------------------
# CatalystEvent persistence
# --------------------------------------------------------------------------

async def _upsert_catalyst_event(session: AsyncSession, event, now: datetime) -> None:
    existing = await session.scalar(
        select(CatalystEventRecord).where(CatalystEventRecord.source_url == event.source_url)
    )
    if existing:
        existing.ticker = event.ticker or existing.ticker
        existing.company_name = event.company_name or existing.company_name
        existing.horizon_status = event.horizon_status
        existing.drug_or_trial_candidate = event.drug_or_trial_candidate
        existing.resolved_nct_id = event.resolved_nct_id
        existing.resolution_tier = event.resolution_tier
        existing.last_seen_at = now
    else:
        session.add(
            CatalystEventRecord(
                source_url=event.source_url,
                event_type=event.event_type,
                ticker=event.ticker,
                company_name=event.company_name,
                cik=event.cik,
                title=event.title,
                start_date=event.start_date,
                end_date=event.end_date,
                center=event.center,
                committee=event.committee,
                raw_source=event.raw_source,
                horizon_status=event.horizon_status,
                drug_or_trial_candidate=event.drug_or_trial_candidate,
                resolved_nct_id=event.resolved_nct_id,
                resolution_tier=event.resolution_tier,
                first_seen_at=now,
                last_seen_at=now,
            )
        )


async def upsert_catalyst_event(event) -> None:
    """
    Inserts a new catalyst, or updates last_seen_at + mutable fields
    (horizon_status, resolution fields can change between scans as more
    filings are found) if source_url already exists.
    """
    await upsert_catalyst_events([event])


async def upsert_catalyst_events(events) -> None:
    """Upserts a whole scan's events in one session / one transaction."""
    factory = get_session_factory()
    now = datetime.now(timezone.utc)
    async with factory() as session:
        for event in events:
            await _upsert_catalyst_event(session, event, now)
        await session.commit()


# --------------------------------------------------------------------------
# PipelineResult (prediction) persistence
# --------------------------------------------------------------------------

async def save_prediction(result, scanned_at: Optional[datetime] = None) -> int:
    """Persists one pipeline.PipelineResult. Returns the new row's id."""
    c, fs, plan = result.candidate, result.fundamental_score, result.execution_plan
    factory = get_session_factory()

    record = PredictionRecord(
        ticker=c.ticker,
        nct_id=fs.nct_id,
        catalyst_type=c.catalyst_type,
        catalyst_date=c.catalyst_date,
        p_fundamental=fs.p_fundamental,
        fundamental_breakdown=[
            {"label": a.label, "delta": a.delta, "reason": a.reason} for a in fs.adjustments
        ],
        fundamental_base_rate=fs.base_rate,
        spot_price=c.spot_price,
        p_market=c.p_market,
        p_market_confidence=c.p_market_confidence,
        iv_rank=c.iv_rank,
        event_implied_move=c.event_implied_move,
        signal_notes=c.signal_notes,
        iv_rank_estimated=c.iv_rank_estimated,
        evidence_basis=c.evidence_basis,
        point_score=c.point_score,
        open_interest=c.open_interest,
        daily_volume=c.daily_options_volume,
        divergence_delta=c.divergence_delta,
        arbitrage_signal=c.arbitrage_signal,
        hold_state=plan.hold_state,
        use_options=int(plan.use_options),
        allocation_pct=plan.allocation_pct,
        allocation_dollars=plan.allocation_dollars,
        trailing_stop_pct=plan.trailing_stop_pct,
        scanned_at=scanned_at or datetime.now(timezone.utc),
        **rules.stamp(),
    )

    async with factory() as session:
        session.add(record)
        await session.commit()
        return record.id


async def get_latest_predictions(limit: int = 500) -> list[PredictionRecord]:
    """
    The most recent prediction for each (ticker, catalyst date), newest
    first — every scan adds a row per candidate, so this is "where each
    candidate stands now" rather than the full history. Looks at the
    newest `limit` rows only.
    """
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(PredictionRecord).order_by(PredictionRecord.scanned_at.desc()).limit(limit)
        )
        latest: dict[tuple[str, date], PredictionRecord] = {}
        for row in rows:
            latest.setdefault((row.ticker, row.catalyst_date), row)
        return list(latest.values())


async def get_catalysts_in_entry_window() -> list[CatalystEventRecord]:
    """
    Catalysts inside their entry window as of the most recent scan that
    reached each source. Restricted to that scan's rows because
    horizon_status is only refreshed when an event is seen again — an
    event that has since dropped out of the source would otherwise show a
    stale status.

    "Most recent" is taken PER SOURCE (FDA calendar, SEC filings), not
    across the whole table: a scan where one source was unreachable only
    refreshes the other source's rows, and judged by a single global
    "latest scan" every catalyst from the unreachable source would vanish
    from this list until the next clean scan. (Seen for real: one scan
    without SEC access emptied the dashboard's entry-window section.)
    """
    same_source = aliased(CatalystEventRecord)
    latest_for_source = (
        select(func.max(same_source.last_seen_at))
        .where(same_source.raw_source == CatalystEventRecord.raw_source)
        .scalar_subquery()
    )
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(CatalystEventRecord)
            .where(
                CatalystEventRecord.last_seen_at == latest_for_source,
                CatalystEventRecord.horizon_status.like("%ENTRY_WINDOW"),
            )
            .order_by(CatalystEventRecord.start_date)
        )
        return list(rows)


async def get_upcoming_catalysts(today: date) -> list[CatalystEventRecord]:
    """
    Every FDA decision and advisory meeting still ahead that names a
    listed company, soonest first — the dashboard's main list. One row
    per (ticker, event type, date): the same catalyst is often stated in
    several filings, and the row that carries a trial match is preferred.
    Restricted, like the other "current" queries, to rows the latest
    scan of their source returned.
    """
    same_source = aliased(CatalystEventRecord)
    latest_for_source = (
        select(func.max(same_source.last_seen_at))
        .where(same_source.raw_source == CatalystEventRecord.raw_source)
        .scalar_subquery()
    )
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(CatalystEventRecord)
            .where(
                CatalystEventRecord.event_type.in_(("PDUFA", "ADCOM")),
                CatalystEventRecord.ticker.is_not(None),
                CatalystEventRecord.last_seen_at == latest_for_source,
                CatalystEventRecord.start_date >= today,
            )
            .order_by(CatalystEventRecord.start_date, CatalystEventRecord.ticker)
        )
        unique: dict[tuple[str, str, date], CatalystEventRecord] = {}
        for row in rows:
            key = (row.ticker, row.event_type, row.start_date)
            if key not in unique or (row.resolved_nct_id and not unique[key].resolved_nct_id):
                unique[key] = row
        return list(unique.values())


async def get_iv_history_days() -> int:
    """How many distinct days of implied-volatility readings exist — the
    progress toward the IV_RANK_MIN_READINGS that IV Rank needs."""
    factory = get_session_factory()
    async with factory() as session:
        return await session.scalar(select(func.count(func.distinct(IVSnapshotRecord.as_of)))) or 0


async def get_last_scan_time() -> Optional[datetime]:
    factory = get_session_factory()
    async with factory() as session:
        return await session.scalar(select(func.max(CatalystEventRecord.last_seen_at)))


async def get_phase3_watchlist(limit: int = 200) -> list[CatalystEventRecord]:
    """
    Phase 3 readouts the most recent watch-list scan returned, soonest
    first. Includes trials whose completion date is earlier this month:
    the registry often gives only a month, stored as its first day, so
    "this month" must not be dropped as already past.
    """
    same_source = aliased(CatalystEventRecord)
    latest_for_source = (
        select(func.max(same_source.last_seen_at))
        .where(same_source.raw_source == CatalystEventRecord.raw_source)
        .scalar_subquery()
    )
    month_start = date.today().replace(day=1)
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(CatalystEventRecord)
            .where(
                CatalystEventRecord.event_type == "PHASE_3_READOUT",
                CatalystEventRecord.last_seen_at == latest_for_source,
                CatalystEventRecord.start_date >= month_start,
            )
            .order_by(CatalystEventRecord.start_date, CatalystEventRecord.ticker)
            .limit(limit)
        )
        return list(rows)


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

OUTCOME_VALUES = ("APPROVED", "CRL", "DELAYED", "WITHDRAWN", "INVALID")


async def get_catalysts_needing_outcome(today: date, horizon_days: int, give_up_days: int) -> list[CatalystEventRecord]:
    """
    PDUFA catalysts with a ticker and no CONFIRMED outcome, whose date is
    between `give_up_days` ago and `horizon_days` ahead — the ones worth
    checking for a decision. Dates still ahead are included because the
    FDA sometimes decides early. One row per (ticker, date), the most
    recently seen. Restricted to rows the latest scan of their source
    returned, so a company since dropped as too large, or a date that has
    vanished from the filings, is not chased.
    """
    same_source = aliased(CatalystEventRecord)
    latest_for_source = (
        select(func.max(same_source.last_seen_at))
        .where(same_source.raw_source == CatalystEventRecord.raw_source)
        .scalar_subquery()
    )
    factory = get_session_factory()
    async with factory() as session:
        confirmed = {
            (r.ticker, r.catalyst_date)
            for r in await session.scalars(
                select(CatalystOutcomeRecord).where(
                    CatalystOutcomeRecord.status == "CONFIRMED", CatalystOutcomeRecord.event_type == "PDUFA"
                )
            )
        }
        rows = await session.scalars(
            select(CatalystEventRecord)
            .where(
                CatalystEventRecord.event_type == "PDUFA",
                CatalystEventRecord.ticker.is_not(None),
                CatalystEventRecord.last_seen_at == latest_for_source,
                CatalystEventRecord.start_date >= today - timedelta(days=give_up_days),
                CatalystEventRecord.start_date <= today + timedelta(days=horizon_days),
            )
            .order_by(CatalystEventRecord.start_date.desc(), CatalystEventRecord.last_seen_at.desc())
        )
        unique: dict[tuple[str, date], CatalystEventRecord] = {}
        for row in rows:
            if (row.ticker, row.start_date) not in confirmed:
                unique.setdefault((row.ticker, row.start_date), row)
        return list(unique.values())


async def get_outcome_rows() -> dict[tuple[str, str, date], CatalystOutcomeRecord]:
    """Every outcome row in any status, keyed by (ticker, event type, catalyst date)."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(select(CatalystOutcomeRecord))
        return {(r.ticker, r.event_type, r.catalyst_date): r for r in rows}


async def record_outcome_check(
    ticker: str, event_type: str, catalyst_date: date,
    outcome: Optional[str] = None, decision_date: Optional[date] = None,
    evidence_url: Optional[str] = None, evidence_text: Optional[str] = None,
) -> None:
    """
    Stores the result of looking for a decision: SUGGESTED with the
    evidence if one was found, PENDING if not. Never touches a CONFIRMED
    row — a person's decision is not overwritten by text matching.
    """
    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(
            select(CatalystOutcomeRecord).where(
                CatalystOutcomeRecord.ticker == ticker,
                CatalystOutcomeRecord.event_type == event_type,
                CatalystOutcomeRecord.catalyst_date == catalyst_date,
            )
        )
        if row is not None and row.status == "CONFIRMED":
            return
        if row is None:
            row = CatalystOutcomeRecord(ticker=ticker, event_type=event_type, catalyst_date=catalyst_date, status="PENDING")
            session.add(row)
        row.checked_at = datetime.now(timezone.utc)
        if outcome is not None:
            row.status, row.outcome, row.decision_date = "SUGGESTED", outcome, decision_date
            row.evidence_url, row.evidence_text = evidence_url, evidence_text
        await session.commit()


async def confirm_outcome(
    ticker: str, event_type: str, catalyst_date: date, outcome: str, decision_date: date,
    confirmed_by: Optional[int] = None,
) -> None:
    """An administrator's decision. Clears any stored price reaction,
    because it was measured around the previous decision date."""
    if outcome not in OUTCOME_VALUES:
        raise ValueError(f"unknown outcome {outcome!r}")
    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(
            select(CatalystOutcomeRecord).where(
                CatalystOutcomeRecord.ticker == ticker,
                CatalystOutcomeRecord.event_type == event_type,
                CatalystOutcomeRecord.catalyst_date == catalyst_date,
            )
        )
        if row is None:
            row = CatalystOutcomeRecord(ticker=ticker, event_type=event_type, catalyst_date=catalyst_date, status="PENDING")
            session.add(row)
        row.status, row.outcome, row.decision_date = "CONFIRMED", outcome, decision_date
        row.confirmed_by, row.confirmed_at = confirmed_by, datetime.now(timezone.utc)
        row.price_before = row.price_after = row.price_change_pct = None
        await session.commit()


async def unconfirm_outcome(ticker: str, event_type: str, catalyst_date: date) -> bool:
    """Undo a confirmation: the row is removed and the catalyst goes back
    to being looked up on the next scan."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            delete(CatalystOutcomeRecord).where(
                CatalystOutcomeRecord.ticker == ticker,
                CatalystOutcomeRecord.event_type == event_type,
                CatalystOutcomeRecord.catalyst_date == catalyst_date,
            )
        )
        await session.commit()
        return result.rowcount > 0


async def get_outcomes_needing_price() -> list[CatalystOutcomeRecord]:
    """Confirmed approvals and rejections with no price reaction stored yet."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(CatalystOutcomeRecord).where(
                CatalystOutcomeRecord.status == "CONFIRMED",
                CatalystOutcomeRecord.outcome.in_(("APPROVED", "CRL")),
                CatalystOutcomeRecord.decision_date.is_not(None),
                CatalystOutcomeRecord.price_change_pct.is_(None),
            )
        )
        return list(rows)


async def save_outcome_price(outcome_id: int, price_before: float, price_after: float) -> None:
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(CatalystOutcomeRecord, outcome_id)
        if row is not None:
            row.price_before, row.price_after = price_before, price_after
            row.price_change_pct = round(price_after / price_before - 1, 4) if price_before else None
            await session.commit()


async def get_track_record() -> list[dict]:
    """
    Every confirmed FDA decision (approvals and rejections), newest
    first, each joined to what this system said BEFORE the decision: the
    last prediction scanned before the decision date, and the last
    event-implied move recorded before it. Either can be missing — a
    catalyst may never have been scored — and is then None.

    "Before the decision" is enforced on the timestamps, not assumed: a
    score or reading taken on or after the decision date is never used,
    because by then the outcome was known.
    """
    factory = get_session_factory()
    async with factory() as session:
        outcomes = list(
            await session.scalars(
                select(CatalystOutcomeRecord)
                .where(
                    CatalystOutcomeRecord.status == "CONFIRMED",
                    CatalystOutcomeRecord.outcome.in_(("APPROVED", "CRL")),
                )
                .order_by(CatalystOutcomeRecord.decision_date.desc(), CatalystOutcomeRecord.ticker)
            )
        )
        record = []
        for o in outcomes:
            cutoff = datetime(o.decision_date.year, o.decision_date.month, o.decision_date.day, tzinfo=timezone.utc)
            prediction = await session.scalar(
                select(PredictionRecord)
                .where(
                    PredictionRecord.ticker == o.ticker,
                    PredictionRecord.catalyst_date == o.catalyst_date,
                    PredictionRecord.scanned_at < cutoff,
                )
                .order_by(PredictionRecord.scanned_at.desc())
                .limit(1)
            )
            reading = await session.scalar(
                select(EventMoveSnapshotRecord)
                .where(
                    EventMoveSnapshotRecord.ticker == o.ticker,
                    EventMoveSnapshotRecord.catalyst_date == o.catalyst_date,
                    EventMoveSnapshotRecord.as_of < o.decision_date,
                )
                .order_by(EventMoveSnapshotRecord.as_of.desc())
                .limit(1)
            )
            record.append(
                {
                    "ticker": o.ticker,
                    "event_type": o.event_type,
                    "catalyst_date": o.catalyst_date,
                    "decision_date": o.decision_date,
                    "outcome": o.outcome,
                    "price_change_pct": o.price_change_pct,
                    "p_fundamental": prediction.p_fundamental if prediction else None,
                    "signal": prediction.arbitrage_signal if prediction else None,
                    "event_move_priced": reading.event_move if reading else None,
                }
            )
        return record


# --------------------------------------------------------------------------
# Confirmed trials (manual review)
# --------------------------------------------------------------------------

async def set_trial_override(
    ticker: str, event_type: str, catalyst_date: date, nct_id: str, trial_title: str,
    confirmed_by: Optional[int] = None,
) -> None:
    """Confirms (or changes) the trial for one catalyst."""
    factory = get_session_factory()
    async with factory() as session:
        existing = await session.scalar(
            select(TrialOverrideRecord).where(
                TrialOverrideRecord.ticker == ticker,
                TrialOverrideRecord.event_type == event_type,
                TrialOverrideRecord.catalyst_date == catalyst_date,
            )
        )
        now = datetime.now(timezone.utc)
        if existing:
            existing.nct_id, existing.trial_title = nct_id, trial_title
            existing.confirmed_by, existing.confirmed_at = confirmed_by, now
        else:
            session.add(
                TrialOverrideRecord(
                    ticker=ticker, event_type=event_type, catalyst_date=catalyst_date,
                    nct_id=nct_id, trial_title=trial_title, confirmed_by=confirmed_by, confirmed_at=now,
                )
            )
        await session.commit()


async def remove_trial_override(ticker: str, event_type: str, catalyst_date: date) -> bool:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            delete(TrialOverrideRecord).where(
                TrialOverrideRecord.ticker == ticker,
                TrialOverrideRecord.event_type == event_type,
                TrialOverrideRecord.catalyst_date == catalyst_date,
            )
        )
        await session.commit()
        return result.rowcount > 0


async def save_llm_review(ticker: str, event_type: str, catalyst_date: date, review, model: str) -> None:
    """Stores (or replaces) the review for one catalyst, and adds it to
    the permanent history. `review` is an llm_review.LLMReview, or None
    to record that nothing was found."""
    fields = dict(
        reviewed_at=datetime.now(timezone.utc), model=model,
        status="REVIEWED" if review is not None else "NO_EVIDENCE",
        score=review.score if review else None,
        primary_endpoint=review.primary_endpoint if review else None,
        evidence_quality=review.evidence_quality if review else None,
        drug=review.drug if review else None,
        indication=review.indication if review else None,
        application=review.application if review else None,
        summary=review.summary if review else None,
        positives=list(review.positives) if review else [],
        risks=list(review.risks) if review else [],
        sources=list(review.sources) if review else [],
        date_moved_to=getattr(review, "date_moved_to", None) if review else None,
    )
    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(
            select(LLMReviewRecord).where(
                LLMReviewRecord.ticker == ticker,
                LLMReviewRecord.event_type == event_type,
                LLMReviewRecord.catalyst_date == catalyst_date,
            )
        )
        key = dict(ticker=ticker, event_type=event_type, catalyst_date=catalyst_date)
        if row is None:
            session.add(LLMReviewRecord(**key, **fields))
        else:
            # A review written before the history existed is copied into
            # it first, so that replacing it here does not lose it.
            kept = await session.scalar(
                select(func.count()).select_from(LLMReviewHistoryRecord).where(
                    LLMReviewHistoryRecord.ticker == ticker,
                    LLMReviewHistoryRecord.event_type == event_type,
                    LLMReviewHistoryRecord.catalyst_date == catalyst_date,
                )
            )
            if not kept:
                session.add(LLMReviewHistoryRecord(**key, **{name: getattr(row, name) for name in _REVIEW_FIELDS}))
            for name, value in fields.items():
                setattr(row, name, value)
        session.add(LLMReviewHistoryRecord(**key, **fields, **rules.stamp()))
        await session.commit()


async def touch_llm_review(ticker: str, event_type: str, catalyst_date: date) -> None:
    """Marks a review as checked today without changing it — used when
    nothing new has been filed about the drug since it was written."""
    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(
            select(LLMReviewRecord).where(
                LLMReviewRecord.ticker == ticker,
                LLMReviewRecord.event_type == event_type,
                LLMReviewRecord.catalyst_date == catalyst_date,
            )
        )
        if row is not None:
            row.reviewed_at = datetime.now(timezone.utc)
            await session.commit()


async def get_llm_reviews() -> dict[tuple[str, str, date], LLMReviewRecord]:
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(select(LLMReviewRecord))
        return {(r.ticker, r.event_type, r.catalyst_date): r for r in rows}


async def get_trial_overrides() -> dict[tuple[str, str, date], TrialOverrideRecord]:
    """Every confirmed trial, keyed by (ticker, event type, catalyst date)."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(select(TrialOverrideRecord))
        return {(r.ticker, r.event_type, r.catalyst_date): r for r in rows}


# --------------------------------------------------------------------------
# Phase A screen-outs
# --------------------------------------------------------------------------

async def save_screened_candidate(
    ticker: str,
    catalyst_type: str,
    catalyst_date: date,
    spot_price: Optional[float],
    open_interest: int,
    daily_volume: int,
    reasons: list[str],
    scanned_at: Optional[datetime] = None,
) -> int:
    factory = get_session_factory()
    record = ScreenedCandidateRecord(
        ticker=ticker,
        catalyst_type=catalyst_type,
        catalyst_date=catalyst_date,
        spot_price=spot_price,
        open_interest=open_interest,
        daily_volume=daily_volume,
        reasons=reasons,
        scanned_at=scanned_at or datetime.now(timezone.utc),
    )
    async with factory() as session:
        session.add(record)
        await session.commit()
        return record.id


async def get_latest_screened(limit: int = 500) -> list[ScreenedCandidateRecord]:
    """The most recent screen-out for each (ticker, catalyst date)."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(ScreenedCandidateRecord).order_by(ScreenedCandidateRecord.scanned_at.desc()).limit(limit)
        )
        latest: dict[tuple[str, date], ScreenedCandidateRecord] = {}
        for row in rows:
            latest.setdefault((row.ticker, row.catalyst_date), row)
        return list(latest.values())


# --------------------------------------------------------------------------
# Implied-volatility history
# --------------------------------------------------------------------------

async def save_iv_snapshot(
    ticker: str, as_of: date, atm_iv: float, expiration: date, spot_price: float,
    hv_low: Optional[float] = None, hv_high: Optional[float] = None,
) -> None:
    """Records the day's reading; a second scan on the same day replaces it."""
    factory = get_session_factory()
    async with factory() as session:
        existing = await session.scalar(
            select(IVSnapshotRecord).where(IVSnapshotRecord.ticker == ticker, IVSnapshotRecord.as_of == as_of)
        )
        if existing:
            existing.atm_iv, existing.expiration, existing.spot_price = atm_iv, expiration, spot_price
            existing.hv_low, existing.hv_high = hv_low, hv_high
        else:
            session.add(
                IVSnapshotRecord(ticker=ticker, as_of=as_of, atm_iv=atm_iv, expiration=expiration,
                                 spot_price=spot_price, hv_low=hv_low, hv_high=hv_high)
            )
        await session.commit()


async def get_iv_snapshot(ticker: str, as_of: date) -> Optional[IVSnapshotRecord]:
    factory = get_session_factory()
    async with factory() as session:
        return await session.scalar(
            select(IVSnapshotRecord).where(IVSnapshotRecord.ticker == ticker, IVSnapshotRecord.as_of == as_of)
        )


async def get_iv_history(ticker: str, since: date) -> list[tuple[date, float]]:
    """(day, IV) readings for the ticker on or after `since`, oldest first."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.execute(
            select(IVSnapshotRecord.as_of, IVSnapshotRecord.atm_iv)
            .where(IVSnapshotRecord.ticker == ticker, IVSnapshotRecord.as_of >= since)
            .order_by(IVSnapshotRecord.as_of)
        )
        return [(day, iv) for day, iv in rows]


# --------------------------------------------------------------------------
# Event-implied move history
# --------------------------------------------------------------------------

async def save_event_move_snapshot(
    ticker: str, event_type: str, catalyst_date: date, as_of: date, event_move: float,
    baseline_iv: float, post_event_iv: float, event_share: float, confidence: str, spot_price: float,
) -> None:
    """Records the day's reading; a second scan on the same day replaces it."""
    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(
            select(EventMoveSnapshotRecord).where(
                EventMoveSnapshotRecord.ticker == ticker,
                EventMoveSnapshotRecord.catalyst_date == catalyst_date,
                EventMoveSnapshotRecord.as_of == as_of,
            )
        )
        if row is None:
            row = EventMoveSnapshotRecord(ticker=ticker, catalyst_date=catalyst_date, as_of=as_of, event_type=event_type,
                                          event_move=0.0, baseline_iv=0.0, post_event_iv=0.0, event_share=0.0,
                                          confidence="", spot_price=0.0)
            session.add(row)
        row.event_type, row.event_move, row.baseline_iv = event_type, event_move, baseline_iv
        row.post_event_iv, row.event_share, row.confidence, row.spot_price = post_event_iv, event_share, confidence, spot_price
        await session.commit()


async def get_latest_event_moves(limit: int = 2000) -> list[EventMoveSnapshotRecord]:
    """The most recent reading for each catalyst that hasn't happened
    yet, soonest catalyst first."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(EventMoveSnapshotRecord)
            .where(EventMoveSnapshotRecord.catalyst_date >= date.today())
            .order_by(EventMoveSnapshotRecord.as_of.desc())
            .limit(limit)
        )
        latest: dict[tuple[str, date], EventMoveSnapshotRecord] = {}
        for row in rows:
            latest.setdefault((row.ticker, row.catalyst_date), row)
    return sorted(latest.values(), key=lambda r: (r.catalyst_date, r.ticker))


# --------------------------------------------------------------------------
# Exit reminders
# --------------------------------------------------------------------------

async def claim_exit_notice(key: str) -> bool:
    """True if this call is the one that gets to send the reminder for
    `key`; False if it was already claimed. Same primary-key trick as
    core/cache.py's locks, without an expiry."""
    factory = get_session_factory()
    async with factory() as session:
        session.add(ExitNoticeRecord(key=key, sent_at=datetime.now(timezone.utc)))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
    return True


async def release_exit_notice(key: str) -> None:
    """Undo a claim whose reminder reached nobody, so the next scan retries."""
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(ExitNoticeRecord).where(ExitNoticeRecord.key == key))
        await session.commit()


# --------------------------------------------------------------------------
# Alert persistence (dashboard webhook)
# --------------------------------------------------------------------------

async def save_alert(ticker: str, arbitrage_signal: str, catalyst_date: date, payload: dict) -> int:
    """Stores one received alert. `payload` must already be JSON-safe."""
    factory = get_session_factory()
    record = AlertRecord(
        ticker=ticker,
        arbitrage_signal=arbitrage_signal,
        catalyst_date=catalyst_date,
        payload=payload,
        received_at=datetime.now(timezone.utc),
    )
    async with factory() as session:
        session.add(record)
        await session.commit()
        return record.id


async def save_alert_once(
    ticker: str,
    arbitrage_signal: str,
    catalyst_date: date,
    payload: dict,
    within_seconds: float,
) -> Optional[int]:
    """
    Stores the alert unless the same (ticker, catalyst date, signal) was
    already stored in the last `within_seconds`. Returns the new row's
    id, or None if it was a repeat. This is what lets every scan report
    an actionable signal without the dashboard filling with copies.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
    factory = get_session_factory()
    async with factory() as session:
        recent = await session.scalar(
            select(AlertRecord.id)
            .where(
                AlertRecord.ticker == ticker,
                AlertRecord.arbitrage_signal == arbitrage_signal,
                AlertRecord.catalyst_date == catalyst_date,
                AlertRecord.received_at > cutoff,
            )
            .limit(1)
        )
    if recent is not None:
        return None
    return await save_alert(ticker, arbitrage_signal, catalyst_date, payload)


async def get_recent_alerts(limit: int = 100) -> list[AlertRecord]:
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(AlertRecord).order_by(AlertRecord.received_at.desc(), AlertRecord.id.desc()).limit(limit)
        )
        return list(rows)


async def get_predictions_for_ticker(ticker: str) -> list[PredictionRecord]:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.scalars(
            select(PredictionRecord)
            .where(PredictionRecord.ticker == ticker)
            .order_by(PredictionRecord.scanned_at.desc())
        )
        return list(result)


async def get_predictions_awaiting_outcome(as_of: Optional[date] = None) -> list[PredictionRecord]:
    """
    Predictions whose catalyst date has already passed but whose outcome
    hasn't been recorded yet — exactly the backlog a future outcome-
    recording step would work through.
    """
    as_of = as_of or date.today()
    factory = get_session_factory()
    async with factory() as session:
        result = await session.scalars(
            select(PredictionRecord).where(
                PredictionRecord.catalyst_date < as_of,
                PredictionRecord.outcome_recorded_at.is_(None),
            )
        )
        return list(result)
