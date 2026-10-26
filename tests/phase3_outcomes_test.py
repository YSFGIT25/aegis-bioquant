"""Phase 3 results: the fixed rule, the quote check, finding filings, timing, grading, prices. No network."""
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db
from aegis_bioquant.engines.clinical_nlp.llm_review import LLMError
from aegis_bioquant.ingestion import fda_scraper as F
from aegis_bioquant.ingestion import market_data_feed as M
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.phase3 import constants as k
from aegis_bioquant.phase3 import outcomes as O
from aegis_bioquant.phase3 import review as R
from aegis_bioquant.phase3 import scan, store

TODAY = date.today()
NOW = datetime.now(timezone.utc)
EASTERN = ZoneInfo("America/New_York")
failed = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failed.append(name)


RESULT = ("Acme today announced topline results from BRAVE-3, its Phase 3 trial of zorbimab in rare flu. "
          "The BRAVE-3 trial met its primary endpoint, with a statistically significant reduction in time to "
          "symptom relief (p<0.001) versus placebo. ") * 2
PLAIN = "Acme will present at an investor conference about zorbimab and BRAVE-3 next month. " * 5
QUOTE = "The BRAVE-3 trial met its primary endpoint, with a statistically significant reduction in time to symptom relief (p<0.001) versus placebo."


def anthropic(fields):
    def handler(request):
        handler.calls.append(json.loads(request.content))
        return httpx.Response(200, json={"stop_reason": "tool_use", "model": "claude-opus-5-5",
                                         "usage": {"input_tokens": 5000, "output_tokens": 500},
                                         "content": [{"type": "tool_use", "name": "record_outcome", "input": fields}]})
    handler.calls = []
    return handler


MET = {"reports_this_trial": True, "outcome": "MET", "quote": QUOTE, "first_announced_on": "", "reason": "Met with p<0.001."}

searches = []
HITS = {}


async def fake_get(client, url):
    searches.append(url)

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"hits": {"hits": [{"_id": d, "_source": {"file_date": f.isoformat()}} for d, f in HITS.items()]}}
    return Resp()


async def fake_text(client, url):
    return RESULT if "result" in url else PLAIN


async def classify(fields, text=RESULT):
    async with httpx.AsyncClient(transport=httpx.MockTransport(anthropic(fields))) as llm:
        return await O.classify_filing(llm, trial_text="Registry ID: NCT01234567\nTrial: BRAVE-3", filing_text=text)


def event(nct, ticker, completion, title):
    return CatalystEvent(event_type="PHASE_3_READOUT", ticker=ticker, company_name=f"{ticker} Inc", title=title,
                         start_date=completion, source_url=f"https://clinicaltrials.gov/study/{nct}", raw_source="CTGOV_PHASE3",
                         drug_or_trial_candidate=title, resolved_nct_id=nct, resolution_tier="DIRECT")


def a_review(score=72):
    return R.Phase3Review(status=R.STATUS_REVIEWED, score=score, evidence_quality="ADEQUATE", drug="zorbimab",
                          condition="rare flu", science="s", team="t", experts="e", warning_signs="w", summary="sum")


async def run_check(handler, ciks={"ACME": "1234"}, now=NOW):
    async with httpx.AsyncClient() as sec, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as llm:
        return await O.check(sec, llm, ciks, now.date(), now)


