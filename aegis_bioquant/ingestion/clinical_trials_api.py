"""
ingestion/clinical_trials_api.py
----------------------------------
Ingests NCT ID JSON schemas from ClinicalTrials.gov (API v2).

Pulls the full study protocol for a given NCT ID and validates it into a
strict Pydantic model (CT.gov's own schema, not XML — the legacy
XML/classic API is deprecated; v2 JSON is the live one). Static JSON, no
JS rendering, no auth required.

IMPORTANT ARCHITECTURAL FINDING (verified live against NCT05767346, the
Cytokinetics/aficamten MAPLE-HCM trial behind the CYTK PDUFA catalyst
fda_scraper.py found):

For any trial that already has `resultsSection.POSTED` — which, in
practice, means essentially every pivotal trial behind an actionable
PDUFA catalyst, since a sponsor cannot file an NDA/BLA without reporting
its pivotal results — ClinicalTrials.gov's structured JSON ALREADY
contains exactly the fields the manual assigns to scispacy NER:

  - Patient Sample Size (N)      -> designModule.enrollmentInfo.count
  - Dropout Ratios                -> resultsSection.participantFlowModule
  - Primary Biomarker Variances    -> resultsSection.outcomeMeasuresModule
                                      (per-arm mean/spread + the
                                      statistical analysis: p-value, CI,
                                      effect size)
  - Toxicity / AE Frequencies      -> resultsSection.adverseEventsModule
                                      (per-arm + per-MedDRA-term counts)

So this module computes those four fields directly from structured JSON
for any trial with posted results, rather than guessing them out of free
text. scispacy NLP (engines/clinical_nlp/extractor.py) remains necessary
for: (a) trials without posted results yet (earlier-stage / still
recruiting), and (b) genuinely unstructured narrative — mechanism of
action, eligibility criteria language, biomarker rationale in
descriptionModule — which stays out of scope here.

KNOWN SEAM: a PDUFA CatalystEvent from fda_scraper.py carries a ticker and
company name, not a drug name or NCT ID — SEC press releases name the
drug/trial acronym in free text (e.g. "MAPLE-HCM" for CYTK's aficamten
PDUFA), and that free-text lookup was done manually to find this module's
test case. search_studies() below accepts a drug/trial-acronym term once
you have one; automating "read the filing, extract the drug name" is not
built here — flag if you want that wired up.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import httpx
from pydantic import BaseModel, Field

CTGOV_API_BASE = "https://clinicaltrials.gov/api/v2"
_USER_AGENT = "FDAOdds/1.0 (clinicaltrials.gov API v2 client)"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class StudySummary(BaseModel):
    """Lightweight result row from search_studies()."""

    nct_id: str
    acronym: Optional[str] = None
    brief_title: str
    overall_status: Optional[str] = None
    phases: list[str] = Field(default_factory=list)
    sponsor: Optional[str] = None


class ParticipantFlowArm(BaseModel):
    """Per-arm enrollment/completion counts -> dropout ratio."""

    group_id: str
    label: str
    started: int
    completed: int

    @property
    def dropout_count(self) -> int:
        return max(self.started - self.completed, 0)

    @property
    def dropout_rate(self) -> Optional[float]:
        if self.started <= 0:
            return None
        return round(self.dropout_count / self.started, 4)


class AdverseEventArm(BaseModel):
    """Per-arm aggregate adverse-event counts (CT.gov's own tallies)."""

    group_id: str
    label: str
    deaths_affected: int = 0
    deaths_at_risk: int = 0
    serious_affected: int = 0
    serious_at_risk: int = 0
    other_affected: int = 0
    other_at_risk: int = 0

    @property
    def serious_event_rate(self) -> Optional[float]:
        if self.serious_at_risk <= 0:
            return None
        return round(self.serious_affected / self.serious_at_risk, 4)

    @property
    def other_event_rate(self) -> Optional[float]:
        if self.other_at_risk <= 0:
            return None
        return round(self.other_affected / self.other_at_risk, 4)


class OutcomeArmMeasurement(BaseModel):
    """
    One arm's measured value for an outcome. Kept as raw strings — CT.gov
    encodes values inconsistently (ranges, "NA", "<x"); numeric coercion
    with per-case tolerance is left to the consumer (engines/clinical_nlp).
    """

    group_id: str
    label: Optional[str] = None
    value: Optional[str] = None
    spread: Optional[str] = None  # meaning depends on dispersion_type (SD/SEM/CI)


class PrimaryOutcomeAnalysis(BaseModel):
    """
    One PRIMARY outcome measure's result: per-arm measurements plus,
    when CT.gov reports a formal between-arm comparison, the statistical
    analysis (this is the "Primary Biomarker Variance" input the manual
    calls for — effect size + its variance/CI + significance).
    """

    measure: str
    unit_of_measure: Optional[str] = None
    param_type: Optional[str] = None          # e.g. "MEAN"
    dispersion_type: Optional[str] = None     # e.g. "Standard Deviation"
    arm_measurements: list[OutcomeArmMeasurement] = Field(default_factory=list)

    comparison_param_type: Optional[str] = None   # e.g. "Least Squares Mean Difference"
    comparison_value: Optional[str] = None
    p_value: Optional[str] = None
    ci_pct: Optional[str] = None
    ci_lower: Optional[str] = None
    ci_upper: Optional[str] = None
    statistical_method: Optional[str] = None


class StudyProtocol(BaseModel):
    """Top-level validated output of fetch_study_protocol()."""

    nct_id: str
    acronym: Optional[str] = None
    brief_title: str
    sponsor: Optional[str] = None
    phases: list[str] = Field(default_factory=list)
    overall_status: str

    conditions: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)

    enrollment_count: Optional[int] = None
    enrollment_type: Optional[str] = None  # "ACTUAL" | "ESTIMATED"

    start_date: Optional[date] = None
    primary_completion_date: Optional[date] = None

    has_posted_results: bool = False
    participant_flow: list[ParticipantFlowArm] = Field(default_factory=list)
    adverse_events: list[AdverseEventArm] = Field(default_factory=list)
    primary_outcome_analyses: list[PrimaryOutcomeAnalysis] = Field(default_factory=list)

    source_url: str


