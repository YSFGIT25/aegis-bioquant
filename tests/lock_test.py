import asyncio, sys
from aegis_bioquant.core.database import init_db
from aegis_bioquant.core.cache import acquire_lock, release_lock, is_locked, pipeline_lockout, pipeline_lock_key

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

async def main():
    await init_db()
    k = pipeline_lock_key("cytk")
    check("key normalised", k == "pipeline:CYTK")
    t1 = await acquire_lock(k)
    check("first acquire gets token", t1 is not None)
    check("second acquire refused", await acquire_lock(k) is None)
    check("is_locked true", await is_locked(k))
    check("other key independent", (t_other := await acquire_lock(pipeline_lock_key("SMMT"))) is not None)
    check("release with wrong token refused", not await release_lock(k, "nope"))
    check("still locked after wrong-token release", await is_locked(k))
    check("release with right token", await release_lock(k, t1))
    check("unlocked after release", not await is_locked(k))
    check("double release returns False", not await release_lock(k, t1))
    # TTL expiry + takeover
    t2 = await acquire_lock(k, ttl_seconds=1)
    check("short-ttl acquire", t2 is not None)
    check("refused before expiry", await acquire_lock(k) is None)
    await asyncio.sleep(1.3)
    check("is_locked false after expiry", not await is_locked(k))
    t3 = await acquire_lock(k)
    check("takeover after expiry", t3 is not None)
    check("stale holder cannot release new holder's lock", not await release_lock(k, t2))
    check("new holder still locked", await is_locked(k))
    await release_lock(k, t3)
    # context manager, incl. exception path
    async with pipeline_lockout(k) as got:
        check("ctx acquired", got)
        async with pipeline_lockout(k) as got2:
            check("nested ctx refused", not got2)
        check("refused ctx did not release holder", await is_locked(k))
    check("ctx released", not await is_locked(k))
    try:
        async with pipeline_lockout(k):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("ctx released after exception", not await is_locked(k))
    await release_lock(pipeline_lock_key("SMMT"), t_other)

asyncio.run(main())
print("ALL PASSED")
