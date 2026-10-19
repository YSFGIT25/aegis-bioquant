"""
analysis/run_analysis.py
-------------------------
The pre-registered analysis of Aegis-BioQuant's version-1 record, as set
out in the analysis plan:
https://claude.ai/code/artifact/45bc3b6d-d2b0-4da7-af67-fe2f53e7094b

Written on 2026-10-18, before any scored FDA decision had resolved (the
first, IONS, is due 2026-10-26), and tested only on made-up data
(tests/analysis_test.py). Every number the plan fixes is a constant at
the top of this file, so the code can be checked against the plan line
by line. Changing any of them after a scored decision has resolved is a
change to the plan and goes in its change log.

    python analysis/run_analysis.py interim      descriptive only (cut-off 2027-04-15)
    python analysis/run_analysis.py primary      the H1 test (cut-off 2027-10-31)
    python analysis/run_analysis.py extension    rerun, only if year one had < 4 CRLs

It reads the database named by DATABASE_URL and never writes to it: the
session is put in read-only mode before the first query. The report is
printed and saved under analysis/output/, with one CSV row per decision.

ANNOUNCEMENT TIMES: the plan's primary score is the last score stored at
least 24 hours before a decision's first public announcement. The
database stores the decision's date but not the time it became public,
so the reviewer records that time in analysis/announcements.csv when
confirming each outcome: the earliest timestamp of the company's press
release or SEC filing, in UTC, with the link it came from. A confirmed
decision with no row there is listed and left out; `primary` refuses to
report until every one has a row.

Not part of the scanner. Nothing here is in the files the rules
fingerprint covers, and the hosted image does not include this folder.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
from sqlalchemy import select, text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from aegis_bioquant.core import database as db  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Fixed by the plan. Copied here, not imported from config/constants.py, so a
# later change to the scanner's settings cannot move the analysis.
# ---------------------------------------------------------------------------
RULES_VERSION = 1
FINGERPRINT = "a185ea010c25"
RECORD_START = date(2026, 10, 12)
BASE_RATE = 0.83                      # FUNDAMENTAL_BASE_RATE_PRIOR at rules-v1
EVENT_TYPE = "PDUFA"                  # Phase 3 readouts are never scored
ANNOUNCEMENT_GAP = timedelta(hours=24)
ENTRY_WINDOW_DAYS = 35                # PDUFA_WINDOW_MIN_DAYS at rules-v1
STALE_AFTER_DAYS = 7
MIN_CRLS_FOR_H1 = 4
MIN_CRLS_FOR_H2_H3 = 3
BANDS = ((0.0, 0.60), (0.60, 0.80), (0.80, 1.01))   # under 0.60, 0.60-0.80, 0.80 or more
CLIP = (0.01, 0.99)                   # log loss
RESAMPLES = 10_000
SEED = 20261012                       # the record's start date, fixed in advance
AI_REVIEWED = {"CLAUDE", "CLAUDE_THIN"}
NO_SIGNAL = "NO_SIGNAL"


@dataclass(frozen=True)
class Run:
    name: str
    last_decision: date               # decisions dated up to this day
    confirmed_by: date                # ... and confirmed by the end of this day
    tests: bool                       # False = descriptive only


RUNS = {
    "interim": Run("interim", date(2027, 3, 31), date(2027, 4, 15), tests=False),
    "primary": Run("primary", date(2027, 10, 12), date(2027, 10, 31), tests=True),
    "extension": Run("extension", date(2028, 10, 12), date(2028, 10, 31), tests=True),
}


# ---------------------------------------------------------------------------
# Reading the record
# ---------------------------------------------------------------------------
@dataclass
class Score:
    scanned_at: datetime
    p: float
    rules_version: Optional[int]
    fingerprint: Optional[str]
    evidence_basis: Optional[str]
    signal: str
    iv_rank_estimated: Optional[bool]


@dataclass
class Record:
    outcomes: list                    # CatalystOutcomeRecord rows, CONFIRMED only
    scores: dict                      # (ticker, catalyst_date) -> [Score], oldest first
    moves: dict                       # (ticker, catalyst_date) -> [(as_of, event_move)]
    scan_days: set                    # days with at least one version-1 score


def _utc(moment: datetime) -> datetime:
    # SQLite hands timestamps back without a zone; everything is stored in UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def load_record() -> Record:
    factory = db.get_session_factory()
    async with factory() as session:
        dialect = session.bind.dialect.name
        if dialect == "postgresql":
            await session.execute(text("SET TRANSACTION READ ONLY"))
        elif dialect == "sqlite":
            await session.execute(text("PRAGMA query_only = ON"))

        outcomes = list(await session.scalars(
            select(db.CatalystOutcomeRecord).where(
                db.CatalystOutcomeRecord.status == "CONFIRMED",
                db.CatalystOutcomeRecord.event_type == EVENT_TYPE,
            )
        ))
        scores: dict = {}
        scan_days: set = set()
        for row in await session.scalars(
            select(db.PredictionRecord)
            .where(db.PredictionRecord.catalyst_type == EVENT_TYPE)
            .order_by(db.PredictionRecord.scanned_at)
        ):
            s = Score(_utc(row.scanned_at), row.p_fundamental, row.rules_version, row.rules_fingerprint,
                      row.evidence_basis, row.arbitrage_signal, row.iv_rank_estimated)
            scores.setdefault((row.ticker, row.catalyst_date), []).append(s)
            if s.rules_version == RULES_VERSION:
                scan_days.add(s.scanned_at.date())
        moves: dict = {}
        for row in await session.scalars(
            select(db.EventMoveSnapshotRecord)
            .where(db.EventMoveSnapshotRecord.event_type == EVENT_TYPE)
            .order_by(db.EventMoveSnapshotRecord.as_of)
        ):
            moves.setdefault((row.ticker, row.catalyst_date), []).append((row.as_of, row.event_move))
    await db.dispose_db()
    return Record(outcomes, scores, moves, scan_days)


def load_announcements(path: pathlib.Path) -> dict:
    """(ticker, catalyst_date) -> announcement time in UTC."""
    if not path.exists():
        return {}
    found = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("ticker", "").strip():
                continue
            moment = datetime.fromisoformat(row["announced_at_utc"].strip().replace("Z", "+00:00"))
            if moment.tzinfo is None:
                raise ValueError(f"{row['ticker']}: announced_at_utc needs a time zone (end it with Z)")
            found[(row["ticker"].strip(), date.fromisoformat(row["catalyst_date"].strip()))] = moment
    return found


# ---------------------------------------------------------------------------
# Choosing one prediction per decision
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    ticker: str
    catalyst_date: date
    decision_date: date
    announced_at: datetime
    outcome: int                      # 1 approved, 0 CRL
    price_change: Optional[float]
    primary: Score
    secondary: Optional[Score]
    event_move: Optional[float]
    stale: bool


@dataclass
class Selection:
    decisions: list = field(default_factory=list)
    excluded: dict = field(default_factory=dict)     # reason -> [ticker catalyst_date]
    off_fingerprint: list = field(default_factory=list)

    def exclude(self, reason: str, ticker: str, catalyst_date: date) -> None:
        self.excluded.setdefault(reason, []).append(f"{ticker} {catalyst_date}")


def select_decisions(record: Record, announcements: dict, run: Run,
                     keep: Callable[[Score], bool] = lambda s: True) -> Selection:
    """Applies the plan's eligibility rules. `keep` drops scores before
    selection; the fingerprint check uses it to rerun without the
    scores stamped with an unexpected fingerprint."""
    out = Selection()
    confirmed_cutoff = datetime(run.confirmed_by.year, run.confirmed_by.month, run.confirmed_by.day,
                                tzinfo=timezone.utc) + timedelta(days=1)
    for o in sorted(record.outcomes, key=lambda o: (o.decision_date or o.catalyst_date, o.ticker)):
        when = o.decision_date or o.catalyst_date
        if not (RECORD_START <= when <= run.last_decision):
            continue
        if o.confirmed_at is None or _utc(o.confirmed_at) >= confirmed_cutoff:
            out.exclude(f"confirmed after {run.confirmed_by}", o.ticker, o.catalyst_date)
            continue
        if o.outcome not in ("APPROVED", "CRL"):
            out.exclude(f"outcome {o.outcome}", o.ticker, o.catalyst_date)
            continue

        all_scores = record.scores.get((o.ticker, o.catalyst_date), [])
        v1 = [s for s in all_scores if s.rules_version == RULES_VERSION and keep(s)]
        if not v1:
            reason = "only scores from before 2026-10-12" if all_scores else "never scored"
            out.exclude(reason, o.ticker, o.catalyst_date)
            continue
        announced = announcements.get((o.ticker, o.catalyst_date))
        if announced is None:
            out.exclude("no announcement time in announcements.csv", o.ticker, o.catalyst_date)
            continue
        eligible = [s for s in v1 if s.scanned_at <= announced - ANNOUNCEMENT_GAP]
        if not eligible:
            out.exclude("no score 24 hours before the announcement", o.ticker, o.catalyst_date)
            continue

        primary = eligible[-1]
        window_opens = o.catalyst_date - timedelta(days=ENTRY_WINDOW_DAYS)
        if window_opens < RECORD_START:
            secondary = eligible[0]
        else:
            secondary = next((s for s in eligible if s.scanned_at.date() >= window_opens), None)
        moves = [m for as_of, m in record.moves.get((o.ticker, o.catalyst_date), [])
                 if as_of <= primary.scanned_at.date()]
        out.decisions.append(Decision(
            ticker=o.ticker, catalyst_date=o.catalyst_date, decision_date=when, announced_at=announced,
            outcome=1 if o.outcome == "APPROVED" else 0, price_change=o.price_change_pct,
            primary=primary, secondary=secondary, event_move=moves[-1] if moves else None,
            stale=(when - primary.scanned_at.date()).days > STALE_AFTER_DAYS,
        ))
        out.off_fingerprint += [f"{o.ticker} {o.catalyst_date} {s.scanned_at:%Y-%m-%d %H:%M} {s.fingerprint}"
                                for s in v1 if s.fingerprint != FINGERPRINT]
    return out


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------
def brier(p: np.ndarray, o: np.ndarray) -> float:
    return float(np.mean((p - o) ** 2))


def log_loss(p: np.ndarray, o: np.ndarray) -> float:
    p = np.clip(p, *CLIP)
    return float(-np.mean(o * np.log(p) + (1 - o) * np.log(1 - p)))


def auc(p: np.ndarray, o: np.ndarray) -> Optional[float]:
    """Chance an approval scored higher than a CRL; ties count half."""
    approved, crl = p[o == 1], p[o == 0]
    if len(approved) == 0 or len(crl) == 0:
        return None
    wins = (approved[:, None] > crl[None, :]).sum() + 0.5 * (approved[:, None] == crl[None, :]).sum()
    return float(wins / (len(approved) * len(crl)))


def bands(p: np.ndarray, o: np.ndarray) -> list:
    """(label, count, mean score, approval rate) per score band."""
    rows = []
    for lo, hi in BANDS:
        inside = (p >= lo) & (p < hi)
        n = int(inside.sum())
        label = f"{lo:.2f} to {min(hi, 1):.2f}" if hi <= 1 else f"{lo:.2f} or more"
        rows.append((label, n, float(p[inside].mean()) if n else None, float(o[inside].mean()) if n else None))
    return rows


def decomposition(p: np.ndarray, o: np.ndarray) -> dict:
    """Brier split over the plan's bands. With bands rather than exact
    score values, reliability - resolution + uncertainty differs from the
    Brier score by a small within-band term, reported as `within_band`."""
    n, rate = len(o), float(o.mean())
    reliability = resolution = 0.0
    for _, count, mean_p, mean_o in bands(p, o):
        if count:
            reliability += count / n * (mean_p - mean_o) ** 2
            resolution += count / n * (mean_o - rate) ** 2
    uncertainty = rate * (1 - rate)
    return {"reliability": reliability, "resolution": resolution, "uncertainty": uncertainty,
            "within_band": brier(p, o) - (reliability - resolution + uncertainty)}


def company_bootstrap(companies: list, stat: Callable[[np.ndarray], Optional[float]]) -> tuple:
    """Resamples companies with replacement, keeping all of a company's
    decisions together. Returns (low, high, resamples used) for a 95%
    percentile interval. A resample where the statistic is undefined (an
    AUC with no CRL drawn) is skipped and counted."""
    names = sorted(set(companies))
    rows_of = {name: np.flatnonzero(np.array(companies) == name) for name in names}
    rng = np.random.default_rng(SEED)
    values = []
    for _ in range(RESAMPLES):
        drawn = rng.choice(len(names), size=len(names), replace=True)
        idx = np.concatenate([rows_of[names[k]] for k in drawn])
        v = stat(idx)
        if v is not None:
            values.append(v)
    low, high = np.percentile(values, [2.5, 97.5]) if values else (float("nan"), float("nan"))
    return float(low), float(high), len(values)


def mann_whitney(a: np.ndarray, b: np.ndarray) -> tuple:
    """U for a against b, and a two-sided permutation p-value (10,000 shuffles)."""
    def u(x, y):
        return (x[:, None] > y[None, :]).sum() + 0.5 * (x[:, None] == y[None, :]).sum()
    observed = u(a, b)
    centre = len(a) * len(b) / 2
    pooled = np.concatenate([a, b])
    rng = np.random.default_rng(SEED)
    extreme = 0
    for _ in range(RESAMPLES):
        rng.shuffle(pooled)
        if abs(u(pooled[:len(a)], pooled[len(a):]) - centre) >= abs(observed - centre):
            extreme += 1
    return float(observed), (extreme + 1) / (RESAMPLES + 1)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def _f(x: Optional[float], digits: int = 3) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def h1_lines(decisions: list, which: str) -> list:
    return h1(decisions, which)[0]


def h1(decisions: list, which: str) -> tuple:
    """The H1 gap and its company-bootstrap interval for one set of
    scores: (report lines, upper end of the interval)."""
    use = [d for d in decisions if (d.primary if which == "primary" else d.secondary) is not None]
    if not use:
        return [f"  {which} score: no decisions"], float("nan")
    p = np.array([(d.primary if which == "primary" else d.secondary).p for d in use])
    o = np.array([d.outcome for d in use], dtype=float)
    companies = [d.ticker for d in use]
    gap = lambda idx: brier(p[idx], o[idx]) - brier(np.full(len(idx), BASE_RATE), o[idx])
    low, high, used = company_bootstrap(companies, gap)
    whole = np.arange(len(use))
    return [f"  {which} score: N={len(use)}, CRLs={int((o == 0).sum())}, "
            f"Brier(score)={brier(p, o):.4f}, Brier(0.83)={brier(np.full(len(o), BASE_RATE), o):.4f}",
            f"    gap = {gap(whole):+.4f}   95% interval [{low:+.4f}, {high:+.4f}]   ({used} resamples)"], high


def build_report(record: Record, announcements: dict, run: Run, today: date) -> tuple:
    sel = select_decisions(record, announcements, run)
    ds = sel.decisions
    L = [f"Aegis-BioQuant analysis: {run.name}",
         f"Run on {today}. Decisions dated {RECORD_START} to {run.last_decision}, confirmed by {run.confirmed_by}.",
         f"Rules version {RULES_VERSION}, fingerprint {FINGERPRINT}, base rate {BASE_RATE}, seed {SEED}.", ""]

    n_crl = sum(d.outcome == 0 for d in ds)
    L += [f"DECISIONS: {len(ds)} in the sample ({len(ds) - n_crl} approved, {n_crl} CRL)"]
    for reason, items in sorted(sel.excluded.items()):
        L.append(f"  excluded, {reason}: {len(items)}  ({', '.join(items)})")
    L.append("")
    L.append(f"  {'ticker':<6} {'PDUFA date':<11} {'decided':<11} {'outcome':<8} {'score':>6} "
             f"{'scored at (UTC)':<17} {'basis':<16} {'window':>6}  flags")
    for d in ds:
        flags = "STALE" if d.stale else ""
        L.append(f"  {d.ticker:<6} {d.catalyst_date!s:<11} {d.decision_date!s:<11} "
                 f"{'APPROVED' if d.outcome else 'CRL':<8} {d.primary.p:>6.3f} "
                 f"{d.primary.scanned_at:%Y-%m-%d %H:%M} {d.primary.evidence_basis or '-':<16} "
                 f"{_f(d.secondary.p if d.secondary else None):>6}  {flags}")
    stale = [d for d in ds if d.stale]
    L.append(f"  last score more than {STALE_AFTER_DAYS} days before the decision: {len(stale)}")

    weekdays = [RECORD_START + timedelta(days=k) for k in range((min(today, run.last_decision) - RECORD_START).days + 1)]
    missed = [day for day in weekdays if day.weekday() < 5 and day not in record.scan_days]
    L.append(f"  weekdays with no version-1 score (market holidays included; give each a cause): "
             f"{len(missed)}  {', '.join(map(str, missed))}")
    L.append("")

    if not run.tests:
        L.append("Interim report: descriptive only. No hypothesis is tested and nothing here changes the plan.")
        return L, ds

    p = np.array([d.primary.p for d in ds])
    o = np.array([d.outcome for d in ds], dtype=float)

    L.append("H1 (primary): Brier gap, evidence score minus a flat 0.83")
    if not ds:
        L.append("  no decisions in the sample")
        return L, ds
    lines, gap_high = h1(ds, "primary")
    L += lines
    if n_crl < MIN_CRLS_FOR_H1:
        verdict = f"INCONCLUSIVE: {n_crl} CRLs, fewer than {MIN_CRLS_FOR_H1}, whatever the gap shows"
        if run.name == "primary":
            verdict += ". The plan now runs once more on decisions made by 2028-10-12 (`extension`)."
    elif gap_high < 0:
        verdict = "SUPPORTED: the whole interval is below 0 (better calibration than 0.83, not discrimination)"
    else:
        verdict = "NOT SUPPORTED: the interval reaches 0 or above"
    L += [f"  H1: {verdict}", ""]

    rate = float(o.mean())
    ref = lambda idx: brier(p[idx], o[idx]) - brier(np.full(len(idx), rate), o[idx])
    low, high, _ = company_bootstrap([d.ticker for d in ds], ref)
    L += [f"H1 reference (context only): against the sample's own approval rate {rate:.3f}",
          f"  gap = {ref(np.arange(len(ds))):+.4f}   95% interval [{low:+.4f}, {high:+.4f}]", ""]

    L += ["H1, secondary score (first score in the entry window)"] + h1_lines(ds, "secondary") + [""]

    L.append("H1 by evidence basis")
    for label, members in (("AI-reviewed", AI_REVIEWED), ("fallback", None)):
        part = [d for d in ds if (d.primary.evidence_basis in AI_REVIEWED) == (members is not None)]
        L += [f" {label}:"] + h1_lines(part, "primary")
    L.append("")

    off = sorted(set(sel.off_fingerprint))
    L.append(f"Fingerprint check: {len(off)} version-1 scores with a fingerprint other than {FINGERPRINT}")
    L += [f"  {line}" for line in off]
    if off:
        clean = select_decisions(record, announcements, run, keep=lambda s: s.fingerprint == FINGERPRINT)
        L += ["  H1 repeated without them:"] + h1_lines(clean.decisions, "primary")
    L.append("")

    dec = decomposition(p, o)
    L += ["Calibration by score band (no fitted curve)"]
    L += [f"  {label:<13} n={n:<3} mean score {_f(mp)}  approved {_f(mo)}" for label, n, mp, mo in bands(p, o)]
    L += [f"  Brier split: reliability {dec['reliability']:.4f}, resolution {dec['resolution']:.4f}, "
          f"uncertainty {dec['uncertainty']:.4f}, within-band {dec['within_band']:+.4f}",
          f"  log loss: score {log_loss(p, o):.4f}, flat 0.83 {log_loss(np.full(len(o), BASE_RATE), o):.4f}", ""]

    L.append("H2 (secondary): AUC, approvals scored above CRLs")
    if n_crl >= MIN_CRLS_FOR_H2_H3 and n_crl >= MIN_CRLS_FOR_H1:
        low, high, used = company_bootstrap([d.ticker for d in ds], lambda idx: auc(p[idx], o[idx]))
        L.append(f"  AUC = {auc(p, o):.3f}   95% interval [{low:.3f}, {high:.3f}]   ({used} resamples with both outcomes)")
    else:
        why = "fewer than 4 CRLs, so H1 is inconclusive" if n_crl >= MIN_CRLS_FOR_H2_H3 else f"fewer than {MIN_CRLS_FOR_H2_H3} CRLs"
        L.append(f"  not tested ({why}); each CRL's score:")
    L += [f"    CRL {d.ticker} {d.decision_date}: {d.primary.p:.3f}" for d in ds if d.outcome == 0]
    L.append("")

    L.append("H3 (exploratory): options-implied move before the decision, CRL vs approved")
    with_move = [d for d in ds if d.event_move is not None]
    crl_m = np.array([d.event_move for d in with_move if d.outcome == 0])
    ok_m = np.array([d.event_move for d in with_move if d.outcome == 1])
    L.append(f"  median move: CRL {_f(float(np.median(crl_m)) if len(crl_m) else None)} (n={len(crl_m)}), "
             f"approved {_f(float(np.median(ok_m)) if len(ok_m) else None)} (n={len(ok_m)})")
    if len(crl_m) >= MIN_CRLS_FOR_H2_H3 and len(ok_m):
        u, pval = mann_whitney(crl_m, ok_m)
        L.append(f"  Mann-Whitney U = {u:.1f}, two-sided permutation p = {pval:.4f}")
    else:
        L.append(f"  descriptive only (fewer than {MIN_CRLS_FOR_H2_H3} CRLs with a reading)")
    L.append("")

    L.append("H4 (exploratory, descriptive): price reaction by the signal at the primary score")
    for label, pick in (("signalled", lambda d: d.primary.signal != NO_SIGNAL),
                        ("no signal", lambda d: d.primary.signal == NO_SIGNAL)):
        L.append(_reaction_line(f"  {label}", [d for d in ds if pick(d)]))
    for signal in sorted({d.primary.signal for d in ds}):
        L.append(_reaction_line(f"    {signal}", [d for d in ds if d.primary.signal == signal]))
    for label, flag in (("options cost estimated from price history", True), ("options cost from options history", False)):
        L.append(_reaction_line(f"  signalled, {label}",
                                [d for d in ds if d.primary.signal != NO_SIGNAL and bool(d.primary.iv_rank_estimated) == flag]))
    return L, ds


def _reaction_line(label: str, group: list) -> str:
    moves = np.array([d.price_change for d in group if d.price_change is not None])
    if not len(moves):
        return f"{label}: n=0"
    return f"{label}: n={len(moves)}, mean {moves.mean():+.1%}, median {np.median(moves):+.1%}"


def write_outputs(lines: list, decisions: list, run: Run, today: date, folder: pathlib.Path) -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    report = folder / f"{run.name}-{today}.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (folder / f"{run.name}-{today}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "pdufa_date", "decision_date", "announced_at_utc", "outcome", "primary_score",
                    "primary_scanned_at_utc", "evidence_basis", "fingerprint", "secondary_score", "signal",
                    "event_move", "price_change", "stale"])
        for d in decisions:
            w.writerow([d.ticker, d.catalyst_date, d.decision_date, d.announced_at.isoformat(), d.outcome,
                        d.primary.p, d.primary.scanned_at.isoformat(), d.primary.evidence_basis, d.primary.fingerprint,
                        d.secondary.p if d.secondary else "", d.primary.signal, d.event_move if d.event_move is not None else "",
                        d.price_change if d.price_change is not None else "", int(d.stale)])
    return report


def main(argv: Optional[list] = None, today: Optional[date] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run", choices=sorted(RUNS))
    parser.add_argument("--announcements", type=pathlib.Path, default=HERE / "announcements.csv")
    parser.add_argument("--out", type=pathlib.Path, default=HERE / "output")
    args = parser.parse_args(argv)
    run, today = RUNS[args.run], today or datetime.now(timezone.utc).date()

    # The plan runs each analysis once, after its cut-off. Running it
    # early would be a look at the result before the data is complete.
    if run.tests and today <= run.confirmed_by:
        print(f"The {run.name} analysis runs after {run.confirmed_by}; today is {today}.")
        return 2

    record = asyncio.run(load_record())
    announcements = load_announcements(args.announcements)
    lines, decisions = build_report(record, announcements, run, today)
    missing = select_decisions(record, announcements, run).excluded.get("no announcement time in announcements.csv")
    if run.tests and missing:
        print("Every confirmed decision needs an announcement time first. Missing: " + ", ".join(missing))
        return 3
    print("\n".join(lines))
    print(f"\nSaved to {write_outputs(lines, decisions, run, today, args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
