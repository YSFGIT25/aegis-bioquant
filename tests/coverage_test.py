import asyncio, sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from aegis_bioquant.config import constants as c
from aegis_bioquant.core import database as db
from aegis_bioquant.ingestion import fda_scraper as F, phase3_readouts as P3
from aegis_bioquant.ingestion.company_tickers import TickerIndex
from aegis_bioquant.ingestion.market_data_feed import MarketSnapshot, OptionChainSnapshot, OptionContract, Quote, front_month_liquidity
from aegis_bioquant.telemetry import dashboard as D

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

TODAY = date.today()
FILED = TODAY - timedelta(days=20)
real_sleep = asyncio.sleep
async def no_sleep(_): await real_sleep(0)
F.asyncio.sleep = no_sleep

def fmt(d): return d.strftime("%B %d, %Y").replace(" 0", " ")
D1, D2, PAST = TODAY + timedelta(days=40), TODAY + timedelta(days=120), TODAY - timedelta(days=30)

FILINGS = {   # cik -> (form, company display name, text)
    "0000000001": ("8-K", "Alpha Bio, Inc.  (ALPH)  (CIK 0000000001)",
                   f"Alpha announced the NDA for ALPHA-ONE was accepted with a PDUFA date of {fmt(D1)}. " + "Filler. " * 80 +
                   f"Separately the sNDA for zetamab has a PDUFA target action date of {fmt(D2)}."),
    "0000000002": ("8-K", "Beta Therapeutics Corp  (BETA)  (CIK 0000000002)",
                   f"Our BRAVO study met its endpoint. " + "Unrelated pipeline text. " * 60 + f"FDA set a PDUFA date of {fmt(D1)}."),
    "0000000003": ("6-K", "Gamma Pharma Ltd  (CIK 0000000003)", f"The PDUFA goal date is {fmt(D1)} for our application."),
    "0000000004": ("8-K", "Delta Corp  (DLTA)  (CIK 0000000004)", f"Approved ahead of its PDUFA date. Earlier PDUFA date of {fmt(PAST)}."),
    "0000000005": ("8-K", "Echo Inc  (ECHO)  (CIK 0000000005)", "Potential PDUFA date with priority review."),
    "0000000006": ("8-K", "Foxtrot Inc  (FOXT)  (CIK 0000000006)", f"The NDA for adults with disease has a PDUFA date of {fmt(D1)}; HARMONY trial results support it."),
}
ORDER = ["0000000001", "0000000001", "0000000002", "0000000003", "0000000004", "0000000005", "0000000006"]  # first filing listed twice (newer + older)

async def fake_search(client, lookback_days, problems=None):
    return [{"_id": f"0000-00-{i:06d}:doc{i}.htm", "_source": {"ciks": [cik], "display_names": [FILINGS[cik][1]],
             "file_date": FILED.isoformat(), "form": FILINGS[cik][0]}} for i, cik in enumerate(ORDER)]
async def fake_text(client, url):
    cik = url.split("/data/")[1].split("/")[0]
    return FILINGS[f"{int(cik):010d}"][2]
searched = []
async def fake_ct(query, sponsor=None, page_size=10):
    searched.append(query)
    S = lambda nct, acr, sp: SimpleNamespace(nct_id=nct, acronym=acr, sponsor=sp, brief_title="t", overall_status="x", phases=[])
    return {"ALPHA-ONE": [S("NCT00000011", "ALPHA-ONE", "Alpha Bio")],
            "zetamab": [S("NCT00000012", None, "Alpha Bio, Inc.")],
            "BRAVO": [S("NCT00000021", "BRAVO-2", "Beta Therapeutics"), S("NCT00000022", "BRAVO-3", "Beta Therapeutics")],
            "HARMONY": [S("NCT00000061", "HARMONY", "Foxtrot")],
            "aficamten": [S("NCT00000071", "MAPLE", "Cytokinetics")]}.get(query, [])
F._search_pdufa_filings, F._fetch_filing_text, F.search_studies = fake_search, fake_text, fake_ct
F._build_user_agent = lambda: "test-agent"

