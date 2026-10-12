"""
ingestion/phase3_readouts.py
-----------------------------
Phase A, third catalyst type: upcoming Phase 3 data readouts.

The manual: "Scan upcoming quarter windows, but tag status as
WAIT_FOR_FLOW_SIGNAL. Do not deploy capital on purely estimated
timelines without secondary market confirmation." So this builds a WATCH
LIST only. Nothing here is scored, sized or alerted on, and pipeline.py
never runs these events through the engines.

SOURCE: ClinicalTrials.gov — every Phase 3 trial run by a company (not a
university or government body) whose primary completion date falls in
the next PHASE3_LOOKAHEAD_DAYS. Each sponsor is matched to a stock
ticker with ingestion/company_tickers.py; trials whose sponsor isn't a
listed company are dropped, since there is nothing to trade.

WHAT THE DATE MEANS, stated plainly: "primary completion date" is when
the last patient's main measurement is taken — the trial's own estimate,
often revised. It is NOT the day results are announced; companies
typically report top-line data weeks to a few months later. Treat it as
"results are getting close", which is all a watch list needs.

The "flow signal" the manual wants before acting on one of these
(unusual options activity) is not built. Until it is, these stay on the
watch list indefinitely.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import httpx

from aegis_bioquant.config import constants as c
from aegis_bioquant.ingestion.clinical_trials_api import CTGOV_API_BASE, _USER_AGENT, _parse_partial_date
from aegis_bioquant.ingestion.company_tickers import TickerIndex

# Trials that can still read out. Not-yet-recruiting trials list an
# aspirational date; withdrawn / terminated / suspended ones won't report.
_ACTIVE_STATUSES = {"ACTIVE_NOT_RECRUITING", "RECRUITING", "ENROLLING_BY_INVITATION"}

_FIELDS = "NCTId,Acronym,BriefTitle,LeadSponsorName,PrimaryCompletionDate,OverallStatus"


async def fetch_phase3_readouts(tickers: TickerIndex, today: Optional[date] = None):
    """
    Returns CatalystEvent rows (event_type "PHASE_3_READOUT") for listed
    companies' Phase 3 trials nearing primary completion. The caller tags
    them with the WAIT_FOR_FLOW_SIGNAL status (classify_catalyst_window).
    """
    from aegis_bioquant.ingestion.fda_scraper import CatalystEvent  # avoid a circular import

    today = today or date.today()
    window_end = today + timedelta(days=c.PHASE3_LOOKAHEAD_DAYS)
    params = {
        "filter.advanced": (
            "AREA[Phase]PHASE3 AND AREA[LeadSponsorClass]INDUSTRY AND "
            f"AREA[PrimaryCompletionDate]RANGE[{today.isoformat()},{window_end.isoformat()}]"
        ),
        "fields": _FIELDS,
        "pageSize": 100,
    }

    events = []
    seen: set[str] = set()
    fetched = 0
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=30.0) as client:
        while fetched < c.PHASE3_MAX_STUDIES:
            resp = await client.get(f"{CTGOV_API_BASE}/studies", params=params)
            resp.raise_for_status()
            payload = resp.json()
            studies = payload.get("studies", [])
            fetched += len(studies)

            for study in studies:
                section = study.get("protocolSection", {})
                ident = section.get("identificationModule", {})
                status = section.get("statusModule", {})
                sponsor = section.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name")
                nct_id = ident.get("nctId")

                if not nct_id or nct_id in seen or status.get("overallStatus") not in _ACTIVE_STATUSES:
                    continue
                ticker = tickers.lookup(sponsor)
                if ticker is None:
                    continue  # private, foreign-only or academic sponsor — nothing to trade
                completion = _parse_partial_date((status.get("primaryCompletionDateStruct") or {}).get("date"))
                if completion is None:
                    continue

                seen.add(nct_id)
                events.append(
                    CatalystEvent(
                        event_type="PHASE_3_READOUT",
                        ticker=ticker,
                        company_name=sponsor,
                        title=ident.get("briefTitle", "")[:300],
                        start_date=completion,
                        source_url=f"https://clinicaltrials.gov/study/{nct_id}",
                        raw_source="CTGOV_PHASE3",
                        drug_or_trial_candidate=ident.get("acronym"),
                        resolved_nct_id=nct_id,
                        resolution_tier="DIRECT",  # the trial ID came from the registry itself, not text matching
                    )
                )

            token = payload.get("nextPageToken")
            if not token or not studies:
                break
            params["pageToken"] = token

    return events