# --------------------------------------------------------------------------
# Parsing helpers (raw CT.gov v2 JSON -> models above)
# --------------------------------------------------------------------------

def _parse_partial_date(raw: Optional[str]) -> Optional[date]:
    """CT.gov dates are sometimes year-month only ('2023-06') or year only."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_participant_flow(results_section: dict) -> list[ParticipantFlowArm]:
    pf = results_section.get("participantFlowModule")
    if not pf or not pf.get("periods"):
        return []

    groups = {g["id"]: g.get("title", g["id"]) for g in pf.get("groups", [])}
    period = pf["periods"][0]  # first period covers the whole-study flow in simple designs

    started: dict[str, int] = {}
    completed: dict[str, int] = {}
    for milestone in period.get("milestones", []):
        bucket = started if milestone.get("type") == "STARTED" else (
            completed if milestone.get("type") == "COMPLETED" else None
        )
        if bucket is None:
            continue
        for achievement in milestone.get("achievements", []):
            gid = achievement.get("groupId")
            try:
                bucket[gid] = int(achievement.get("numSubjects", 0))
            except (TypeError, ValueError):
                continue

    arms = []
    for gid, label in groups.items():
        if gid in started:
            arms.append(
                ParticipantFlowArm(
                    group_id=gid,
                    label=label,
                    started=started.get(gid, 0),
                    completed=completed.get(gid, started.get(gid, 0)),
                )
            )
    return arms


def _parse_adverse_events(results_section: dict) -> list[AdverseEventArm]:
    ae = results_section.get("adverseEventsModule")
    if not ae:
        return []

    arms = []
    for group in ae.get("eventGroups", []):
        arms.append(
            AdverseEventArm(
                group_id=group["id"],
                label=group.get("title", group["id"]),
                deaths_affected=group.get("deathsNumAffected", 0) or 0,
                deaths_at_risk=group.get("deathsNumAtRisk", 0) or 0,
                serious_affected=group.get("seriousNumAffected", 0) or 0,
                serious_at_risk=group.get("seriousNumAtRisk", 0) or 0,
                other_affected=group.get("otherNumAffected", 0) or 0,
                other_at_risk=group.get("otherNumAtRisk", 0) or 0,
            )
        )
    return arms


def _parse_primary_outcome_analyses(results_section: dict) -> list[PrimaryOutcomeAnalysis]:
    om = results_section.get("outcomeMeasuresModule", {})
    measures = om.get("outcomeMeasures", [])

    group_labels = {}
    analyses: list[PrimaryOutcomeAnalysis] = []

    for measure in measures:
        if measure.get("type") != "PRIMARY":
            continue

        group_labels = {g["id"]: g.get("title", g["id"]) for g in measure.get("groups", [])}

        arm_measurements: list[OutcomeArmMeasurement] = []
        for cls in measure.get("classes", []):
            for category in cls.get("categories", []):
                for m in category.get("measurements", []):
                    gid = m.get("groupId")
                    arm_measurements.append(
                        OutcomeArmMeasurement(
                            group_id=gid,
                            label=group_labels.get(gid, gid),
                            value=m.get("value"),
                            spread=m.get("spread"),
                        )
                    )

        comparison = (measure.get("analyses") or [{}])[0]

        analyses.append(
            PrimaryOutcomeAnalysis(
                measure=measure.get("title", ""),
                unit_of_measure=measure.get("unitOfMeasure"),
                param_type=measure.get("paramType"),
                dispersion_type=measure.get("dispersionType"),
                arm_measurements=arm_measurements,
                comparison_param_type=comparison.get("paramType"),
                comparison_value=comparison.get("paramValue"),
                p_value=comparison.get("pValue"),
                ci_pct=comparison.get("ciPctValue"),
                ci_lower=comparison.get("ciLowerLimit"),
                ci_upper=comparison.get("ciUpperLimit"),
                statistical_method=comparison.get("statisticalMethod"),
            )
        )

    return analyses


def _parse_study_protocol(raw: dict) -> StudyProtocol:
    ps = raw["protocolSection"]
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    conditions_mod = ps.get("conditionsModule", {})
    arms_mod = ps.get("armsInterventionsModule", {})
    sponsor_mod = ps.get("sponsorCollaboratorsModule", {})

    results_section = raw.get("resultsSection", {})
    has_results = bool(results_section)

    enrollment = design.get("enrollmentInfo", {})

    return StudyProtocol(
        nct_id=ident["nctId"],
        acronym=ident.get("acronym"),
        brief_title=ident.get("briefTitle", ""),
        sponsor=sponsor_mod.get("leadSponsor", {}).get("name"),
        phases=design.get("phases", []),
        overall_status=status.get("overallStatus", "UNKNOWN"),
        conditions=conditions_mod.get("conditions", []),
        interventions=[
            i.get("name", "") for i in arms_mod.get("interventions", [])
        ],
        enrollment_count=enrollment.get("count"),
        enrollment_type=enrollment.get("type"),
        start_date=_parse_partial_date(status.get("startDateStruct", {}).get("date")),
        primary_completion_date=_parse_partial_date(
            status.get("primaryCompletionDateStruct", {}).get("date")
        ),
        has_posted_results=has_results,
        participant_flow=_parse_participant_flow(results_section) if has_results else [],
        adverse_events=_parse_adverse_events(results_section) if has_results else [],
        primary_outcome_analyses=(
            _parse_primary_outcome_analyses(results_section) if has_results else []
        ),
        source_url=f"https://clinicaltrials.gov/study/{ident['nctId']}",
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

async def fetch_study_protocol(nct_id: str) -> StudyProtocol:
    """Fetches and validates one study's full protocol + results by NCT ID."""
    url = f"{CTGOV_API_BASE}/studies/{nct_id}"
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=20.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return _parse_study_protocol(resp.json())


