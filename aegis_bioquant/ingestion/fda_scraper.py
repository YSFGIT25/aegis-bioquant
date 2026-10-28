"""
ingestion/fda_scraper.py
-------------------------
Ingests PDUFA, AdCom, and Phase 3 timelines (Phase A: Catalyst Ingestion).

Two independent adapters feed a common CatalystEvent model:

1. ADCOM  -> FDA's own Advisory Committee Calendar
   (https://www.fda.gov/advisory-committees/advisory-committee-calendar)
   This page is REAL but JS-rendered (jQuery DataTables) — httpx/bs4 alone
   see an empty table shell, so this adapter drives a headless browser
   (Playwright) instead. Verified live: setting the "Show entries" select
   to -1 ("All") renders every row (197/197 at verification time) in one
   pass, no pagination needed.

2. PDUFA  -> SEC EDGAR full-text search (https://efts.sec.gov/LATEST/search-index)
   FDA does NOT publish PDUFA dates itself — they first surface in a
   sponsor's own 8-K / press-release exhibit when FDA accepts the NDA/BLA
   filing. EDGAR's full-text search is a free, public, static-JSON
   government API (no JS, no auth) — the legitimate source for this.

   NOTE ON SCOPE: a third-party aggregator (BioPharmCatalyst) was evaluated
   first since it's the conventional retail source for this data, but its
   FDA/PDUFA calendar gates exactly the fields we need (drug name, NCT
   number, catalyst date/type) behind a paid subscription — verified live,
   those cells render literally as "Please upgrade to see drug name" /
   "Locked" for free accounts. That is explicitly paywalled content, not
   public data, so no adapter was built against it. If a paid API key for
   that service (or an equivalent) is available later, add a third adapter
   here following the same CatalystEvent contract.

3. PHASE 3 READOUTS -> ingestion/phase3_readouts.py (ClinicalTrials.gov),
   pulled in by ingest_catalysts() below. A watch list only: Phase A tags
   them PHASE_3_STATUS_TAG ("WAIT_FOR_FLOW_SIGNAL") and nothing scores or
   alerts on them, per the manual.

ADCOM DETAIL: the calendar row names only the committee. For upcoming
meetings _enrich_adcom_events() reads the meeting's own page, which
states the application, drug and sponsoring company, and maps the
company to a ticker (ingestion/company_tickers.py).

A LIMIT OF THE ADCOM SOURCE ITSELF: the FDA usually announces a meeting
only a few weeks ahead — on 2026-10-06 the calendar held exactly one
drug meeting in the next 150 days, 24 days out. The manual's AdCom entry
window is 55-70 days before the meeting, so this source will rarely
show a meeting while it is still inside that window. Companies disclose
AdCom dates earlier in their SEC filings; reading those is not built.

PDUFA -> NCT LINKING (closes the gap flagged when clinical_trials_api.py
shipped): a PDUFA 8-K names a drug or trial acronym in free text (e.g.
"The Supplemental New Drug Application for MAPLE-HCM was accepted..."),
not an NCT ID. fetch_pdufa_catalysts() now extracts candidate drug/trial
names from the filing text (trial-acronym pattern first, then common INN
drug-name suffixes), and resolves each candidate against
clinical_trials_api.search_studies(), scoped by sponsor name, accepting
the first hit whose sponsor actually matches. Verified live: resolves
CYTK's filing straight to NCT05767346 (MAPLE-HCM) with no manual lookup.
This is still a heuristic over free text, not a guarantee — see
_extract_drug_candidates()'s docstring for its real failure modes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator

from aegis_bioquant.config import constants as c
from aegis_bioquant.ingestion.clinical_trials_api import search_studies
from aegis_bioquant.ingestion.company_tickers import TickerIndex, fetch_ticker_index
from aegis_bioquant.ingestion.phase3_readouts import fetch_phase3_readouts

logger = logging.getLogger(__name__)

FDA_ADCOM_CALENDAR_URL = "https://www.fda.gov/advisory-committees/advisory-committee-calendar"
EDGAR_FTS_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# FDA centers in scope for a drug/biologic catalyst play. CDRH (medical
# devices) rows share the same calendar table and are noise for this system.
ADCOM_RELEVANT_CENTERS = {
    "Center for Drug Evaluation and Research",
    "Center for Biologics Evaluation and Research",
}

EventType = Literal["ADCOM", "PDUFA", "PHASE_3_READOUT"]


class CatalystEvent(BaseModel):
    """Structured, validated output of every adapter in this module."""

    event_type: EventType
    ticker: Optional[str] = None
    company_name: Optional[str] = None
    cik: Optional[str] = None

    title: str
    start_date: date
    end_date: Optional[date] = None

    center: Optional[str] = None          # ADCOM only
    committee: Optional[str] = None       # ADCOM only

    source_url: str
    raw_source: str                       # "FDA_ADCOM_CALENDAR" | "SEC_EDGAR_FTS"

    horizon_status: Optional[str] = None  # populated by classify_catalyst_window()

    # PDUFA only — populated by _resolve_drug_candidate_to_nct()
    drug_or_trial_candidate: Optional[str] = None  # the free-text name that resolved
    resolved_nct_id: Optional[str] = None          # None if no confident match found
    resolution_tier: Optional[str] = None          # which pattern matched — see
    # _extract_drug_candidates(). Three confidence buckets:
    #   HIGH   "STUDY_TRIAL_NAME" / "ACRONYM" — a specific named trial whose
    #          name equals the registry's own acronym for it.
    #   REVIEW "NAME_UNCONFIRMED" — a trial name was found in the text but
    #          no trial is registered under exactly that acronym, so the
    #          match is a best guess among similarly named trials.
    #   REVIEW "ADCOM_AGENDA" — a drug named on an advisory committee
    #          agenda; names the drug, not which of its trials.
    #   MEDIUM "NDA_FOR" — grammatically tied to the filing, but often a
    #          bare compound name rather than a trial name.
    #   LOW    "INN_SUFFIX" — bare compound name, no grammatical anchor.
    # MEDIUM/LOW can resolve to the WRONG specific NCT when a sponsor runs
    # that compound across several trials (e.g. a Phase 1 PK study instead
    # of the real pivotal trial) — verified live on an earlier build of
    # this resolver before STUDY_TRIAL_NAME/ACRONYM were given priority
    # over NDA_FOR. Treat MEDIUM/LOW resolutions as "needs a human look".

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v


# --------------------------------------------------------------------------
# Adapter 1: FDA Advisory Committee Calendar (Playwright — JS-rendered)
# --------------------------------------------------------------------------

async def fetch_adcom_calendar(timeout_ms: int = 30_000) -> list[CatalystEvent]:
    """
    Drives a headless browser against the live FDA AdCom calendar, expands
    the DataTable to show all rows in one pass, and parses the table body.

    Requires the Playwright dependency + `playwright install chromium`.
    """
    from playwright.async_api import async_playwright  # local import: optional/heavy dep

    events: list[CatalystEvent] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(FDA_ADCOM_CALENDAR_URL, timeout=timeout_ms)
            await page.wait_for_selector("table", timeout=timeout_ms)

            # Expand "Show entries" to All (-1) so every row renders in one page.
            await page.select_option(
                "select[name='DataTables_Table_0_length']", "-1"
            )
            await page.wait_for_function(
                "document.body.innerText.includes('Showing 1 to')",
                timeout=timeout_ms,
            )

            rows = await page.query_selector_all("table tbody tr")
            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 4:
                    continue

                start_raw = (await cells[0].inner_text()).strip()
                end_raw = (await cells[1].inner_text()).strip()
                link_el = await cells[2].query_selector("a")
                title = (await (link_el or cells[2]).inner_text()).strip()
                href = await link_el.get_attribute("href") if link_el else None
                center = (await cells[3].inner_text()).strip()

                if center not in ADCOM_RELEVANT_CENTERS:
                    continue  # Phase A noise filter: drop device-panel rows

                start_dt = _parse_fda_datetime(start_raw)
                end_dt = _parse_fda_datetime(end_raw)
                if start_dt is None:
                    logger.warning("Could not parse AdCom start date: %r", start_raw)
                    continue

                events.append(
                    CatalystEvent(
                        event_type="ADCOM",
                        title=title,
                        start_date=start_dt.date(),
                        end_date=end_dt.date() if end_dt else None,
                        center=center,
                        committee=_extract_committee_name(title),
                        source_url=(
                            f"https://www.fda.gov{href}" if href else FDA_ADCOM_CALENDAR_URL
                        ),
                        raw_source="FDA_ADCOM_CALENDAR",
                    )
                )
        finally:
            await browser.close()

    return events


def _parse_fda_datetime(raw: str) -> Optional[datetime]:
    """FDA renders e.g. '11/19/2026 09:00 AM EST' — strip the tz abbreviation,
    which strptime can't parse portably, then parse month/day/year + time."""
    cleaned = re.sub(r"\s+(EST|EDT|CST|CDT|MST|MDT|PST|PDT)$", "", raw.strip())
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _extract_committee_name(title: str) -> Optional[str]:
    """Best-effort pull of the committee name out of a free-text meeting title."""
    match = re.search(r"([A-Za-z,\s]+?Advisory Committee)", title)
    return match.group(1).strip() if match else None


