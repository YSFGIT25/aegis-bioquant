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
from aegis_bioquant.core.database import dispose_db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan

received = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(500 if self.path.startswith("/fail") else 200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

# Simulated inputs: one in-window, high-confidence PDUFA event whose pipeline run yields an actionable signal.
async def fake_ingest(problems=None):
    return [CatalystEvent(event_type="PDUFA", ticker="FAKE", title="simulated", start_date=date(2026, 11, 14),
                          source_url="https://example.invalid/sim", raw_source="SEC_EDGAR_FTS",
                          horizon_status="PDUFA_ENTRY_WINDOW", resolved_nct_id="NCT00000000", resolution_tier="ACRONYM")]
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

settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, settings.TELEGRAM_API_BASE_URL = "1:SECRET", "-1", BASE
settings.DISCORD_WEBHOOK_URL, settings.DASHBOARD_WEBHOOK_URL = BASE + "/fail/discord", BASE + "/dash"

async def main():
    scan = await P.run_discovery_scan()
    report = P._format_discovery_report(scan)
    print(report)
    check("scan 1: alert PARTIAL (discord stand-in returns 500)", [a.status for a in scan.alerts] == ["PARTIAL"])
    check("scan 1: failed channel surfaced in errors", scan.errors == [("ALERT", "FAKE discord: HTTP 500")])
    check("scan 1: three requests made", len(received) == 3)
    check("report leaks no token", "SECRET" not in report)
    scan2 = await P.run_discovery_scan()
    check("scan 2: suppressed by cooldown, nothing sent", [a.status for a in scan2.alerts] == ["SUPPRESSED_COOLDOWN"] and len(received) == 3 and not scan2.errors)
    scan3 = await P.run_discovery_scan(notify=False)
    check("notify=False: no alert attempted", scan3.alerts == [] and len(received) == 3)
    await dispose_db()

asyncio.run(main())
print("ALL PASSED")
