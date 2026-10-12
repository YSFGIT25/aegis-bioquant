import asyncio, json, sys, threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

from aegis_bioquant import pipeline as P
from aegis_bioquant.config import settings as _settings
_settings.REQUIRE_MARKET_HOURS = False   # these tests run at any hour
async def _no_snapshot(ticker, catalyst_date): raise RuntimeError('tests stay off the network')
P.fetch_market_snapshot = _no_snapshot
async def _no_quote(ticker): raise RuntimeError('tests stay off the network')
P.fetch_quote = _no_quote
from aegis_bioquant.config import settings
from aegis_bioquant.core import accounts as AC
from aegis_bioquant.core.database import dispose_db, init_db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan

received = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append(self.path); self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(404 if self.path.startswith("/fail") else 200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

async def fake_ingest(problems=None):
    if problems is not None and fake_ingest.fail:
        problems.append("ADCOM: source unavailable (TimeoutError)")
    return [CatalystEvent(event_type="PDUFA", ticker="FAKE", title="simulated", start_date=date(2026, 11, 14),
                          source_url="https://example.invalid/sim", raw_source="SEC_EDGAR_FTS",
                          horizon_status="PDUFA_ENTRY_WINDOW", resolved_nct_id="NCT00000000", resolution_tier="ACRONYM")]
fake_ingest.fail = False
async def fake_run(ticker, nct_id, catalyst_type, catalyst_date, total_cash=100_000.0, **kw):
    cand = Candidate(ticker=ticker, catalyst_type=catalyst_type, catalyst_date=catalyst_date, spot_price=7.42,
                     open_interest=4512, daily_options_volume=2002, p_fundamental=0.83, p_market=0.40, p_market_confidence="GOOD", event_implied_move=0.25, event_move_confidence="GOOD", iv_rank=20.0)
    evaluate_candidate(cand)
    fs = FundamentalScore(nct_id=nct_id, base_rate=0.83, p_fundamental=0.83,
                          adjustments=[ScoreAdjustment("primary_endpoint_significance", 0.05, "simulated")])
    return P.PipelineResult(candidate=cand, fundamental_score=fs, market_snapshot=None,
                            execution_plan=build_execution_plan(cand, total_cash=total_cash))
P.ingest_catalysts, P.run_candidate_pipeline = fake_ingest, fake_run
async def _no_iv(ticker, min_days): return None
P.fetch_iv_reference = _no_iv

settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, settings.TELEGRAM_API_BASE_URL = "1:SECRET", None, BASE
settings.DISCORD_WEBHOOK_URL, settings.DASHBOARD_WEBHOOK_URL = None, BASE + "/dash"

async def main():
    await init_db()
    a = await AC.create_user("alice@test.invalid", "correct horse battery")
    b = await AC.create_user("bob@test.invalid", "bob has a long password")
    await AC._set_destination(a.id, "telegram", "555001")
    await AC._set_destination(a.id, "discord", BASE + "/discord/alice-secret-path")
    await AC._set_destination(b.id, "discord", BASE + "/fail/bob-secret-path")
    scan = await P.run_discovery_scan()
    report = P._format_discovery_report(scan)
    print("\n".join(l for l in report.splitlines() if l.startswith(("Alerts", "Errors", "  FAKE |", "  ALERT", "-- Phase E", "-- Errors"))))
    check("report counts customer deliveries without listing them",
          "FAKE | LONG_RUN_UP_ALPHA | PARTIAL | dashboard=HTTP 200, customers: 2 of 3 delivered" in report)
    check("customer failures reported as a count", ("ALERT", "FAKE 1 customer delivery(ies) failed") in scan.errors)
    check("report carries no email, chat id, webhook path or token",
          not any(s in report for s in ("@test.invalid", "555001", "secret-path", "SECRET")))
    fake_ingest.fail = True
    scan2 = await P.run_discovery_scan()
    check("ingest problem surfaces as an INGEST error", ("INGEST", "ADCOM: source unavailable (TimeoutError)") in scan2.errors)
    await dispose_db()

asyncio.run(main())
print("ALL PASSED")
