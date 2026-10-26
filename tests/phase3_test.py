"""Phase 3 forecasts: registry reading, the review's validation and cost, storage, the daily run. No network."""
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

from aegis_bioquant.config import rules as study_rules
from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db
from aegis_bioquant.engines.clinical_nlp.llm_review import LLMError
from aegis_bioquant.ingestion import fda_scraper as F
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.phase3 import constants as k
from aegis_bioquant.phase3 import llm as P3L
from aegis_bioquant.phase3 import review as R
from aegis_bioquant.phase3 import rules, scan, store

TODAY = date.today()
NOW = datetime.now(timezone.utc)
failed = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failed.append(name)


STUDY = {
    "hasResults": False,
    "protocolSection": {
        "identificationModule": {"nctId": "NCT01234567", "acronym": "BRAVE-3", "briefTitle": "Zorbimab in rare flu"},
        "conditionsModule": {"conditions": ["Rare flu"]},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme Bio"}, "collaborators": [{"name": "BigPharma"}]},
        "armsInterventionsModule": {"interventions": [
            {"type": "DRUG", "name": "Zorbimab", "otherNames": ["ZB-101"]},
            {"type": "DRUG", "name": "Placebo"},
            {"type": "BEHAVIORAL", "name": "Diet advice"},
        ]},
        "designModule": {"phases": ["PHASE3"], "designInfo": {"allocation": "RANDOMIZED", "maskingInfo": {"masking": "DOUBLE"}},
                         "enrollmentInfo": {"count": 400}},
        "statusModule": {"overallStatus": "ACTIVE_NOT_RECRUITING", "primaryCompletionDateStruct": {"date": "2026-12"}},
        "outcomesModule": {"primaryOutcomes": [{"measure": "Time to symptom relief"}]},
    },
}

GOOD = {"success_likelihood": 64, "evidence_quality": "ADEQUATE", "results_already_announced": False,
        "results_announced_on": "", "expected_readout": "first quarter of 2027",
        "science": "Phase 2 showed a clear effect.", "team": "The CEO led two approved drugs.",
        "experts": "Dr. X at Y calls it promising.", "warning_signs": "None found",
        "positives": ["Phase 2 met its goal"], "risks": ["Endpoint has failed before"],
        "summary": "It  worked in Phase 2. The endpoint is hard.",
        "web_sources": ["https://journal.example/paper", "https://made-up.example/x", "http://journal.example/plain"]}


def anthropic(fields, *, stop="tool_use", usage=None, model="claude-opus-5-5", pause_first=False, status=200):
    def handler(request):
        body = json.loads(request.content)
        handler.calls.append((body, dict(request.headers)))
        if pause_first and len(handler.calls) == 1:
            return httpx.Response(200, json={"stop_reason": "pause_turn", "model": model,
                                             "usage": {"input_tokens": 1000, "output_tokens": 100},
                                             "content": [{"type": "server_tool_use", "name": "web_search"}]})
        content = [
            {"type": "web_search_tool_result", "content": [{"url": "https://journal.example/paper"},
                                                           {"url": "http://journal.example/plain"}]},
            {"type": "tool_use", "name": "record_forecast", "input": fields},
        ]
        return httpx.Response(status, json={
            "stop_reason": stop, "model": model, "content": content,
            "stop_details": {"type": "refusal", "category": "bio"} if stop == "refusal" else None,
            "usage": usage or {"input_tokens": 30000, "output_tokens": 4000, "server_tool_use": {"web_search_requests": 5}},
        })
    handler.calls = []
    return handler


def registry(request):
    return httpx.Response(200, json=STUDY) if "NCT01234567" in str(request.url) else httpx.Response(404)


async def fake_get(client, url):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"hits": {"hits": [{"_id": "0001-26-000001:r.htm", "_source": {"file_date": (TODAY - timedelta(days=20)).isoformat()}}]}}
    return Resp()


async def fake_text(client, url):
    return "In Phase 2, zorbimab met its primary endpoint (p=0.01) versus placebo; it was well tolerated. " * 8


async def review(handler, nct="NCT01234567", cik="1234"):
    async with httpx.AsyncClient() as sec, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as llm, \
            httpx.AsyncClient(transport=httpx.MockTransport(registry)) as http:
        return await R.review_trial(sec, llm, http, ticker="ACME", company="Acme Bio", cik=cik, nct_id=nct,
                                    title="Zorbimab in rare flu", completion=date(2026, 12, 1), today=TODAY)


def event(nct, ticker, completion, title="A trial"):
    return CatalystEvent(event_type="PHASE_3_READOUT", ticker=ticker, company_name=f"{ticker} Inc", title=title,
                         start_date=completion, source_url=f"https://clinicaltrials.gov/study/{nct}", raw_source="CTGOV_PHASE3",
                         drug_or_trial_candidate=title, resolved_nct_id=nct, resolution_tier="DIRECT")


