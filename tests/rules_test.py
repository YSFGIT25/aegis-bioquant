"""Rules version: the fingerprint, the stamp on stored scores, and the review history. No network."""
import asyncio
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from aegis_bioquant.config import rules as R
from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.engines.clinical_nlp.llm_review import LLMReview
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan

WHEN = date.today() + timedelta(days=40)
failed = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failed.append(name)


def result(ticker, pf):
    cand = Candidate(ticker=ticker, catalyst_type="PDUFA", catalyst_date=WHEN, spot_price=30, open_interest=5000, daily_options_volume=900, p_fundamental=pf)
    evaluate_candidate(cand)
    return SimpleNamespace(candidate=cand, fundamental_score=FundamentalScore(nct_id="NCT1", base_rate=0.83, p_fundamental=pf, adjustments=[ScoreAdjustment("x", 0.0, "t")]),
                           market_snapshot=None, execution_plan=build_execution_plan(cand, total_cash=1e5))


def review(score, summary):
    return LLMReview(score=score, primary_endpoint="MET", evidence_quality="ADEQUATE", drug="zorbimab", indication="flu",
                     application="NDA", summary=summary, positives=["p"], risks=["r"], sources=["u"])


async def history(ticker):
    async with db.get_session_factory()() as s:
        return list(await s.scalars(select(db.LLMReviewHistoryRecord).where(db.LLMReviewHistoryRecord.ticker == ticker).order_by(db.LLMReviewHistoryRecord.id)))


async def main():
    # ---- the fingerprint ----
    code = 'X = 1\ndef f(a):\n    """Doc."""\n    return a + X  # note\n'
    same = '# heading\nX = 1\n\n\ndef f(a):\n    """A different explanation,\n    on two lines."""\n    return a + X\n'
    check("comments, blank lines and docstrings are not part of the rules", R._code_tokens(code) == R._code_tokens(same))
    check("a changed number is", R._code_tokens(code) != R._code_tokens(code.replace("X = 1", "X = 2")))
    check("a changed message string is (it is not a docstring)", R._code_tokens('m = "a"\n') != R._code_tokens('m = "b"\n'))
    check("moving a line into or out of a block is", R._code_tokens("if a:\n    b()\nc()\n") != R._code_tokens("if a:\n    b()\n    c()\n"))
    fp = R.fingerprint()
    check("the rules as they stand are the ones written up in the log", not R.unlogged_change() and len(fp) == 12)
    check("the newest log entry is for the current version, and versions only go up",
          R.RULES_LOG[-1][0] == R.RULES_VERSION and [v for v, _, _ in R.RULES_LOG] == sorted({v for v, _, _ in R.RULES_LOG}))
    R.fingerprint.cache_clear(); old_model = settings.LLM_REVIEW_MODEL; settings.LLM_REVIEW_MODEL = "another-model"
    check("judging the evidence with a different model is a rule change", R.fingerprint() != fp and R.unlogged_change())
    settings.LLM_REVIEW_MODEL = old_model; R.fingerprint.cache_clear()
    check("...and putting it back restores the fingerprint", R.fingerprint() == fp)

    # ---- an older database gains the columns ----
    path = settings.DATABASE_URL.split("///", 1)[1]
    await db.init_db()
    await db.dispose_db()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE predictions DROP COLUMN rules_version"); con.execute("ALTER TABLE predictions DROP COLUMN rules_fingerprint")
    con.execute("DROP TABLE llm_review_history"); con.commit(); con.close()
    await db.init_db()

    # ---- the stamp on a stored score ----
    pid = await db.save_prediction(result("AAA", 0.88))
    async with db.get_session_factory()() as s:
        row = await s.get(db.PredictionRecord, pid)
    check("every stored score says which rules made it", (row.rules_version, row.rules_fingerprint) == (R.RULES_VERSION, fp))

    # ---- every review is kept ----
    await db.save_llm_review("AAA", "PDUFA", WHEN, review(80, "first"), "m1")
    await db.save_llm_review("AAA", "PDUFA", WHEN, review(60, "second"), "m1")
    await db.save_llm_review("AAA", "PDUFA", WHEN, None, "m1")
    h = await history("AAA")
    check("a catalyst reviewed three times has three reviews on record, each as it was written",
          [(r.score, r.summary, r.status) for r in h] == [(80, "first", "REVIEWED"), (60, "second", "REVIEWED"), (None, None, "NO_EVIDENCE")]
          and h[0].positives == ["p"] and h[0].sources == ["u"] and h[0].reviewed_at <= h[1].reviewed_at)
    check("...each stamped with the rules", all((r.rules_version, r.rules_fingerprint) == (R.RULES_VERSION, fp) for r in h))
    now = (await db.get_llm_reviews())[("AAA", "PDUFA", WHEN)]
    check("the page still reads only the newest", (now.status, now.score) == ("NO_EVIDENCE", None) and len(await db.get_llm_reviews()) == 1)

    # a review from before the history existed: only in llm_reviews
    async with db.get_session_factory()() as s:
        s.add(db.LLMReviewRecord(ticker="OLD", event_type="PDUFA", catalyst_date=WHEN, reviewed_at=datetime(2026, 10, 9, tzinfo=timezone.utc), model="m0",
                                 status="REVIEWED", score=90, summary="before", positives=["a"], risks=[], sources=[]))
        await s.commit()
    await db.touch_llm_review("OLD", "PDUFA", WHEN)
    check("looking at a review again without rewriting it adds nothing to the record", await history("OLD") == [])
    await db.save_llm_review("OLD", "PDUFA", WHEN, review(70, "after"), "m1")
    h = await history("OLD")
    check("replacing a review written before the record began keeps it too, marked as made under no numbered rules",
          [(r.score, r.summary, r.model, r.rules_version) for r in h] == [(90, "before", "m0", None), (70, "after", "m1", R.RULES_VERSION)])
    await db.save_llm_review("OLD", "PDUFA", WHEN, review(75, "again"), "m1")
    check("...once, not on every later review", [r.score for r in await history("OLD")] == [90, 70, 75])
    await db.dispose_db()


asyncio.run(main())
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL PASSED")
