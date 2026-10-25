"""Automatic outcome confirmation: the two-source rules, against a local
stand-in for openFDA. No network."""

import asyncio, json, sys, threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from aegis_bioquant.config import settings
from aegis_bioquant.core import auto_confirm as A
from aegis_bioquant.core import database as db
from aegis_bioquant.core import outcome_review as R

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def sub(kind, cls, when, status="AP"):
    return {"submission_type": kind, "submission_class_code": cls, "submission_status": status, "submission_status_date": when}

DRUGSFDA = [
    {"application_number": "NDA218614", "sponsor_name": "IONIS PHARMS INC",
     "products": [{"brand_name": "TRYNGOLZA", "active_ingredients": [{"name": "OLEZARSEN SODIUM"}]}],
     "submissions": [sub("SUPPL", "EFFICACY", "20261026"), sub("SUPPL", "LABELING", "20261027")]},
    {"application_number": "NDA299999", "sponsor_name": "PARTNER PHARMA AG",
     "products": [{"brand_name": "BEZUVO", "active_ingredients": [{"name": "BEZUCLASTINIB"}]}],
     "submissions": [sub("ORIG", "TYPE 1", "20261119")]},
    {"application_number": "NDA221276", "sponsor_name": "GLAXOSMITHKLINE LLC",
     "products": [{"brand_name": "HIBSAGO", "active_ingredients": [{"name": "BEPIROVIRSEN"}]}],
     "submissions": [sub("ORIG", "TYPE 1", "20261020")]},
    {"application_number": "ANDA200001", "sponsor_name": "GENERIC CO",
     "products": [{"brand_name": "NADOLOL", "active_ingredients": [{"name": "NADOLOL SODIUM"}]}],
     "submissions": [sub("ORIG", "TYPE 1", "20261102")]},
]
LETTERS = [{"letter_date": "11/03/2026", "company_name": "Praxis Precision Medicines, Inc.", "file_name": "CRL_NDA1_20261103.pdf"}]
state = {"updated": "2026-11-30", "fail": False}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if state["fail"]:
            self.send_response(500); self.end_headers(); return
        u = urlparse(self.path); q = parse_qs(u.query)
        skip, limit = int(q.get("skip", ["0"])[0]), int(q.get("limit", ["1000"])[0])
        rows = DRUGSFDA if u.path.endswith("drugsfda.json") else LETTERS
        page = rows[skip:skip + limit]
        body = {"meta": {"last_updated": state["updated"], "results": {"skip": skip, "limit": limit, "total": len(rows)}},
                "results": page}
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(body).encode())
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
settings.OPENFDA_BASE_URL = f"http://127.0.0.1:{srv.server_port}"

# --- names
check("company names: Inc. and Drugs@FDA's abbreviations are the same sponsor",
      A.same_company("Ionis Pharmaceuticals, Inc.", "IONIS PHARMS INC")
      and A.same_company("Praxis Precision Medicines, Inc.", "PRAXIS PRECISION MEDICINES INC"))
check("company names: different companies are not", not A.same_company("Ionis Pharmaceuticals, Inc.", "IPSEN BIOPHARM LTD")
      and not A.same_company("Bio Inc.", "Bio Pharma Ltd"))
check("drug words: salts and forms are not distinctive", A.drug_words("olezarsen sodium injection") == {"OLEZARSEN"})

# --- decide(), pure
recs = A.FdaRecords(A.parse_approvals(DRUGSFDA, date(2026, 10, 1), date(2026, 12, 31)), date(2026, 11, 30),
                    A.parse_letters(LETTERS))
check("only originals and efficacy supplements count as approvals",
      sorted((a.application, a.kind) for a in recs.approvals) ==
      [("ANDA200001", "ORIG"), ("NDA218614", "EFFICACY"), ("NDA221276", "ORIG"), ("NDA299999", "ORIG")])
v = A.decide("APPROVED", date(2026, 10, 26), "Ionis Pharmaceuticals, Inc.", "olezarsen", recs)
check("APPROVED + matching Drugs@FDA approval -> confirmed, FDA date kept", v.confirmed and v.source == A.FDA_APPROVAL
      and v.fda_date == date(2026, 10, 26) and "NDA218614" in v.evidence)
