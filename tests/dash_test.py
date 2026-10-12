import asyncio, sys
from datetime import date
from types import SimpleNamespace
import httpx
from aegis_bioquant.config import settings
from aegis_bioquant.core.database import init_db, dispose_db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan
from aegis_bioquant.telemetry import alerts as A

BASE = sys.argv[1]
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def make(ticker, pf, pm, oi=4512, reason="primary endpoint strongly significant (p~0.0001)"):
    cand = Candidate(ticker=ticker, catalyst_type="PDUFA", catalyst_date=date(2026, 11, 14), spot_price=7.42,
                     open_interest=oi, daily_options_volume=2002, p_fundamental=pf, p_market=pm,
                     event_implied_move=(0.25 if pm is not None else None), event_move_confidence=("GOOD" if pm is not None else None),
                     iv_rank=(None if pm is None else (20.0 if pf >= 0.75 else 70.0)),
                     p_market_confidence="GOOD" if pm is not None else "LOW_CONFIDENCE_WIDE_SPREAD")
    evaluate_candidate(cand)
    fs = FundamentalScore(nct_id="NCT05767346", base_rate=0.83, p_fundamental=pf, adjustments=[
        ScoreAdjustment("primary_endpoint_significance", 0.05, reason),
        ScoreAdjustment("safety_mortality_imbalance", -0.05, "1 more death(s) on treatment arm than control")])
    return SimpleNamespace(candidate=cand, fundamental_score=fs, market_snapshot=None,
                           execution_plan=build_execution_plan(cand, total_cash=100_000.0))

async def main():
    settings.DASHBOARD_WEBHOOK_URL = BASE + "/webhook"
    await init_db()
    async with httpx.AsyncClient(timeout=10) as c:
        s0 = (await c.get(BASE + "/api/state")).json()
        check("state before: no alerts, real scan data present",
              s0["alerts"] == [] and [p["ticker"] for p in s0["predictions"]] == ["CYTK"]
              and sorted(e["ticker"] for e in s0["catalysts_in_window"]) == ["CYTK", "SMMT"])
        for res in (make("TEST1", 0.83, 0.40), make("TEST2", 0.90, None, oi=1200),
                    make("TEST3", 0.35, 0.80, reason='<img src=x onerror="document.title=\'XSS\'"> <b>bold</b>')):
            r = await A.notify_if_actionable(res)
            check(f"{res.candidate.ticker} delivered through the real alert path", r.status == "SENT" and r.outcomes[0].detail == "HTTP 200")
        s1 = (await c.get(BASE + "/api/state")).json()
        check("three alerts stored, newest first", [a["payload"]["ticker"] for a in s1["alerts"]] == ["TEST3", "TEST2", "TEST1"])
        check("stored payload is the full schema", A.AlertPayload.model_validate(s1["alerts"][0]["payload"]).signal.arbitrage_signal == "STRUCTURAL_COLLAPSE_AVOID")
        bad = await c.post(BASE + "/webhook", json={"ticker": "JUNK"})
        check("malformed webhook body rejected with 422", bad.status_code == 422)
        s2 = (await c.get(BASE + "/api/state")).json()
        check("rejected body not stored", len(s2["alerts"]) == 3)
        page = await c.get(BASE + "/")
        check("page served", page.status_code == 200 and "Aegis-BioQuant" in page.text)
    await dispose_db()

asyncio.run(main())
print("ALL PASSED")
