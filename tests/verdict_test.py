"""The three checks and the verdict on each dashboard row."""
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

from aegis_bioquant.telemetry import dashboard as D

TODAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)
failed = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failed.append(name)


def event(ticker, days, kind="PDUFA", nct=None, tier=None):
    return NS(ticker=ticker, company_name=ticker + " Inc", event_type=kind, start_date=TODAY + timedelta(days=days),
              resolved_nct_id=nct, resolution_tier=tier, drug_or_trial_candidate=None, source_url="https://www.sec.gov/x")


def pred(ticker, days, p_fund, move, rank, signal="NO_SIGNAL", est=True, basis="BASELINE", notes="n", when=NOW):
    return NS(ticker=ticker, catalyst_date=TODAY + timedelta(days=days), p_fundamental=p_fund, event_implied_move=move,
              iv_rank=rank, iv_rank_estimated=est, evidence_basis=basis, arbitrage_signal=signal, signal_notes=notes, scanned_at=when, point_score=None)


def move(ticker, days, value, conf="GOOD"):
    return NS(ticker=ticker, catalyst_date=TODAY + timedelta(days=days), event_move=value, confidence=conf)


events = [
    event("BUY", 40), event("SOON", 80), event("NEAR", 90), event("NOPE", 60), event("LATE", 10),
    event("OUT", 40), event("BLANK", 70), event("BAD", 40), event("ROUGH", 70), event("REAL", 40, nct="NCT00000001", tier="CONFIRMED"),
    event("OLDROW", 70),
]
preds = [
    pred("BUY", 40, 0.83, 0.22, 12.0, "LONG_RUN_UP_ALPHA"),
    pred("SOON", 80, 0.83, 0.18, 20.0, "LONG_RUN_UP_ALPHA"),
    pred("NEAR", 90, 0.83, 0.15, 40.0),
    pred("NOPE", 60, 0.83, 0.0, 80.0),
    pred("LATE", 10, 0.83, 0.30, 10.0, "LONG_RUN_UP_ALPHA"),
    pred("OUT", 40, 0.83, 0.2, 10.0, when=NOW - timedelta(days=1)),
    pred("BAD", 40, 0.35, 0.25, 70.0, "STRUCTURAL_COLLAPSE_AVOID", est=False, basis="TRIAL_RESULTS"),
    pred("ROUGH", 70, 0.83, 0.3, 10.0),
    pred("REAL", 40, 0.91, 0.2, 15.0, "LONG_RUN_UP_ALPHA", est=False, basis="TRIAL_RESULTS"),
    NS(ticker="OLDROW", catalyst_date=TODAY + timedelta(days=70), p_fundamental=0.9, event_implied_move=None, iv_rank=None,
       iv_rank_estimated=None, evidence_basis=None, arbitrage_signal="NO_SIGNAL", signal_notes=None, scanned_at=NOW, point_score=None),
]
screened = [NS(ticker="OUT", catalyst_date=TODAY + timedelta(days=40), scanned_at=NOW, reasons=["company value $31.0B is above the $20B ceiling"])]
moves = [move("ROUGH", 70, 0.3, "LOW_CONFIDENCE_SHORT_BASELINE"), move("BUY", 40, 0.22)]

rows = D._catalyst_rows(events, preds, screened, {}, moves, TODAY)
by = {r["ticker"]: r for r in rows}
st = lambda t: (by[t]["status"], by[t]["status_label"])  # noqa: E731
states = lambda t: [c["state"] for c in by[t]["checks"]]  # noqa: E731

