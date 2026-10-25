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
LOG_LINES_PER_SECOND = 200   # Railway drops lines over 500/sec; see run_scan()


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


def run_scan() -> tuple[bool, list[str]]:
    """Runs one scan as a child process: (exited cleanly?, its report lines).

    The scan prints its whole report at the end, several hundred lines at
    once. Passed straight through, Railway stores those lines in chunks
    that share one timestamp and hands them back in no fixed order, and
    the burst goes over its 500 lines/sec limit, which drops lines. So
    the report is relayed here one line at a time, at most
    LOG_LINES_PER_SECOND. (Done here rather than in the pipeline because
    pipeline.py counts towards the rules fingerprint.)"""
    _log("scan starting")
    child = subprocess.Popen(
        [sys.executable, "-m", "aegis_bioquant.pipeline"],
        stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert child.stdout is not None
    sys.stdout.flush()
    # Relayed as bytes: decoding and re-encoding here could fail on a
    # character the scheduler's own output encoding lacks. A decoded copy
    # is kept for the status report (telemetry/status.py).
    lines: list[str] = []
    for line in child.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        lines.append(line.decode("utf-8", errors="replace"))
        time.sleep(1.0 / LOG_LINES_PER_SECOND)
    code = child.wait()
    _log(f"scan finished, exit code {code}")
    return code == 0, lines


def run_scan_with_retry() -> None:
    ok, lines = run_scan()
    retried = False
    if not ok:
        _log(f"scan failed; retrying once in {RETRY_DELAY_SECONDS // 60} minutes")
        time.sleep(RETRY_DELAY_SECONDS)
        retried = True
        ok, lines = run_scan()
        if not ok:
            _log("retry failed too; waiting for the next scheduled scan")
    after_scan(ok, retried, lines)


def after_scan(ok: bool, retried: bool, lines: list[str], now: Optional[datetime] = None) -> None:
    """The weekly OSF snapshot (core/snapshots.py) and the operator's
    status message (telemetry/status.py). Neither may stop the scheduler,
    so every failure here is logged and swallowed."""
    from aegis_bioquant.config import settings
    from aegis_bioquant.telemetry import status

    now = now or datetime.now(timezone.utc)
    snapshot_note: Optional[str] = None
    if ok and settings.OSF_TOKEN and now.weekday() == settings.SNAPSHOT_WEEKDAY:
        from aegis_bioquant.core.snapshots import SnapshotError, upload_snapshot

        try:
            snap = asyncio.run(upload_snapshot(now))
            rows = sum(f["rows"] for f in snap.files)
            snapshot_note = f"uploaded to OSF as {snap.folder} ({rows} rows)"
        except SnapshotError as exc:
            snapshot_note = f"FAILED ({exc})"
        except Exception as exc:  # noqa: BLE001 — see docstring
            snapshot_note = f"FAILED ({type(exc).__name__})"
        _log(f"weekly snapshot: {snapshot_note}")

    try:
        awaiting = asyncio.run(status.awaiting_confirmation())
    except Exception as exc:  # noqa: BLE001
        awaiting = [f"(could not read pending outcomes: {type(exc).__name__})"]
    text = status.format_report(ok, retried, status.summarize(lines), awaiting, snapshot_note)
    if asyncio.run(status.send(text)):
        _log("status report sent")


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
