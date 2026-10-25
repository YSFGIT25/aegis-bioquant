"""
core/auto_confirm.py
---------------------
Recording FDA outcomes without a person: an outcome the SEC-filing
reader suggested (ingestion/outcome_finder.py) becomes CONFIRMED only
when the FDA's own public records agree with it.

Adopted 2026-10-25, before the study's first decision, and logged in the
analysis plan's change log and on OSF. While settings.OUTCOMES_AUTOMATIC
is on, nobody confirms or edits outcomes by hand: the website's and
Telegram's confirm controls refuse.

THE TWO SOURCES
  1. The company's SEC filing (8-K / 6-K), read by the outcome finder.
  2. The FDA, through openFDA:
       Drugs@FDA       api.fda.gov/drug/drugsfda.json — every approved
                       application with its action date; updated about
                       weekly (seen 2026-10-25: data to 2026-10-23).
       Published CRLs  api.fda.gov/transparency/crl.json — the FDA's
                       complete response letters; updated months late
                       (seen 2026-10-25: newest letter 2026-08-07).

THE RULES (decide() below; tests/auto_confirm_test.py)
  APPROVED  the filing says approved AND Drugs@FDA lists an approval of
            an original application or an efficacy supplement (a new
            indication) for the same company or the same drug, with an
            action date within MATCH_DAYS of the filing's decision date.
  CRL       the filing says CRL, Drugs@FDA lists NO approval for that
            company or drug from MATCH_DAYS before the decision date to
            QUIET_DAYS after it, AND either
              a) the FDA's published letters include one to the same
                 company dated within MATCH_DAYS, or
              b) Drugs@FDA's data has been updated at least QUIET_DAYS
                 after the decision date.
            (b) exists because the letters are published months late,
            and the primary analysis counts only outcomes recorded by
            2027-10-31.
  Everything else stays SUGGESTED and is checked again after every scan:
  DELAYED, WITHDRAWN and INVALID (the FDA publishes none of them, and the
  analysis uses only approvals and CRLs); CBER products such as vaccines
  and gene or cell therapies (not in Drugs@FDA); a partner's application
  under a name that matches neither the company nor the drug; and any
  disagreement between the sources.

  The matching is asymmetric on purpose. For APPROVED a matching approval
  must be found: an approval is never assumed. For CRL any approval that
  matches the company OR the drug blocks it, so a broad match only makes
  a CRL harder to record.

THE ANNOUNCEMENT TIME (for the analysis's 24-hour rule) is the earlier of
the filing's SEC acceptance time and midnight US Eastern at the start of
the FDA's action date or the filing's decision date, whichever is
earlier. The decision
cannot be public before the FDA acted that day, and taking the earlier
time can only exclude a score from the analysis, never admit one made
after the news.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db

MATCH_DAYS = 3
QUIET_DAYS = 14
AUTO_SOURCE = "AUTO"       # announcement_source of a time set here
FDA_APPROVAL, FDA_CRL_LETTER, NO_FDA_APPROVAL = "FDA_APPROVAL", "FDA_CRL_LETTER", "NO_FDA_APPROVAL"
_EASTERN = ZoneInfo("America/New_York")
_PAGE = 1000               # openFDA's largest page

# Words that say what kind of company it is, not which one. Dropped before
# comparing names: "Ionis Pharmaceuticals, Inc." and Drugs@FDA's
# "IONIS PHARMS INC" are the same sponsor.
_GENERIC_WORDS = frozenset("""
    THE AND OF INC INCORPORATED CORP CORPORATION CO COMPANY LTD LIMITED LLC LP PLC AG SA NV SE AB AS SPA BV GMBH KK
    HOLDINGS HOLDING GROUP US USA NORTH AMERICA AMERICAS INTERNATIONAL INTL GLOBAL
    PHARMACEUTICALS PHARMACEUTICAL PHARMS PHARM PHARMA THERAPEUTICS THERAP THERAPS
    BIOSCIENCES BIOSCIENCE BIOTHERAPEUTICS BIOPHARMA BIOPHARMACEUTICALS BIOTECH BIOTECHNOLOGY BIOLOGICS BIO
    SCIENCES SCIENCE LABORATORIES LABS LAB MEDICAL MEDICINES ONCOLOGY HEALTH HEALTHCARE