async def search_studies(
    query_term: str,
    sponsor: Optional[str] = None,
    page_size: int = 10,
) -> list[StudySummary]:
    """
    Free-text search against CT.gov (e.g. a drug name or trial acronym
    pulled from a PDUFA filing). Optionally narrow by sponsor name.
    """
    params = {"query.term": query_term, "pageSize": page_size}
    if sponsor:
        params["query.spons"] = sponsor

    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=20.0) as client:
        resp = await client.get(f"{CTGOV_API_BASE}/studies", params=params)
        resp.raise_for_status()
        payload = resp.json()

    results = []
    for study in payload.get("studies", []):
        ident = study.get("protocolSection", {}).get("identificationModule", {})
        design = study.get("protocolSection", {}).get("designModule", {})
        sponsor_mod = study.get("protocolSection", {}).get("sponsorCollaboratorsModule", {})
        status = study.get("protocolSection", {}).get("statusModule", {})

        results.append(
            StudySummary(
                nct_id=ident.get("nctId", ""),
                acronym=ident.get("acronym"),
                brief_title=ident.get("briefTitle", ""),
                overall_status=status.get("overallStatus"),
                phases=design.get("phases", []),
                sponsor=sponsor_mod.get("leadSponsor", {}).get("name"),
            )
        )
    return results


