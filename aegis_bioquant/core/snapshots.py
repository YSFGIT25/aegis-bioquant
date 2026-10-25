"""
core/snapshots.py
------------------
Weekly dated copies of the study's record, uploaded to the OSF project.

WHY: the paper rests on scores stored before each FDA decision. The
database itself can't prove when a row was written — whoever runs it could
in principle change one. A copy held by OSF, timestamped by OSF on upload
and never replaced (each week gets its own folder), shows what the record
held on that date. A reader can later check that a score used in the
analysis was already in an earlier snapshot.

WHAT IS COPIED: the tables the study reads or reports on — predictions,
outcomes, evidence reviews and their history, event-move and IV readings,
screened-out candidates, catalysts and confirmed trials. Each becomes a
gzipped CSV. Nothing about customers is copied: no users, sessions, alert
destinations or deliveries.

manifest.json lists every file with its row count and the SHA-256 of its
uncompressed CSV, plus the rules version and fingerprint in force, so a
copy can be checked against the hash after download.

Uploads use OSF's files API (WaterButler) with a personal access token
(settings.OSF_TOKEN). Errors carry a status code or an exception class
name only, never the request, so the token cannot reach a log line.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from sqlalchemy import select

from aegis_bioquant.config import rules, settings
from aegis_bioquant.core import database as db

TABLES = (
    "predictions",
    "catalyst_outcomes",
    "llm_reviews",
    "llm_review_history",
    "event_move_snapshots",
    "iv_snapshots",
    "screened_candidates",
    "catalyst_events",
    "trial_overrides",
)


class SnapshotError(RuntimeError):
    """A failed upload. The message is safe to log."""


@dataclass
class Snapshot:
    folder: str
    files: list[dict] = field(default_factory=list)   # {name, rows, sha256_csv}


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


async def export_tables() -> list[tuple[str, int, bytes]]:
    """(table name, row count, CSV bytes) for each table in TABLES, rows in
    primary-key order so the same data always gives the same bytes."""
    await db.init_db()
    out = []
    async with db.get_session_factory()() as session:
        for name in TABLES:
            table = db.Base.metadata.tables[name]
            rows = (await session.execute(select(table).order_by(*table.primary_key.columns))).all()
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow([col.name for col in table.columns])
            for row in rows:
                writer.writerow([_cell(v) for v in row])
            out.append((name, len(rows), buf.getvalue().encode("utf-8")))
    return out


async def _put(client: httpx.AsyncClient, url: str, content: bytes = b"") -> dict:
    try:
        resp = await client.put(url, content=content, headers={"Authorization": f"Bearer {settings.OSF_TOKEN}"})
    except httpx.HTTPError as exc:
        raise SnapshotError(f"upload failed: {type(exc).__name__}") from None
    if resp.status_code not in (200, 201):
        raise SnapshotError(f"OSF returned HTTP {resp.status_code}")
    return resp.json()


async def upload_snapshot(now: Optional[datetime] = None) -> Snapshot:
    """Exports the tables and uploads them, with the manifest, into a new
    folder named by the UTC time, e.g. record-snapshot-2026-10-25T1902Z."""
    if not settings.OSF_TOKEN:
        raise SnapshotError("OSF_TOKEN is not set")
    now = now or datetime.now(timezone.utc)
    snap = Snapshot(folder=f"record-snapshot-{now:%Y-%m-%dT%H%M}Z")
    tables = await export_tables()

    blobs = []
    for name, count, data in tables:
        snap.files.append({"name": f"{name}.csv.gz", "rows": count, "sha256_csv": hashlib.sha256(data).hexdigest()})
        blobs.append((f"{name}.csv.gz", gzip.compress(data, mtime=0)))
    manifest = {
        "created_at_utc": now.isoformat(),
        "rules_version": rules.RULES_VERSION,
        "rules_fingerprint": rules.fingerprint(),
        "note": "Weekly copy of the FDAOdds (formerly Aegis-BioQuant) study record. sha256_csv is the hash of "
                "each file after gunzip. Customer tables are not included.",
        "files": snap.files,
    }
    blobs.append(("manifest.json", json.dumps(manifest, indent=2).encode("utf-8")))

    root = f"{settings.OSF_FILES_BASE_URL}/resources/{settings.OSF_PROJECT_ID}/providers/osfstorage/"
    async with httpx.AsyncClient(timeout=120.0) as client:
        made = await _put(client, f"{root}?kind=folder&name={quote(snap.folder)}")
        folder_path = made["data"]["attributes"]["path"]          # e.g. "/5f1a.../"
        for name, content in blobs:
            await _put(client, f"{root.rstrip('/')}{folder_path}?kind=file&name={quote(name)}", content)
    return snap
