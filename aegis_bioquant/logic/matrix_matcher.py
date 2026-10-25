"""
logic/matrix_matcher.py
-------------------------
Phase C: The Arbitrage Matrix Filter.

Maps Engine 1 (Clinical NLP -> P_Fundamental) against Engine 2
(Options Quant -> event-implied move, IV_Rank, liquidity) to locate
market inefficiencies and assign a candidate.arbitrage_signal.

Signal routing (checked in priority order):

1. LONG_TERM_EQUITY_ACCUMULATION (pivot track)
   P_Fundamental >= 0.90 (review not THIN) AND (IV_Rank > 50 OR Open_Interest < 2000)
   -> Clinical conviction is exceptional, but the options structure is
      either overpriced or untradeable. Route to the long-term equity
      track instead of burning premium on a bad options trade. Checked
      first because it is a hard structural override on the options
      track below.

2. LONG_RUN_UP_ALPHA (short-term options track)
   P_Fundamental >= 0.75
   AND the event matters (options price an extra move >= 10% for it)
   AND the premium is still cheap (IV_Rank < 30)
   -> The manual's Run-Up Play: a strong drug, an event big enough to
      move the stock, and options not yet bid up for it.

3. STRUCTURAL_COLLAPSE_AVOID (short/avoidance track)
   P_Fundamental <= 0.40
   AND the event matters (>= 10%)
   AND the premium is rich (IV_Rank > 50)
   -> Weak drug, real event, expensive options: avoid, or exit
      existing exposure.

4. NO_SIGNAL
   -> Candidate stays in the monitoring pool; candidate.signal_notes
      says which test it missed.

WHY RULES 2 AND 3 NO LONGER FOLLOW THE MANUAL'S WORDING: the manual
compares P_Fundamental with a market-implied approval probability,
P_Market (<= 0.50 to buy, >= 0.70 to avoid). The only P_Market that can
be read from an option chain weeks ahead is "the chance the stock ends
above today's price", and measured live on 2026-10-06 that came out
between 0.44 and 0.59 for every stock tried — including Coca-Cola and
Exxon, which have no FDA event. A threshold at 0.50 on such a number
fires on about half of all candidates for no reason connected to the
drug. On the same day it called CYTK a buy while the options were
pricing no extra movement at all for CYTK's event.

A genuine approval probability cannot be backed out of these chains:
ordinary volatility over the weeks to expiry is larger than the event,
so the event's up/down odds are not separable from noise. What CAN be
read reliably is the SIZE of move priced for the event, and whether
premium is cheap or rich against the stock's own history. Rules 2 and 3
are rebuilt on those two, which is also closer to what the manual's
run-up play says in prose: buy "underpriced premiums where market
participants have not yet begun chasing the catalyst".

p_market and divergence_delta are still computed and stored, for the
record. NO RULE READS THEM.

IV_RANK BEFORE THERE IS HISTORY: true IV_Rank needs IV_RANK_MIN_READINGS
daily readings of the stock's implied volatility, which this system
only started recording on 2026-10-06. Until a stock has them,
candidate.iv_rank holds metrics.options_cost_estimate — today's implied
volatility ranked against the stock's own past year of price swings —
and candidate.iv_rank_estimated is True. The rules below read it the
same way; it leans toward calling options dear, so it holds signals
back rather than inventing them. Without it no rule 2 or 3 signal could
be raised for the first six weeks of tracking any stock.

WHERE P_FUNDAMENTAL COMES FROM: since 2026-10-09 (the owner's decision)
it is Claude's reading of the evidence wherever one exists
(candidate.evidence_basis "CLAUDE" / "CLAUDE_THIN"), and the registry
point system otherwise. The thresholds below were written for the point
system, whose scores cluster at 0.83; Claude's spread from 0.05 to 0.95,
so rule 1 (>= 0.90) and rule 3 (<= 0.40) now fire where before they
almost never could.

EVIDENCE WITHOUT A CONFIRMED TRIAL (point system only): p_fundamental is then the base rate
(candidate.evidence_basis says so). The base rate clears rule 2's 0.75
bar on its own — that is the manual's design, where the clinical score
mainly exists to veto drugs with bad results — so a signal can rest on
baseline evidence, and is labelled as such wherever it is shown.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from aegis_bioquant.config import constants as c


@dataclass
class Candidate:
    """A single ticker that has survived Phase A ingestion/liquidity filters."""

    ticker: str
    catalyst_type: str                  # "PDUFA" | "ADCOM" | "PHASE_3_READOUT"
    catalyst_date: date

    spot_price: float
    open_interest: int
    daily_options_volume: int

    p_fundamental: float                 # Engine 1 output, [0.00, 1.00]
    p_market: Optional[float] = None     # Engine 2 output, [0.00, 1.00] — None if the
    # chain had no trustworthy two-sided market to price it from (see
    # engines/options_quant/metrics.py's confidence gating)
    iv_rank: Optional[float] = None      # Engine 2 output, [0, 100] — None until a
    # 52-week historical IV source exists (flagged gap, see metrics.py/compute_iv_rank)
    p_market_confidence: Optional[str] = None  # metrics.RiskNeutralProbability.confidence,
    # carried through purely for audit/logging — the pipeline (not this module) decides
    # whether a non-"GOOD" confidence should null out p_market before evaluate_candidate()
    market_cap: Optional[float] = None   # company value in dollars; None if the data source didn't say
    # metrics.event_implied_move — the extra move the options price for the catalyst
    # itself. Read by the long and avoid rules ("the event matters").
    event_implied_move: Optional[float] = None
    event_move_confidence: Optional[str] = None
    # True when iv_rank is metrics.options_cost_estimate (ranked against the
    # stock's own price history) rather than true IV Rank. The rules treat
    # the two alike; everything shown to a person says which it is.
    iv_rank_estimated: bool = False
    # What p_fundamental rests on: "TRIAL_RESULTS" (scored from the trial's
    # published results), "TRIAL_NO_RESULTS" (trial known, nothing published
    # yet) or "BASELINE" (no trial confirmed) — the last two are the plain
    # base rate for a drug the FDA has agreed to review.
    # "CLAUDE" / "CLAUDE_THIN": p_fundamental is Claude's reading of the
    # company's filings and published results (engines/clinical_nlp/
    # llm_review.py); THIN means it found little to go on. point_score then
    # holds what the registry point system said, for comparison only.
    evidence_basis: str = "TRIAL_RESULTS"
    point_score: Optional[float] = None

    # Populated by evaluate_candidate()
    divergence_delta: Optional[float] = field(default=None)   # recorded only — see module docstring
    arbitrage_signal: str = field(default=c.SIGNAL_NONE)
    signal_notes: str = field(default="")   # one line: why this signal, or which test was missed


def calculate_divergence_delta(p_fundamental: float, p_market: Optional[float]) -> Optional[float]:
    """Delta = |P_Fundamental - P_Market|. None if P_Market couldn't be priced."""
    if p_market is None:
        return None
    return abs(p_fundamental - p_market)