async def fetch_study_protocol_by_search(
    query_term: str, sponsor: Optional[str] = None
) -> Optional[StudyProtocol]:
    """Convenience: search, then fetch the full protocol for the top hit."""
    hits = await search_studies(query_term, sponsor=sponsor, page_size=1)
    if not hits:
        return None
    return await fetch_study_protocol(hits[0].nct_id)


# --------------------------------------------------------------------------
# Trial picker support (the website's manual review page)
# --------------------------------------------------------------------------

class TrialOption(BaseModel):
    """One trial as shown to an administrator choosing which trial a
    catalyst is about."""

    nct_id: str
    acronym: Optional[str] = None
    brief_title: str
    sponsor: Optional[str] = None
    phases: list[str] = Field(default_factory=list)
    overall_status: Optional[str] = None
    has_results: bool = False          # the clinical score needs posted results to say anything
    primary_completion: Optional[str] = None


def _to_trial_option(study: dict) -> TrialOption:
    section = study.get("protocolSection", {})
    ident = section.get("identificationModule", {})
    status = section.get("statusModule", {})
    return TrialOption(
        nct_id=ident.get("nctId", ""),
        acronym=ident.get("acronym"),
        brief_title=ident.get("briefTitle", ""),
        sponsor=section.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name"),
        phases=section.get("designModule", {}).get("phases", []) or [],
        overall_status=status.get("overallStatus"),
        has_results=bool(study.get("hasResults")),
        primary_completion=(status.get("primaryCompletionDateStruct") or {}).get("date"),
    )


async def list_trial_options(sponsor: str, term: Optional[str] = None, limit: int = 12) -> list[TrialOption]:
    """
    A sponsor's trials for a drug (or all of them if `term` is None or
    finds nothing), most useful first: Phase 3 before earlier phases,
    trials with posted results before those without.
    """
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=20.0) as client:
        studies: list[dict] = []
        for query in ([term, None] if term else [None]):
            params = {"query.spons": sponsor, "pageSize": limit}
            if query:
                params["query.term"] = query
            resp = await client.get(f"{CTGOV_API_BASE}/studies", params=params)
            resp.raise_for_status()
            studies = resp.json().get("studies", [])
            if studies:
                break

    options = [_to_trial_option(s) for s in studies]
    options = [o for o in options if o.nct_id]
    options.sort(key=lambda o: (not any("3" in p for p in o.phases), not o.has_results))
    return options


async def fetch_trial_option(nct_id: str) -> Optional[TrialOption]:
    """The registry's record for one trial ID, or None if no such trial exists."""
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=20.0) as client:
        resp = await client.get(f"{CTGOV_API_BASE}/studies/{nct_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _to_trial_option(resp.json())
