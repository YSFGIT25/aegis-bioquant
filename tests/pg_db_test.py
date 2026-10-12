import asyncio, sys
from datetime import date, datetime, timezone
from types import SimpleNamespace
from sqlalchemy import text
from aegis_bioquant.core import database as db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def result(ticker, pf, pm):
    cand = Candidate(ticker=ticker, catalyst_type="PDUFA", catalyst_date=date(2026, 11, 14), spot_price=7.42,
                     open_interest=4512, daily_options_volume=2002, p_fundamental=pf, p_market=pm, p_market_confidence="GOOD")
    evaluate_candidate(cand)
    fs = FundamentalScore(nct_id="NCT05767346", base_rate=0.83, p_fundamental=pf,
                          adjustments=[ScoreAdjustment("primary_endpoint_significance", 0.05, "test")])
    return SimpleNamespace(candidate=cand, fundamental_score=fs, market_snapshot=None,
                           execution_plan=build_execution_plan(cand, total_cash=100_000.0))

def event(n, status):
    return CatalystEvent(event_type="PDUFA", ticker=f"T{n}", title=f"event {n}", start_date=date(2026, 11, 14),
                         source_url=f"https://example.invalid/{n}", raw_source="SEC_EDGAR_FTS", horizon_status=status,
                         resolved_nct_id="NCT00000000", resolution_tier="ACRONYM")

async def main():
    await db.reset_db()
    async with db.get_engine().connect() as conn:
        print("server:", (await conn.execute(text("select version()"))).scalar().split(",")[0])
        cols = (await conn.execute(text("select column_name, data_type from information_schema.columns where table_name='predictions' and column_name in ('scanned_at','fundamental_breakdown','use_options')"))).all()
        print("column types:", sorted(cols))
    await db.upsert_catalyst_events([event(1, "PDUFA_ENTRY_WINDOW"), event(2, "OUTSIDE_WINDOW")])
    await db.upsert_catalyst_events([event(1, "PDUFA_ENTRY_WINDOW"), event(2, "PDUFA_ENTRY_WINDOW"), event(3, None)])
    inwin = await db.get_catalysts_in_entry_window()
    check("upsert twice: 3 rows, statuses updated, in-window query works", sorted(e.ticker for e in inwin) == ["T1", "T2"])
    check("timestamps come back timezone-aware", inwin[0].last_seen_at.tzinfo is not None and inwin[0].last_seen_at > inwin[0].first_seen_at)
    id1 = await db.save_prediction(result("CYTK", 0.83, 0.40), scanned_at=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    id2 = await db.save_prediction(result("CYTK", 0.83, 0.45))
    await db.save_prediction(result("SMMT", 0.35, 0.80))
    preds = await db.get_predictions_for_ticker("CYTK")
    check("predictions saved and read back, newest first", [p.id for p in preds] == [id2, id1] and preds[0].fundamental_breakdown[0]["label"] == "primary_endpoint_significance")
    latest = await db.get_latest_predictions()
    check("latest-per-candidate dedupes", sorted((p.ticker, p.id) for p in latest) == [("CYTK", id2), ("SMMT", id2 + 1)])
    check("awaiting-outcome query", len(await db.get_predictions_awaiting_outcome(as_of=date(2027, 1, 1))) == 3 and not await db.get_predictions_awaiting_outcome(as_of=date(2026, 10, 6)))
    aid = await db.save_alert("CYTK", "LONG_RUN_UP_ALPHA", date(2026, 11, 14), {"ticker": "CYTK", "nested": {"a": [1, 2]}})
    alerts = await db.get_recent_alerts()
    check("alert saved and read back with JSON intact", alerts[0].id == aid and alerts[0].payload["nested"]["a"] == [1, 2])
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
