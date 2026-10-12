import asyncio, sys, threading
from datetime import date
from aegis_bioquant.core.database import (
    init_db, save_prediction, get_predictions_for_ticker,
    get_predictions_awaiting_outcome, dispose_db,
)
from aegis_bioquant.pipeline import run_candidate_pipeline

async def main():
    await init_db()
    print("DB initialized OK", flush=True)
    result = await run_candidate_pipeline(
        ticker="CYTK", nct_id="NCT05767346", catalyst_type="PDUFA", catalyst_date=date(2026, 11, 14)
    )
    pred_id = await save_prediction(result)
    print(f"Saved prediction id={pred_id}", flush=True)
    preds = await get_predictions_for_ticker("CYTK")
    p = preds[0]
    print(f"Queried back {len(preds)} prediction(s) for CYTK")
    print(f"  p_fundamental={p.p_fundamental} arbitrage_signal={p.arbitrage_signal} breakdown={p.fundamental_breakdown}")
    print(f"  p_market={p.p_market} confidence={p.p_market_confidence}")
    awaiting = await get_predictions_awaiting_outcome(as_of=date(2027, 1, 1))
    print(f"Predictions awaiting outcome (simulated as_of 2027-01-01): {len(awaiting)}", flush=True)
    if "--dispose" in sys.argv:
        await dispose_db()
        # engine must rebuild lazily after dispose
        print("after dispose, re-query:", len(await get_predictions_for_ticker("CYTK")), flush=True)

asyncio.run(main())
print("threads at exit:", [(t.name, t.daemon) for t in threading.enumerate()], flush=True)