""".split())
# Salt and dosage-form words in drug names: "olezarsen sodium" must not
# match every other sodium salt.
_FORM_WORDS = frozenset("""
    SODIUM POTASSIUM CALCIUM MAGNESIUM HYDROCHLORIDE ACETATE CITRATE SULFATE MESYLATE PHOSPHATE MALEATE
    TARTRATE BROMIDE CHLORIDE SUCCINATE FUMARATE BESYLATE TOSYLATE HYDRATE MONOHYDRATE
    INJECTION TABLET TABLETS CAPSULE CAPSULES ORAL SOLUTION SUSPENSION EXTENDED RELEASE DELAYED
""".split())


# --------------------------------------------------------------------------
# Matching names
# --------------------------------------------------------------------------

def company_key(name: Optional[str]) -> str:
    words = re.findall(r"[A-Z0-9]+", (name or "").upper())
    return " ".join(w for w in words if w not in _GENERIC_WORDS)


def same_company(a: Optional[str], b: Optional[str]) -> bool:
    """Same sponsor once the generic words are gone: the whole remaining
    name, or a distinctive (5+ letters) first word."""
    ka, kb = company_key(a), company_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    first_a, first_b = ka.split()[0], kb.split()[0]
    return first_a == first_b and len(first_a) >= 5


def drug_words(candidate: Optional[str]) -> set[str]:
    """Distinctive words of the catalyst's drug (or trial) name."""
    return {w for w in re.findall(r"[A-Z][A-Z0-9-]{4,}", (candidate or "").upper()) if w not in _GENERIC_WORDS and w not in _FORM_WORDS}


# --------------------------------------------------------------------------
# The FDA's records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Approval:
    application: str           # e.g. NDA218614
    sponsor: str
    names: frozenset           # brand, generic and active-ingredient words, upper case
    action_date: date
    kind: str                  # ORIG or EFFICACY


@dataclass(frozen=True)
class Letter:
    company: str
    letter_date: date
    file_name: str


@dataclass
class FdaRecords:
    approvals: list = field(default_factory=list)
    approvals_updated: Optional[date] = None   # Drugs@FDA's "last_updated"
    letters: list = field(default_factory=list)


def _ymd(text: str) -> Optional[date]:
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def parse_approvals(results: list, start: date, end: date) -> list[Approval]:
    """Approvals (original or efficacy supplement) with an action date
    from `start` to `end`, from Drugs@FDA results."""
    out = []
    for app in results:
        words: set[str] = set()
        for product in app.get("products") or []:
            words.update(re.findall(r"[A-Z0-9-]{3,}", (product.get("brand_name") or "").upper()))
            for ingredient in product.get("active_ingredients") or []:
                words.update(re.findall(r"[A-Z0-9-]{3,}", (ingredient.get("name") or "").upper()))
        for key in ("brand_name", "generic_name", "substance_name"):
            for value in (app.get("openfda") or {}).get(key) or []:
                words.update(re.findall(r"[A-Z0-9-]{3,}", value.upper()))
        for sub in app.get("submissions") or []:
            when = _ymd(sub.get("submission_status_date"))
            if sub.get("submission_status") != "AP" or when is None or not (start <= when <= end):
                continue
            if sub.get("submission_type") == "ORIG":
                kind = "ORIG"
            elif sub.get("submission_type") == "SUPPL" and sub.get("submission_class_code") == "EFFICACY":
                kind = "EFFICACY"
            else:
                continue
            out.append(Approval(app.get("application_number") or "?", app.get("sponsor_name") or "",
                                frozenset(words), when, kind))
    return out


def parse_letters(results: list) -> list[Letter]:
    out = []
    for r in results:
        try:
            when = datetime.strptime(r.get("letter_date") or "", "%m/%d/%Y").date()
        except ValueError:
            continue
        out.append(Letter(r.get("company_name") or "", when, r.get("file_name") or ""))
    return out


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    resp = await client.get(url, params=params)
    if resp.status_code == 404:          # openFDA's "No matches found!"
        return {"meta": {}, "results": []}
    resp.raise_for_status()
    return resp.json()


