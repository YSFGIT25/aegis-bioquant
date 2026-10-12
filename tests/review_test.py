import asyncio, os, re, sys
os.environ["SESSION_COOKIE_SECURE"] = "0"
from datetime import date, timedelta

import httpx
from aegis_bioquant import pipeline as P
from aegis_bioquant.config import settings as _settings
_settings.REQUIRE_MARKET_HOURS = False   # these tests run at any hour
async def _no_snapshot(ticker, catalyst_date): raise RuntimeError('tests stay off the network')
P.fetch_market_snapshot = _no_snapshot
async def _no_quote(ticker): raise RuntimeError('tests stay off the network')
P.fetch_quote = _no_quote
from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.ingestion import clinical_trials_api as C
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan
from aegis_bioquant.telemetry import dashboard as D
from aegis_bioquant.web.app import app

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

TODAY = date.today()
WHEN = TODAY + timedelta(days=40)
PW = "correct horse battery"

def ev(ticker, tier, nct, candidate, company, status="PDUFA_ENTRY_WINDOW", when=WHEN):
    return CatalystEvent(event_type="PDUFA", ticker=ticker, company_name=company, title="x", start_date=when,
                         source_url=f"https://www.sec.gov/{ticker}{when}", raw_source="SEC_EDGAR_FTS", horizon_status=status,
                         drug_or_trial_candidate=candidate, resolved_nct_id=nct, resolution_tier=tier)
EVENTS = [ev("SMMT", "NAME_UNCONFIRMED", "NCT06396065", "HARMONi", "Summit Therapeutics Inc."),
          ev("BTAI", "NDA_FOR", "NCT06041646", "IGALMI", "BioXcel Therapeutics, Inc."),
          ev("CYTK", "ACRONYM", "NCT05767346", "MAPLE-HCM", "CYTOKINETICS INC"),
          ev("NOMATCH", None, None, None, "Quiet Bio Corp"),
          ev("FAR", "NDA_FOR", "NCT00000001", "x", "Far Co", status="OUTSIDE_WINDOW", when=TODAY + timedelta(days=200))]

opt = lambda nct, acr, title, results, phases=("PHASE3",): C.TrialOption(nct_id=nct, acronym=acr, brief_title=title, sponsor="s",
        phases=list(phases), overall_status="ACTIVE_NOT_RECRUITING", has_results=results, primary_completion="2026-06")
REGISTRY = {"NCT06396065": opt("NCT06396065", None, "Phase III Study of AK112 for NSCLC Patients", False),
            "NCT05184712": opt("NCT05184712", "HARMONi", "Ivonescimab plus chemotherapy in EGFR+ NSCLC <b>bold</b>", True),
            "NCT05899608": opt("NCT05899608", "HARMONi-3", "Ivonescimab first-line squamous NSCLC", False)}
lookups, registry_down = [], [False]
async def fake_list(sponsor, term=None, limit=12):
    lookups.append((sponsor, term))
    if registry_down[0]: raise httpx.ConnectError("down")
    return list(REGISTRY.values()) if "summit" in sponsor else []
async def fake_fetch(nct_id):
    if registry_down[0]: raise httpx.ConnectError("down")
    return REGISTRY.get(nct_id)
C.list_trial_options, C.fetch_trial_option = fake_list, fake_fetch

FORM = {"ticker": "SMMT", "event_type": "PDUFA", "catalyst_date": WHEN.isoformat()}