v = A.decide("APPROVED", date(2026, 11, 20), "Cogent Biosciences, Inc.", "bezuclastinib", recs)
check("a partner's application matches on the drug name", v.confirmed and "NDA299999" in v.evidence)
check("APPROVED with no matching approval -> waits",
      not A.decide("APPROVED", date(2026, 10, 26), "Someone Else Therapeutics", "zzzmab", recs).confirmed)
check("APPROVED more than 3 days from the FDA's date -> waits",
      not A.decide("APPROVED", date(2026, 10, 31), "Ionis Pharmaceuticals, Inc.", None, recs).confirmed)
v = A.decide("CRL", date(2026, 10, 26), "Ionis Pharmaceuticals, Inc.", None, recs)
check("CRL but the FDA lists an approval -> sources disagree, waits", not v.confirmed and "disagree" in v.evidence)
v = A.decide("CRL", date(2026, 11, 3), "Praxis Precision Medicines, Inc.", "EMERALD", recs)
check("CRL + the FDA's published letter -> confirmed", v.confirmed and v.source == A.FDA_CRL_LETTER and v.fda_date == date(2026, 11, 3))
v = A.decide("CRL", date(2026, 11, 10), "Quiet Bio, Inc.", "quietinib", recs)
check("CRL, no letter, no approval and Drugs@FDA updated 14+ days later -> confirmed", v.confirmed and v.source == A.NO_FDA_APPROVAL)
late = A.FdaRecords(recs.approvals, date(2026, 11, 20), recs.letters)
check("...but not while Drugs@FDA's data is less than 14 days past the decision",
      not A.decide("CRL", date(2026, 11, 10), "Quiet Bio, Inc.", "quietinib", late).confirmed)
check("CRL blocked by an approval of the same drug under another sponsor",
      not A.decide("CRL", date(2026, 11, 18), "Cogent Biosciences, Inc.", "bezuclastinib", recs).confirmed)
check("DELAYED is never confirmed automatically", not A.decide("DELAYED", date(2026, 10, 26), "Ionis", None, recs).confirmed)

# --- the announcement time
sec = datetime(2026, 10, 26, 20, 5, tzinfo=timezone.utc)
check("announcement: midnight US Eastern on the FDA's date when the filing came later",
      A.announcement_time(sec, date(2026, 10, 26)) == datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc))
check("announcement: the SEC time when it is earlier",
      A.announcement_time(datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc), date(2026, 10, 26))
      == datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc))
check("announcement: winter time is UTC-5",
      A.announcement_time(None, date(2026, 12, 2)) == datetime(2026, 12, 2, 5, 0, tzinfo=timezone.utc))


async def event(ticker, when, company, drug):
    factory = db.get_session_factory()
    async with factory() as s:
        now = datetime.now(timezone.utc)
        s.add(db.CatalystEventRecord(source_url=f"https://x/{ticker}", event_type="PDUFA", ticker=ticker, company_name=company,
                                     title="t", start_date=when, raw_source="EDGAR", drug_or_trial_candidate=drug,
                                     first_seen_at=now, last_seen_at=now))
        await s.commit()