async def fetch_records(client: httpx.AsyncClient, start: date, end: date) -> FdaRecords:
    base = settings.OPENFDA_BASE_URL.rstrip("/")
    records = FdaRecords()

    async def every(url: str, params: dict) -> tuple[list, dict]:
        results, skip, first_meta = [], 0, None
        while True:
            data = await _get_json(client, url, {**params, "limit": _PAGE, "skip": skip})
            page, meta = data.get("results") or [], data.get("meta") or {}
            first_meta = first_meta or meta
            results += page
            skip += len(page)
            if not page or skip >= (meta.get("results") or {}).get("total", 0):
                return results, first_meta

    results, meta = await every(f"{base}/drug/drugsfda.json",
                                {"search": f"submissions.submission_status_date:[{start:%Y%m%d} TO {end:%Y%m%d}]"})
    if meta.get("last_updated"):
        records.approvals_updated = date.fromisoformat(meta["last_updated"])
    records.approvals = parse_approvals(results, start, end)
    letters, _ = await every(f"{base}/transparency/crl.json", {})
    records.letters = parse_letters(letters)
    if records.approvals_updated is None:
        # A window with no approvals at all still says how fresh the data is.
        data = await _get_json(client, f"{base}/drug/drugsfda.json", {"limit": 1})
        updated = (data.get("meta") or {}).get("last_updated")
        records.approvals_updated = date.fromisoformat(updated) if updated else None
    return records


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    confirmed: bool
    source: str = ""           # FDA_APPROVAL | FDA_CRL_LETTER | NO_FDA_APPROVAL when confirmed
    evidence: str = ""         # the FDA record that agreed, or why it is still waiting
    fda_date: Optional[date] = None


def decide(outcome: Optional[str], decision_date: Optional[date], company: Optional[str],
           drug: Optional[str], fda: FdaRecords) -> Verdict:
    if outcome not in ("APPROVED", "CRL"):
        return Verdict(False, evidence=f"the FDA publishes no record of {outcome or 'this'} outcomes")
    if decision_date is None:
        return Verdict(False, evidence="no decision date was read from the filing")
    words = drug_words(drug)

    def matches(a: Approval) -> bool:
        return same_company(a.sponsor, company) or bool(words & a.names)

    near = [a for a in fda.approvals if matches(a) and abs((a.action_date - decision_date).days) <= MATCH_DAYS]
    if outcome == "APPROVED":
        if near:
            a = min(near, key=lambda a: abs((a.action_date - decision_date).days))
            return Verdict(True, FDA_APPROVAL, f"Drugs@FDA {a.application} ({a.sponsor}), {a.kind} approved {a.action_date}",
                           a.action_date)
        return Verdict(False, evidence="Drugs@FDA does not list a matching approval yet")

    quiet_end = decision_date + timedelta(days=QUIET_DAYS)
    blocking = [a for a in fda.approvals if matches(a) and decision_date - timedelta(days=MATCH_DAYS) <= a.action_date <= quiet_end]
    if blocking:
        a = blocking[0]
        return Verdict(False, evidence=f"sources disagree: Drugs@FDA lists {a.application} ({a.sponsor}) approved {a.action_date}")
    letters = [l for l in fda.letters if same_company(l.company, company)
               and abs((l.letter_date - decision_date).days) <= MATCH_DAYS]
    if letters:
        l = letters[0]
        return Verdict(True, FDA_CRL_LETTER, f"FDA letter {l.file_name} to {l.company}, dated {l.letter_date}", l.letter_date)
    if fda.approvals_updated is not None and fda.approvals_updated >= quiet_end:
        return Verdict(True, NO_FDA_APPROVAL,
                       f"no matching approval in Drugs@FDA {decision_date - timedelta(days=MATCH_DAYS)} to {quiet_end} "
                       f"(data updated {fda.approvals_updated})")
    return Verdict(False, evidence=f"waiting for Drugs@FDA data through {quiet_end}")


