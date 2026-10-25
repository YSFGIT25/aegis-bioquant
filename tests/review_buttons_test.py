"""One-tap outcome confirmation from Telegram, the SEC acceptance time, and
the export into announcements.csv. A local stand-in for Telegram; no network."""

import asyncio, csv, gzip, io, json, os, pathlib, sys, tempfile, threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["SESSION_COOKIE_SECURE"] = "0"
from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db
from aegis_bioquant.core import outcome_review as R
from aegis_bioquant.telemetry import status

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "analysis"))
import export_announcements as X  # noqa: E402

sent = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sent.append((self.path.rsplit("/", 1)[-1], body))
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

URL = "https://www.sec.gov/Archives/edgar/data/1234567/000119312526123456/d1ex991.htm"
check("header file address built from a filing document",
      R.index_headers_url(URL) == "https://www.sec.gov/Archives/edgar/data/1234567/000119312526123456/0001193125-26-123456-index-headers.html")
check("non-EDGAR address gives no header file", R.index_headers_url("https://example.com/x") is None)
check("acceptance time: Eastern daylight time to UTC",
      R.parse_acceptance("<ACCEPTANCE-DATETIME>20261026160512\n") == datetime(2026, 10, 26, 20, 5, 12, tzinfo=timezone.utc))
check("acceptance time: Eastern standard time to UTC",
      R.parse_acceptance("<ACCEPTANCE-DATETIME>20261202080000") == datetime(2026, 12, 2, 13, 0, tzinfo=timezone.utc))