def _is_equity_accumulation_setup(candidate: Candidate) -> bool:
    """
    IF P_Fundamental >= 0.90 AND (IV_Rank > 50 OR Open_Interest < 2000):
        -> options are overpriced (rich IV) or structurally untradeable
           (thin OI), despite exceptional fundamental conviction.

    Deliberately does NOT depend on p_market — this track exists
    precisely for candidates where the options side of the picture is
    bad or untrustworthy, so it has to stay evaluable even when p_market
    is None. iv_rank being None means "unknown", not "not overpriced" —
    it just can't trigger this specific leg of the OR; open_interest is
    always known (it's a raw chain count, not a derived/confidence-gated
    metric) so the illiquidity leg always can.
    """
    overpriced = candidate.iv_rank is not None and candidate.iv_rank > c.EQUITY_ACCUMULATION_IV_RANK_OVERPRICED
    illiquid = candidate.open_interest < c.EQUITY_ACCUMULATION_MIN_OPEN_INTEREST
    # "Very high conviction" has to rest on something. A review that found
    # little published about the results (CLAUDE_THIN) can score 0.90 only
    # because nothing argued against it; that is not conviction. Seen on
    # the first Claude-driven scan: three thin reviews raised this signal.
    well_founded = candidate.evidence_basis != "CLAUDE_THIN"
    return (
        candidate.p_fundamental >= c.EQUITY_ACCUMULATION_P_FUNDAMENTAL_MIN
        and well_founded
        and (overpriced or illiquid)
    )


def _event_matters(candidate: Candidate) -> bool:
    """
    The options price at least EVENT_MATTERS_MIN_MOVE of extra movement
    for the catalyst. Unknown is not "matters".

    A reading taken off a baseline expiry only days away is not trusted
    here: it is the jumpy case, and can show a large "event" out of
    noise. The other low-confidence flag (baseline expiry right before
    the catalyst) is accepted — that one UNDERSTATES the event, so a
    reading that still clears the bar clears it honestly.
    """
    return (
        candidate.event_implied_move is not None
        and candidate.event_move_confidence != "LOW_CONFIDENCE_SHORT_BASELINE"
        and candidate.event_implied_move >= c.EVENT_MATTERS_MIN_MOVE
    )


