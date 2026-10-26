"""python -m aegis_bioquant.phase3 [--limit N]: one Phase 3 run by hand (see scan.py)."""

import argparse
import asyncio

from aegis_bioquant.core.database import dispose_db
from aegis_bioquant.phase3 import scan


async def _main(limit: int) -> None:
    try:
        report = await scan.run(max_reviews=limit)
        print(report.line())
    finally:
        await dispose_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Phase 3 forecasts once.")
    parser.add_argument("--limit", type=int, default=scan.k.P3_MAX_REVIEWS_PER_SCAN, help="most reviews to run")
    asyncio.run(_main(parser.parse_args().limit))
