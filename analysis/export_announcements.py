"""
analysis/export_announcements.py
---------------------------------
Writes the reviewer-checked announcement times into analysis/announcements.csv,
the file the pre-registered script (run_analysis.py, tagged analysis-v1)
reads. Added 2026-10-25, before any scored decision resolved; the
analysis script itself is unchanged.

Where the times come from: the reviewer confirms each outcome and accepts
(or corrects) its announcement time in Telegram (core/outcome_review.py).
The time is stored on the outcome row and copied into every weekly OSF
snapshot. This reads a snapshot's catalyst_outcomes table:

    python analysis/export_announcements.py --osf
        the newest snapshot on the OSF project (osf.io/zfg9b), checked
        against its manifest's SHA-256 before use
    python analysis/export_announcements.py path/to/catalyst_outcomes.csv.gz
        a snapshot file already downloaded

Only CONFIRMED APPROVED/CRL outcomes whose time the reviewer accepted
(announcement_source REVIEWER) are written. Rows already in the CSV are
kept as they are; a disagreement with the snapshot is printed, not
overwritten, so a hand-entered correction is never lost.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import pathlib
import sys
import urllib.request
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
CSV_PATH = HERE / "announcements.csv"
FIELDS = ["ticker", "catalyst_date", "announced_at_utc", "source_url"]
OSF_ROOT = "https://api.osf.io/v2/nodes/zfg9b/files/osfstorage/"


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def newest_snapshot_outcomes() -> tuple[str, bytes]:
    """(folder name, catalyst_outcomes CSV bytes) from the newest snapshot,
    after checking the file against the snapshot's manifest."""
    folders = [f for f in json.loads(_get(OSF_ROOT + "?page%5Bsize%5D=100"))["data"]
               if f["attributes"]["kind"] == "folder" and f["attributes"]["name"].startswith("record-snapshot-")]
    if not folders:
        raise SystemExit("No record snapshot found on OSF.")
    folder = max(folders, key=lambda f: f["attributes"]["name"])
    files = json.loads(_get(folder["relationships"]["files"]["links"]["related"]["href"]))["data"]
    by_name = {f["attributes"]["name"]: f["links"]["download"] for f in files}
    manifest = json.loads(_get(by_name["manifest.json"]))
    data = gzip.decompress(_get(by_name["catalyst_outcomes.csv.gz"]))
    expected = next(f["sha256_csv"] for f in manifest["files"] if f["name"] == "catalyst_outcomes.csv.gz")
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit("catalyst_outcomes.csv.gz does not match its manifest hash; not used.")
    return folder["attributes"]["name"], data


def reviewer_times(outcomes_csv: bytes) -> dict:
    """(ticker, catalyst_date) -> (announced_at_utc 'YYYY-MM-DDTHH:MM:SSZ', url)."""
    found = {}
    for row in csv.DictReader(io.StringIO(outcomes_csv.decode("utf-8"))):
        if (row.get("status") == "CONFIRMED" and row.get("outcome") in ("APPROVED", "CRL")
                and row.get("announcement_source") == "REVIEWER" and row.get("announced_at")):
            moment = datetime.fromisoformat(row["announced_at"].replace("Z", "+00:00"))
            moment = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
            found[(row["ticker"], row["catalyst_date"])] = (moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                           row.get("announcement_url") or "")
    return found


def merge(existing_path: pathlib.Path, times: dict) -> tuple[list[dict], int, list[str]]:
    rows: list[dict] = []
    if existing_path.exists():
        with existing_path.open(newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("ticker") or "").strip()]
    have = {(r["ticker"].strip(), r["catalyst_date"].strip()): r for r in rows}
    added, conflicts = 0, []
    for key, (when, url) in sorted(times.items(), key=lambda kv: kv[0][1]):
        if key in have:
            if have[key]["announced_at_utc"].strip() != when:
                conflicts.append(f"{key[0]} {key[1]}: CSV has {have[key]['announced_at_utc']}, snapshot has {when} (CSV kept)")
            continue
        rows.append({"ticker": key[0], "catalyst_date": key[1], "announced_at_utc": when, "source_url": url})
        added += 1
    rows.sort(key=lambda r: (r["catalyst_date"], r["ticker"]))
    return rows, added, conflicts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", nargs="?", help="a catalyst_outcomes.csv.gz from a snapshot")
    parser.add_argument("--osf", action="store_true", help="use the newest snapshot on OSF")
    parser.add_argument("--out", type=pathlib.Path, default=CSV_PATH)
    args = parser.parse_args(argv)
    if args.osf:
        name, data = newest_snapshot_outcomes()
        print(f"Using OSF snapshot {name}")
    elif args.source:
        raw = pathlib.Path(args.source).read_bytes()
        data = gzip.decompress(raw) if args.source.endswith(".gz") else raw
    else:
        parser.error("give a snapshot file or --osf")
    rows, added, conflicts = merge(args.out, reviewer_times(data))
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{added} announcement time(s) added; {len(rows)} in {args.out.name}.")
    for line in conflicts:
        print("CHECK: " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