# --------------------------------------------------------------------------
# Adapter 2: SEC EDGAR full-text search (PDUFA dates, static JSON + HTML)
# --------------------------------------------------------------------------

_DISPLAY_NAME_RE = re.compile(r"^(.*?)\s*\(([A-Z]{1,6})\)\s*\(CIK", re.IGNORECASE)

# A calendar date as press releases actually write it. Each of these was a
# real miss in the first version, which only understood "November 14, 2026":
#   "Oct. 10, 2026"            abbreviated month, with a period
#   "September      28, 2026"  a line break inside the date
#   "May 24 th"                ordinal suffix, and no year at all
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
# (?!\d) after the day: without it "November 2026" reads as "November 20".
_DATE = rf"({_MONTH})\s+(\d{{1,2}})(?!\d)(?:\s*(?:st|nd|rd|th)\b)?(?:,?\s+(\d{{4}}))?"

# "... PDUFA [target action] date [of / set for / is] <date>" — the date
# FOLLOWS the phrase, in the same sentence, within a short distance.
# Case-sensitive on purpose: with IGNORECASE the month "May" would also
# match the verb "may".
_PDUFA_DATE_AFTER_RE = re.compile(
    rf"(?:PDUFA|Prescription Drug User Fee Act|[Tt]arget [Aa]ction)[^.;]{{0,60}}?[Dd]ate[^.;]{{0,50}}?\b{_DATE}"
)
# "... decision by <date> Prescription Drug User Fee Act (PDUFA) date" —
# the date comes immediately BEFORE the phrase. Kept tight (nothing but
# whitespace between) so "approved on June 26, 2026, ahead of the PDUFA
# date" is not read as a PDUFA date of June 26.
_PDUFA_DATE_BEFORE_RE = re.compile(
    rf"\b{_DATE}\s+(?:Prescription Drug User Fee Act\s*\(PDUFA\)|PDUFA)\s+"
    r"(?:[Tt]arget [Aa]ction |[Gg]oal )?[Dd]ate"
)
_MONTH_NUMBER = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _build_user_agent() -> str:
    """
    SEC's Fair Access policy requires a descriptive User-Agent with a real
    contact email on every automated request (see sec.gov/os/accessing-edgar-data)
    — unidentified bulk scraping risks an IP ban. Pull the contact email from
    settings rather than hardcoding one, so it's the operator's explicit choice.
    """
    from aegis_bioquant.config import settings as s

    contact = getattr(s, "SEC_EDGAR_CONTACT_EMAIL", None)
    if not contact:
        raise RuntimeError(
            "SEC_EDGAR_CONTACT_EMAIL is not set in config/settings.py — "
            "SEC requires a real contact email in the User-Agent for "
            "automated EDGAR access. Set it before calling fetch_pdufa_catalysts()."
        )
    return f"FDAOdds/1.0 ({contact})"


