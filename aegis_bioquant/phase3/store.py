"""
phase3/store.py
----------------
The Phase 3 forecasts' tables, kept beside the main ones in the same
database (they share core/database.py's Base and connection) but written
only by this package.

  phase3_reviews         the newest review of each trial, replaced when redone
  phase3_review_history  every review ever written, never changed
  phase3_scores          one row per trial per daily scan: the forecast as it
                         stood that day, stamped with the Phase 3 rules. These
                         rows are what the track record will grade.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from aegis_bioquant.core.database import Base, CatalystEventRecord, get_engine, get_session_factory
from aegis_bioquant.phase3 import constants as k
from aegis_bioquant.phase3 import rules


class _ReviewColumns:
    ticker: Mapped[str] = mapped_column(String, index=True)
    trial: Mapped[str] = mapped_column(String)
    completion_date: Mapped[date] = mapped_column(Date)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)        # review.STATUS_*
    score: Mapped[int] = mapped_column(Integer)
    evidence_quality: Mapped[str] = mapped_column(String)
    drug: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    condition: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    science: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    team: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    experts: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    warning_signs: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    positives: Mapped[list] = mapped_column(JSON)
    risks: Mapped[list] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON)
    expected_readout: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    results_announced_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Phase3ReviewRecord(_ReviewColumns, Base):
    __tablename__ = "phase3_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nct_id: Mapped[str] = mapped_column(String, unique=True, index=True)


class Phase3ReviewHistoryRecord(_ReviewColumns, Base):
    __tablename__ = "phase3_review_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nct_id: Mapped[str] = mapped_column(String, index=True)
    rules_version: Mapped[int] = mapped_column(Integer)
    rules_fingerprint: Mapped[str] = mapped_column(String)


class Phase3ScoreRecord(Base):
    __tablename__ = "phase3_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nct_id: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    trial: Mapped[str] = mapped_column(String)
    completion_date: Mapped[date] = mapped_column(Date)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    p_success: Mapped[float] = mapped_column(Float)
    base_rate: Mapped[float] = mapped_column(Float)
    evidence_quality: Mapped[str] = mapped_column(String)
    spot_price: Mapped[float] = mapped_column(Float)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # the review this score came from
    rules_version: Mapped[int] = mapped_column(Integer)
    rules_fingerprint: Mapped[str] = mapped_column(String)


_FIELDS = ("status", "score", "evidence_quality", "drug", "condition", "science", "team", "experts", "warning_signs",
           "summary", "positives", "risks", "sources", "expected_readout", "results_announced_on", "model", "cost_usd")


async def init_tables() -> None:
    """Creates the Phase 3 tables if missing (create_all never alters an
    existing table). Safe to call on every run."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def trials_to_forecast(today: date) -> list[CatalystEventRecord]:
    """Phase 3 trials of listed companies whose estimated primary
    completion falls in the forecast window, soonest first, one row per
    trial."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await session.scalars(
            select(CatalystEventRecord)
            .where(
                CatalystEventRecord.event_type == "PHASE_3_READOUT",
                CatalystEventRecord.ticker.is_not(None),
                CatalystEventRecord.resolved_nct_id.is_not(None),
                CatalystEventRecord.start_date >= today - timedelta(days=k.P3_LOOKBACK_DAYS),
                CatalystEventRecord.start_date <= today + timedelta(days=k.P3_LOOKAHEAD_DAYS),
            )
            .order_by(CatalystEventRecord.last_seen_at.desc())
        )
        newest: dict[str, CatalystEventRecord] = {}
        for row in rows:
            newest.setdefault(row.resolved_nct_id, row)
    return sorted(newest.values(), key=lambda r: (r.start_date, r.ticker or ""))


async def get_reviews() -> dict[str, Phase3ReviewRecord]:
    factory = get_session_factory()
    async with factory() as session:
        return {r.nct_id: r for r in await session.scalars(select(Phase3ReviewRecord))}


async def save_review(event: CatalystEventRecord, review, now: Optional[datetime] = None) -> Phase3ReviewRecord:
    """Stores (or replaces) the review of one trial and adds it to the
    history. `review` is a review.Phase3Review."""
    now = now or datetime.now(timezone.utc)
    fields = {name: getattr(review, name) for name in _FIELDS}
    fields["positives"], fields["risks"], fields["sources"] = list(review.positives), list(review.risks), list(review.sources)
    fields.update(ticker=event.ticker, trial=_trial_name(event), completion_date=event.start_date, reviewed_at=now)
    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(select(Phase3ReviewRecord).where(Phase3ReviewRecord.nct_id == event.resolved_nct_id))
        if row is None:
            row = Phase3ReviewRecord(nct_id=event.resolved_nct_id, **fields)
            session.add(row)
        else:
            for name, value in fields.items():
                setattr(row, name, value)
        session.add(Phase3ReviewHistoryRecord(nct_id=event.resolved_nct_id, **fields, **rules.stamp()))
        await session.commit()
        return row


async def touch_review(nct_id: str, now: Optional[datetime] = None) -> None:
    """Marks a review as checked without changing it (nothing new filed)."""
    factory = get_session_factory()
    async with factory() as session:
        row = await session.scalar(select(Phase3ReviewRecord).where(Phase3ReviewRecord.nct_id == nct_id))
        if row is not None:
            row.reviewed_at = now or datetime.now(timezone.utc)
            await session.commit()


async def save_score(event: CatalystEventRecord, review: Phase3ReviewRecord, spot_price: float,
                     now: Optional[datetime] = None) -> None:
    factory = get_session_factory()
    async with factory() as session:
        session.add(Phase3ScoreRecord(
            nct_id=event.resolved_nct_id, ticker=event.ticker, trial=_trial_name(event), completion_date=event.start_date,
            scored_at=now or datetime.now(timezone.utc), p_success=review.score / 100, base_rate=k.P3_BASE_RATE,
            evidence_quality=review.evidence_quality, spot_price=spot_price, reviewed_at=review.reviewed_at,
            **rules.stamp(),
        ))
        await session.commit()


async def get_scores(nct_id: Optional[str] = None) -> list[Phase3ScoreRecord]:
    factory = get_session_factory()
    async with factory() as session:
        query = select(Phase3ScoreRecord).order_by(Phase3ScoreRecord.scored_at)
        if nct_id:
            query = query.where(Phase3ScoreRecord.nct_id == nct_id)
        return list(await session.scalars(query))


def _trial_name(event: CatalystEventRecord) -> str:
    return (event.drug_or_trial_candidate or event.title or event.resolved_nct_id or "")[:300]