async def main():
    F._get_with_retry, F._fetch_filing_text = fake_get, fake_text
    settings.ANTHROPIC_API_KEY = "sk-test-secret"
    settings.SEC_EDGAR_CONTACT_EMAIL = "test@example.test"

    # --- the rule and the quote check ---
    decision, cost, model = await classify(MET)
    check("classify: MET with the exact quote", decision["outcome"] == "MET" and decision["quote"] == QUOTE and cost > 0)
    curly = await classify({**MET, "quote": QUOTE.replace("  ", " ")}, text=RESULT.replace("'", "’"))
    check("classify: spacing and curly quotes don't break the match", curly[0] is not None)
    for name, fields in (("a quote not in the filing", {**MET, "quote": "The trial met its primary endpoint with flying colours."}),
                         ("a quote too short to check", {**MET, "quote": "met"})):
        try:
            await classify(fields)
            check(f"classify: {name} is refused", False)
        except LLMError:
            check(f"classify: {name} is refused", True)
    check("classify: another trial -> no decision", (await classify({**MET, "reports_this_trial": False}))[0] is None)
    check("classify: NONE -> no decision", (await classify({**MET, "outcome": "NONE"}))[0] is None)
    check("classify: an outcome outside the rule -> no decision", (await classify({**MET, "outcome": "MIXED"}))[0] is None)
    first = (await classify({**MET, "first_announced_on": "2026-10-01"}))[0]
    check("classify: an earlier announcement date is kept", first["first_announced_on"] == date(2026, 10, 1))
    system = anthropic(MET)
    async with httpx.AsyncClient(transport=httpx.MockTransport(system)) as llm:
        await O.classify_filing(llm, trial_text="t", filing_text=RESULT)
    body = system.calls[0]
    check("classify: the fixed rule is in the prompt, no web search",
          "every one must have been met" in body["system"] and "stopped for futility" in body["system"]
          and all(t.get("type") != "web_search_20260209" for t in body["tools"]))

    # --- timing ---
    t = O.announcement_time(date(2026, 11, 3), None)
    check("time: midnight US Eastern at the start of the filing date",
          t == datetime(2026, 11, 3, 0, 0, tzinfo=EASTERN).astimezone(timezone.utc))
    check("time: an earlier announcement date wins", O.announcement_time(date(2026, 11, 3), date(2026, 11, 1))
          == datetime(2026, 11, 1, 0, 0, tzinfo=EASTERN).astimezone(timezone.utc))

    # --- finding and recording a result ---
    await db.init_db()
    await store.init_tables()
    await db.upsert_catalyst_events([event("NCT01234567", "ACME", TODAY - timedelta(days=10), "BRAVE-3")])
    ev = (await store.trials_to_forecast(TODAY))[0]
    review = await store.save_review(ev, a_review(72), NOW - timedelta(days=5))
    await store.save_score(ev, review, 10.0, NOW - timedelta(days=3))
    review = await store.save_review(ev, a_review(80), NOW - timedelta(hours=2))
    await store.save_score(ev, review, 10.0, NOW - timedelta(hours=1))

    HITS.clear()
    HITS.update({"0001-26-000001:plain.htm": TODAY - timedelta(days=2), "0001-26-000002:result.htm": TODAY})
    h = anthropic(MET)
    rep = await run_check(h)
    out = (await O.get_outcomes())["NCT01234567"]
    check("check: the result is recorded from the filing", out.outcome == "MET" and out.filing_url.endswith("/result.htm")
          and out.quote == QUOTE and rep.recorded and out.rules_version == 0)
    check("check: a filing without results talk is not shown to the model", len(h.calls) == 1 and rep.read == 1)
    check("check: searched by drug, trial name and registry ID",
          any("zorbimab" in s for s in searches) and any("BRAVE-3" in s for s in searches) and any("NCT01234567" in s for s in searches))
    announced = out.announced_at if out.announced_at.tzinfo else out.announced_at.replace(tzinfo=timezone.utc)
    check("check: announced at midnight Eastern on the filing date",
          announced == datetime.combine(TODAY, datetime.min.time(), tzinfo=EASTERN).astimezone(timezone.utc))

    searches.clear()
    await run_check(anthropic(MET))
    check("check: a trial with a result is not searched again", searches == [])

    # --- grading ---
    rows = await O.graded()
    g = rows[0]
    # The score 1 hour ago is after the announcement time minus 24h, so the one 3 days ago counts.
    expected = 0.72 if NOW - timedelta(days=3) <= announced - timedelta(hours=24) else None
    check("grade: the last score stored 24h before the announcement counts", len(rows) == 1 and g["p_success"] == expected
          and g["brier"] == round((0.72 - 1) ** 2, 4) and g["base_brier"] == round((k.P3_BASE_RATE - 1) ** 2, 4))

    # --- a finished trial is no longer scored ---
    async def fake_quote(ticker):
        class Q: last_price = 10.0
        return Q()

    async def ciks(sec):
        return {"ACME": "1234"}

    async def no_review(*a, **kw):
        raise AssertionError("should not review a finished trial")

    scan.fetch_quote, scan.cik_by_ticker, R.review_trial = fake_quote, ciks, no_review
    before = len(await store.get_scores())
    srep = await scan.run(TODAY, now=NOW)
    check("scan: a trial with a result is skipped", srep.finished == 1 and len(await store.get_scores()) == before
          and "1 finished" in srep.line())

    # --- prices ---
    day = announced.astimezone(EASTERN).date()

    async def closes(ticker, start, end):
        return [(day - timedelta(days=1), 10.0), (day, 11.0), (day + timedelta(days=1), 15.0)]

    M.fetch_daily_closes = closes
    check("price: nothing before the day after", await O.fill_prices(day) == 0)
    check("price: filled once the day after has closed", await O.fill_prices(day + timedelta(days=2)) == 1)
    out = (await O.get_outcomes())["NCT01234567"]
    check("price: close before to close after", out.price_before == 10.0 and out.price_after == 15.0 and out.price_change_pct == 0.5)

    # --- trouble leaves the trial to be searched again ---
    await db.reset_db()
    await db.upsert_catalyst_events([event("NCT07654321", "ACME", TODAY - timedelta(days=10), "SWIFT-2")])
    ev = (await store.trials_to_forecast(TODAY))[0]
    review = await store.save_review(ev, a_review(50), NOW - timedelta(days=5))
    await store.save_score(ev, review, 10.0, NOW - timedelta(days=3))
    HITS.clear()
    HITS.update({"0001-26-000003:result.htm": TODAY})
    rep = await run_check(anthropic({**MET, "quote": "The trial met its primary endpoint beautifully, said nobody."}))
    out = (await O.get_outcomes())["NCT07654321"]
    check("trouble: an unverifiable quote records nothing", out.outcome is None and rep.errors)
    check("trouble: the filing is read again next time", not out.checked_docs and out.checked_through is None)
    k.P3_OUTCOME_MAX_READS_PER_SCAN = 0
    rep = await run_check(anthropic(MET))
    out = (await O.get_outcomes())["NCT07654321"]
    check("cap: no reads left -> nothing recorded, the day not marked done", out.outcome is None and out.checked_through is None)
    k.P3_OUTCOME_MAX_READS_PER_SCAN = 20
    rep = await run_check(anthropic({**MET, "outcome": "NOT_MET"}), ciks={})
    check("no CIK -> not searched", rep.checked == 0)
    rep = await run_check(anthropic({**MET, "outcome": "NOT_MET"}))
    out = (await O.get_outcomes())["NCT07654321"]
    check("NOT_MET recorded and graded as a miss", out.outcome == "NOT_MET"
          and (await O.graded())[0]["brier"] == round(0.5 ** 2, 4))

    await db.dispose_db()
    print(f"\n{'ALL PASSED' if not failed else str(len(failed)) + ' FAILED'}")
    sys.exit(1 if failed else 0)


asyncio.run(main())