async def main():
    events = await F.fetch_pdufa_catalysts()
    by = {(e.cik[-1], e.start_date): e for e in events}
    check("one event per company per date; the duplicate filing adds nothing", len(events) == 6 and len(by) == 6)
    a1, a2 = by[("1", D1)], by[("1", D2)]
    check("two drugs in one filing -> two events, each tied to its own drug",
          (a1.drug_or_trial_candidate, a1.resolved_nct_id, a1.resolution_tier) == ("ALPHA-ONE", "NCT00000011", "ACRONYM")
          and (a2.drug_or_trial_candidate, a2.resolved_nct_id, a2.resolution_tier) == ("zetamab", "NCT00000012", "NDA_FOR"))
    check("second date in a filing gets its own database key; the first keeps the plain URL", "#" not in a1.source_url and a2.source_url.endswith(f"#pdufa-{D2.isoformat()}") and a1.source_url != a2.source_url)
    b = by[("2", D1)]
    check("single-date filing: drug found elsewhere in the document", b.drug_or_trial_candidate == "BRAVO")
    check("trial name with no exactly matching registered acronym -> best guess, marked unconfirmed", (b.resolved_nct_id, b.resolution_tier) == ("NCT00000021", "NAME_UNCONFIRMED"))
    f6 = by[("6", D1)]
    check("filler word after 'NDA for' is skipped; the real trial name is used and confirmed", (f6.drug_or_trial_candidate, f6.resolution_tier) == ("HARMONY", "STUDY_TRIAL_NAME") and "adults" not in searched)
    g = by[("3", D1)]
    check("6-K filings are read; title names the form", g.title.startswith("PDUFA date disclosed in 6-K") and g.ticker is None)
    d = by[("4", PAST)]
    check("past PDUFA date kept as a record but no trial lookup spent on it", d.resolved_nct_id is None and d.start_date == PAST)
    check("'potential PDUFA date' with no date -> no event", not any(e.cik.endswith("5") for e in events))

    # ---- ticker index + AdCom enrichment + Phase 3 through ingest_catalysts ----
    tickers = TickerIndex([{"ticker": "GAMA", "title": "GAMMA PHARMA LTD"}, {"ticker": "CYTK", "title": "CYTOKINETICS INC"},
                           {"ticker": "BIGP", "title": "Big Pharma Inc"}])
    check("strict name matching", tickers.lookup("Gamma Pharma Ltd") == "GAMA" and tickers.lookup("Gamma") is None and tickers.lookup("Sydnexis") is None)

    meeting = TODAY + timedelta(days=60)
    def adcom(n, d): return F.CatalystEvent(event_type="ADCOM", title=f"Advisory Committee Meeting {n}", start_date=d,
                                           source_url=f"https://www.fda.gov/meeting-{n}", raw_source="FDA_ADCOM_CALENDAR", center="CDER")
    async def fake_calendar(): return [adcom(1, meeting), adcom(2, meeting), adcom(3, meeting), adcom(4, TODAY - timedelta(days=5))]
    pages = {"1": "<p>The committee will discuss supplemental new drug application (sNDA) 215152/S-012 for aficamten tablets, submitted by Cytokinetics, Incorporated, for the treatment of adults.</p>",
             "2": "<p>The Committee will discuss new drug application (NDA) 219694, for atropine sulfate ophthalmic solution 0.01%, submitted by Sydnexis. Inc. The proposed indication is myopia.</p>",
             "3": "<p>The committee will discuss the selection of strains for the influenza vaccine.</p>"}
    fetched = []
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k):
            fetched.append(url)
            return SimpleNamespace(text=pages.get(url[-1], ""), raise_for_status=lambda: None, json=lambda: PAYLOAD)
    real_client = F.httpx.AsyncClient
    F.fetch_adcom_calendar = fake_calendar
    async def fake_index(ua): return tickers
    F.fetch_ticker_index = fake_index

    soon = (TODAY + timedelta(days=30)).isoformat()
    def study(nct, sponsor, status, when, acr=None):
        return {"protocolSection": {"identificationModule": {"nctId": nct, "briefTitle": f"Study {nct}", "acronym": acr},
                "statusModule": {"overallStatus": status, "primaryCompletionDateStruct": {"date": when}},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor}}}}
    PAYLOAD = {"studies": [study("NCT1", "Big Pharma Inc.", "ACTIVE_NOT_RECRUITING", soon, "BIG-1"), study("NCT2", "Big Pharma Inc.", "WITHDRAWN", soon),
                           study("NCT3", "Private Biotech GmbH", "RECRUITING", soon), study("NCT4", "Big Pharma", "RECRUITING", soon[:7]),
                           study("NCT1", "Big Pharma Inc.", "ACTIVE_NOT_RECRUITING", soon), study("NCT5", "Big Pharma Inc.", "NOT_YET_RECRUITING", soon)]}
    F.httpx.AsyncClient = FakeClient
    P3.httpx.AsyncClient = FakeClient
    problems = []
    all_events = await F.ingest_catalysts(problems=problems)
    F.httpx.AsyncClient = real_client; P3.httpx.AsyncClient = real_client

    ad = {e.title[-1]: e for e in all_events if e.event_type == "ADCOM"}
    check("upcoming AdCom pages are read; a past meeting's page is not", sorted(u[-1] for u in fetched if "fda.gov" in u) == ["1", "2", "3"])
    check("AdCom with a listed sponsor: company, ticker, drug and a trial filled in, marked for review",
          (ad["1"].ticker, ad["1"].company_name, ad["1"].drug_or_trial_candidate, ad["1"].resolved_nct_id, ad["1"].resolution_tier) == ("CYTK", "Cytokinetics, Incorporated", "aficamten", "NCT00000071", "ADCOM_AGENDA"))
    check("AdCom with a private sponsor: company and drug recorded, no ticker", (ad["2"].ticker, ad["2"].company_name, ad["2"].drug_or_trial_candidate) == (None, "Sydnexis", "atropine sulfate"))
    check("AdCom with no application on the agenda: left as it was", ad["3"].company_name is None and ad["3"].ticker is None)
    check("AdCom agenda tier is not one the pipeline scores unattended", "ADCOM_AGENDA" not in ("STUDY_TRIAL_NAME", "ACRONYM"))
    check("a PDUFA filer with no ticker in its filing header gets one from the SEC list", next(e for e in all_events if e.event_type == "PDUFA" and e.cik.endswith("3")).ticker == "GAMA")

    p3 = [e for e in all_events if e.event_type == "PHASE_3_READOUT"]
    check("Phase 3 list: listed sponsor + active trial only; withdrawn, not-yet-recruiting, private and duplicate rows dropped", sorted(e.resolved_nct_id for e in p3) == ["NCT1", "NCT4"])
    check("Phase 3 rows are tagged wait-for-flow-signal and carry the ticker", all(e.horizon_status == c.PHASE_3_STATUS_TAG and e.ticker == "BIGP" for e in p3))
    check("month-only completion date is accepted", next(e for e in p3 if e.resolved_nct_id == "NCT4").start_date.day == 1)
    check("no problems reported on a clean run", problems == [])

    # ---- stored and shown ----
    await db.reset_db()
    await db.upsert_catalyst_events(all_events)
    st = await D.build_state()
    check("dashboard data carries the Phase 3 watch list", sorted(x["nct_id"] for x in st["phase3_watch"]) == ["NCT1", "NCT4"] and st["phase3_watch"][0]["ticker"] == "BIGP")
    check("Phase 3 rows never appear as in-window candidates", all(e["event_type"] != "PHASE_3_READOUT" for e in st["catalysts_in_window"]))
    ad1 = next(e for e in st["catalysts_in_window"] if e["event_type"] == "ADCOM" and e["ticker"] == "CYTK")
    check("the enriched AdCom shows in the entry window with its ticker and drug", ad1["drug_or_trial_candidate"] == "aficamten")
    await db.upsert_catalyst_events([adcom(2, meeting).model_copy(update={"ticker": "LATE", "company_name": "Late Co", "horizon_status": "ADCOM_ENTRY_WINDOW"})])
    check("a ticker learned on a later scan updates the existing row", any(e["ticker"] == "LATE" for e in (await D.build_state())["catalysts_in_window"]))
    await db.dispose_db()

    # ---- liquidity reading uses the heavily held expiry, not a thin weekly ----
    now = datetime.now(timezone.utc)
    def chain(exp, oi, vol): return OptionChainSnapshot(ticker="T", expiration=exp, fetched_at=now, calls=[
        OptionContract(contract_symbol="c", strike=10, option_type="CALL", open_interest=oi, volume=vol)])
    snap = MarketSnapshot(ticker="T", catalyst_date=D1, quote=Quote(ticker="T", last_price=10, fetched_at=now), fetched_at=now,
                          expiry_before_catalyst=chain(D1 - timedelta(days=1), 3, 1), expiry_after_catalyst=chain(D1 + timedelta(days=6), 9000, 2500))
    liq = front_month_liquidity(snap)
    check("liquidity is counted across both expirations around the catalyst, so a thin weekly can't hide a liquid monthly", (liq.total_open_interest, liq.total_volume) == (9003, 2501) and liq.expiration == D1 + timedelta(days=6))

asyncio.run(main())
print("ALL PASSED")