async def main():
    await db.reset_db()
    await db.upsert_catalyst_events(EVENTS)
    settings.RESEND_API_KEY = settings.EMAIL_FROM = settings.PUBLIC_BASE_URL = None
    settings.ADMIN_EMAILS = frozenset()

    async with client() as admin, client() as user, client() as anon:
        # the owner creates their account BEFORE being listed as an administrator
        await admin.post("/signup", data={"email": "Owner@test.invalid", "password": PW})
        await user.post("/signup", data={"email": "customer@test.invalid", "password": PW})
        check("with no administrators set, the review page does not exist for anyone", (await admin.get("/review")).status_code == 404)
        settings.ADMIN_EMAILS = frozenset({"owner@test.invalid", "future-admin@test.invalid"})

        check("review page: not logged in -> login", (await anon.get("/review")).status_code == 303)
        check("review page: ordinary customer -> 404", (await user.get("/review")).status_code == 404)
        check("ordinary customer's menu has no Review link", 'href="/review"' not in (await user.get("/dashboard")).text)
        check("administrator's menu has the Review link", 'href="/review"' in (await admin.get("/dashboard")).text)
        r = await anon.post("/signup", data={"email": "Future-Admin@test.invalid", "password": PW})
        check("an administrator address with no account yet cannot be signed up by a stranger", r.status_code == 400 and "already exists" in r.text
              and await db.get_session_factory()().__aenter__() is not None and (await __import__("aegis_bioquant.core.accounts", fromlist=["x"]).get_user_by_email("future-admin@test.invalid")) is None)

        r = await admin.get("/review")
        page = r.text
        tok = csrf(page)
        check("review page lists in-window catalysts only", r.status_code == 200 and all(t in page for t in ("SMMT ·", "BTAI ·", "CYTK ·", "NOMATCH ·")) and "FAR ·" not in page)
        check("the registry was asked for each company's trials for the named drug", ("summit therapeutics", "HARMONi") in lookups and ("cytokinetics", "MAPLE-HCM") in lookups)
        check("unsure match is labelled as an unconfirmed guess; a confident one as automatic; none as none",
              "Unconfirmed guess:" in page and "Matched automatically" in page and "No trial matched." in page)
        check("candidate trials shown with what matters: results posted or not", "HARMONi-3" in page and "results posted" in page and "no results posted" in page)
        check("the current guess is pre-selected", re.search(r'value="NCT06396065"\s+checked', page) is not None)
        check("text from the registry is escaped", "<b>bold</b>" not in page and "&lt;b&gt;bold&lt;/b&gt;" in page)

        # ---- who may confirm ----
        r = await user.post("/review/confirm", data={**FORM, "nct_choice": "NCT05184712", "csrf_token": csrf((await user.get("/settings")).text)})
        check("ordinary customer cannot confirm a trial", r.status_code == 404 and not await db.get_trial_overrides())
        r = await admin.post("/review/confirm", data={**FORM, "nct_choice": "NCT05184712"})
        check("confirm without CSRF token refused", r.headers["location"] == "/login" and not await db.get_trial_overrides())
        r = await admin.post("/review/confirm", data={**FORM, "nct_choice": "NCT05184712", "csrf_token": tok}, headers={"Origin": "https://evil.example"})
        check("confirm from another site refused", r.status_code == 403 and not await db.get_trial_overrides())

        # ---- validation ----
        post = lambda **d: admin.post("/review/confirm", data={**FORM, "csrf_token": tok, **d})
        check("nothing selected -> asked to choose", (await post()).headers["location"] == "/review?msg=trial_none")
        check("malformed ID refused", (await post(nct_manual="12345")).headers["location"] == "/review?msg=trial_bad_id")
        check("an ID with extra text refused", (await post(nct_manual="NCT05184712; drop table")).headers["location"] == "/review?msg=trial_bad_id")
        check("well-formed ID the registry doesn't know refused", (await post(nct_manual="NCT99999999")).headers["location"] == "/review?msg=trial_unknown")
        check("catalyst not in an entry window refused", (await post(ticker="FAR", catalyst_date=(TODAY + timedelta(days=200)).isoformat(), nct_choice="NCT05184712")).headers["location"] == "/review?msg=trial_no_event")
        check("made-up catalyst refused", (await post(ticker="ZZZZ", nct_choice="NCT05184712")).headers["location"] == "/review?msg=trial_no_event")
        registry_down[0] = True
        check("registry unreachable -> not saved, told to retry", (await post(nct_choice="NCT05184712")).headers["location"] == "/review?msg=trial_unreachable")
        down_page = (await admin.get("/review")).text
        check("registry unreachable -> page still loads and still takes a typed ID", "could not be reached" in down_page and 'name="nct_manual"' in down_page)
        registry_down[0] = False
        check("none of the refused attempts saved anything", not await db.get_trial_overrides())

        # ---- confirm ----
        r = await post(nct_choice="NCT05899608", nct_manual="nct05184712")
        ov = (await db.get_trial_overrides())[("SMMT", "PDUFA", WHEN)]
        check("confirm: typed ID wins over the selection, case-insensitive; title taken from the registry",
              r.headers["location"] == "/review?msg=trial_confirmed" and ov.nct_id == "NCT05184712" and ov.trial_title.startswith("HARMONi: Ivonescimab") and ov.confirmed_by is not None)
        page = (await admin.get("/review?msg=trial_confirmed")).text
        check("review page shows it confirmed, with an undo", "Trial confirmed. It will be scored at the next scan." in page and "Confirmed:</span> NCT05184712" in page and "Remove confirmation" in page)
        st = await D.build_state()
        check("dashboard data carries the confirmation", [(x["ticker"], x["nct_id"]) for x in st["confirmed"]] == [("SMMT", "NCT05184712")])
        await post(nct_choice="NCT05899608")
        check("confirming again replaces the choice (one row)", len(await db.get_trial_overrides()) == 1 and (await db.get_trial_overrides())[("SMMT", "PDUFA", WHEN)].nct_id == "NCT05899608")
        await post(nct_manual="NCT05184712")

        # ---- the scan uses it ----
        scored = []
        async def fake_ingest(problems=None): return [e.model_copy() for e in EVENTS]
        async def fake_run(ticker, nct_id, catalyst_type, catalyst_date, total_cash=100_000.0, **kw):
            scored.append((ticker, nct_id))
            cand = Candidate(ticker=ticker, catalyst_type=catalyst_type, catalyst_date=catalyst_date, spot_price=7.4, open_interest=5000,
                             daily_options_volume=900, p_fundamental=0.83, p_market=None)
            evaluate_candidate(cand)
            fs = FundamentalScore(nct_id=nct_id, base_rate=0.83, p_fundamental=0.83, adjustments=[ScoreAdjustment("x", 0.0, "t")])
            return P.PipelineResult(candidate=cand, fundamental_score=fs, market_snapshot=None, execution_plan=build_execution_plan(cand, total_cash=total_cash))
        async def no_iv(ticker, min_days): return None
        P.ingest_catalysts, P.run_candidate_pipeline, P.fetch_iv_reference = fake_ingest, fake_run, no_iv
        scan = await P.run_discovery_scan(notify=False)
        check("scan scores the confirmed catalyst on the CONFIRMED trial, not the text-matching guess", ("SMMT", "NCT05184712") in scored and ("SMMT", "NCT06396065") not in scored)
        check("scan still scores the automatic match on its trial and lists the unconfirmed ones for review", ("CYTK", "NCT05767346") in scored and sorted(e.ticker for e in scan.needs_review_pdufa) == ["BTAI", "NOMATCH"])
        check("an unconfirmed catalyst is still scored, on the baseline (no trial), never on the guess", ("BTAI", None) in scored and ("NOMATCH", None) in scored and all(n is None for t, n in scored if t in ("BTAI", "NOMATCH")))
        rows = {e.ticker: e for e in await db.get_catalysts_in_entry_window()}
        check("the stored catalyst now carries the confirmed trial", (rows["SMMT"].resolved_nct_id, rows["SMMT"].resolution_tier) == ("NCT05184712", "CONFIRMED"))
        preds = await db.get_predictions_for_ticker("SMMT")
        check("the saved prediction is for the confirmed trial", len(preds) == 1 and preds[0].nct_id == "NCT05184712")

        # ---- undo ----
        r = await admin.post("/review/remove", data={**FORM, "csrf_token": tok})
        check("remove confirmation", r.headers["location"] == "/review?msg=trial_removed" and not await db.get_trial_overrides())
        scored.clear()
        scan = await P.run_discovery_scan(notify=False)
        check("after removal the catalyst goes back to needing review and to the baseline score", ("SMMT", None) in scored and "SMMT" in [e.ticker for e in scan.needs_review_pdufa]
              and not any(t == "SMMT" and n for t, n in scored))

        # ---- once email is configured, an admin must have a confirmed address ----
        settings.RESEND_API_KEY, settings.EMAIL_FROM, settings.PUBLIC_BASE_URL = "k", "a@b.c", "https://x.example"
        check("with email on, an admin whose address isn't confirmed loses access", (await admin.get("/review")).status_code == 404)
        from aegis_bioquant.core import accounts as AC
        await AC.mark_email_verified((await AC.get_user_by_email("owner@test.invalid")).id)
        check("...and regains it once the address is confirmed", (await admin.get("/review")).status_code == 200)
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