def _is_long_run_up_alpha(candidate: Candidate) -> bool:
    return (
        candidate.p_fundamental >= c.LONG_ALPHA_P_FUNDAMENTAL_MIN
        and _event_matters(candidate)
        and candidate.iv_rank is not None
        and candidate.iv_rank < c.IV_RANK_UNDERPRICED_THRESHOLD
    )


def _is_structural_collapse(candidate: Candidate) -> bool:
    return (
        candidate.p_fundamental <= c.SHORT_ALPHA_P_FUNDAMENTAL_MAX
        and _event_matters(candidate)
        and candidate.iv_rank is not None
        and candidate.iv_rank > c.COLLAPSE_IV_RANK_RICH
    )


def _describe(candidate: Candidate) -> str:
    """One line a person can read: the facts behind a signal, or the
    test(s) a non-signal missed."""
    move = candidate.event_implied_move
    move_text = "not available" if move is None else ("none" if move == 0 else f"{move:.0%}")
    rank_text = "not available yet" if candidate.iv_rank is None else (
        f"{candidate.iv_rank:.0f}" + (" (estimate from price history)" if candidate.iv_rank_estimated else "")
    )
    facts = f"clinical score {candidate.p_fundamental:.2f}; event move priced {move_text}; IV Rank {rank_text}"

    if candidate.arbitrage_signal != c.SIGNAL_NONE:
        return facts

    middling = c.SHORT_ALPHA_P_FUNDAMENTAL_MAX < candidate.p_fundamental < c.LONG_ALPHA_P_FUNDAMENTAL_MIN
    if middling:
        return (
            f"clinical score {candidate.p_fundamental:.2f} is between the avoid level "
            f"({c.SHORT_ALPHA_P_FUNDAMENTAL_MAX:.2f}) and the buy level ({c.LONG_ALPHA_P_FUNDAMENTAL_MIN:.2f})"
        )

    missed: list[str] = []
    if move is None:
        missed.append("no reading of the event move (needs live quotes on the options before and after the date)")
    elif candidate.event_move_confidence == "LOW_CONFIDENCE_SHORT_BASELINE":
        missed.append("event-move reading not trusted (the pre-event options expire within days)")
    elif move < c.EVENT_MATTERS_MIN_MOVE:
        missed.append(
            f"options price {'no' if move == 0 else f'only {move:.0%} of'} extra move for the event "
            f"(needs at least {c.EVENT_MATTERS_MIN_MOVE:.0%})"
        )
    strong = candidate.p_fundamental >= c.LONG_ALPHA_P_FUNDAMENTAL_MIN
    if candidate.iv_rank is None:
        missed.append(f"IV Rank not available yet (needs {c.IV_RANK_MIN_READINGS} daily readings)")
    elif strong and candidate.iv_rank >= c.IV_RANK_UNDERPRICED_THRESHOLD:
        missed.append(f"IV Rank {candidate.iv_rank:.0f} is not under {c.IV_RANK_UNDERPRICED_THRESHOLD} (premium not cheap)")
    elif not strong and candidate.iv_rank <= c.COLLAPSE_IV_RANK_RICH:
        missed.append(f"IV Rank {candidate.iv_rank:.0f} is not over {c.COLLAPSE_IV_RANK_RICH} (premium not rich)")
    return "; ".join(missed) if missed else facts


def evaluate_candidate(candidate: Candidate) -> Candidate:
    """
    Mutates and returns `candidate` with arbitrage_signal and
    signal_notes populated per the priority order described above (and
    divergence_delta, which is recorded but decides nothing).
    """
    candidate.divergence_delta = calculate_divergence_delta(
        candidate.p_fundamental, candidate.p_market
    )

    if _is_equity_accumulation_setup(candidate):
        candidate.arbitrage_signal = c.SIGNAL_LONG_TERM_EQUITY_ACCUMULATION
    elif _is_long_run_up_alpha(candidate):
        candidate.arbitrage_signal = c.SIGNAL_LONG_RUN_UP_ALPHA
    elif _is_structural_collapse(candidate):
        candidate.arbitrage_signal = c.SIGNAL_STRUCTURAL_COLLAPSE_AVOID
    else:
        candidate.arbitrage_signal = c.SIGNAL_NONE

    candidate.signal_notes = _describe(candidate)
    return candidate
