import asyncio, sys
from datetime import date
from aegis_bioquant.core import database as db
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def ev(n, kind, status, source):
    return CatalystEvent(event_type=kind, ticker=f"T{n}", title=f"event {n}", start_date=date(2026, 11, 14),
                         source_url=f"https://example.invalid/{n}", raw_source=source, horizon_status=status)

async def main():
    await db.reset_db()
    full = [ev(1, "PDUFA", "PDUFA_ENTRY_WINDOW", "SEC_EDGAR_FTS"), ev(2, "PDUFA", "OUTSIDE_WINDOW", "SEC_EDGAR_FTS"),
            ev(3, "ADCOM", "ADCOM_ENTRY_WINDOW", "FDA_ADCOM_CALENDAR"), ev(4, "ADCOM", "OUTSIDE_WINDOW", "FDA_ADCOM_CALENDAR")]
    await db.upsert_catalyst_events(full)
    check("clean scan: both sources' in-window events listed", sorted(e.ticker for e in await db.get_catalysts_in_entry_window()) == ["T1", "T3"])
    await asyncio.sleep(0.05)
    await db.upsert_catalyst_events([e for e in full if e.raw_source == "FDA_ADCOM_CALENDAR"])  # SEC unreachable this scan
    check("scan with SEC unreachable: SEC's in-window event is still listed", sorted(e.ticker for e in await db.get_catalysts_in_entry_window()) == ["T1", "T3"])
    await asyncio.sleep(0.05)
    await db.upsert_catalyst_events([ev(2, "PDUFA", "OUTSIDE_WINDOW", "SEC_EDGAR_FTS"), full[2], full[3]])  # T1 dropped out of SEC results
    check("event no longer returned by its source drops off the list", sorted(e.ticker for e in await db.get_catalysts_in_entry_window()) == ["T3"])
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
