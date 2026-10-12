"""
ingestion/company_tickers.py
-----------------------------
Company name -> stock ticker, from the SEC's own published list
(https://www.sec.gov/files/company_tickers.json — every company with a
registered ticker, about 10,000 rows, free, one request).

Needed wherever a source names a COMPANY rather than a ticker: an FDA
advisory committee agenda ("...submitted by Acme Pharmaceuticals, Inc.")
or a ClinicalTrials.gov sponsor field.

MATCHING IS DELIBERATELY STRICT: a name matches only if, after stripping
legal suffixes (Inc, Corp, Ltd...) and punctuation, it is IDENTICAL to a
listed company's name. No partial or fuzzy matching. That misses some
real companies — a sponsor recorded as "Galderma R&D" will not match
"Galderma Group AG" — and that is the intended trade: a missed company
is one fewer row on a watch list, while a wrong match would attach a
drug to the wrong stock. Private companies simply don't match, which is
correct.
"""

from __future__ import annotations

import re
from typing import Optional

import httpx

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def normalize_company_name(name: str) -> str:
    """Strips legal-entity suffixes and punctuation so the same company
    written two ways compares equal."""
    cleaned = re.sub(
        r"\b(incorporated|inc|corporation|corp|co|ltd|limited|llc|plc|holdings?|sa|ag|nv|se)\.?\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[^a-z0-9 ]", "", cleaned.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


class TickerIndex:
    def __init__(self, rows: list[dict]):
        # The SEC lists some companies more than once (share classes,
        # warrants). The file is ordered largest-first, so the first row
        # seen for a name is its main listing — keep that one.
        self._by_name: dict[str, str] = {}
        for row in rows:
            key = normalize_company_name(str(row.get("title", "")))
            ticker = str(row.get("ticker", "")).upper()
            if key and ticker and key not in self._by_name:
                self._by_name[key] = ticker

    def __len__(self) -> int:
        return len(self._by_name)

    def lookup(self, company_name: Optional[str]) -> Optional[str]:
        if not company_name:
            return None
        return self._by_name.get(normalize_company_name(company_name))


async def fetch_ticker_index(user_agent: str) -> TickerIndex:
    """One request to sec.gov. `user_agent` must be the identified one
    SEC's fair-access policy requires (see fda_scraper._build_user_agent)."""
    async with httpx.AsyncClient(headers={"User-Agent": user_agent}, timeout=30.0) as client:
        resp = await client.get(COMPANY_TICKERS_URL)
        resp.raise_for_status()
        return TickerIndex(list(resp.json().values()))
