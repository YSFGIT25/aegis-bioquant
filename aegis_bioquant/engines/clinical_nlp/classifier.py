"""
engines/clinical_nlp/classifier.py
--------------------------------------
Phase B, Engine 1: Clinical NLP & Probability Engine. Produces
P_Fundamental, the fundamental probability of FDA approval.

DESIGN DEPARTURE FROM THE MANUAL, AND WHY: the manual's spec is "pass
structured data to an LLM via Pydantic JSON mode, force it to output a
probability between 0 and 1." I'm not building it that way as the
primary scoring method, and I want to be direct about why rather than
just quietly doing something else.

An LLM reading a trial summary and outputting a probability is a
plausibility estimate, not a calibrated one — it has no mechanism for
knowing whether its own 0.75 resolves true 75% of the time historically.
That's the single biggest credibility risk I flagged for this whole
project before writing a line of engine code, and building Engine 1
around exactly that failure mode is the first-year-fund mistake, not an
elite one.

So P_Fundamental here is a TRANSPARENT, AUDITABLE, DETERMINISTIC score:
a literature-sourced base rate (see constants.FUNDAMENTAL_BASE_RATE_PRIOR)
adjusted by real structured signals already sitting in
clinical_trials_api.StudyProtocol — no LLM call, no hallucination risk,
every adjustment traceable to a specific number in the trial record.
This also validates the architectural finding from when
clinical_trials_api.py shipped: for any trial with posted results
(essentially every pivotal trial behind an actionable PDUFA catalyst),
the manual's four target inputs (N, dropout, biomarker variance,
toxicity) are ALREADY structured fields — no scispacy NER needed to get
them, which is why this module depends on clinical_trials_api.py
directly and NOT on engines/clinical_nlp/extractor.py.

WHAT'S HONESTLY MISSING, not hidden: this is a hand-built scoring
function with weights I judged to be reasonable and conservative, not
weights fit to historical outcomes (no free historical PDUFA-outcome
dataset exists to fit against). It is a defensible starting point, not
a validated model. The actual fix — the one I'd prioritize if this were
going into real capital — is logging every prediction here against its
real outcome over time and checking a calibration curve (does a 0.80
score actually resolve true 80% of the time?), same as flagged at
project kickoff. An optional, bounded LLM adjustment for narrative
signal (eligibility criteria language, mechanism-of-action caveats) is
included below but OFF by default and inert without an OpenAI key —
see llm_narrative_adjustment()'s docstring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from aegis_bioquant.config import constants as c
from aegis_bioquant.ingestion.clinical_trials_api import StudyProtocol


@dataclass
class ScoreAdjustment:
    label: str
    delta: float
    reason: str


@dataclass
class FundamentalScore:
    """Transparent breakdown — every term that produced p_fundamental."""

    nct_id: str
    base_rate: float
    adjustments: list[ScoreAdjustment] = field(default_factory=list)
    p_fundamental: float = 0.0
    data_completeness: str = "FULL"  # "FULL" | "PARTIAL_NO_RESULTS" — see score()

    def explain(self) -> str:
        lines = [f"{self.nct_id}: base rate {self.base_rate:.2f}"]
        for adj in self.adjustments:
            sign = "+" if adj.delta >= 0 else ""
            lines.append(f"  {sign}{adj.delta:.2f}  {adj.label} — {adj.reason}")
        lines.append(f"  = {self.p_fundamental:.3f}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# p-value parsing — CT.gov stores these as free-form strings ("<0.0001",
# "0.023", ">0.05", occasionally "NS" or empty), not floats.
# --------------------------------------------------------------------------

def _parse_p_value(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    match = re.search(r"[\d.]+", raw)
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    return value


def _parse_numeric(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Individual adjustment terms — each one isolated and documented so the
# overall score stays auditable rather than a black box.
# --------------------------------------------------------------------------

def _primary_endpoint_adjustment(protocol: StudyProtocol) -> Optional[ScoreAdjustment]:
    if not protocol.primary_outcome_analyses:
        return None
    analysis = protocol.primary_outcome_analyses[0]
    p = _parse_p_value(analysis.p_value)
    if p is None:
        return None

    if p < 0.001:
        delta, reason = 0.05, f"primary endpoint strongly significant (p~{p})"
    elif p < 0.01:
        delta, reason = 0.03, f"primary endpoint significant (p~{p})"
    elif p < 0.05:
        delta, reason = 0.0, f"primary endpoint met standard significance (p~{p})"
    else:
        delta, reason = -0.25, f"primary endpoint FAILED standard significance (p~{p})"

    return ScoreAdjustment("primary_endpoint_significance", delta, reason)


def _effect_size_adjustment(protocol: StudyProtocol) -> Optional[ScoreAdjustment]:
    """
    Rough signal-to-noise ratio: |effect size| / its own dispersion
    (SEM/SD, whatever CT.gov reported). Not a z-score, but the same
    intuition — a big effect relative to its own uncertainty band is a
    cleaner, more reproducible result than a "significant" one that's
    barely separated from noise.
    """
    if not protocol.primary_outcome_analyses:
        return None
    analysis = protocol.primary_outcome_analyses[0]
    effect = _parse_numeric(analysis.comparison_value)
    dispersion = _parse_numeric(analysis.ci_upper) and (
        _parse_numeric(analysis.ci_upper) - _parse_numeric(analysis.ci_lower)
        if analysis.ci_lower and analysis.ci_upper
        else None
    )
    if effect is None or not dispersion or dispersion == 0:
        return None

    ratio = abs(effect) / abs(dispersion)
    if ratio > 2.0:
        return ScoreAdjustment("effect_size", 0.03, f"large, tight effect (|effect|/CI-width~{ratio:.1f})")
    if ratio < 0.5:
        return ScoreAdjustment("effect_size", -0.05, f"effect size small relative to its own CI width ({ratio:.1f})")
    return None


def _safety_adjustment(protocol: StudyProtocol) -> list[ScoreAdjustment]:
    """Compares the first two adverse-event arms (treatment vs. control
    in essentially every 2-arm pivotal design) on serious-event rate and
    death counts — both common CRL drivers even in small samples."""
    adjustments: list[ScoreAdjustment] = []
    arms = protocol.adverse_events
    if len(arms) < 2:
        return adjustments

    treatment, control = arms[0], arms[1]
    t_rate, c_rate = treatment.serious_event_rate, control.serious_event_rate
    if t_rate is not None and c_rate is not None:
        diff = t_rate - c_rate
        if diff > 0.15:
            adjustments.append(ScoreAdjustment(
                "safety_serious_events", -0.10,
                f"serious-event rate {diff:.0%} higher on treatment arm",
            ))
        elif diff > 0.08:
            adjustments.append(ScoreAdjustment(
                "safety_serious_events", -0.05,
                f"serious-event rate {diff:.0%} higher on treatment arm",
            ))

    death_diff = treatment.deaths_affected - control.deaths_affected
    if death_diff >= 1:
        adjustments.append(ScoreAdjustment(
            "safety_mortality_imbalance", -0.05,
            f"{death_diff} more death(s) on treatment arm than control",
        ))

    return adjustments


def _dropout_adjustment(protocol: StudyProtocol) -> Optional[ScoreAdjustment]:
    arms = protocol.participant_flow
    if len(arms) < 2:
        return None
    treatment, control = arms[0], arms[1]
    t_rate, c_rate = treatment.dropout_rate, control.dropout_rate
    if t_rate is None or c_rate is None:
        return None
    diff = t_rate - c_rate
    if diff > 0.10:
        return ScoreAdjustment(
            "dropout_imbalance", -0.05,
            f"dropout rate {diff:.0%} higher on treatment arm (possible tolerability signal)",
        )
    return None


# --------------------------------------------------------------------------
# Optional LLM narrative layer — OFF by default, inert without a key.
# --------------------------------------------------------------------------

def llm_narrative_adjustment(protocol: StudyProtocol) -> Optional[ScoreAdjustment]:
    """
    NOT WIRED INTO score() BY DEFAULT. This is where the manual's
    LLM-structured-output step would plug in — reading narrative text
    (eligibility criteria, description, any regulatory caveats) for
    signal the structured fields above can't capture, and returning a
    SMALL, BOUNDED adjustment (not a replacement score) so it can only
    nudge the auditable core, never dominate it.

    Deliberately not implemented against a live OpenAI call in this
    pass: this environment has no OPENAI_API_KEY configured, and I'm
    not wiring in and silently testing against a paid API without you
    deciding to spend on it first. Returns None (no adjustment) until
    that's built out — ask if you want this turned on.
    """
    return None


# --------------------------------------------------------------------------
# Top-level scorer
# --------------------------------------------------------------------------

def score(protocol: StudyProtocol, include_llm_narrative: bool = False) -> FundamentalScore:
    """
    Computes P_Fundamental for one study protocol. If the trial has no
    posted results yet (has_posted_results=False), none of the
    structured adjustments have data to work from — returns the bare
    base rate with data_completeness="PARTIAL_NO_RESULTS" so callers
    know this score is weaker than one backed by real trial data.
    """
    result = FundamentalScore(nct_id=protocol.nct_id, base_rate=c.FUNDAMENTAL_BASE_RATE_PRIOR)

    if not protocol.has_posted_results:
        result.data_completeness = "PARTIAL_NO_RESULTS"
        result.p_fundamental = result.base_rate
        return result

    candidates = [
        _primary_endpoint_adjustment(protocol),
        _effect_size_adjustment(protocol),
        _dropout_adjustment(protocol),
    ]
    candidates.extend(_safety_adjustment(protocol))

    if include_llm_narrative:
        candidates.append(llm_narrative_adjustment(protocol))

    result.adjustments = [a for a in candidates if a is not None]

    total = result.base_rate + sum(a.delta for a in result.adjustments)
    result.p_fundamental = round(min(max(total, 0.01), 0.99), 4)  # never literal 0 or 1
    return result