_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
_SEC_ATTEMPTS = 3


async def _get_with_retry(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """
    One GET to sec.gov, retried on the responses that mean "try again":
    429 (too many requests) and the 5xx family (service trouble).

    sec.gov answers 429 in bursts when run from a hosted server —
    observed on the first cloud run: every filing fetch refused, while
    the identical run minutes later had none. Cloud providers share
    outbound IPs, and SEC's limit is per IP. So a refused request is
    backed off and retried rather than given up on at once. Any other
    status (a 404, say) is returned straight away; the caller decides.
    """
    for attempt in range(1, _SEC_ATTEMPTS + 1):
        resp = await client.get(url)
        if resp.status_code not in _RETRYABLE_STATUS or attempt == _SEC_ATTEMPTS:
            break
        retry_after = resp.headers.get("Retry-After", "")
        delay = float(retry_after) if retry_after.isdigit() else 2.0 * attempt
        await asyncio.sleep(min(delay, 15.0))
    return resp


_EMPTY_SEARCH_WAIT_SECONDS = 30.0


async def _search_pdufa_filings(
    client: httpx.AsyncClient, lookback_days: int, problems: Optional[list[str]] = None
) -> list[dict]:
    """
    Pages through EDGAR full-text search for recent filings that mention
    a PDUFA date in any of its common wordings.

    The first version searched 8-Ks for the exact phrase "PDUFA date"
    (81 filings in 180 days). Measured on 2026-10-06, that missed:
      - "PDUFA target action date" / "PDUFA goal date" / "target action
        date" wordings: searching "PDUFA" OR "target action date" finds
        172 8-Ks;
      - foreign companies, which file a 6-K instead of an 8-K: 32 more.

    The results come a page at a time. Each page is retried on a
    transient error; if one still fails, the pages already read are KEPT
    and the shortfall is noted in `problems`. (Before this, one failed
    page — seen live as a single error response from the search service
    — discarded every PDUFA date for that scan.) Only a failure on the
    very first page, with nothing read, is raised.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)

    hits: list[dict] = []
    offset = 0
    asked_again = False

    while len(hits) < c.EDGAR_MAX_FILINGS_PER_RUN:
        params = {
            # The Act's full name too: an 8-K announcing an EXTENDED date (EXEL,
            # 2026-09-11) said only "the updated Prescription Drug User Fee Act
            # action date is March 3, 2027" and was missed for a month.
            "q": '"PDUFA" OR "target action date" OR "Prescription Drug User Fee Act"',
            "forms": "8-K,6-K",
            "dateRange": "custom",
            "startdt": start.isoformat(),
            "enddt": end.isoformat(),
            "from": offset,
        }
        try:
            resp = await _get_with_retry(client, f"{EDGAR_FTS_URL}?{urlencode(params)}")
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if not hits:
                raise
            logger.warning("EDGAR search stopped after %d filings: %s", len(hits), type(exc).__name__)
            if problems is not None:
                problems.append(
                    f"PDUFA: filing search cut short after {len(hits)} filings ({type(exc).__name__})"
                )
            break

        page_hits = payload.get("hits", {}).get("hits", [])
        if not page_hits and not hits and not asked_again:
            # Seen 2026-10-28 19:00Z: a valid answer with no filings at all,
            # while the same search minutes later found 91 PDUFA dates. Over
            # the lookback window a real empty result does not happen, so
            # ask once more before believing it.
            asked_again = True
            await asyncio.sleep(_EMPTY_SEARCH_WAIT_SECONDS)
            continue
        if not page_hits:
            if not hits and problems is not None:
                # Reported, not raised: the scan goes on, but the day is
                # marked as missing its PDUFA dates rather than a quiet one.
                problems.append("PDUFA: filing search returned no filings at all, twice")
            break

        hits.extend(page_hits)
        offset += len(page_hits)  # whatever page size the service actually used
        total = payload.get("hits", {}).get("total", {}).get("value", 0)
        if offset >= total:
            break

        await asyncio.sleep(0.15)  # courtesy throttle, well under SEC's 10 req/s cap

    return hits[: c.EDGAR_MAX_FILINGS_PER_RUN]


def _filing_doc_url(cik: str, doc_id: str) -> str:
    """doc_id looks like '0001193125-25-199706:d947259dex991.htm'."""
    accession, _, filename = doc_id.partition(":")
    accession_no_dashes = accession.replace("-", "")
    return f"{EDGAR_ARCHIVES_BASE}/{int(cik)}/{accession_no_dashes}/{filename}"


async def _fetch_filing_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await _get_with_retry(client, url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def _to_date(month: str, day: str, year: Optional[str], filed: Optional[date]) -> Optional[date]:
    """Builds a date from a regex match. A missing year ("PDUFA date set
    for May 24th") is filled in from the filing date: the first May 24 on
    or after the day the filing was made. Without a filing date a
    year-less date is not guessed."""
    number = _MONTH_NUMBER.get(month[:3].lower())
    if number is None:
        return None
    try:
        if year:
            return date(int(year), number, int(day))
        if filed is None:
            return None
        candidate = date(filed.year, number, int(day))
        return candidate if candidate >= filed else date(filed.year + 1, number, int(day))
    except ValueError:  # "February 30"
        return None


# A press-release dateline: "PETACH TIKVA, Israel, " / "BOSTON, Mass., " /
# "CARLSBAD, Calif., " sitting immediately before the date.
_DATELINE_RE = re.compile(r"[A-Z]{3,}(?:\s+[A-Z]{2,})*,\s*(?:[A-Z][A-Za-z.]*,?\s*){0,2}$")


def _is_release_date(lead_in: str) -> bool:
    """
    True if the date that follows `lead_in` (the text from "PDUFA" up to
    the date) is the date of the press release rather than a PDUFA date.
    Both cases below were recorded as real catalysts before this check:
      "...with November 28, 2026 PDUFA goal date PETACH TIKVA, Israel,
       October 1, 2026 -- PolyPid Ltd..."          (headline, then dateline)
      "...Extension of PDUFA Target Action Date as FDA Continues Review
       of Deramiocel BLA", dated August 24, 2026."  (title, then "dated")
    """
    tail = lead_in[lead_in.lower().rfind("date") + 4:]
    if tail[:1] == "d":  # the last "date" was the start of "dated"
        return True
    return _DATELINE_RE.search(tail) is not None


def _extract_pdufa_dates(text: str, filed: Optional[date] = None) -> list[tuple[date, int]]:
    """
    Every distinct PDUFA date stated in the text, as (date, position in
    text), in the order they appear. A filing can state several — a
    company with two drugs under review has two — and the first version
    of this took only the first, so the second drug was never seen.
    """
    found: list[tuple[date, int]] = []
    seen: set[date] = set()
    matches = [(m.start(), m, True) for m in _PDUFA_DATE_AFTER_RE.finditer(text)]
    matches += [(m.start(), m, False) for m in _PDUFA_DATE_BEFORE_RE.finditer(text)]
    for position, match, year_optional in sorted(matches, key=lambda item: item[0]):
        month, day, year = match.group(1), match.group(2), match.group(3)
        if not year and not year_optional:
            continue
        if year_optional and _is_release_date(text[match.start(): match.start(1)]):
            continue
        parsed = _to_date(month, day, year, filed)
        if parsed is not None and parsed not in seen:
            seen.add(parsed)
            found.append((parsed, position))
    return found


def _extract_pdufa_date(text: str, filed: Optional[date] = None) -> Optional[date]:
    """The first PDUFA date in the text, or None."""
    dates = _extract_pdufa_dates(text, filed)
    return dates[0][0] if dates else None


def _parse_ticker_company(display_names: list[str]) -> tuple[Optional[str], Optional[str]]:
    for name in display_names:
        match = _DISPLAY_NAME_RE.match(name)
        if match:
            company, ticker = match.groups()
            return ticker, company.strip()
    if not display_names:
        return None, None
    # No ticker in the header (common for foreign 6-K filers): the name
    # still ends in "(CIK 0001234567)", which has to come off or it never
    # matches the same company in the SEC's ticker list.
    return None, re.sub(r"\s*\(CIK.*$", "", display_names[0]).strip() or None


# --------------------------------------------------------------------------
# PDUFA -> NCT linking: extract a drug/trial name from filing text, then
# resolve it against ClinicalTrials.gov via clinical_trials_api.search_studies()
# --------------------------------------------------------------------------

# Trial acronyms (e.g. "MAPLE-HCM", "SEQUOIA-HCM"): ALL-CAPS, often hyphenated.
_TRIAL_ACRONYM_RE = re.compile(r"\b[A-Z]{3,}(?:-[A-Z0-9]{1,8}){1,2}\b")

# Common INN (International Nonproprietary Name) stems for drug names that
# won't look like an acronym (e.g. "aficamten", "osimertinib"). Not
# exhaustive — this is a best-effort net, not a drug-name dictionary.
_INN_SUFFIXES = (
    "mab", "zumab", "ximab", "tinib", "nib", "parib", "ciclib", "vir",
    "navir", "prazole", "sartan", "olol", "camten", "parin", "statin",
    "cillin", "mycin", "dronavir", "degib", "lisib", "vastatin",
)
_INN_DRUG_NAME_RE = re.compile(
    r"\b[a-z]{4,24}(?:" + "|".join(_INN_SUFFIXES) + r")\b"
)

# Generic false positives the acronym regex would otherwise pick up from
# boilerplate (legal/forward-looking-statements language, not a trial name).
_ACRONYM_STOPWORDS = {
    "FDA", "SEC", "NDA", "BLA", "PDUFA", "EDGAR", "GAAP", "CEO", "CFO",
    "LLC", "INC", "USA", "EU", "UK",
}

# _NDA_FOR_RE's "for X" capture needs this: "NDA for the treatment of..."
# or "application for our lead program..." grab a filler word, not a drug
# name. Verified live necessary — PRAX and VERA's filings both phrased it
# this way and the filler word "the" alone, searched against CT.gov, was
# permissive enough to return a (wrong) match rather than zero results.
_FILLER_WORD_STOPWORDS = {
    "the", "a", "an", "our", "its", "this", "that", "these", "those",
    "treatment", "use", "patients", "patient", "drug", "therapy",
    "approval", "review", "priority",
    # generic adjectives that precede "study"/"trial" without naming one
    # (STUDY_TRIAL_NAME false positive, verified live: "Clinical trial
    # collaboration..." in an INOVIO filing matched "Clinical" as if it
    # were a trial name)
    "clinical", "preclinical", "pivotal", "ongoing", "global",
    "registrational", "international", "additional", "upcoming",
    "related", "new", "potential", "planned",
    # seen live once the search was widened: "sBLA for adults with...",
    # "NDA for relapsed...", "application for full approval" each matched a
    # trial as if the word were a drug name
    "adults", "adult", "pediatric", "children", "people", "relapsed",
    "refractory", "full", "accelerated", "traditional", "certain", "both",
    "all", "moderate", "severe", "advanced", "metastatic", "newly",
    "previously", "first", "second", "once", "use", "review",
}

# Highest-precision pattern: "NDA/BLA for X", searched only within a
# window around a "PDUFA" mention (not whole-document — an unrelated NDA
# can be named elsewhere, e.g. a combination-therapy comparator).
# Deliberately NOT anchored to a specific verb like "accepted" — verified
# live that two real filings phrase this differently ("...for MAPLE-HCM
# was accepted for filing... with a PDUFA date" vs. Corcept's "NDA for
# relacorilant in platinum-resistant ovarian cancer... has a... PDUFA
# date"); requiring "accepted" immediately after missed the second case.
_NDA_FOR_RE = re.compile(
    r"(?:New Drug Application|Biologics License Application|"
    r"supplemental New Drug Application|NDA|BLA|sNDA)"
    r"(?:\s*\([A-Za-z]{2,10}\))?"  # tolerates "...Application (BLA) for X"
    r"\s+for\s+([A-Za-z][\w‐-]{1,30})",
    re.IGNORECASE,
)

# Second-highest precision: "<NAME> study/trial" — catches bare (non-
# hyphenated) trial names like "ROSELLA" that _TRIAL_ACRONYM_RE's
# hyphen requirement misses, without the false-positive risk of matching
# any bare all-caps word in the document.
_STUDY_TRIAL_NAME_RE = re.compile(r"\b([A-Z][A-Za-z]{3,})\s+(?:study|trial)\b")


_PDUFA_MENTION_RE = re.compile(r"PDUFA", re.IGNORECASE)
_PROXIMITY_WINDOW = 300  # chars on each side of a "PDUFA" mention to search within


def _extract_drug_candidates(text: str, max_candidates: int = 3) -> list[tuple[str, str]]:
    """
    Best-effort extraction of candidate drug/trial names from a PDUFA 8-K's
    free text. A press release's boilerplate "about our pipeline" section
    often names several unrelated trials — pulling candidates from the
    whole document in document order picks those up ahead of the one
    actually tied to this PDUFA date. So this scans a window AROUND each
    "PDUFA" mention first, and only falls back to whole-document order if
    nothing turns up nearby.

    All patterns below are scoped to a window around each "PDUFA" mention,
    not whole-document — a press release can name an unrelated drug/trial
    elsewhere (a combination-therapy comparator, a different program in
    the pipeline section), and whole-document order would surface that
    instead.

    Two phases, not a flat precision ranking — this distinction turned out
    to matter more than relative tier order:

      Phase 1 — ANCHORED patterns, grammatically tied to specific wording
      ("NDA/BLA for X", "X study"/"X trial"). Tried together; ALL anchored
      hits are collected before any unanchored pattern runs at all.

      Phase 2 — UNANCHORED patterns (bare ALL-CAPS acronym shape, bare
      INN-suffix compound name) — only tried if Phase 1 found nothing in
      this window. These just match a token *shape* anywhere in the
      window, with no grammatical tie to the PDUFA sentence, so they're
      vulnerable to grabbing an unrelated trial/compound the press
      release happens to also mention nearby (e.g. a prior-quarter data
      readout bullet crammed into the same headline block).

    Verified live why this has to be two phases rather than one ranked
    list: CYTK's headline block contains BOTH "...ACACIA-HCM Met Dual
    Primary Endpoints..." (unrelated) AND "...NDA for MAPLE-HCM Accepted
    for Filing... with PDUFA Date..." (the real one) in the same 300-char
    window. A flat ranking that puts the unanchored ACRONYM pattern
    anywhere above NDA_FOR risks matching ACACIA-HCM first, purely
    because it happens to sit earlier in the text. Running NDA_FOR (an
    anchored pattern) to completion before ACRONYM ever runs fixes that.
    Separately, CORT's filing has both "NDA for relacorilant" (ambiguous
    bare compound) and "our ROSELLA study" (specific trial) as two
    anchored hits in the same window — STUDY_TRIAL_NAME is tried before
    NDA_FOR within Phase 1 so the specific trial name wins there too.

    Known failure mode, not hidden: a press release with no anchored or
    unanchored match near the PDUFA sentence yields zero candidates —
    resolution is then skipped for that event, not guessed.

    Returns (candidate_text, tier_name) pairs — see CatalystEvent.resolution_tier
    for what each tier means for confidence.
    """
    ANCHORED = (
        ("STUDY_TRIAL_NAME", _STUDY_TRIAL_NAME_RE),
        ("NDA_FOR", _NDA_FOR_RE),
    )
    UNANCHORED = (
        ("ACRONYM", _TRIAL_ACRONYM_RE),
        ("INN_SUFFIX", _INN_DRUG_NAME_RE),
    )

    def _scan_tiers(segment: str, tiers) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        seen_tokens: set[str] = set()
        for tier, pattern in tiers:
            for match in pattern.finditer(segment):
                token = match.group(1) if match.groups() else match.group(0)
                token = token.rstrip(".,;:")
                if (
                    token.upper() in _ACRONYM_STOPWORDS
                    or token.lower() in _FILLER_WORD_STOPWORDS
                    or token in seen_tokens
                ):
                    continue
                seen_tokens.add(token)
                # Re-tag: an NDA_FOR capture that is ITSELF acronym-shaped
                # (e.g. "NDA for MAPLE-HCM") is just as unambiguous as a
                # direct ACRONYM-tier match — it only came through NDA_FOR
                # because that pattern happened to fire first in this
                # window. Only a genuinely bare word/compound name (e.g.
                # "NDA for relacorilant") should carry NDA_FOR's medium-
                # confidence label. Verified live this mislabeling is real:
                # CYTK's own ground-truth MAPLE-HCM resolution was tagged
                # NDA_FOR and excluded from pipeline.py's high-confidence
                # auto-run bucket purely on this technicality.
                effective_tier = tier
                if tier == "NDA_FOR" and _TRIAL_ACRONYM_RE.fullmatch(token):
                    effective_tier = "ACRONYM"
                found.append((token, effective_tier))
                if len(found) >= max_candidates:
                    return found
        return found

    def _scan(segment: str) -> list[tuple[str, str]]:
        return _scan_tiers(segment, ANCHORED) or _scan_tiers(segment, UNANCHORED)

    for mention in _PDUFA_MENTION_RE.finditer(text):
        start = max(mention.start() - _PROXIMITY_WINDOW, 0)
        end = min(mention.end() + _PROXIMITY_WINDOW, len(text))
        candidates = _scan(text[start:end])
        if candidates:
            return candidates

    return _scan(text)  # fallback: whole-document scan


def _normalize_company_name(name: str) -> str:
    """Strips legal-entity suffixes and punctuation for fuzzy sponsor matching."""
    cleaned = re.sub(
        r"\b(incorporated|inc|corporation|corp|co|ltd|llc|plc|holdings?)\.?\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[^a-z0-9 ]", "", cleaned.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _sponsor_matches(company_name: Optional[str], candidate_sponsor: Optional[str]) -> bool:
    if not company_name or not candidate_sponsor:
        return False
    a, b = _normalize_company_name(company_name), _normalize_company_name(candidate_sponsor)
    if not a or not b:
        return False
    return a in b or b in a or a.split(" ")[0] == b.split(" ")[0]


async def _resolve_drug_candidate_to_nct(
    candidates: list[tuple[str, str]], company_name: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Tries each (candidate, tier) pair (in precision order) against
    ClinicalTrials.gov, scoped by sponsor, and accepts the first hit
    whose sponsor actually matches the filer. Returns
    (resolved_nct_id, matched_candidate_name, resolution_tier) — all
    None if nothing resolved confidently.
    """
    if not company_name:
        return None, None, None  # no sponsor to verify against — don't guess

    # CT.gov's query.spons filter is brittle on legal-entity suffixes —
    # verified live: "CYTOKINETICS INC" returns 0 hits, "Cytokinetics"
    # (normalized) returns the correct one. Normalize before using it as
    # a server-side filter; _sponsor_matches() below re-normalizes both
    # sides anyway as a belt-and-suspenders client-side check.
    normalized_sponsor_filter = _normalize_company_name(company_name)

    for candidate, tier in candidates:
        try:
            hits = await search_studies(candidate, sponsor=normalized_sponsor_filter, page_size=5)
        except httpx.HTTPError as exc:
            logger.warning("CT.gov lookup failed for candidate %r: %s", candidate, exc)
            continue

        matching = [hit for hit in hits if _sponsor_matches(company_name, hit.sponsor)]
        if matching:
            if tier in ("STUDY_TRIAL_NAME", "ACRONYM"):
                # These two tiers are trusted enough to be scored unattended,
                # so they must really identify ONE trial. A text search for
                # "HARMONi" also returns HARMONi-3 and HARMONi-7; only a
                # trial whose registered acronym IS the name counts. Anything
                # looser keeps its best guess but is labelled unconfirmed,
                # which sends it to manual review instead of auto-scoring.
                exact = next(
                    (hit for hit in matching if (hit.acronym or "").strip().lower() == candidate.lower()),
                    None,
                )
                if exact is not None:
                    return exact.nct_id, candidate, tier
                return matching[0].nct_id, candidate, "NAME_UNCONFIRMED"
            return matching[0].nct_id, candidate, tier

        await asyncio.sleep(0.1)  # courtesy throttle between CT.gov calls

    return None, None, None


async def fetch_pdufa_catalysts(
    lookback_days: int = c.EDGAR_PDUFA_LOOKBACK_DAYS,
    problems: Optional[list[str]] = None,
) -> list[CatalystEvent]:
    """
    Searches SEC EDGAR full-text search for recent 8-K / 6-K exhibits
    disclosing a PDUFA date, fetches each filing, and extracts every
    PDUFA date it states.

    ONE EVENT PER COMPANY PER DATE. Filings arrive newest-first, and a
    company restates its PDUFA date in most later filings, so for each
    (company, date) the newest filing that states it is the one kept. A
    company with two drugs under review yields two events. Known
    weakness, not hidden: if the FDA moves a date, the company's older
    filings still state the old one, and both dates appear as events
    until the old filing ages out of the search window.

    Trial matching (the ClinicalTrials.gov lookups) is only done for
    dates that are still ahead — a past PDUFA date is kept as a record
    but will never be traded, so it isn't worth the requests.

    If `problems` is given, a one-line description is appended to it when
    filings could not be fetched — see ingest_catalysts().
    """
    headers = {"User-Agent": _build_user_agent()}
    events: dict[tuple[str, date], CatalystEvent] = {}  # (cik, PDUFA date) -> newest filing stating it
    attempted = failed = 0
    today = date.today()

    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        hits = await _search_pdufa_filings(client, lookback_days, problems=problems)

        for hit in hits:
            source = hit.get("_source", {})
            ciks = source.get("ciks") or []
            if not ciks:
                continue
            cik = ciks[0]

            doc_url = _filing_doc_url(cik, hit["_id"])
            attempted += 1
            try:
                text = await _fetch_filing_text(client, doc_url)
            except httpx.HTTPError as exc:
                failed += 1
                logger.warning("Failed to fetch filing %s: %s", doc_url, exc)
                continue
            await asyncio.sleep(0.15)  # courtesy throttle, well under SEC's 10 req/s cap

            filed = None
            try:
                filed = date.fromisoformat(source.get("file_date", ""))
            except ValueError:
                pass

            ticker, company_name = _parse_ticker_company(source.get("display_names", []))
            form = source.get("form") or (source.get("root_forms") or ["filing"])[0]

            dates = _extract_pdufa_dates(text, filed)
            for index, (pdufa_date, position) in enumerate(dates):
                key = (cik, pdufa_date)
                if key in events:
                    continue  # a newer filing already gave us this company + date

                resolved_nct_id = matched_candidate = resolution_tier = None
                if pdufa_date >= today:
                    # Look for the drug name around THIS date's mention, so a
                    # filing covering two drugs ties each date to its own.
                    nearby = text[max(position - _PROXIMITY_WINDOW, 0): position + _PROXIMITY_WINDOW]
                    candidates = _extract_drug_candidates(nearby)
                    if not candidates and len(dates) == 1:
                        # Only one PDUFA date in the filing, so a drug named
                        # anywhere in it is very likely the one — widen out.
                        # With several dates this would guess which drug
                        # goes with which date, so it is not done then.
                        candidates = _extract_drug_candidates(text)
                    resolved_nct_id, matched_candidate, resolution_tier = await _resolve_drug_candidate_to_nct(
                        candidates, company_name
                    )

                events[key] = CatalystEvent(
                    event_type="PDUFA",
                    ticker=ticker,
                    company_name=company_name,
                    cik=cik,
                    title=f"PDUFA date disclosed in {form} ({source.get('file_date', 'unknown date')})",
                    start_date=pdufa_date,
                    # The first date in a filing keeps the plain filing URL (so
                    # rows saved by earlier versions stay the same row); further
                    # dates in the same filing need their own key in the database.
                    source_url=doc_url if index == 0 else f"{doc_url}#pdufa-{pdufa_date.isoformat()}",
                    raw_source="SEC_EDGAR_FTS",
                    drug_or_trial_candidate=matched_candidate,
                    resolved_nct_id=resolved_nct_id,
                    resolution_tier=resolution_tier,
                )

    if failed and problems is not None:
        problems.append(f"PDUFA: {failed} of {attempted} SEC filings could not be fetched")

    return list(events.values())


# --------------------------------------------------------------------------
# AdCom enrichment: which drug and company is a meeting about?
# --------------------------------------------------------------------------

# The calendar row only says e.g. "Oncologic Drugs Advisory Committee
# Meeting". The meeting's own page states the agenda in a fixed legal
# form, verified live: "The Committee will discuss new drug application
# (NDA) 219694, for atropine sulfate ophthalmic solution 0.01%, submitted
# by Sydnexis. Inc. The proposed indication..."
_ADCOM_AGENDA_RE = re.compile(
    # [^.;]{0,60}? skips the application number in whatever form it takes
    # ("(NDA) 219694,", "(sNDA) 215152/S-012") up to the first "for".
    r"(?:application|NDA|BLA|sNDA|sBLA)s?\b[^.;]{0,60}?\bfor\s+"
    # The drug text may contain a decimal ("0.01%") but not a sentence end:
    # without that, "...application process for new members. Comments may
    # be submitted by the public" parses as a drug and a sponsor.
    r"((?:[^.;]|\.(?=\d)){3,160}?),?\s+submitted\s+by\s+(.{2,90}?)"
    r"(?=,\s+for\b|\s+for\s+the\b|\.\s+[A-Z]|\.\s*$|;)",
    re.IGNORECASE,
)
# The agenda names the product as "<drug> <dosage form> <strength>"; only
# the drug name is useful for a trial search.
_DOSAGE_WORD_RE = re.compile(
    r"\s+(?:\(|injection|tablets?|capsules?|ophthalmic|oral|topical|intravenous|subcutaneous|"
    r"solution|suspension|gel|cream|spray|for\b|\d).*$",
    re.IGNORECASE,
)
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


def _parse_adcom_agenda(page_text: str) -> Optional[tuple[str, str]]:
    """(drug name, company name) from a meeting page, or None if the page
    doesn't state an application — many meetings are about policy,
    vaccine strain selection or a drug class, with no single sponsor."""
    match = _ADCOM_AGENDA_RE.search(page_text)
    if not match:
        return None
    drug = _DOSAGE_WORD_RE.sub("", match.group(1).strip()).strip(" ,.")
    company = match.group(2).strip(" ,.")
    return (drug, company) if drug and company else None


async def _enrich_adcom_events(events: list[CatalystEvent], tickers: Optional[TickerIndex]) -> int:
    """
    For each UPCOMING advisory committee meeting, reads the meeting page
    and fills in the company, its ticker, and — where a trial can be
    matched — the trial. Returns how many meetings got a ticker.

    The trial match is tagged "ADCOM_AGENDA", which pipeline.py does not
    treat as high confidence: the agenda names a drug, not a trial, and
    one drug usually has several trials. So a matched meeting shows up as
    "needs manual review" with the ticker and drug filled in, rather than
    being scored on a possibly wrong trial.
    """
    today = date.today()
    upcoming = [e for e in events if e.event_type == "ADCOM" and e.start_date >= today
                and e.source_url.startswith("https://www.fda.gov/")]
    enriched = 0
    async with httpx.AsyncClient(headers={"User-Agent": _BROWSER_UA}, timeout=30.0,
                                 follow_redirects=True) as client:
        for event in upcoming:
            try:
                resp = await client.get(event.source_url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("AdCom page fetch failed for %s: %s", event.source_url, type(exc).__name__)
                continue
            agenda = _parse_adcom_agenda(BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True))
            if agenda is None:
                continue
            drug, company = agenda
            event.company_name = company
            event.drug_or_trial_candidate = drug
            event.ticker = tickers.lookup(company) if tickers else None
            if event.ticker:
                enriched += 1
                nct_id, _, tier = await _resolve_drug_candidate_to_nct([(drug, "ADCOM_AGENDA")], company)
                event.resolved_nct_id, event.resolution_tier = nct_id, tier
            await asyncio.sleep(0.5)
    return enriched


# --------------------------------------------------------------------------
# Phase A: horizon classification (shared by both adapters)
# --------------------------------------------------------------------------

def classify_catalyst_window(event: CatalystEvent, today: Optional[date] = None) -> str:
    """
    Tags each event per the manual's Multi-Horizon Catalyst Filters:
      PDUFA : 35-45 days out  -> "PDUFA_ENTRY_WINDOW"
      ADCOM : 55-70 days out  -> "ADCOM_ENTRY_WINDOW"
      otherwise               -> "OUTSIDE_WINDOW"
    Mutates and returns event.horizon_status.
    """
    today = today or date.today()
    days_out = (event.start_date - today).days

    if event.event_type == "PDUFA" and c.PDUFA_WINDOW_MIN_DAYS <= days_out <= c.PDUFA_WINDOW_MAX_DAYS:
        event.horizon_status = "PDUFA_ENTRY_WINDOW"
    elif event.event_type == "ADCOM" and c.ADCOM_WINDOW_MIN_DAYS <= days_out <= c.ADCOM_WINDOW_MAX_DAYS:
        event.horizon_status = "ADCOM_ENTRY_WINDOW"
    elif event.event_type == "PHASE_3_READOUT":
        event.horizon_status = c.PHASE_3_STATUS_TAG
    else:
        event.horizon_status = "OUTSIDE_WINDOW"

    return event.horizon_status


async def _fetch_adcom_calendar_with_retry() -> list[CatalystEvent]:
    """One retry: the FDA page intermittently fails to render its table
    within the timeout when loaded from a hosted server."""
    try:
        return await fetch_adcom_calendar()
    except Exception as exc:  # noqa: BLE001 — retried once, then re-raised
        logger.warning("ADCOM adapter failed, retrying once: %s", exc)
        await asyncio.sleep(5)
        return await fetch_adcom_calendar()


async def ingest_catalysts(
    pdufa_lookback_days: int = c.EDGAR_PDUFA_LOOKBACK_DAYS,
    problems: Optional[list[str]] = None,
) -> list[CatalystEvent]:
    """
    Top-level entry point: runs the AdCom and PDUFA adapters concurrently,
    then fills in each upcoming AdCom's company / ticker / drug from its
    meeting page, then adds the Phase 3 readout watch list
    (ingestion/phase3_readouts.py). Tags every event with its horizon
    window and returns the combined list (unfiltered by window — callers
    decide what to do with OUTSIDE_WINDOW events, e.g. keep monitoring
    vs. evict).

    A failed source does not raise — the other source's events are still
    returned. Pass a list as `problems` to find out: each source that
    failed, fully or partly, appends one line to it. Without that, a scan
    that reached neither source is indistinguishable from a day with no
    catalysts.
    """
    adcom_events, pdufa_events = await asyncio.gather(
        _fetch_adcom_calendar_with_retry(),
        fetch_pdufa_catalysts(pdufa_lookback_days, problems=problems),
        return_exceptions=True,
    )

    all_events: list[CatalystEvent] = []
    for result, label in ((adcom_events, "ADCOM"), (pdufa_events, "PDUFA")):
        if isinstance(result, Exception):
            logger.error("%s adapter failed: %s", label, result)
            if problems is not None:
                problems.append(f"{label}: source unavailable ({type(result).__name__})")
            continue
        all_events.extend(result)

    # Both steps below need company -> ticker, and both are additions to
    # the two core sources: a failure here is reported, never fatal.
    tickers: Optional[TickerIndex] = None
    try:
        tickers = await fetch_ticker_index(_build_user_agent())
    except Exception as exc:  # noqa: BLE001
        logger.error("Ticker list unavailable: %s", type(exc).__name__)
        if problems is not None:
            problems.append(f"TICKERS: SEC company list unavailable ({type(exc).__name__})")

    if tickers is not None:
        for event in all_events:
            if event.event_type == "PDUFA" and not event.ticker:
                event.ticker = tickers.lookup(event.company_name)

    try:
        await _enrich_adcom_events(all_events, tickers)
    except Exception as exc:  # noqa: BLE001
        logger.error("AdCom enrichment failed: %s", type(exc).__name__)

    if tickers is not None:
        try:
            all_events.extend(await fetch_phase3_readouts(tickers))
        except Exception as exc:  # noqa: BLE001
            logger.error("Phase 3 watch list failed: %s", exc)
            if problems is not None:
                problems.append(f"PHASE3: source unavailable ({type(exc).__name__})")

    today = date.today()
    for event in all_events:
        classify_catalyst_window(event, today)

    return all_events
