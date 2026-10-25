"""The operator's scan report and the weekly OSF snapshot, against a local
stand-in for Telegram and OSF. No network."""

import asyncio, gzip, hashlib, json, sys, threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db
from aegis_bioquant.core import snapshots
from aegis_bioquant.telemetry import status
from aegis_bioquant import entrypoint

received = []
class H(BaseHTTPRequestHandler):
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""
    def do_POST(self):
        received.append(("POST", self.path, json.loads(self._body()), self.headers.get("Authorization")))
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
    def do_PUT(self):
        body = self._body()
        received.append(("PUT", self.path, body, self.headers.get("Authorization")))
        if "kind=folder" in self.path:
            reply = {"data": {"attributes": {"path": "/folder123/"}}}
        else:
            reply = {"data": {"attributes": {"path": "/file"}}}
        self.send_response(201); self.end_headers(); self.wfile.write(json.dumps(reply).encode())
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

REPORT = """=== Discovery Scan @ 2026-10-25T19:00:00+00:00 ===
Total catalysts scanned: 412
Scored: 23 upcoming catalyst(s)
Claude evidence reviews: 3 written, 1 with nothing to review, 2 failed
Errors: 2

-- Errors --
  LLM: 2 evidence review(s) failed, retried next scan. Last reason: API returned HTTP 400: credit balance is too low
  RULES: the scoring rules changed without being logged (now abcdef123456): see config/rules.py
""".splitlines(keepends=True)

s = status.summarize(REPORT)
check("report: counts read", s["scored"] == 23 and s["catalysts"] == 412 and s["errors"] == 2 and s["reviews"] == (3, 1, 2))
check("report: error lines kept, rules change noticed", len(s["error_lines"]) == 2 and s["rules_changed"])
text = status.format_report(True, False, s, ["IONS 2026-10-26: APPROVED (https://sec.example/f)"], None)
check("message: says OK, the counts, the warning and the pending decision",
      text.startswith("FDAOdds scan OK") and "Scored 23" in text and "2 failed" in text and "WARNING" in text
      and "IONS 2026-10-26: APPROVED" in text and "Waiting for you" in text)
check("message: failure after retry is said plainly",
      status.format_report(False, True, {}, [], None).startswith("FDAOdds scan FAILED (also on retry"))
closed = status.summarize(["US MARKET CLOSED: catalyst list refreshed only.\n", "Scored: 0 upcoming catalyst(s)\n"])
check("message: a weekend scan is described as normal", "Market closed" in status.format_report(True, False, closed, [], None))

async def main():
    await db.reset_db()
    settings.TELEGRAM_API_BASE_URL, settings.TELEGRAM_BOT_TOKEN = BASE, "1:TEST"
    settings.STATUS_TELEGRAM_CHAT_ID = None
    check("no chat id -> nothing sent", not await status.send("x") and not received)
    settings.STATUS_TELEGRAM_CHAT_ID = "42"
    check("sent to the operator chat only", await status.send("hello") and received[-1][2]["chat_id"] == "42")

    await db.record_outcome_check("IONS", "PDUFA", date(2026, 10, 26), "APPROVED", date(2026, 10, 26),
                                  "https://sec.example/f", "The FDA approved ...")
    waiting = await status.awaiting_confirmation()
    check("pending outcome listed", waiting == ["IONS 2026-10-26: APPROVED (https://sec.example/f)"])
    await db.confirm_outcome("IONS", "PDUFA", date(2026, 10, 26), "APPROVED", date(2026, 10, 26))
    check("a confirmed outcome is no longer listed", await status.awaiting_confirmation() == [])

    settings.OSF_TOKEN = None
    try:
        await snapshots.upload_snapshot()
        check("no token -> refused", False)
    except snapshots.SnapshotError:
        check("no token -> refused", True)

    received.clear()
    settings.OSF_TOKEN, settings.OSF_FILES_BASE_URL, settings.OSF_PROJECT_ID = "osf-test-token", BASE, "zfg9b"
    snap = await snapshots.upload_snapshot(datetime(2026, 10, 25, 19, 2, tzinfo=timezone.utc))
    puts = [r for r in received if r[0] == "PUT"]
    check("snapshot: one dated folder, then one file per table plus the manifest",
          snap.folder == "record-snapshot-2026-10-25T1902Z" and "kind=folder" in puts[0][1]
          and len(puts) == 1 + len(snapshots.TABLES) + 1)
    check("snapshot: files go into that folder, with the token as a header only",
          all("/osfstorage/folder123/?kind=file" in p[1] for p in puts[1:])
          and all(p[3] == "Bearer osf-test-token" for p in puts) and all("osf-test-token" not in p[1] for p in puts))
    names = [p[1].split("name=")[1] for p in puts[1:]]
    check("snapshot: no customer tables", not any(n.startswith(("users", "sessions", "alert_dest", "alert_deliv")) for n in names))
    outcomes = next(p[2] for p in puts if "name=catalyst_outcomes.csv.gz" in p[1])
    csv_text = gzip.decompress(outcomes)
    manifest = json.loads(next(p[2] for p in puts if "name=manifest.json" in p[1]))
    entry = next(f for f in manifest["files"] if f["name"] == "catalyst_outcomes.csv.gz")
    check("snapshot: the confirmed outcome is in it, and the manifest hash matches the CSV",
          b"IONS" in csv_text and b"CONFIRMED" in csv_text and entry["rows"] == 1
          and entry["sha256_csv"] == hashlib.sha256(csv_text).hexdigest())
    check("snapshot: manifest carries the rules stamp", manifest["rules_version"] == 1 and len(manifest["rules_fingerprint"]) == 12)
    await db.dispose_db()

asyncio.run(main())

# The scheduler's after-scan step: snapshot only on the set weekday, report always.
received.clear()
settings.SNAPSHOT_WEEKDAY = 6
entrypoint.after_scan(True, False, REPORT, now=datetime(2026, 10, 24, 19, 2, tzinfo=timezone.utc))   # a Saturday
check("after scan, not the snapshot day: report sent, no upload",
      all(r[0] == "POST" for r in received) and "Weekly record snapshot" not in received[0][2]["text"])
received.clear()
entrypoint.after_scan(True, False, REPORT, now=datetime(2026, 10, 25, 19, 2, tzinfo=timezone.utc))   # a Sunday
check("after scan, snapshot day: upload, then a report that mentions it",
      received[0][0] == "PUT" and "uploaded to OSF as record-snapshot-2026-10-25T1902Z" in next(r for r in received if r[0] == "POST")[2]["text"])
received.clear()
entrypoint.after_scan(False, True, [], now=datetime(2026, 10, 25, 19, 2, tzinfo=timezone.utc))
check("a failed scan: no snapshot, and the report says FAILED",
      all(r[0] == "POST" for r in received) and received[0][2]["text"].startswith("FDAOdds scan FAILED"))
srv.shutdown()
