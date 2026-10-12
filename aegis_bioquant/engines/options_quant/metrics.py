"""
engines/options_quant/metrics.py
------------------------------------
IV Rank & Market-Implied Probability solver (Phase B, Engine 2, steps 2-4).

Consumes ingestion/market_data_feed.py's OptionChainSnapshot/Quote models.
Computes no network calls itself — pure math over already-fetched data.

METHODOLOGY NOTE — the manual's own description of "Market-Implied
Probability" ("solve for P_Market using the pricing structure of OTM
calls/puts relative to the straddle") is informal, not a rigorous
derivation. Rather than invent a formula to match that prose literally,
this module implements the actual, standard quant techniques for the two
things that description is gesturing at, named correctly:

  1. ATM_STRADDLE_IMPLIED_MOVE — "how big a move is the market pricing
     in" (straddle price / spot). This IS what "straddle valuation" means
     in practice.

  2. RISK_NEUTRAL_PROB_ABOVE — "what probability is the market assigning
     to the stock finishing above a price level". Derived from real
     options-pricing theory (digital-option replication via the first
     derivative of the call price curve w.r.t. strike: dC/dK =
     -e^(-rT) * P(S_T > K)), not guessed.

HONEST LIMITATION, stated plainly rather than hidden: (2) gives the
risk-neutral probability the STOCK finishes above a price level — it does
NOT directly give "probability FDA approves." Options markets price the
stock's terminal distribution, which is informed by investors' approval
beliefs plus everything else (macro moves, unrelated news, noise).
market_implied_approval_probability() below uses spot price itself as the
threshold — i.e. "will the stock be higher than it is right now" — as the
standard proxy biotech-catalyst options traders actually use, on the
assumption that near a binary catalyst, the terminal distribution is
dominated by the event outcome. That assumption can be wrong (a sector
selloff, a secondary offering, unrelated company news between now and
expiry), so treat this as a proxy signal, not ground truth — exactly the
same posture fda_scraper.py and clinical_trials_api.py already take
toward their own uncertain outputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from aegis_bioquant.config import constants as c
from aegis_bioquant.ingestion.market_data_feed import OptionChainSnapshot, OptionContract


# --------------------------------------------------------------------------
# IV Rank
# --------------------------------------------------------------------------

def compute_iv_rank(
    current_iv: float,
    historical_iv_min: Optional[float],
    historical_iv_max: Optional[float],
) -> Optional[float]:
    """
    IV Rank = (IV_current - IV_min) / (IV_max - IV_min) * 100, over the
    trailing 52 weeks per the manual.

    There is no free source for 52-week historical IV, so the history
    is one this system builds itself: the scan stores each tracked
    ticker's IV once a day (pipeline.record_iv_snapshots) and
    iv_rank_from_history() below feeds the accumulated min/max in here.
    Returns None, not a fabricated number, when they're not supplied.
    """
    if historical_iv_min is None or historical_iv_max is None:
        return None
    spread = historical_iv_max - historical_iv_min
    if spread <= 0:
        return None
    return round((current_iv - historical_iv_min) / spread * 100, 2)


def atm_implied_volatility(chain: OptionChainSnapshot, spot: float) -> Optional[float]:
    """
    The chain's at-the-money implied volatility: the average of the ATM
    call's and put's IV, using only legs that have a real two-sided quote
    and a plausible value. yfinance back-solves IV from whatever price it
    has, so a contract with no live quote reports nonsense (0.00001, or
    several thousand percent) — those are ignored rather than averaged
    in. None if neither leg is usable.
    """
    straddle = chain.atm_straddle(spot)
    if straddle is None:
        return None
    readings = [
        leg.implied_volatility
        for leg in straddle
        if leg.has_quoted_market
        and leg.implied_volatility is not None
        and c.IV_PLAUSIBLE_MIN < leg.implied_volatility < c.IV_PLAUSIBLE_MAX
    ]
    if not readings:
        return None
    return round(sum(readings) / len(readings), 4)


def iv_rank_from_history(current_iv: Optional[float], history: list[float]) -> Optional[float]:
    """
    IV Rank of `current_iv` against this system's own stored readings
    (see compute_iv_rank). Stays None until there are at least
    IV_RANK_MIN_READINGS of them.

    HONEST LIMITATION: the manual's IV Rank is over 52 weeks. Until a
    ticker has been tracked for a year, this is a rank over however long
    it HAS been tracked — a shorter window, which can call IV "low" or
    "high" relative to a few quiet or noisy weeks. It converges on the
    manual's definition as history accumulates.
    """
    if current_iv is None or len(history) < c.IV_RANK_MIN_READINGS:
        return None
    return compute_iv_rank(current_iv, min(history), max(history))


def realized_volatility_range(closes: list[float], window: int = 30) -> Optional[tuple[float, float]]:
    """
    (lowest, highest) annualised volatility the stock actually showed over
    any `window`-trading-day stretch in `closes` (daily closing prices,
    oldest first). None without enough history to mean anything.
    """
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(returns) < window * 2:
        return None
    vols = []
    for end in range(window, len(returns) + 1):
        chunk = returns[end - window:end]
        mean = sum(chunk) / window
        variance = sum((r - mean) ** 2 for r in chunk) / (window - 1)
        vols.append(math.sqrt(variance * 252))
    return min(vols), max(vols)


def options_cost_estimate(current_iv: Optional[float], hv_low: Optional[float], hv_high: Optional[float]) -> Optional[float]:
    """
    A stand-in for IV Rank that works on day one: where today's implied
    volatility sits between the lowest and highest volatility the stock
    actually showed over the past year, 0 to 100.

    WHY IT EXISTS: real IV Rank compares today's implied volatility with
    a year of PAST implied volatility, which this system has to collect
    itself and will not have for IV_RANK_MIN_READINGS trading days per
    stock. Without a stand-in, no buy or avoid signal could be raised for
    the first six weeks of tracking any stock.

    HOW IT DIFFERS FROM THE REAL THING, stated plainly: implied
    volatility normally runs ABOVE the volatility a stock goes on to
    show, more so ahead of a known event. Measured against realised
    volatility, options therefore read as dearer than true IV Rank would
    say. The bias is toward "not cheap" — fewer buy signals, not more.
    Anything built on this is marked as an estimate, and the real IV Rank
    replaces it for a stock as soon as its history is long enough.
    """
    if current_iv is None or hv_low is None or hv_high is None or hv_high <= hv_low:
        return None
    return round(min(max((current_iv - hv_low) / (hv_high - hv_low) * 100, 0.0), 100.0), 2)


def classify_iv_richness(iv_rank: Optional[float]) -> str:
    """
    Per the manual's Phase B step 3: IV Rank < 30 is a candidate
    ("underpriced"), < 20 is ideal. "UNKNOWN" when IV Rank couldn't be
    computed at all (the common case right now, given the gap above) —
    deliberately distinct from "RICH", since they mean different things
    downstream (one is "don't know", the other is "know, and it's bad").
    """
    if iv_rank is None:
        return "UNKNOWN"
    if iv_rank < c.IV_RANK_IDEAL_THRESHOLD:
        return "IDEAL"
    if iv_rank < c.IV_RANK_UNDERPRICED_THRESHOLD:
        return "ACCEPTABLE"
    return "RICH"


# --------------------------------------------------------------------------
# Straddle-implied move
# --------------------------------------------------------------------------

@dataclass
class StraddleImpliedMove:
    expiration_straddle_price: float
    implied_move_pct: float          # straddle price as a % of spot
    call_spread_pct: Optional[float]
    put_spread_pct: Optional[float]
    quote_quality: str               # "GOOD" | "WIDE" | "NO_MARKET"


def atm_straddle_implied_move(
    chain: OptionChainSnapshot, spot: float
) -> Optional[StraddleImpliedMove]:
    """
    Standard "options-implied move": ATM call mid + ATM put mid, as a %
    of spot. This is literally what "straddle valuation" means in
    options trading — the market's own estimate of the expected move by
    expiration, with no probability-of-approval interpretation attached.
    """
    straddle = chain.atm_straddle(spot)
    if straddle is None:
        return None
    call, put = straddle

    if not (call.has_quoted_market and put.has_quoted_market):
        return None  # no two-sided market on one leg — can't price a straddle at all

    call_mid = (call.bid + call.ask) / 2
    put_mid = (put.bid + put.ask) / 2
    straddle_price = call_mid + put_mid

    worst_spread = max(call.bid_ask_spread_pct or 0, put.bid_ask_spread_pct or 0)
    quality = "GOOD" if worst_spread < 0.15 else "WIDE"

    return StraddleImpliedMove(
        expiration_straddle_price=round(straddle_price, 4),
        implied_move_pct=round(straddle_price / spot, 4),
        call_spread_pct=call.bid_ask_spread_pct,
        put_spread_pct=put.bid_ask_spread_pct,
        quote_quality=quality,
    )


# --------------------------------------------------------------------------
# Risk-neutral probability (digital-option replication)
# --------------------------------------------------------------------------

@dataclass
class RiskNeutralProbability:
    threshold: float
    probability_above: float
    lower_strike: float
    upper_strike: float
    confidence: str  # "GOOD" | "LOW_CONFIDENCE_WIDE_SPREAD" | "LOW_CONFIDENCE_SPARSE_STRIKES"


def _nearest_straddling_calls(
    calls: list[OptionContract], threshold: float
) -> Optional[tuple[OptionContract, OptionContract]]:
    below = [c for c in calls if c.strike < threshold]
    above = [c for c in calls if c.strike > threshold]
    if not below or not above:
        return None
    return max(below, key=lambda c: c.strike), min(above, key=lambda c: c.strike)


def risk_neutral_prob_above(
    chain: OptionChainSnapshot,
    threshold: float,
    risk_free_rate: float = 0.0,
    time_to_expiry_years: float = 0.0,
) -> Optional[RiskNeutralProbability]:
    """
    P(S_T > threshold) via digital-option replication:

        C(K) = e^(-rT) * E[max(S_T - K, 0)]
        dC/dK = -e^(-rT) * P(S_T > K)
        => P(S_T > K) ~= -e^(rT) * (C(K_upper) - C(K_lower)) / (K_upper - K_lower)

    using the two listed strikes nearest the threshold on either side.
    This is the first-derivative digital-replication method, not the
    second-derivative Breeden-Litzenberger density — deliberately: BL's
    second derivative is numerically unstable on raw (unsmoothed) strike
    grids, especially with the wide, noisy spreads typical of thin
    biotech option chains. The first derivative only needs two clean
    quotes, which is what we can actually trust here.

    risk_free_rate defaults to 0.0 (r*T is negligible for the short
    dated, weeks-to-months windows this system trades) — pass a real
    rate if the caller has one.
    """
    straddling = _nearest_straddling_calls(chain.calls, threshold)
    if straddling is None:
        return None
    lower, upper = straddling

    if not (lower.has_quoted_market and upper.has_quoted_market):
        return None

    lower_mid = (lower.bid + lower.ask) / 2
    upper_mid = (upper.bid + upper.ask) / 2

    discount = math.exp(risk_free_rate * time_to_expiry_years)
    raw_prob = -discount * (upper_mid - lower_mid) / (upper.strike - lower.strike)
    probability = min(max(raw_prob, 0.0), 1.0)  # clip: numerical noise can push slightly outside [0,1]

    worst_spread = max(lower.bid_ask_spread_pct or 0, upper.bid_ask_spread_pct or 0)
    strike_gap_pct = (upper.strike - lower.strike) / threshold if threshold else 1.0

    if worst_spread >= 0.5:
        confidence = "LOW_CONFIDENCE_WIDE_SPREAD"
    elif strike_gap_pct > 0.15:
        confidence = "LOW_CONFIDENCE_SPARSE_STRIKES"
    else:
        confidence = "GOOD"

    return RiskNeutralProbability(
        threshold=threshold,
        probability_above=round(probability, 4),
        lower_strike=lower.strike,
        upper_strike=upper.strike,
        confidence=confidence,
    )


def market_implied_approval_probability(
    chain: OptionChainSnapshot,
    spot: float,
    risk_free_rate: float = 0.0,
    time_to_expiry_years: float = 0.0,
) -> Optional[RiskNeutralProbability]:
    """
    P_Market proxy: risk-neutral P(S_T > spot) at the expiry bounding the
    catalyst — "will the market have pushed the stock above where it is
    right now." See this module's docstring for why this is a proxy for
    approval odds, not approval odds itself.
    """
    return risk_neutral_prob_above(chain, spot, risk_free_rate, time_to_expiry_years)


# --------------------------------------------------------------------------
# Event-implied move
# --------------------------------------------------------------------------

@dataclass
class EventImpliedMove:
    move: float                 # one-standard-deviation EXTRA move the options assign to the event, e.g. 0.23 = 23%
    baseline_iv: float          # ATM IV of the expiry BEFORE the catalyst (no event inside it)
    post_event_iv: float        # ATM IV of the expiry AFTER the catalyst (event inside it)
    days_to_baseline_expiry: int
    days_to_post_event_expiry: int
    event_share: float          # fraction of the post-event expiry's total variance that is the event, 0..1
    confidence: str             # "GOOD" | "LOW_CONFIDENCE_SHORT_BASELINE" | "LOW_CONFIDENCE_BASELINE_NEAR_EVENT"


def event_implied_move(
    before: Optional[OptionChainSnapshot],
    after: Optional[OptionChainSnapshot],
    spot: float,
    catalyst_date,
    today,
) -> Optional[EventImpliedMove]:
    """
    How big a move the options market is pricing for the catalyst ITSELF,
    separated from the stock's ordinary day-to-day movement.

    WHY THIS EXISTS: market_implied_approval_probability() above answers
    "will the stock end higher than it is now", which measured live came
    out between 0.44 and 0.59 for every stock tried, including ones with
    no FDA event at all — it cannot tell an event from no event. This
    measure can: on the same day it read 0% for Coca-Cola and Exxon, 2%
    for a large biotech's label expansion, 23% and 52% for two small
    companies facing a make-or-break decision.

    METHOD (standard variance arithmetic, no model beyond it): variance
    adds up over time. The expiry before the catalyst prices only
    ordinary movement; the expiry after it prices ordinary movement PLUS
    the event. So

        event variance = ((post-event IV)^2 - (baseline IV)^2) * T_after

    i.e. everything the later expiry prices, minus what the same number
    of ordinary days would cost at the pre-event rate. What is left is
    the EXTRA movement priced for the event, over and above an ordinary
    day; the move is its square root. A stock with no event therefore
    reads 0, not its usual daily swing. A zero or negative result means
    the options price nothing extra for the event, returned as 0.

    WHAT IT IS NOT: it is a size, not a direction and not a probability
    of approval. It says nothing about which way the stock goes.

    HONEST LIMITATIONS:
      - It rests on two implied-volatility readings from thinly traded
        options; either being off moves the answer. Needs live quotes on
        the at-the-money options of BOTH expiries, or it returns None.
      - The FDA can act before its PDUFA date. If the market expects
        that, some event risk is already inside the "baseline" expiry,
        which makes the event look smaller than it is — flagged when the
        baseline expiry falls within a few days of the catalyst.
      - A baseline expiry only days away gives a jumpy baseline — also
        flagged.
    Returns None if either expiry or either IV is unavailable.
    """
    if before is None or after is None:
        return None
    baseline_iv = atm_implied_volatility(before, spot)
    post_iv = atm_implied_volatility(after, spot)
    if baseline_iv is None or post_iv is None:
        return None

    days_before = (before.expiration - today).days
    days_after = (after.expiration - today).days
    if days_before < 1 or days_after <= days_before:
        return None

    total_variance = post_iv ** 2 * days_after / 365
    event_variance = total_variance - baseline_iv ** 2 * days_after / 365
    move = math.sqrt(event_variance) if event_variance > 0 else 0.0
    share = max(event_variance, 0.0) / total_variance if total_variance > 0 else 0.0

    if days_before < c.EVENT_MOVE_MIN_BASELINE_DAYS:
        confidence = "LOW_CONFIDENCE_SHORT_BASELINE"
    elif (catalyst_date - before.expiration).days <= c.EVENT_MOVE_BASELINE_EVENT_GAP_DAYS:
        confidence = "LOW_CONFIDENCE_BASELINE_NEAR_EVENT"
    else:
        confidence = "GOOD"

    return EventImpliedMove(
        move=round(move, 4),
        baseline_iv=baseline_iv,
        post_event_iv=post_iv,
        days_to_baseline_expiry=days_before,
        days_to_post_event_expiry=days_after,
        event_share=round(share, 4),
        confidence=confidence,
    )