def announcement_time(sec_time: Optional[datetime], fda_day: date) -> datetime:
    """The earlier of the SEC acceptance time and midnight US Eastern at
    the start of the FDA's action date, in UTC."""
    midnight = datetime.combine(fda_day, time(0, 0), tzinfo=_EASTERN).astimezone(timezone.utc)
    if sec_time is None:
        return midnight
    sec_time = sec_time if sec_time.tzinfo else sec_time.replace(tzinfo=timezone.utc)
    return min(sec_time, midnight)


# --------------------------------------------------------------------------
# Running it (after every scheduled scan, entrypoint.after_scan)
# --------------------------------------------------------------------------

@dataclass
class Result:
    confirmed: list = field(default_factory=list)   # one line per outcome recorded now
    waiting: list = field(default_factory=list)     # one line per suggestion still open
    error: Optional[str] = None


async def _catalyst(session, ticker: str, catalyst_date: date) -> Optional[db.CatalystEventRecord]:
    return await session.scalar(
        select(db.CatalystEventRecord).where(
            db.CatalystEventRecord.ticker == ticker,
            db.CatalystEventRecord.event_type == "PDUFA",
            db.CatalystEventRecord.start_date == catalyst_date,
        ).order_by(db.CatalystEventRecord.last_seen_at.desc()).limit(1)
    )


async def run(today: Optional[date] = None, client: Optional[httpx.AsyncClient] = None) -> Result:
    """Checks every SUGGESTED outcome against the FDA's records and
    confirms the ones that agree. Never raises: a failure is returned in
    Result.error and everything is tried again after the next scan."""
    from aegis_bioquant.core.outcome_review import fill_acceptance_times

    today = today or datetime.now(timezone.utc).date()
    result = Result()
    try:
        await fill_acceptance_times()
    except Exception:  # noqa: BLE001 — the FDA-date bound still applies without it
        pass
    factory = db.get_session_factory()
    async with factory() as session:
        rows = list(await session.scalars(select(db.CatalystOutcomeRecord).where(
            db.CatalystOutcomeRecord.status == "SUGGESTED").order_by(db.CatalystOutcomeRecord.catalyst_date)))
        if not rows:
            return result
        dated = [r.decision_date for r in rows if r.outcome in ("APPROVED", "CRL") and r.decision_date]
        fda = FdaRecords()
        if dated:
            start = min(dated) - timedelta(days=MATCH_DAYS)
            end = min(max(dated) + timedelta(days=QUIET_DAYS), today)
            own = client is None
            client = client or httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "FDAOdds outcome check"})
            try:
                fda = await fetch_records(client, start, end)
            except Exception as exc:  # noqa: BLE001
                result.error = f"FDA records not reachable ({type(exc).__name__}); nothing confirmed this time"
                result.waiting = [f"{r.ticker} {r.catalyst_date}: {r.outcome}" for r in rows]
                return result
            finally:
                if own:
                    await client.aclose()

        now = datetime.now(timezone.utc)
        for row in rows:
            event = await _catalyst(session, row.ticker, row.catalyst_date)
            verdict = decide(row.outcome, row.decision_date, event.company_name if event else None,
                             event.drug_or_trial_candidate if event else None, fda)
            label = f"{row.ticker} {row.catalyst_date}: {row.outcome}"
            if not verdict.confirmed:
                result.waiting.append(f"{label} ({verdict.evidence})")
                continue
            sec_time = row.announced_at if row.announcement_source == "SEC_ACCEPTANCE" else None
            row.status = "CONFIRMED"
            earliest_day = min(verdict.fda_date or row.decision_date, row.decision_date)
            row.decision_date = verdict.fda_date or row.decision_date
            row.confirmed_by, row.confirmed_at = None, now
            row.confirmation_source, row.fda_evidence = verdict.source, verdict.evidence
            row.price_before = row.price_after = row.price_change_pct = None
            row.announced_at = announcement_time(sec_time, earliest_day)
            row.announcement_url = row.announcement_url or row.evidence_url
            row.announcement_source = AUTO_SOURCE
            result.confirmed.append(f"{label}, decided {row.decision_date} ({verdict.evidence})")
        await session.commit()
    return result