async def main():
    await db.reset_db()
    async def no_sec_lookup():
        return 0
    R.fill_acceptance_times = no_sec_lookup     # run() calls it first; tested in review_buttons_test
    await event("IONS", date(2026, 10, 26), "Ionis Pharmaceuticals, Inc.", "olezarsen")
    await event("PRAX", date(2026, 11, 3), "Praxis Precision Medicines, Inc.", "EMERALD")
    await event("NOPE", date(2026, 11, 5), "Nope Therapeutics, Inc.", "nopeximab")
    # The live case: Ionis's partner GSK holds the application, and the
    # catalyst's own drug field names a trial endpoint, not the drug.
    await event("IONZ", date(2026, 10, 26), "Ionis Pharmaceuticals, Inc.", "GMFM-88")
    async with db.get_session_factory()() as s:
        s.add(db.LLMReviewRecord(ticker="IONZ", event_type="PDUFA", catalyst_date=date(2026, 10, 26),
                                 reviewed_at=datetime.now(timezone.utc), model="m", status="REVIEWED", drug="Bepirovirsen",
                                 positives=[], risks=[], sources=[]))
        await s.commit()
    await db.record_outcome_check("IONZ", "PDUFA", date(2026, 10, 26), "APPROVED", date(2026, 10, 21), "https://sec/ionz", "GSK approved")
    await db.record_outcome_check("IONS", "PDUFA", date(2026, 10, 26), "APPROVED", date(2026, 10, 27), "https://sec/ions", "approved")
    await db.record_outcome_check("PRAX", "PDUFA", date(2026, 11, 3), "CRL", date(2026, 11, 3), "https://sec/prax", "CRL")
    await db.record_outcome_check("NOPE", "PDUFA", date(2026, 11, 5), "APPROVED", date(2026, 11, 5), "https://sec/nope", "approved")
    await db.record_outcome_check("DLAY", "PDUFA", date(2026, 11, 6), "DELAYED", date(2026, 11, 1), "https://sec/dlay", "extended")
    async with db.get_session_factory()() as s:   # an SEC time looked up earlier, after the FDA's midnight
        row = await s.scalar(db.select(db.CatalystOutcomeRecord).where(db.CatalystOutcomeRecord.ticker == "IONS"))
        row.announced_at, row.announcement_source = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc), R.SEC_SOURCE
        await s.commit()

    state["fail"] = True
    res = await A.run(date(2026, 11, 30))
    check("FDA unreachable: nothing confirmed, said plainly", not res.confirmed and "not reachable" in res.error and len(res.waiting) == 5)
    state["fail"] = False
    res = await A.run(date(2026, 11, 30))
    rows = await db.get_outcome_rows()
    ions, prax = rows[("IONS", "PDUFA", date(2026, 10, 26))], rows[("PRAX", "PDUFA", date(2026, 11, 3))]
    check("run: the approval and the CRL are confirmed, the others wait",
          len(res.confirmed) == 3 and ions.status == "CONFIRMED" and prax.status == "CONFIRMED"
          and rows[("NOPE", "PDUFA", date(2026, 11, 5))].status == "SUGGESTED"
          and rows[("DLAY", "PDUFA", date(2026, 11, 6))].status == "SUGGESTED" and len(res.waiting) == 2)
    check("run: confirmed by no person, with the FDA evidence stored",
          ions.confirmed_by is None and ions.confirmation_source == A.FDA_APPROVAL and "NDA218614" in ions.fda_evidence)
    check("run: decision date is the FDA's action date; announcement is the earlier bound, source AUTO",
          ions.decision_date == date(2026, 10, 26) and ions.announcement_source == A.AUTO_SOURCE
          and ions.announced_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc))
    ionz = rows[("IONZ", "PDUFA", date(2026, 10, 26))]
    check("run: a partner's approval matches on the evidence review's drug name; FDA date and its midnight used",
          ionz.status == "CONFIRMED" and "NDA221276" in ionz.fda_evidence and ionz.decision_date == date(2026, 10, 20)
          and ionz.announced_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc))
    check("run: a second run changes nothing", (await A.run(date(2026, 11, 30))).confirmed == [])
    await db.record_outcome_check("IONS", "PDUFA", date(2026, 10, 26), "CRL", date(2026, 10, 26), "https://sec/x", "x")
    check("the finder never overwrites an automatic confirmation",
          (await db.get_outcome_rows())[("IONS", "PDUFA", date(2026, 10, 26))].outcome == "APPROVED")

    settings.OUTCOMES_AUTOMATIC = True
    check("manual Telegram confirmation refuses while automatic",
          (await R.handle_button("ok:1:A")) == R.AUTOMATIC_REPLY and (await R.handle_announced("/announced IONS 2026-10-26 10:00")) == R.AUTOMATIC_REPLY)

    from aegis_bioquant.telemetry import status
    text = status.format_report(True, False, {}, res.waiting, None, res.confirmed, automatic=True)
    check("report: lists what was recorded and what waits, asks nothing",
          "recorded automatically" in text and "nothing for you to do" in text and "Waiting for you" not in text)
    await db.dispose_db()

asyncio.run(main())
srv.shutdown()