async def main():
    await db.reset_db()
    settings.TELEGRAM_API_BASE_URL = f"http://127.0.0.1:{srv.server_port}"
    settings.TELEGRAM_BOT_TOKEN, settings.STATUS_TELEGRAM_CHAT_ID = "1:TEST", "777"
    settings.TELEGRAM_WEBHOOK_SECRET = "hook-secret"

    await db.record_outcome_check("IONS", "PDUFA", date(2026, 10, 26), "APPROVED", date(2026, 10, 26), URL,
                                  "Ionis announced that the FDA approved olezarsen.")
    async def fake_time(client, url):
        return datetime(2026, 10, 26, 20, 5, 12, tzinfo=timezone.utc)
    R.sec_acceptance_time = fake_time
    settings.SEC_EDGAR_CONTACT_EMAIL = "test@example.test"
    check("acceptance time filled for the suggestion", await R.fill_acceptance_times() == 1)
    check("...and not looked up twice", await R.fill_acceptance_times() == 0)
    row = (await db.get_outcome_rows())[("IONS", "PDUFA", date(2026, 10, 26))]
    check("stored as an unchecked SEC time", row.announcement_source == R.SEC_SOURCE and row.announced_at is not None)

    pend = await R.pending_reviews()
    check("one message, showing the sentence, link and SEC time, with two buttons",
          len(pend) == 1 and "olezarsen" in pend[0].text and URL in pend[0].text and "20:05 UTC" in pend[0].text
          and [b[0]["callback_data"] for b in pend[0].buttons] == [f"ok:{row.id}:A", f"no:{row.id}:A"]
          and "no earlier press release" in pend[0].buttons[0][0]["text"])

    from fastapi.testclient import TestClient
    from aegis_bioquant.web.app import app
    with TestClient(app) as web:
        def press(data, user="777"):
            return web.post("/telegram/webhook", headers={"x-telegram-bot-api-secret-token": "hook-secret"},
                            json={"callback_query": {"id": "q1", "from": {"id": int(user)}, "data": data,
                                                     "message": {"message_id": 5, "chat": {"id": int(user)}}}})
        sent.clear()
        r = web.post("/telegram/webhook", headers={"x-telegram-bot-api-secret-token": "wrong"},
                     json={"callback_query": {"id": "q", "from": {"id": 777}, "data": f"ok:{row.id}:A"}})
        check("wrong webhook secret refused", r.status_code == 403)
        press(f"ok:{row.id}:A", user="999")
        still = (await db.get_outcome_rows())[("IONS", "PDUFA", date(2026, 10, 26))]
        check("someone else's tap is refused and changes nothing",
              still.status == "SUGGESTED" and sent[-1] == ("answerCallbackQuery", {"callback_query_id": "q1", "text": "Not allowed."}))
        press(f"ok:{row.id}:C")
        check("a button for a different suggestion is refused",
              (await db.get_outcome_rows())[("IONS", "PDUFA", date(2026, 10, 26))].status == "SUGGESTED")
        sent.clear()
        press(f"ok:{row.id}:A")
        done = (await db.get_outcome_rows())[("IONS", "PDUFA", date(2026, 10, 26))]
        check("operator's tap confirms the outcome and accepts the SEC time",
              done.status == "CONFIRMED" and done.outcome == "APPROVED" and done.decision_date == date(2026, 10, 26)
              and done.announcement_source == R.REVIEWER_SOURCE and done.confirmed_at is not None)
        methods = [m for m, _ in sent]
        check("tap answered, buttons removed, result sent",
              methods == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]
              and sent[-1][1]["text"].startswith("Confirmed: IONS"))
        press(f"ok:{row.id}:A")
        check("a second tap does nothing new", sent[-1][1]["text"].startswith("Already confirmed"))
        check("nothing left to review", await R.pending_reviews() == [])

        sent.clear()
        web.post("/telegram/webhook", headers={"x-telegram-bot-api-secret-token": "hook-secret"},
                 json={"message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "/announced IONS 2026-10-26 19:30 https://press.example/r"}})
        fixed = (await db.get_outcome_rows())[("IONS", "PDUFA", date(2026, 10, 26))]
        check("/announced records an earlier press-release time, in UTC",
              fixed.announced_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 26, 19, 30, tzinfo=timezone.utc)
              and fixed.announcement_url == "https://press.example/r" and "19:30 UTC" in sent[-1][1]["text"])
        sent.clear()
        web.post("/telegram/webhook", headers={"x-telegram-bot-api-secret-token": "hook-secret"},
                 json={"message": {"chat": {"id": 999}, "from": {"id": 999}, "text": "/announced IONS 2026-10-27 00:00"}})
        check("/announced from anyone else is ignored",
              not sent and (await db.get_outcome_rows())[("IONS", "PDUFA", date(2026, 10, 26))].announced_at.hour == 19)
        check("a malformed /announced gets the format",
              (await R.handle_announced("/announced IONS tomorrow")).startswith("Format:"))

    # A decision confirmed on the website: the SEC time still needs accepting.
    await db.record_outcome_check("PRAX", "PDUFA", date(2026, 11, 3), "CRL", date(2026, 11, 3), URL, "received a complete response letter")
    await db.confirm_outcome("PRAX", "PDUFA", date(2026, 11, 3), "CRL", date(2026, 11, 3))
    await R.fill_acceptance_times()
    pend = await R.pending_reviews()
    prax = (await db.get_outcome_rows())[("PRAX", "PDUFA", date(2026, 11, 3))]
    check("confirmed on the website: asked only to accept the time",
          len(pend) == 1 and "announcement time not checked" in pend[0].text and pend[0].buttons[0][0]["callback_data"] == f"at:{prax.id}:C")
    check("accepting it records the time", (await R.handle_button(f"at:{prax.id}:C")).startswith("Announcement time recorded"))

    # The daily report sends the summary, then one message per pending item.
    await db.record_outcome_check("COGT", "PDUFA", date(2026, 11, 20), "APPROVED", date(2026, 11, 19), URL, "the FDA approved bezuclastinib")
    sent.clear()
    await status.send_report(True, False, ["Scored: 3 upcoming catalyst(s)\n"], None)
    check("daily report, then the decision with its buttons",
          len(sent) == 2 and "Waiting for you" in sent[0][1]["text"] and "COGT" in sent[1][1]["text"]
          and sent[1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("ok:"))

    # Export: a snapshot's outcomes table -> announcements.csv
    table = db.Base.metadata.tables["catalyst_outcomes"]
    from sqlalchemy import select
    async with db.get_session_factory()() as s:
        rows = (await s.execute(select(table))).all()
    buf = io.StringIO(); w = csv.writer(buf); w.writerow([c.name for c in table.columns])
    for r in rows: w.writerow(["" if v is None else (v.isoformat() if hasattr(v, "isoformat") else v) for v in r])
    times = X.reviewer_times(buf.getvalue().encode())
    check("export: only reviewer-checked confirmed times",
          times == {("IONS", "2026-10-26"): ("2026-10-26T19:30:00Z", "https://press.example/r"),
                    ("PRAX", "2026-11-03"): ("2026-10-26T20:05:12Z", URL)})
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp, "announcements.csv")
        out.write_text("ticker,catalyst_date,announced_at_utc,source_url\nIONS,2026-10-26,2026-10-26T18:00:00Z,https://hand.example\n", encoding="utf-8")
        merged, added, conflicts = X.merge(out, times)
        check("export: hand-entered rows kept, new ones added, disagreements reported",
              added == 1 and len(merged) == 2 and merged[0]["announced_at_utc"] == "2026-10-26T18:00:00Z" and len(conflicts) == 1)
        gz = pathlib.Path(tmp, "catalyst_outcomes.csv.gz"); gz.write_bytes(gzip.compress(buf.getvalue().encode()))
        X.main([str(gz), "--out", str(out)])
        import run_analysis as ra
        loaded = ra.load_announcements(out)
        check("export: the pre-registered script reads the file it writes",
              loaded[("PRAX", date(2026, 11, 3))] == datetime(2026, 10, 26, 20, 5, 12, tzinfo=timezone.utc)
              and loaded[("IONS", date(2026, 10, 26))] == datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc))
    await db.dispose_db()

asyncio.run(main())
srv.shutdown()
