import asyncio, sys
import httpx
from aegis_bioquant.ingestion import fda_scraper as F

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

sleeps = []
real_sleep = asyncio.sleep
async def fast_sleep(d): sleeps.append(d); await real_sleep(0)
F.asyncio.sleep = fast_sleep

async def main():
    calls = {"n": 0}
    def flaky(request):
        calls["n"] += 1
        if calls["n"] <= 2: return httpx.Response(429, headers={"Retry-After": "1"} if calls["n"] == 1 else {})
        return httpx.Response(200, text="<html><body>PDUFA <b>goal</b> date</body></html>")
    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as c:
        text = await F._fetch_filing_text(c, "https://www.sec.gov/x")
    check("429, 429, 200 -> succeeds on third attempt", calls["n"] == 3 and "PDUFA goal date" in text)
    check("backoff honoured Retry-After then grew", sleeps == [1.0, 4.0])

    calls["n"] = 0
    def always_429(request): calls["n"] += 1; return httpx.Response(429)
    async with httpx.AsyncClient(transport=httpx.MockTransport(always_429)) as c:
        try: await F._fetch_filing_text(c, "https://www.sec.gov/x"); raised = False
        except httpx.HTTPStatusError: raised = True
    check("persistent 429 -> gives up after 3 attempts and raises", raised and calls["n"] == 3)

    calls["n"] = 0
    def not_found(request): calls["n"] += 1; return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(not_found)) as c:
        try: await F._fetch_filing_text(c, "https://www.sec.gov/x"); raised = False
        except httpx.HTTPStatusError: raised = True
    check("404 is not retried", raised and calls["n"] == 1)

    adcom_calls = {"n": 0}
    async def adcom_down(): adcom_calls["n"] += 1; raise TimeoutError("table never rendered")
    async def pdufa_partial(lookback_days, problems=None):
        problems.append("PDUFA: 20 of 24 SEC filings could not be fetched"); return []
    F.fetch_adcom_calendar, F.fetch_pdufa_catalysts = adcom_down, pdufa_partial
    from aegis_bioquant.ingestion.company_tickers import TickerIndex
    async def no_index(ua): return TickerIndex([])
    async def no_phase3(tickers, today=None): return []
    F.fetch_ticker_index, F.fetch_phase3_readouts, F._build_user_agent = no_index, no_phase3, (lambda: "test-agent")
    problems = []
    events = await F.ingest_catalysts(problems=problems)
    check("both sources failing -> no raise, both reported, AdCom retried once",
          events == [] and adcom_calls["n"] == 2 and sorted(problems) == ["ADCOM: source unavailable (TimeoutError)", "PDUFA: 20 of 24 SEC filings could not be fetched"])
    check("problems list optional (old call style still works)", await F.ingest_catalysts() == [])

asyncio.run(main())
print("ALL PASSED")