check("passes the rule inside the window -> Buy setup", st("BUY") == ("signal", "Buy setup"))
check("...and on stand-ins the checks are marked soft, the measured one pass", states("BUY") == ["soft", "pass", "soft"])
check("real trial results + real IV Rank -> all three plain passes", states("REAL") == ["pass", "pass", "pass"] and by["REAL"]["checks"][0]["big"] == "Strong")
check("passes the rule before the window -> Setting up, says when the window opens", st("SOON") == ("setup", "Setting up") and "opens in 35 days" in by["SOON"]["status_detail"] and by["SOON"]["window_opens_in"] == 35)
check("two of three -> Close, names the missing one", st("NEAR") == ("close", "Close: 2 of 3") and "options are no longer cheap" in by["NEAR"]["status_detail"])
check("one of three -> No setup, lists what fails", st("NOPE")[0] == "none" and "not pricing any move" in by["NOPE"]["status_detail"] and "expensive" in by["NOPE"]["status_detail"])
check("past the window -> Too late, even with a signal", st("LATE") == ("late", "Too late to enter"))
check("screened out more recently than scored -> Filtered out with the reason, checks unknown", st("OUT")[0] == "filtered" and "$20B" in by["OUT"]["status_detail"] and states("OUT")[0] == "unknown")
check("never scored -> Not scored yet", st("BLANK") == ("unscored", "Not scored yet") and states("BLANK") == ["unknown"] * 3)
check("avoid rule -> Avoid", st("BAD") == ("avoid", "Avoid") and states("BAD") == ["fail", "pass", "fail"])
check("a rough move reading does not count as a pass", states("ROUGH")[1] == "fail" and st("ROUGH")[0] == "close")
check("a row saved before these columns existed still renders", st("OLDROW")[0] == "none" and states("OLDROW") == ["pass", "unknown", "unknown"])
check("best first: signals, then setting up, then close, ... late last", [r["ticker"] for r in rows][:2] in (["BAD", "BUY"], ["BUY", "BAD"], ["BAD", "REAL"], ["BUY", "REAL"], ["REAL", "BUY"], ["REAL", "BAD"])
      and [r["status"] for r in rows] == sorted((r["status"] for r in rows), key=D._STATUS_ORDER.get) and rows[-1]["ticker"] == "LATE")
check("baseline score never shows the unconfirmed guess as its trial", by["BUY"]["nct_id"] is None and "Nothing specific to this drug" in by["BUY"]["trial_note"])
check("every check has words", all(c["big"] and c["sub"] for r in rows for c in r["checks"]))

ev3 = [event("CL", 40), event("CLT", 80), event("CLLOW", 40)]
pr3 = [pred("CL", 40, 0.92, 0.2, 15.0, "LONG_RUN_UP_ALPHA", basis="CLAUDE"), pred("CLT", 80, 0.80, 0.2, 15.0, "LONG_RUN_UP_ALPHA", basis="CLAUDE_THIN"),
       pred("CLLOW", 40, 0.35, 0.2, 15.0, basis="CLAUDE")]
for _p in pr3: _p.point_score = 0.83
b3 = {r["ticker"]: r for r in D._catalyst_rows(ev3, pr3, [], {}, [], TODAY)}
check("Claude-driven strong score is a plain pass, labelled as Claude's", b3["CL"]["checks"][0]["state"] == "pass" and "from our review" in b3["CL"]["checks"][0]["sub"] and b3["CL"]["evidence_basis"] == "REVIEW" and "laude" not in str(b3).lower().replace("claude", "laude")[:0] and "laude" not in str(b3) and b3["CL"]["status"] == "signal")
check("a thin Claude review passes softly", b3["CLT"]["checks"][0]["state"] == "soft" and "little published" in b3["CLT"]["checks"][0]["sub"])
check("a low Claude score fails the evidence check and without quoting the retired score", b3["CLLOW"]["checks"][0]["big"] == "Weak" and b3["CLLOW"]["status"] in ("close", "none")
      and "registry-only" not in b3["CLLOW"]["trial_note"] and "our evidence review" in b3["CLLOW"]["trial_note"])
ev2 = [event("STK", 80), event("STKW", 40), event("FAR", 200)]
pr2 = [pred("STK", 80, 0.86, 0.0, 100.0, "LONG_TERM_EQUITY_ACCUMULATION", basis="TRIAL_RESULTS"),
       pred("STKW", 40, 0.86, 0.0, 100.0, "LONG_TERM_EQUITY_ACCUMULATION", basis="TRIAL_RESULTS")]
b2 = {r["ticker"]: r for r in D._catalyst_rows(ev2, pr2, [], {}, [], TODAY)}
check("stock-only rule before the window is not called Setting up", b2["STK"]["status"] == "stock" and "does not pass the three checks" in b2["STK"]["status_detail"])
check("stock-only rule inside the window is a stock-only buy setup", (b2["STKW"]["status"], b2["STKW"]["status_label"]) == ("signal", "Strong evidence, stock only") and "stock-only alert goes out" in b2["STKW"]["status_detail"] and "IV Rank" not in b2["STKW"]["status_detail"])
check("beyond the scoring horizon -> Too far out, says when scoring starts", b2["FAR"]["status_label"] == "Too far out" and "in 50 days" in b2["FAR"]["status_detail"])
print("ALL PASSED" if not failed else f"{len(failed)} FAILED")
sys.exit(1 if failed else 0)
