import sys
from datetime import date
from aegis_bioquant.config import constants as c
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def ev(pf, move, rank, conf="GOOD", oi=5000, pm=0.44):
    x = Candidate(ticker="T", catalyst_type="PDUFA", catalyst_date=date(2026, 11, 14), spot_price=30, open_interest=oi,
                  daily_options_volume=900, p_fundamental=pf, p_market=pm, iv_rank=rank, event_implied_move=move,
                  event_move_confidence=conf if move is not None else None)
    return evaluate_candidate(x)

LONG, AVOID, EQUITY, NONE = c.SIGNAL_LONG_RUN_UP_ALPHA, c.SIGNAL_STRUCTURAL_COLLAPSE_AVOID, c.SIGNAL_LONG_TERM_EQUITY_ACCUMULATION, c.SIGNAL_NONE

check("thresholds: clinical 0.75 / 0.40, event move 10%, IV Rank under 30 / over 50",
      (c.LONG_ALPHA_P_FUNDAMENTAL_MIN, c.SHORT_ALPHA_P_FUNDAMENTAL_MAX, c.EVENT_MATTERS_MIN_MOVE, c.IV_RANK_UNDERPRICED_THRESHOLD, c.COLLAPSE_IV_RANK_RICH) == (0.75, 0.40, 0.10, 30, 50))

# ---- long ----
check("strong score + event that matters + cheap premium -> buy", ev(0.83, 0.23, 18).arbitrage_signal == LONG)
check("exactly at each threshold: 0.75 / 10% count, IV Rank 30 does not", ev(0.75, 0.10, 29.9).arbitrage_signal == LONG and ev(0.75, 0.10, 30).arbitrage_signal == NONE)
check("just under the clinical or event threshold -> nothing", ev(0.749, 0.23, 18).arbitrage_signal == NONE and ev(0.83, 0.099, 18).arbitrage_signal == NONE)
check("premium not cheap -> nothing", ev(0.83, 0.23, 45).arbitrage_signal == NONE)
check("no IV Rank yet -> nothing (it is never assumed cheap)", ev(0.83, 0.23, None).arbitrage_signal == NONE)
check("no event-move reading -> nothing (it is never assumed to matter)", ev(0.83, None, 18).arbitrage_signal == NONE)
check("options price no extra move for the event -> nothing", ev(0.83, 0.0, 18).arbitrage_signal == NONE)
check("jumpy reading (pre-event options expire within days) is not trusted", ev(0.83, 0.40, 18, conf="LOW_CONFIDENCE_SHORT_BASELINE").arbitrage_signal == NONE)
check("understated reading (pre-event expiry right before the catalyst) that still clears 10% counts", ev(0.83, 0.23, 18, conf="LOW_CONFIDENCE_BASELINE_NEAR_EVENT").arbitrage_signal == LONG)

# ---- the retired number decides nothing ----
for pm in (None, 0.05, 0.40, 0.50, 0.51, 0.95):
    a, b = ev(0.83, 0.23, 18, pm=pm), ev(0.83, 0.0, None, pm=pm)
    check(f"old market number {pm}: same outcome whatever it says", a.arbitrage_signal == LONG and b.arbitrage_signal == NONE)
check("yesterday's CYTK reading (clinical 0.83, old number 0.44, no event priced, no IV Rank) is no longer a buy", ev(0.83, 0.0, None, pm=0.44).arbitrage_signal == NONE)
check("the old number is still recorded", ev(0.83, 0.23, 18, pm=0.40).divergence_delta is not None)

# ---- avoid ----
check("weak score + event that matters + rich premium -> avoid", ev(0.35, 0.23, 70).arbitrage_signal == AVOID)
check("weak score but premium not rich -> nothing", ev(0.35, 0.23, 50).arbitrage_signal == NONE and ev(0.35, 0.23, 20).arbitrage_signal == NONE)
check("weak score, rich premium, but no event priced -> nothing", ev(0.35, 0.0, 70).arbitrage_signal == NONE)
check("weak score, no IV Rank -> nothing", ev(0.35, 0.23, None).arbitrage_signal == NONE)
check("middling score never signals", all(ev(pf, 0.23, r).arbitrage_signal == NONE for pf in (0.41, 0.60, 0.74) for r in (5, 70)))

# ---- stock-only track unchanged, and still first ----
check("exceptional score + thin options -> stock-only track (needs no IV Rank)", ev(0.90, 0.0, None, oi=1200).arbitrage_signal == EQUITY)
check("exceptional score + rich premium -> stock-only track", ev(0.90, 0.23, 70).arbitrage_signal == EQUITY)
check("exceptional score + cheap premium + liquid options -> the options buy, not the stock-only track", ev(0.90, 0.23, 18).arbitrage_signal == LONG)

# ---- every outcome explains itself ----
check("a buy states its three facts", ev(0.83, 0.23, 18).signal_notes == "clinical score 0.83; event move priced 23%; IV Rank 18")
check("no event priced + no IV Rank: both reasons given", ev(0.83, 0.0, None).signal_notes == "options price no extra move for the event (needs at least 10%); IV Rank not available yet (needs 30 daily readings)")
check("small event: says how small", "only 6% of extra move" in ev(0.83, 0.06, 18).signal_notes)
check("premium not cheap: says the rank", ev(0.83, 0.23, 45).signal_notes == "IV Rank 45 is not under 30 (premium not cheap)")
check("weak drug, premium not rich: says so", ev(0.35, 0.23, 20).signal_notes == "IV Rank 20 is not over 50 (premium not rich)")
check("middling score: says so", "between the avoid level (0.40) and the buy level (0.75)" in ev(0.60, 0.23, 18).signal_notes)
check("no reading: says what is missing", "no reading of the event move" in ev(0.83, None, 18).signal_notes)
check("untrusted reading: says why", "not trusted" in ev(0.83, 0.40, 18, conf="LOW_CONFIDENCE_SHORT_BASELINE").signal_notes)
check("every candidate gets a note", all(ev(pf, m, r).signal_notes for pf in (0.3, 0.6, 0.83, 0.9) for m in (None, 0.0, 0.2) for r in (None, 10, 40, 80)))

# ---- the plan that follows a signal is unchanged ----
plan = build_execution_plan(ev(0.83, 0.23, 18), total_cash=100_000.0)
check("a buy still gets the options run-up plan with its exit date", plan.hold_state == "SHORT_TERM_RUN_UP" and plan.use_options and any(t.label == "PDUFA_EXIT" and t.active for t in plan.exit_triggers))
check("no signal still gets no allocation", build_execution_plan(ev(0.83, 0.0, None), total_cash=100_000.0).allocation_dollars == 0)
print("ALL PASSED")
