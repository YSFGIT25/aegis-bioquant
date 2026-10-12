import asyncio, os, re, sys
os.environ["SESSION_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date, datetime, timedelta, timezone

import httpx
from aegis_bioquant import pipeline as P
from aegis_bioquant.config import constants as c, settings
from aegis_bioquant.core import database as db
from aegis_bioquant.ingestion import fda_scraper as F, outcome_finder as OF
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.ingestion.market_data_feed import reaction_closes
from aegis_bioquant.telemetry import dashboard as D
from aegis_bioquant.web.app import app
from finder_cases import CASES

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

TODAY = date.today()
d = lambda n: TODAY - timedelta(days=n)
real_sleep = asyncio.sleep
async def no_sleep(_): await real_sleep(0)
P.asyncio.sleep = no_sleep
F.asyncio.sleep = no_sleep

def ev(ticker, when, cik="0000001111", url=None, status="OUTSIDE_WINDOW", source="SEC_EDGAR_FTS", company=None, kind="PDUFA"):
    return CatalystEvent(event_type=kind, ticker=ticker, cik=cik, title="x", start_date=when, company_name=company or (f"{ticker} Inc" if ticker else None),
                         source_url=url or f"https://www.sec.gov/{ticker}-{when}", raw_source=source, horizon_status=status)

def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

async def main():
    # ---------------- reading a filing ----------------
    check("all 20 sample sentences are read correctly", all((OF.classify_filing_text(t) or (None,))[0] == want for t, want in CASES))
    got = OF.classify_filing_text("Background. The FDA has approved LUMVOA for thyroid eye disease. More text follows.")
    check("the quoted evidence is the whole sentence, and only that sentence", got == ("APPROVED", "The FDA has approved LUMVOA for thyroid eye disease."))

    # ---------------- finding the filing ----------------
    DOCS = {"1": "Quarterly results. As previously announced, the FDA has approved Zetamab.",        # filed latest, repeats the news
            "2": "Acme Announces U.S. FDA Approval of Zetamab. BOSTON",                               # the announcement itself
            "3": "If approved, Zetamab would launch next year.",                                      # before the decision
            "4": "The Company received a Complete Response Letter from the FDA."}
    def transport(dates, fail_search=False):
        opened = []
        def handler(request):
            if "search-index" in str(request.url):
                if fail_search: return httpx.Response(500)
                assert request.url.params["ciks"] == "0000001111" and request.url.params["forms"] == "8-K,6-K"
                return httpx.Response(200, json={"hits": {"hits": [{"_id": f"0001-26-00000{k}:doc{k}.htm", "_source": {"file_date": v}} for k, v in dates.items()]}})
            k = str(request.url).split("doc")[1][0]; opened.append(k)
            return httpx.Response(200, text=f"<html><body>{DOCS[k]}</body></html>")
        return httpx.MockTransport(handler), opened
    t, opened = transport({"1": "2026-09-20", "2": "2026-09-04", "3": "2026-08-20"})
    async with httpx.AsyncClient(transport=t) as cl:
        e = await OF.find_outcome_evidence(cl, "1111", date(2026, 9, 22), date(2026, 10, 7))
    check("the first announcement is used, not a later filing that repeats it", e.outcome == "APPROVED" and e.decision_date == date(2026, 9, 4) and "Announces U.S. FDA Approval" in e.sentence and e.filing_url.endswith("doc2.htm"))
    t, opened = transport({"3": "2026-08-20"})
    async with httpx.AsyncClient(transport=t) as cl:
        check("no filing states a decision -> nothing suggested", await OF.find_outcome_evidence(cl, "1111", date(2026, 9, 22), date(2026, 10, 7)) is None)
    t, opened = transport({"4": "2026-09-22"})
    async with httpx.AsyncClient(transport=t) as cl:
        check("a rejection is recognised", (await OF.find_outcome_evidence(cl, "1111", date(2026, 9, 22), date(2026, 10, 7))).outcome == "CRL")
    t, opened = transport({str(k): f"2026-09-{k:02d}" for k in (1, 2, 3, 4)}); c.OUTCOME_MAX_FILINGS_PER_CATALYST, keep = 2, c.OUTCOME_MAX_FILINGS_PER_CATALYST
    async with httpx.AsyncClient(transport=t) as cl:
        await OF.find_outcome_evidence(cl, "1111", date(2026, 9, 22), date(2026, 10, 7))
    c.OUTCOME_MAX_FILINGS_PER_CATALYST = keep
    check("only the few filings nearest the catalyst date are opened", sorted(opened) == ["3", "4"])
    t, _ = transport({}, fail_search=True)
    async with httpx.AsyncClient(transport=t) as cl:
        try: await OF.find_outcome_evidence(cl, "1111", date(2026, 9, 22), date(2026, 10, 7)); raised = False
        except httpx.HTTPError: raised = True
    check("search unreachable -> an error, not 'nothing found'", raised)

    # ---------------- the stock's reaction ----------------
    closes = [(date(2026, 9, 1), 10.0), (date(2026, 9, 2), 11.0), (date(2026, 9, 4), 15.4), (date(2026, 9, 8), 16.0)]
    check("reaction = close before the decision day to close after it", reaction_closes(closes, date(2026, 9, 3)) == (11.0, 15.4) and reaction_closes(closes, date(2026, 9, 4)) == (11.0, 16.0))
    check("no close after the decision yet -> not measured", reaction_closes(closes, date(2026, 9, 8)) is None and reaction_closes([], date(2026, 9, 3)) is None)

    # ---------------- which catalysts need an outcome ----------------
    await db.reset_db()
    EVENTS = [
        ev("DUE", d(5)), ev("DUE", d(5), url="https://www.sec.gov/older-filing-same-date"),   # same catalyst from two filings
        ev("SOON", d(-10)), ev("FAR", d(-90)), ev("OLD", d(c.OUTCOME_GIVE_UP_DAYS + 5)), ev("NOCIK", d(3), cik=None),
        ev(None, d(4)), ev("ADC", d(4), kind="ADCOM", source="FDA_ADCOM_CALENDAR"), ev("DONE", d(20)), ev("LATE", d(40)),
    ]
    await db.upsert_catalyst_events(EVENTS)
    await db.confirm_outcome("DONE", "PDUFA", d(20), "APPROVED", d(21))
    need = await db.get_catalysts_needing_outcome(TODAY, c.OUTCOME_LOOKBACK_DAYS, c.OUTCOME_GIVE_UP_DAYS)
    check("needing an outcome: due and soon-due PDUFA dates with a ticker, once each; far-off, long-past, AdCom, no-ticker and already-confirmed excluded",
          sorted(e.ticker for e in need) == ["DUE", "LATE", "NOCIK", "SOON"])

    # ---------------- the daily step ----------------
    looked_up = []
    async def fake_find(client, cik, catalyst_date, today=None):
        ticker = {d(5): "DUE", d(-10): "SOON", d(40): "LATE"}[catalyst_date]; looked_up.append(ticker)
        if ticker == "DUE": return OF.OutcomeEvidence("APPROVED", d(6), "https://www.sec.gov/doc", "The FDA has approved <b>Duemab</b>.")
        if ticker == "LATE": raise httpx.ConnectError("sec down")
        return None
    P.find_outcome_evidence = fake_find
    F._build_user_agent = lambda: "test-agent"
    closes_asked = []
    async def fake_closes(ticker, start, end):
        closes_asked.append(ticker)
        return [(d(24), 20.0), (d(22), 21.0), (d(20), 30.0)] if ticker == "DONE" else []
    P.fetch_daily_closes = fake_closes
    result = await P.update_outcomes(TODAY)
    rows = await db.get_outcome_rows()
    check("step: each due catalyst with an SEC number is looked up once", sorted(looked_up) == ["DUE", "LATE", "SOON"] and result[:2] == (2, 1))
    check("step: a found decision is stored as a SUGGESTION with its evidence, not as an outcome",
          (rows[("DUE", "PDUFA", d(5))].status, rows[("DUE", "PDUFA", d(5))].outcome, rows[("DUE", "PDUFA", d(5))].decision_date) == ("SUGGESTED", "APPROVED", d(6)))
    check("step: nothing found -> marked as checked; a failed lookup is not marked, so it is retried",
          rows[("SOON", "PDUFA", d(-10))].status == "PENDING" and ("LATE", "PDUFA", d(40)) not in rows and ("NOCIK", "PDUFA", d(3)) not in rows)
    check("step: the confirmed approval got its price reaction (21.00 -> 30.00 = +42.9%)", result[2] == 1 and rows[("DONE", "PDUFA", d(20))].price_change_pct == round(30 / 21 - 1, 4))
    check("a suggestion does not count as a decision anywhere", [r["ticker"] for r in await db.get_track_record()] == ["DONE"])
    looked_up.clear(); closes_asked.clear()
    result = await P.update_outcomes(TODAY)
    check("next scan: a pending suggestion and a just-checked catalyst are left alone; the failed one is retried; no price is re-fetched",
          looked_up == ["LATE"] and closes_asked == [] and result == (0, 0, 0))
    async with db.get_session_factory()() as s:
        row = await s.get(db.CatalystOutcomeRecord, rows[("SOON", "PDUFA", d(-10))].id)
        row.checked_at = datetime.now(timezone.utc) - timedelta(days=c.OUTCOME_RECHECK_DAYS + 1); await s.commit()
    looked_up.clear(); await P.update_outcomes(TODAY)
    check("a catalyst with nothing found is looked up again after the recheck interval", "SOON" in looked_up)
    await db.record_outcome_check("DONE", "PDUFA", d(20), "CRL", d(19), "u", "t")
    check("text matching never overwrites a confirmed outcome", (await db.get_outcome_rows())[("DONE", "PDUFA", d(20))].outcome == "APPROVED")
    keep = c.OUTCOME_MAX_CHECKS_PER_SCAN; c.OUTCOME_MAX_CHECKS_PER_SCAN = 1
    EVENTS += [ev("NEW1", d(2)), ev("NEW2", d(1))]
    await db.upsert_catalyst_events(EVENTS)
    async def find_none(client, cik, catalyst_date, today=None): looked_up.append(catalyst_date); return None
    P.find_outcome_evidence = find_none; looked_up.clear()
    await P.update_outcomes(TODAY); c.OUTCOME_MAX_CHECKS_PER_SCAN = keep
    check("a backlog is worked through a few per scan, most recent date first", looked_up == [d(1)])

    # ---------------- the review page ----------------
    settings.RESEND_API_KEY = settings.EMAIL_FROM = settings.PUBLIC_BASE_URL = None
    settings.ADMIN_EMAILS = frozenset()
    async with client() as admin, client() as user, client() as anon:
        await admin.post("/signup", data={"email": "owner@test.invalid", "password": "correct horse battery"})
        await user.post("/signup", data={"email": "customer@test.invalid", "password": "correct horse battery"})
        settings.ADMIN_EMAILS = frozenset({"owner@test.invalid"})
        check("outcomes page: logged out -> login; ordinary customer -> 404", (await anon.get("/review/outcomes")).status_code == 303 and (await user.get("/review/outcomes")).status_code == 404)
        page = (await admin.get("/review/outcomes")).text
        tok = csrf(page)
        check("page lists catalysts that reached their date; one still ahead is listed only if a filing already states a decision", "DUE · PDUFA" in page and "LATE · PDUFA" in page and "SOON · PDUFA" not in page and "FAR · PDUFA" not in page)
        check("the suggestion is shown with its sentence, escaped, and a link to the filing", "this looks like <strong>Approved</strong>" in page and "The FDA has approved &lt;b&gt;Duemab&lt;/b&gt;." in page and "<b>Duemab</b>" not in page and 'href="https://www.sec.gov/doc"' in page)
        check("the suggested outcome and filing date are pre-filled, but nothing is recorded until confirmed", re.search(r'value="APPROVED"[^>]*checked', page) is not None and f'value="{d(6).isoformat()}"' in page and "DUE</td>" not in page)
        check("already-recorded outcomes are listed with their reaction", "DONE</td>" in page and "+42.9%" in page)
        form = {"ticker": "DUE", "catalyst_date": d(5).isoformat(), "outcome": "APPROVED", "decision_date": d(6).isoformat()}
        r = await user.post("/review/outcomes/confirm", data={**form, "csrf_token": csrf((await user.get("/settings")).text)})
        check("ordinary customer cannot record an outcome", r.status_code == 404)
        r = await admin.post("/review/outcomes/confirm", data=form)
        check("no CSRF token -> refused", r.headers["location"] == "/login")
        post = lambda **kw: admin.post("/review/outcomes/confirm", data={**form, "csrf_token": tok, **kw})
        check("a decision date in the future is refused", (await post(decision_date=d(-1).isoformat())).headers["location"].endswith("outcome_bad"))
        check("a decision date far before the PDUFA date is refused", (await post(decision_date=d(400).isoformat())).headers["location"].endswith("outcome_bad"))
        check("an unknown outcome value is refused", (await post(outcome="MOON")).headers["location"].endswith("outcome_bad"))
        check("a garbage date is refused", (await post(decision_date="yesterday")).headers["location"].endswith("outcome_bad"))
        check("a catalyst the system doesn't know is refused", (await post(ticker="ZZZZ")).headers["location"].endswith("outcome_no_event"))
        check("none of that recorded anything", (await db.get_outcome_rows())[("DUE", "PDUFA", d(5))].status == "SUGGESTED")
        r = await post(outcome="CRL")
        row = (await db.get_outcome_rows())[("DUE", "PDUFA", d(5))]
        check("the administrator's choice is what is recorded, even against the suggestion", r.headers["location"].endswith("outcome_saved") and (row.status, row.outcome, row.decision_date) == ("CONFIRMED", "CRL", d(6)) and row.confirmed_by is not None)
        page = (await admin.get("/review/outcomes?msg=outcome_saved")).text
        check("after recording: gone from the waiting list, shown under Recorded", "DUE · PDUFA" not in page and "DUE</td>" in page and "Rejected (complete response letter)" in page)
        r = await admin.post("/review/outcomes/undo", data={"csrf_token": tok, "ticker": "DUE", "catalyst_date": d(5).isoformat()})
        check("undo puts it back in the waiting list", r.headers["location"].endswith("outcome_undone") and ("DUE", "PDUFA", d(5)) not in await db.get_outcome_rows() and "DUE · PDUFA" in (await admin.get("/review/outcomes")).text)
        r = await admin.post("/review/outcomes/undo", data={"csrf_token": tok, "ticker": "DUE", "catalyst_date": d(5).isoformat()})
        check("undoing something that isn't recorded does nothing", r.headers["location"].endswith("outcome_no_event"))
        await post(outcome="DELAYED")
        check("a moved date is recorded but is not a decision in the track record", [r["ticker"] for r in await db.get_track_record()] == ["DONE"])
        check("the Review page links to Outcomes", 'href="/review/outcomes"' in (await admin.get("/review")).text)

    # ---------------- the track record ----------------
    from types import SimpleNamespace
    from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
    from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
    from aegis_bioquant.logic.risk_manager import build_execution_plan
    def result(ticker, when, pf):
        cand = Candidate(ticker=ticker, catalyst_type="PDUFA", catalyst_date=when, spot_price=30, open_interest=5000, daily_options_volume=900, p_fundamental=pf)
        evaluate_candidate(cand)
        return SimpleNamespace(candidate=cand, fundamental_score=FundamentalScore(nct_id="NCT1", base_rate=0.83, p_fundamental=pf, adjustments=[ScoreAdjustment("x", 0.0, "t")]),
                               market_snapshot=None, execution_plan=build_execution_plan(cand, total_cash=1e5))
    at = lambda day: datetime(day.year, day.month, day.day, 19, 0, tzinfo=timezone.utc)
    await db.save_prediction(result("DONE", d(20), 0.70), scanned_at=at(d(40)))
    await db.save_prediction(result("DONE", d(20), 0.88), scanned_at=at(d(22)))    # the last one before the decision on d(21)
    await db.save_prediction(result("DONE", d(20), 0.99), scanned_at=at(d(21)))    # the decision day itself: outcome may be known
    await db.save_prediction(result("DONE", d(20), 0.99), scanned_at=at(d(10)))    # after the decision
    await db.save_event_move_snapshot("DONE", "PDUFA", d(20), d(23), 0.31, 0.8, 1.0, 0.3, "GOOD", 20.0)
    await db.save_event_move_snapshot("DONE", "PDUFA", d(20), d(21), 0.05, 0.8, 0.8, 0.0, "GOOD", 30.0)   # decision day: ignored
    EVENTS += [ev("REJ", d(8))]
    await db.upsert_catalyst_events(EVENTS); await db.confirm_outcome("REJ", "PDUFA", d(8), "CRL", d(8))
    rec = {r["ticker"]: r for r in await db.get_track_record()}
    check("track record uses the last score taken BEFORE the decision, never one from the decision day or after", rec["DONE"]["p_fundamental"] == 0.88)
    check("...and the last option reading before the decision", rec["DONE"]["event_move_priced"] == 0.31)
    check("a decision that was never scored is still recorded, with no score", rec["REJ"]["p_fundamental"] is None and rec["REJ"]["outcome"] == "CRL")
    st = (await D.build_state())["track_record"]
    check("dashboard summary: counts, and accuracy only over the decisions that had a score",
          (st["decisions"], st["approved"], st["rejected"], st["scored"], st["scored_approved"]) == (2, 1, 1, 1, 1) and abs(st["brier"] - (0.88 - 1) ** 2) < 1e-9 and abs(st["average_score"] - 0.88) < 1e-9)
    check("dashboard rows carry the reaction and what was said before", st["rows"][0]["ticker"] == "REJ" and st["rows"][1]["price_change_pct"] == round(30 / 21 - 1, 4) and st["rows"][1]["event_move_priced"] == 0.31)
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
