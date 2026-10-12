import asyncio, sys
from datetime import date, datetime, timedelta, timezone

from aegis_bioquant import pipeline as P
from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.ingestion.market_data_feed import us_market_open
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan
from aegis_bioquant.telemetry import dashboard as D

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

u = lambda *a: datetime(*a, tzinfo=timezone.utc)
# 2026-10-06 is a Tuesday (US daylight time, UTC-4); 2026-12-08 is a Tuesday (standard time, UTC-5)
check("summer: open 13:30-20:00 UTC", not us_market_open(u(2026,10,6,13,29)) and us_market_open(u(2026,10,6,13,30)) and us_market_open(u(2026,10,6,19,59)) and not us_market_open(u(2026,10,6,20,0)))
check("winter: open 14:30-21:00 UTC", not us_market_open(u(2026,12,8,14,29)) and us_market_open(u(2026,12,8,14,30)) and us_market_open(u(2026,12,8,20,59)) and not us_market_open(u(2026,12,8,21,0)))
check("the 19:00 UTC scan is inside the session in both seasons", us_market_open(u(2026,10,6,19,0)) and us_market_open(u(2026,12,8,19,0)))
check("closed at night and at weekends", not us_market_open(u(2026,10,7,4,24)) and not us_market_open(u(2026,10,10,17,0)) and not us_market_open(u(2026,10,11,17,0)))

TODAY = date.today()
WHEN = TODAY + timedelta(days=40)
ev = lambda t: CatalystEvent(event_type="PDUFA", ticker=t, title="x", start_date=WHEN, source_url=f"https://example.invalid/{t}",
                             raw_source="SEC_EDGAR_FTS", horizon_status="PDUFA_ENTRY_WINDOW", resolved_nct_id="NCT00000001", resolution_tier="ACRONYM")
calls = {"scored": [], "iv": [], "snap": [], "quote": []}
async def fake_ingest(problems=None): return [ev("AAA"), ev("BBB")]
async def fake_run(ticker, nct_id, catalyst_type, catalyst_date, total_cash=100_000.0, **kw):
    calls["scored"].append(ticker)
    cand = Candidate(ticker=ticker, catalyst_type=catalyst_type, catalyst_date=catalyst_date, spot_price=30.0, open_interest=5000,
                     daily_options_volume=900, p_fundamental=0.83, p_market=0.44)
    evaluate_candidate(cand)
    fs = FundamentalScore(nct_id=nct_id, base_rate=0.83, p_fundamental=0.83, adjustments=[ScoreAdjustment("x", 0.0, "t")])
    return P.PipelineResult(candidate=cand, fundamental_score=fs, market_snapshot=None, execution_plan=build_execution_plan(cand, total_cash=total_cash))
async def fake_iv(ticker, min_days): calls["iv"].append(ticker); return None
async def fake_snap(ticker, when): calls["snap"].append(ticker); raise RuntimeError("no data in tests")
async def fake_quote(ticker): calls["quote"].append(ticker); raise RuntimeError("no data in tests")
P.ingest_catalysts, P.run_candidate_pipeline, P.fetch_iv_reference, P.fetch_market_snapshot, P.fetch_quote = fake_ingest, fake_run, fake_iv, fake_snap, fake_quote
real_sleep = asyncio.sleep
async def no_sleep(_): await real_sleep(0)
P.asyncio.sleep = no_sleep

async def main():
    await db.reset_db()
    settings.TELEGRAM_BOT_TOKEN = settings.TELEGRAM_CHAT_ID = settings.DISCORD_WEBHOOK_URL = settings.DASHBOARD_WEBHOOK_URL = None
    settings.REQUIRE_MARKET_HOURS = True

    P.us_market_open = lambda now=None: False
    scan = await P.run_discovery_scan()
    report = P._format_discovery_report(scan)
    check("market closed: nothing is scored and no option reading is attempted", calls["scored"] == [] and calls["iv"] == [] and calls["snap"] == [])
    check("market closed: the catalyst list is still refreshed", scan.total_catalysts_found == 2 and sorted(e.ticker for e in await db.get_catalysts_in_entry_window()) == ["AAA", "BBB"])
    check("market closed: the scan says so and counts what is waiting", scan.market_closed and scan.deferred == 2 and "US MARKET CLOSED" in report and "2 candidate(s) waiting" in report)
    check("market closed: waiting candidates are not mislabelled as needing review or screened out", scan.needs_review_pdufa == [] and scan.evicted == [] and scan.errors == [])
    st = await D.build_state()
    check("market closed: nothing new appears as a score or a screen-out", st["predictions"] == [] and st["screened"] == [])

    P.us_market_open = lambda now=None: True
    scan = await P.run_discovery_scan()
    check("market open: candidates are scored and readings attempted", sorted(calls["scored"]) == ["AAA", "BBB"] and calls["iv"] and calls["snap"] and not scan.market_closed and scan.deferred == 0)
    check("market open: the report carries no closed-market banner", "US MARKET CLOSED" not in P._format_discovery_report(scan))

    for k in calls: calls[k].clear()
    settings.REQUIRE_MARKET_HOURS = False
    P.us_market_open = lambda now=None: False
    scan = await P.run_discovery_scan()
    check("with the check switched off, a scan scores at any hour", sorted(calls["scored"]) == ["AAA", "BBB"] and not scan.market_closed)
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
