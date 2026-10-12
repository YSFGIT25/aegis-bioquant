"""
entrypoint.py
--------------
What the hosted container runs. One image serves both Railway services;
the AEGIS_ROLE environment variable picks the job:

    AEGIS_ROLE=web       the website (web/app.py), always on
    AEGIS_ROLE=scanner   the scan, once a day, forever

WHY THE SCHEDULE LIVES HERE AND NOT IN RAILWAY: Railway has a built-in
cron setting, and the first version of this deployment relied on it
through a railway.json file. Those settings were never applied — the
file was accepted without error and ignored, so the "daily" scanner only
ever ran when it was deployed. Rather than depend on host configuration
that can silently not take effect, the scanner service is a small
long-running process that does its own timekeeping. The cost is one idle
Python process between scans.

WHY 19:00 UTC: option quotes are only live while the US market is open
(9:30-16:00 Eastern). Outside those hours the data source returns zero
bids and asks, which makes every market-implied probability unusable and
every IV reading empty — the scans run in the evening and at night while
this was being built all showed exactly that. 19:00 UTC is 3 PM Eastern
in summer and 2 PM in winter: inside the session all year, and late
enough that the day's option volume — which the Phase A filter compares
against a full-day threshold — is mostly in. (A noon scan saw a stock
with 391 contracts traded "fail" a 500-a-day test it would pass by the
close.) On weekends, and whenever a catch-up scan happens to run
outside market hours, the scan still refreshes the catalyst calendar but
scores nothing and takes no option reading (pipeline.run_discovery_scan).

SCHEDULE: one scan a day at SCAN_HOUR_UTC. On start-up it scans
immediately only if the last scan is more than 20 hours old, so
redeploying several times a day does not fire several scans, and a scan
missed while the service was down is caught up. A failed scan (a
catalyst source was unreachable) is retried once, 30 minutes later.

Each scan runs as a separate process (python -m aegis_bioquant.pipeline),
so the headless browser's memory is fully released between scans and a
crash in one scan cannot take the scheduler down.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

SCAN_HOUR_UTC = int(os.environ.get("SCAN_HOUR_UTC", "19"))  # see WHY 19:00 UTC above
CATCH_UP_IF_OLDER_THAN = timedelta(hours=20)
RETRY_DELAY_SECONDS = 30 * 60


def _log(message: str) -> None:
    print(f"[scheduler {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {message}", flush=True)


def next_run_after(now: datetime) -> datetime:
    """The next SCAN_HOUR_UTC:00 strictly after `now`."""
    run = now.replace(hour=SCAN_HOUR_UTC, minute=0, second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)


async def _last_scan_time() -> Optional[datetime]:
    from sqlalchemy import func, select

    from aegis_bioquant.core.database import (
        CatalystEventRecord,
        dispose_db,
        get_session_factory,
        init_db,
    )

    try:
        await init_db()
        async with get_session_factory()() as session:
            last = await session.scalar(select(func.max(CatalystEventRecord.last_seen_at)))
    finally:
        await dispose_db()
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last


def run_scan() -> bool:
    """Runs one scan as a child process. True if it exited cleanly."""
    _log("scan starting")
    code = subprocess.call([sys.executable, "-m", "aegis_bioquant.pipeline"])
    _log(f"scan finished, exit code {code}")
    return code == 0


def run_scan_with_retry() -> None:
    if run_scan():
        return
    _log(f"scan failed; retrying once in {RETRY_DELAY_SECONDS // 60} minutes")
    time.sleep(RETRY_DELAY_SECONDS)
    if not run_scan():
        _log("retry failed too; waiting for the next scheduled scan")


def run_scheduler() -> None:
    from aegis_bioquant.config import rules

    _log(f"rules version {rules.RULES_VERSION}, fingerprint {rules.fingerprint()}"
         + (" - CHANGED WITHOUT BEING LOGGED, see config/rules.py" if rules.unlogged_change() else ""))
    try:
        last = asyncio.run(_last_scan_time())
    except Exception as exc:  # noqa: BLE001 — can't tell, so scan: a spare scan beats a missed day
        _log(f"could not read the last scan time ({type(exc).__name__}); scanning now")
        last = None

    now = datetime.now(timezone.utc)
    if last is None or now - last > CATCH_UP_IF_OLDER_THAN:
        _log(f"last scan: {last.isoformat() if last else 'none on record'} - catching up now")
        run_scan_with_retry()
    else:
        _log(f"last scan: {last.isoformat()} - recent enough, no catch-up scan")

    while True:
        run_at = next_run_after(datetime.now(timezone.utc))
        _log(f"next scan at {run_at:%Y-%m-%d %H:%M}Z")
        # Sleep in short steps and re-check the clock, so a suspended or
        # clock-adjusted host can't make one long sleep overshoot the hour.
        while (remaining := (run_at - datetime.now(timezone.utc)).total_seconds()) > 0:
            time.sleep(min(remaining, 60))
        run_scan_with_retry()


def run_web() -> None:
    import uvicorn

    # proxy_headers: the site sits behind the host's HTTPS proxy, which
    # forwards the original scheme and client address.
    uvicorn.run(
        "aegis_bioquant.web.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


def main() -> None:
    role = os.environ.get("AEGIS_ROLE", "")
    if role == "web":
        run_web()
    elif role == "scanner":
        run_scheduler()
    else:
        # Refuse to guess: a service started as the wrong thing is worse
        # than one that visibly fails to start.
        sys.exit(f"AEGIS_ROLE must be 'web' or 'scanner' (got {role!r}).")


if __name__ == "__main__":
    main()
