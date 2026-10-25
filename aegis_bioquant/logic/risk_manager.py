"""
logic/risk_manager.py
------------------------
Phase D: Risk Sizing & Asymmetric Exit Triggers.

Builds an ExecutionPlan off a scored Candidate (see logic/matrix_matcher.py).
Three tracks, selected by candidate.arbitrage_signal:

1. LONG_RUN_UP_ALPHA           -> short-term options run-up
     - fractional Kelly sizing, bounded by a hard position cap
     - T-14 (PDUFA) / T-5 (AdCom) time-based exits
     - 50%-in-10-days profit-target override

2. LONG_TERM_EQUITY_ACCUMULATION (alternate track)
     - bypasses the options chain entirely
     - flat 2-5% portfolio-weighted allocation into common stock,
       scaled by fundamental conviction within that band
     - T-14 / T-5 time-based exits are explicitly deactivated
     - long-term hold state; only a 15% trailing stop is active

3. STRUCTURAL_COLLAPSE_AVOID   -> short/avoidance flag on existing exposure
     (sizing for new short exposure is out of scope until the short-side
     risk module is specified; this track only flags the action.)
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional

from aegis_bioquant.config import constants as c
from aegis_bioquant.logic.matrix_matcher import Candidate


@dataclass
class ExitTrigger:
    label: str
    active: bool
    trigger_date: Optional[date] = None
    description: str = ""


@dataclass
class ExecutionPlan:
    ticker: str
    signal: str
    use_options: bool
    hold_state: str                     # SHORT_TERM_RUN_UP | LONG_TERM_HOLD | AVOID_OR_SHORT | NONE
    allocation_pct: float = 0.0
    allocation_dollars: float = 0.0
    kelly_fraction: Optional[float] = None
    exit_triggers: List[ExitTrigger] = field(default_factory=list)
    trailing_stop_pct: Optional[float] = None
    notes: str = ""


def fractional_kelly(
    win_prob: float,
    win_loss_ratio: float,
    kelly_fraction: float = c.KELLY_FRACTION_CAP,
    position_cap: float = c.MAX_POSITION_PCT_OF_CASH,
) -> float:
    """
    Conservative fractional Kelly Criterion, bounded by a hard binary
    stop-loss cap to prevent downside tail risk on any single options trade.

    f* = (b*p - q) / b   where b = win_loss_ratio, p = win_prob, q = 1-p
    Applied size = kelly_fraction * f*, clipped to [0, position_cap].
    """
    p = win_prob
    q = 1.0 - win_prob
    b = win_loss_ratio

    if b <= 0:
        return 0.0

    full_kelly = (b * p - q) / b
    sized = kelly_fraction * full_kelly
    return max(0.0, min(sized, position_cap))


def _options_run_up_exit_triggers(candidate: Candidate) -> List[ExitTrigger]:
    triggers: List[ExitTrigger] = []

    if candidate.catalyst_type == "PDUFA":
        triggers.append(
            ExitTrigger(
                label="PDUFA_EXIT",
                active=True,
                trigger_date=candidate.catalyst_date
                - timedelta(days=c.PDUFA_EXIT_DAYS_PRIOR),
                description=f"Close all of the options position {c.PDUFA_EXIT_DAYS_PRIOR} days before the FDA decision.",
            )
        )
    elif candidate.catalyst_type == "ADCOM":
        triggers.append(
            ExitTrigger(
                label="ADCOM_EXIT",
                active=True,
                trigger_date=candidate.catalyst_date
                - timedelta(days=c.ADCOM_EXIT_DAYS_PRIOR),
                description=(
                    f"Close all of the options position {c.ADCOM_EXIT_DAYS_PRIOR} days before the advisory committee "
                    f"meeting, ahead of the FDA's briefing documents."
                ),
            )
        )

    triggers.append(
        ExitTrigger(
            label="PROFIT_TARGET",
            active=True,
            trigger_date=None,  # condition-based, not calendar-based
            description=(
                f"Take the profit early if the options gain more than {int(c.PROFIT_TARGET_PCT * 100)}% "
                f"within {c.PROFIT_TARGET_WINDOW_DAYS} days of buying."
            ),
        )
    )
    return triggers


def _deactivated_time_based_exit_triggers() -> List[ExitTrigger]:
    """
    Long-Term Equity Accumulation track: T-14 / T-5 are deactivated
    outright (not merely unset) so the orchestrator/telemetry layer can
    see explicitly that these were considered and turned off, rather
    than never evaluated.
    """
    return [
        ExitTrigger(
            label="PDUFA_EXIT",
            active=False,
            trigger_date=None,
            description="Deactivated: equity accumulation track holds through catalyst.",
        ),
        ExitTrigger(
            label="ADCOM_EXIT",
            active=False,
            trigger_date=None,
            description="Deactivated: equity accumulation track holds through catalyst.",
        ),
    ]


def _scale_equity_allocation_pct(p_fundamental: float) -> float:
    """
    Linearly scales the flat 2-5% equity allocation band by conviction
    strength above the 0.90 equity-accumulation floor. A 0.90 score gets
    the 2% floor; a 1.00 score gets the full 5% ceiling.
    """
    floor_p = c.EQUITY_ACCUMULATION_P_FUNDAMENTAL_MIN
    p = min(max(p_fundamental, floor_p), 1.0)

    span = 1.0 - floor_p
    conviction = (p - floor_p) / span if span > 0 else 0.0

    pct_span = c.EQUITY_ALLOCATION_MAX_PCT - c.EQUITY_ALLOCATION_MIN_PCT
    return c.EQUITY_ALLOCATION_MIN_PCT + conviction * pct_span


def _build_options_run_up_plan(candidate: Candidate, total_cash: float, win_loss_ratio: float) -> ExecutionPlan:
    kelly_frac = fractional_kelly(candidate.p_fundamental, win_loss_ratio)

    return ExecutionPlan(
        ticker=candidate.ticker,
        signal=candidate.arbitrage_signal,
        use_options=True,
        hold_state="SHORT_TERM_RUN_UP",
        allocation_pct=kelly_frac,
        allocation_dollars=kelly_frac * total_cash,
        kelly_fraction=kelly_frac,
        exit_triggers=_options_run_up_exit_triggers(candidate),
        trailing_stop_pct=None,
        notes="Short-term options volatility run-up play (Phase D standard track).",
    )


def _build_equity_accumulation_plan(candidate: Candidate, total_cash: float) -> ExecutionPlan:
    allocation_pct = _scale_equity_allocation_pct(candidate.p_fundamental)

    return ExecutionPlan(
        ticker=candidate.ticker,
        signal=candidate.arbitrage_signal,
        use_options=False,  # bypass options chains entirely
        hold_state="LONG_TERM_HOLD",
        allocation_pct=allocation_pct,
        allocation_dollars=allocation_pct * total_cash,
        kelly_fraction=None,  # Kelly/options sizing not applicable to this track
        exit_triggers=_deactivated_time_based_exit_triggers(),
        trailing_stop_pct=c.EQUITY_TRAILING_STOP_PCT,
        notes=(
            "Long-Term Equity Accumulation: options overpriced or illiquid "
            "despite high fundamental conviction. Sized as common-stock "
            "shares via standard portfolio asset weighting; held long-term "
            "to capture post-catalyst market cap expansion, hedged only by "
            f"a {int(c.EQUITY_TRAILING_STOP_PCT * 100)}% trailing stop."
        ),
    )


def _build_avoid_plan(candidate: Candidate) -> ExecutionPlan:
    return ExecutionPlan(
        ticker=candidate.ticker,
        signal=candidate.arbitrage_signal,
        use_options=False,
        hold_state="AVOID_OR_SHORT",
        notes=(
            "Structural premium collapse detected: flag to short equity/puts "
            "or exit existing exposure. New-short sizing logic is out of "
            "scope until the short-side risk module is specified."
        ),
    )


def build_execution_plan(
    candidate: Candidate,
    total_cash: float,
    win_loss_ratio: float = 2.0,
) -> ExecutionPlan:
    """
    Dispatches to the correct sizing/exit routine based on
    candidate.arbitrage_signal (set by logic/matrix_matcher.evaluate_candidate).
    """
    if candidate.arbitrage_signal == c.SIGNAL_LONG_TERM_EQUITY_ACCUMULATION:
        return _build_equity_accumulation_plan(candidate, total_cash)

    if candidate.arbitrage_signal == c.SIGNAL_LONG_RUN_UP_ALPHA:
        return _build_options_run_up_plan(candidate, total_cash, win_loss_ratio)

    if candidate.arbitrage_signal == c.SIGNAL_STRUCTURAL_COLLAPSE_AVOID:
        return _build_avoid_plan(candidate)

    return ExecutionPlan(
        ticker=candidate.ticker,
        signal=c.SIGNAL_NONE,
        use_options=False,
        hold_state="NONE",
        notes="No actionable divergence; candidate remains in the monitoring pool.",
    )
