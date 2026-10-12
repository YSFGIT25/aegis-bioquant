import asyncio, sys
import httpx
from aegis_bioquant.ingestion import fda_scraper as F

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

real_sleep = asyncio.sleep
async def no_sleep(_): await real_sleep(0)
F.asyncio.sleep = no_sleep

def page(offset, total=25, size=10):
    n = max(0, min(size, total - offset))
    return {"hits": {"total": {"value": total}, "hits": [{"_id": f"a:{offset + i}", "_source": {"ciks": ["1"]}} for i in range(n)]}}

def transport(script):
    # script: {offset: [status, status, ...]} consumed per request; missing/exhausted -> 200.
    calls = []
    def handler(request):
        offset = int(request.url.params.get("from", 0))
        calls.append(offset)
        statuses = script.get(offset, [])
        status = statuses.pop(0) if statuses else 200
        if status == "garbage": return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(status, json=page(offset)) if status == 200 else httpx.Response(status)
    return httpx.MockTransport(handler), calls

async def search(script):
    t, calls = transport(script)
    problems = []
    async with httpx.AsyncClient(transport=t) as c:
        try:
            hits = await F._search_pdufa_filings(c, 180, problems=problems)
            return len(hits), problems, calls, None
        except Exception as e:
            return None, problems, calls, type(e).__name__

async def main():
    n, problems, calls, err = await search({})
    check("clean search: every page read", (n, problems, calls, err) == (25, [], [0, 10, 20], None))
    n, problems, calls, err = await search({10: [500]})
    check("one error response on a middle page is retried and nothing is lost", (n, problems, err) == (25, [], None) and calls == [0, 10, 10, 20])
    n, problems, calls, err = await search({0: [429, 503]})
    check("errors on the first page are retried too", (n, problems, err) == (25, [], None) and calls[:3] == [0, 0, 0])
    n, problems, calls, err = await search({20: [500, 500, 500]})
    check("a page that keeps failing: the filings already read are kept, and the shortfall is reported",
          n == 20 and err is None and problems == ["PDUFA: filing search cut short after 20 filings (HTTPStatusError)"])
    n, problems, calls, err = await search({10: ["garbage"]})
    check("a page that isn't valid data: same handling", n == 10 and err is None and "cut short after 10 filings" in problems[0])
    n, problems, calls, err = await search({0: [500, 500, 500]})
    check("nothing read at all: that is still a failed source, raised", n is None and err == "HTTPStatusError" and calls == [0, 0, 0])
    n, problems, calls, err = await search({0: [404]})
    check("a non-transient error is not retried", err == "HTTPStatusError" and calls == [0])

asyncio.run(main())
print("ALL PASSED")