async def main():
    F._get_with_retry, F._fetch_filing_text = fake_get, fake_text
    settings.ANTHROPIC_API_KEY = "sk-test-secret"
    settings.SEC_EDGAR_CONTACT_EMAIL = "test@example.test"

    # --- the study this track must not touch ---
    check("FDA-decision study fingerprint unchanged", study_rules.fingerprint() == study_rules.RECORDED_FINGERPRINT)
    check("Phase 3 rules are a trial run until registered", rules.RULES_VERSION == 0)
    check("Phase 3 fingerprint recorded", not rules.unlogged_change())

    # --- registry ---
    async with httpx.AsyncClient(transport=httpx.MockTransport(registry)) as http:
        trial = await R.fetch_trial(http, "NCT01234567")
        missing = await R.fetch_trial(http, "NCT07654321")
        bad_id = await R.fetch_trial(http, "not-an-id")
    check("registry: drug names without placebo or non-drug arms", trial.drug_names == ["Zorbimab", "ZB-101"])
    check("registry: sponsor, partner, design and goal in the summary",
          trial.sponsor == "Acme Bio" and "BigPharma" in trial.summary and "DOUBLE" in trial.summary
          and "Time to symptom relief" in trial.summary and "participants: 400" in trial.summary)
    check("registry: unreadable or malformed entries give None", missing is None and bad_id is None)

    # --- the review ---
    h = anthropic(GOOD)
    v = await review(h)
    body, headers = h.calls[0]
    check("review: score, fields and tidy text", v.status == R.STATUS_REVIEWED and v.score == 64 and v.evidence_quality == "ADEQUATE"
          and v.drug == "Zorbimab" and v.summary == "It worked in Phase 2. The endpoint is hard." and v.team.startswith("The CEO"))
    check("review: only https pages the search returned are kept as sources",
          "https://journal.example/paper" in v.sources and not any("made-up" in s or s.startswith("http://") for s in v.sources))
    check("review: registry entry and the company filing listed as sources",
          v.sources[0] == "https://clinicaltrials.gov/study/NCT01234567" and any(s.endswith("/r.htm") for s in v.sources))
    check("review: always searches the web, current tool version, capped",
          body["tools"][0] == {"type": "web_search_20260209", "name": "web_search", "max_uses": k.P3_MAX_WEB_SEARCHES})
    check("review: opts into server-side fallback", body.get("fallbacks") == "default"
          and headers.get("anthropic-beta") == P3L.FALLBACK_BETA)
    check("review: no forced tool call, effort set", body["tool_choice"] == {"type": "auto"}
          and body["output_config"] == {"effort": k.P3_REVIEW_EFFORT})
    prompt = body["messages"][0]["content"]
    check("review: prompt carries registry, filings and the four kinds of evidence",
          "<trial_registry_entry>" in prompt and "<filing" in prompt and "Who is behind it" in body["system"]
          and "independent" in body["system"] and "Warning signs" in body["system"] and "58 in 100" in body["system"])
    check("review: cost estimated from usage (30k in, 4k out, 5 searches = $0.25)", abs(v.cost_usd - 0.25) < 1e-9)
    check("review: answering model stored (fallback may change it)", v.model == "claude-opus-5-5")

    v = await review(anthropic({**GOOD, "success_likelihood": 140, "evidence_quality": "GREAT"}))
    check("review: score clamped to 99, unknown quality read as THIN", v.score == 99 and v.evidence_quality == "THIN")
    v = await review(anthropic({**GOOD, "results_already_announced": True, "results_announced_on": (TODAY - timedelta(days=3)).isoformat()}))
    check("review: results already public -> not a forecast", v.status == R.STATUS_ALREADY_REPORTED
          and v.results_announced_on == TODAY - timedelta(days=3))
    v = await review(anthropic({**GOOD, "results_announced_on": (TODAY + timedelta(days=9)).isoformat()}))
    check("review: a date with no announcement, or in the future, is not kept", v.results_announced_on is None and v.status == R.STATUS_REVIEWED)
    v = await review(anthropic({**GOOD, "results_already_announced": "yes"}))
    check("review: only a real true marks results as announced", v.status == R.STATUS_REVIEWED)
    v = await review(anthropic(GOOD, model="claude-opus-4-8"))
    check("review: a fallback model's answer is recorded as that model", v.model == "claude-opus-4-8")

    h = anthropic(GOOD, pause_first=True)
    v = await review(h)
    check("review: an unfinished search is continued, usage added up",
          len(h.calls) == 2 and abs(v.cost_usd - (0.25 + 1000 / 1e6 * 4 + 100 / 1e6 * 20)) < 1e-9)

    for name, handler in (("declined", anthropic(GOOD, stop="refusal")), ("cut off", anthropic(GOOD, stop="max_tokens")),
                          ("HTTP error", anthropic(GOOD, status=529)), ("no score", anthropic({**GOOD, "success_likelihood": "high"}))):
        try:
            await review(handler)
            check(f"review: {name} raises LLMError", False)
        except LLMError as exc:
            check(f"review: {name} raises LLMError without the key", "sk-test" not in str(exc))
    check("review: unreadable registry entry -> no review", await review(anthropic(GOOD), nct="NCT07654321") is None)
    v = await review(anthropic(GOOD), cik=None)
    check("review: no CIK -> web only, still a review", v is not None and not any(s.endswith("/r.htm") for s in v.sources))

    # --- storage ---
    await db.init_db()
    await store.init_tables()
    ev = event("NCT01234567", "ACME", TODAY + timedelta(days=30), "BRAVE-3")
    await db.upsert_catalyst_events([ev])
    rec = (await store.trials_to_forecast(TODAY))[0]
    first = await review(anthropic(GOOD))
    await store.save_review(rec, first, NOW - timedelta(days=1))
    await store.save_review(rec, await review(anthropic({**GOOD, "success_likelihood": 70})), NOW)
    current = (await store.get_reviews())["NCT01234567"]
    factory = db.get_session_factory()
    async with factory() as session:
        history = list(await session.scalars(db.select(store.Phase3ReviewHistoryRecord)))
    check("storage: one current review, replaced", current.score == 70 and current.trial == "BRAVE-3")
    check("storage: every review kept in history with the Phase 3 stamp",
          [h.score for h in history] == [64, 70] and all(h.rules_version == 0 and h.rules_fingerprint == rules.fingerprint() for h in history))
    await store.save_score(rec, current, 12.5, NOW)
    s = (await store.get_scores("NCT01234567"))[0]
    check("storage: daily score stamped, as a probability, with price and base rate",
          s.p_success == 0.70 and s.spot_price == 12.5 and s.base_rate == k.P3_BASE_RATE and s.rules_version == 0)

    # --- the daily run ---
    await db.reset_db()
    await db.upsert_catalyst_events([
        event("NCT00000001", "GOOD", TODAY + timedelta(days=20)),
        event("NCT00000002", "PENNY", TODAY + timedelta(days=25)),
        event("NCT00000003", "OLD", TODAY - timedelta(days=k.P3_LOOKBACK_DAYS + 5)),
        event("NCT00000004", "DONE", TODAY - timedelta(days=10)),
        event("NCT00000005", "LATER", TODAY + timedelta(days=60)),
        event("NCT00000006", "FAR", TODAY + timedelta(days=k.P3_LOOKAHEAD_DAYS + 5)),
        event("NCT00000007", "NOPRICE", TODAY + timedelta(days=40)),
    ])
    prices = {"GOOD": 12.0, "PENNY": 1.5, "DONE": 30.0, "LATER": 8.0}

    class Quote:
        def __init__(self, p): self.last_price = p

    async def fake_quote(ticker):
        if ticker not in prices:
            raise ValueError("no data")
        return Quote(prices[ticker])

    calls = []

    async def fake_review(sec, llm, http, **kw):
        calls.append(kw["ticker"])
        base = R.Phase3Review(status=R.STATUS_REVIEWED, score=61, evidence_quality="ADEQUATE", drug="drugx", condition="c",
                              science="s", team="t", experts="e", warning_signs="w", summary="sum", cost_usd=0.3)
        if kw["ticker"] == "DONE":
            base.status, base.results_announced_on = R.STATUS_ALREADY_REPORTED, TODAY - timedelta(days=2)
        return base

    async def fake_ciks(sec):
        return {}

    scan.fetch_quote, scan.cik_by_ticker, R.review_trial = fake_quote, fake_ciks, fake_review
    rep = await scan.run(TODAY, now=NOW, max_reviews=2)
    check("run: only trials in the window", rep.trials == 5)
    check("run: price screen and missing prices", rep.under_price == 1 and rep.no_price == 1)
    check("run: reviews capped per scan, soonest first", calls == ["DONE", "GOOD"] and rep.reviewed == 2)
    check("run: already-reported trial not scored; uncapped one waits", rep.already_reported == 1 and rep.waiting_for_review == 1
          and rep.scored == 1)
    check("run: cost added up and reported", abs(rep.cost_usd - 0.6) < 1e-9 and "~$0.60" in rep.line())

    calls.clear()
    rep2 = await scan.run(TODAY, now=NOW + timedelta(hours=1))
    check("run: next run reviews only what is due", calls == ["LATER"] and rep2.scored == 2)
    calls.clear()
    rep3 = await scan.run(TODAY + timedelta(days=k.P3_REVIEW_MAX_AGE_DAYS),
                          now=NOW + timedelta(days=k.P3_REVIEW_MAX_AGE_DAYS))
    check("run: an old review is redone; an already-reported trial never is", sorted(calls) == ["GOOD", "LATER"])
    scores = await store.get_scores()
    check("run: one score row per trial per run", sorted(s.ticker for s in scores) == ["GOOD", "GOOD", "GOOD", "LATER", "LATER"])

    row = (await store.get_reviews())["NCT00000001"]
    check("due: a fresh review is not redone", scan.review_due(row, row.reviewed_at.replace(tzinfo=timezone.utc) + timedelta(days=1)) == "")
    check("due: a week-old review is checked for new filings",
          scan.review_due(row, row.reviewed_at.replace(tzinfo=timezone.utc) + timedelta(days=k.P3_REVIEW_RECHECK_DAYS)) == "check")

    await db.dispose_db()
    print(f"\n{'ALL PASSED' if not failed else str(len(failed)) + ' FAILED'}")
    sys.exit(1 if failed else 0)


asyncio.run(main())
