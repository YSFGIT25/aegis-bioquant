import asyncio, sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from aegis_bioquant import pipeline as P
from aegis_bioquant.config import settings as _settings
_settings.REQUIRE_MARKET_HOURS = False   # these tests run at any hour
async def _no_snapshot(ticker, catalyst_date): raise RuntimeError('tests stay off the network')
P.fetch_market_snapshot = _no_snapshot
from aegis_bioquant.config import constants as c, settings
from aegis_bioquant.core import database as db
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.logic import phase_a as A
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.logic.risk_manager import build_execution_plan
from aegis_bioquant.telemetry import alerts as AL, dashboard as D

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

TODAY = date.today()
WHEN = TODAY + timedelta(days=40)
real_sleep = asyncio.sleep
async def no_sleep(_): await real_sleep(0)
P.asyncio.sleep = no_sleep

def ev(ticker, kind="PDUFA", when=WHEN, status="PDUFA_ENTRY_WINDOW"):
    return CatalystEvent(event_type=kind, ticker=ticker, title="x", start_date=when, company_name=f"{ticker} Co",
                         source_url=f"https://example.invalid/{ticker}{kind}{when}", raw_source="SEC_EDGAR_FTS" if kind != "PHASE_3_READOUT" else "CTGOV_PHASE3",
                         horizon_status=status if kind != "PHASE_3_READOUT" else c.PHASE_3_STATUS_TAG,
                         resolved_nct_id="NCT00000001", resolution_tier="ACRONYM")

CAPS = {"BIGP": 348e9, "MIDB": 8.5e9, "EDGE": 20e9, "OVER": 20.1e9, "P3BIG": 216e9, "P3SML": 1.4e9, "NOCAP": None}
asked = []
async def fake_quote(ticker):
    asked.append(ticker)
    if ticker == "DOWN": raise RuntimeError("data source down")
    return SimpleNamespace(market_cap=CAPS[ticker], last_price=30.0)
P.fetch_quote = fake_quote

async def main():
    check("the ceiling is $20 billion and there is no price ceiling", c.MARKET_CAP_MAX == 20e9 and c.SPOT_PRICE_MAX is None and c.SPOT_PRICE_MIN == 2.0)
    check("price: $2 floor only", not A.price_in_range(1.99) and A.price_in_range(2.0) and A.price_in_range(950.0))
    check("company value formatting", (A.format_market_cap(8.5e9), A.format_market_cap(348e9), A.format_market_cap(120e6), A.format_market_cap(None)) == ("$8.5B", "$348.0B", "$120M", "unknown"))

    events = [ev("BIGP"), ev("MIDB"), ev("EDGE"), ev("OVER"), ev("NOCAP"), ev("DOWN"), ev(None),
              ev("P3BIG", kind="PHASE_3_READOUT"), ev("P3SML", kind="PHASE_3_READOUT"),
              ev("OLDCO", when=TODAY - timedelta(days=90), status="OUTSIDE_WINDOW"),
              ev("BIGP", when=TODAY - timedelta(days=90), status="OUTSIDE_WINDOW")]
    kept, too_large = await P.drop_oversized_companies(events, TODAY)
    check("each company with a current or upcoming catalyst is looked up once; one with only past events is not", sorted(asked) == ["BIGP", "DOWN", "EDGE", "MIDB", "NOCAP", "OVER", "P3BIG", "P3SML"])
    check("companies over $20B are dropped, with their value recorded", too_large == {"BIGP": 348e9, "OVER": 20.1e9, "P3BIG": 216e9})
    check("exactly $20B, unknown value, lookup failure and no-ticker events are all kept", sorted(str(e.ticker) for e in kept) == ["DOWN", "EDGE", "MIDB", "NOCAP", "None", "OLDCO", "P3SML"])
    check("a dropped company's past events go with it", not any(e.ticker == "BIGP" for e in kept))

    # ---- through a whole scan ----
    await db.reset_db()
    settings.ENFORCE_PHASE_A_FILTERS = True
    settings.TELEGRAM_BOT_TOKEN = settings.TELEGRAM_CHAT_ID = settings.DISCORD_WEBHOOK_URL = settings.DASHBOARD_WEBHOOK_URL = None
    async def fake_ingest(problems=None): return [e.model_copy() for e in events]
    scored = []
    async def fake_run(ticker, nct_id, catalyst_type, catalyst_date, total_cash=100_000.0, **kw):
        scored.append(ticker)
        cand = Candidate(ticker=ticker, catalyst_type=catalyst_type, catalyst_date=catalyst_date, spot_price=61.0, open_interest=13000,
                         daily_options_volume=870, p_fundamental=0.83, p_market=0.44, p_market_confidence="GOOD", market_cap=CAPS.get(ticker), event_implied_move=0.25, event_move_confidence="GOOD", iv_rank=20.0)
        evaluate_candidate(cand)
        fs = FundamentalScore(nct_id=nct_id, base_rate=0.83, p_fundamental=0.83, adjustments=[ScoreAdjustment("x", 0.0, "t")])
        return P.PipelineResult(candidate=cand, fundamental_score=fs, market_snapshot=None, execution_plan=build_execution_plan(cand, total_cash=total_cash))
    async def no_iv(ticker, min_days): return None
    P.ingest_catalysts, P.run_candidate_pipeline, P.fetch_iv_reference = fake_ingest, fake_run, no_iv
    scan = await P.run_discovery_scan()
    report = P._format_discovery_report(scan)
    check("scan: oversized companies are never scored", sorted(scored) == ["DOWN", "EDGE", "MIDB", "NOCAP"])
    check("scan report names what was dropped, largest first", "Companies dropped as too large (over $20.0B): 3 - BIGP, P3BIG, OVER" in report)
    check("scan report shows each scored company's value", "Company value: $8.5B" in report and "Company value: unknown" in report)
    st = await D.build_state()
    check("dashboard: the large company is in neither the entry-window list nor the watch list",
          "BIGP" not in [e["ticker"] for e in st["catalysts_in_window"]] and [x["ticker"] for x in st["phase3_watch"]] == ["P3SML"])
    mid = next(a for a in st["alerts"] if a["payload"]["ticker"] == "MIDB")["payload"]
    check("alert carries the company value and that it passed", mid["market"]["market_cap"] == 8.5e9 and mid["market"]["liquidity_checks"]["market_cap_ok"] is True)
    text = AL.format_alert_text(AL.AlertPayload.model_validate(mid))
    check("alert text states it", "Stock $61.00, company value $8.5B" in text)
    old = dict(mid); old["market"] = {k: v for k, v in mid["market"].items() if k != "market_cap"}
    old["market"]["liquidity_checks"] = {k: v for k, v in mid["market"]["liquidity_checks"].items() if k != "market_cap_ok"}
    check("an alert stored before this change still reads back", AL.AlertPayload.model_validate(old).market.market_cap is None)

    settings.ENFORCE_PHASE_A_FILTERS = False
    asked.clear(); scored.clear()
    scan = await P.run_discovery_scan(notify=False)
    check("with the filters switched off, nothing is looked up or dropped", asked == [] and "BIGP" in scored and scan.too_large == {})
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
