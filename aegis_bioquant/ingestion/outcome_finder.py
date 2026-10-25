"""
ingestion/outcome_finder.py
----------------------------
What did the FDA decide? Finds the evidence for a catalyst's outcome in
the company's own SEC filings.

WHY SEC FILINGS: an approval or a rejection is material news, so the
company reports it in an 8-K (6-K for foreign companies), usually the
same or next business day. The FDA's own databases list approvals only
after a delay and do not systematically list rejections at all, so they
cannot answer "was it rejected?".

WHAT THIS PRODUCES IS A SUGGESTION, NEVER A RECORDED OUTCOME. The phrase
matching below is a reader of press-release English, and it is wrong in
ordinary ways:
  - a company with several drugs announces an approval for a DIFFERENT
    drug in the same weeks;
  - "if approved", "following approval", "prior approval of..." are
    about other things entirely;
  - a filing can mention a past rejection while announcing an approval.
So the result is only a suggestion. Since 2026-10-25 it is confirmed
automatically only when the FDA's own records agree (core/auto_confirm.py);
before that, an administrator decided on the website's review page.
Nothing downstream (track record, calibration) reads an unconfirmed
suggestion. core/database.CatalystOutcomeRecord holds both states.

OUTCOMES:
    APPROVED   the FDA approved the application
    CRL        a Complete Response Letter — the FDA's rejection
    DELAYED    the FDA moved the decision date
(An administrator can also record WITHDRAWN or INVALID — "this was never
a real decision date" — which the finder does not try to detect.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx

from aegis_bioquant.config import constants as c

APPROVED, CRL, DELAYED = "APPROVED", "CRL", "DELAYED"

# Each pattern is anchored so that it reads as a statement that the thing
# HAPPENED. Bare "approved" / "approval" is deliberately not enough: most
# filings in the weeks around a decision say "if approved" many times.
_FDA = r"(?:FDA|U\.S\. Food and Drug Administration|Food and Drug Administration(?:\s*\(FDA\))?)"
# A gap inside one sentence that may still contain "U.S." — press
# releases write "Announces U.S. FDA Approval", and a gap that stops at
# every full stop cannot get from the verb to "FDA".
_GAP60 = r"(?:U\.S\.|[^.]){0,60}?"
_PATTERNS: list[tuple[str, re.Pattern]] = [
    (CRL, re.compile(
        r"(?:received|issued|receipt of)\b[^.]{0,80}\bcomplete response letter|"
        r"complete response letter\b[^.]{0,60}\b(?:from|issued by)\b[^.]{0,20}" + _FDA,
        re.IGNORECASE)),
    (DELAYED, re.compile(
        r"extend(?:ed|s|ing)\b[^.]{0,40}\b(?:PDUFA|target action|goal|review)\b[^.]{0,30}\b(?:date|period)|"
        r"extension of\b[^.]{0,20}\b(?:PDUFA|target action)\b[^.]{0,30}\bdate|"
        r"(?:new|revised|updated)\s+PDUFA\b[^.]{0,30}\bdate|"
        r"(?:PDUFA|target action)\b[^.]{0,30}\bdate\b[^.]{0,30}\b(?:has been|was)\s+extended",
        re.IGNORECASE)),
    (APPROVED, re.compile(
        # "(FDA) approved therapies" / "FDA approved products" is an
        # adjective, not an event — seen live in "there are currently no
        # FDA approved therapies for...". Hence the (?!...) after "approved".
        _FDA + r"\s+(?:has\s+|today\s+)?(?:approved(?!\s+(?:therap|treatment|product|drug|medicine|medication|option|indication|label))"
        r"|approves|granted\s+(?:accelerated\s+|full\s+|traditional\s+)?approval)\b|"
        r"\b(?:announce[sd]?|received|receives|obtained|secures?|secured)\b" + _GAP60 + r"\b" + _FDA + r"\b[^.]{0,20}\bapproval\b|"
        r"\b(?:was|has been|is now)\s+approved by the\s+" + _FDA,
        re.IGNORECASE)),
]
# Words that turn a match into a hypothetical ("if approved", "we expect
# the FDA approves") or a look back at an earlier decision ("following
# receipt of a complete response letter in 2024"). Checked in the same
# sentence, before the match.
_HEDGE_RE = re.compile(
    r"\b(?:if|once|upon|pending|potential|potentially|anticipat\w*|expect\w*|subject to|"
    r"seeking|seeks|may|might|could|would|should|whether|until|prior|"
    r"following|after|previously|previous|earlier|since|no|not|none|without)\b",
    re.IGNORECASE,
)
# Sentence boundaries for the hedge check and the quoted evidence. "U.S."
# is not one.
_SENTENCE_END_RE = re.compile(r"(?<!U\.S)(?<!\bU\.S\.)[.!?]\s+")


@dataclass
class OutcomeEvidence:
    outcome: str            # APPROVED | CRL | DELAYED
    decision_date: date     # the filing's date — the decision is that day or the business day before
    filing_url: str
    sentence: str           # the sentence the outcome was read from, for a person to judge


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The sentence around text[start:end]."""
    begin = 0
    for boundary in _SENTENCE_END_RE.finditer(text, 0, start):
        begin = boundary.end()
    following = _SENTENCE_END_RE.search(text, end)
    return begin, (following.start() + 1 if following else len(text))


def classify_filing_text(text: str) -> Optional[tuple[str, str]]:
    """
    (outcome, sentence) for the first statement in `text` that an FDA
    decision happened, or None. A rejection or a delay is looked for
    before an approval: a filing announcing a CRL usually still contains
    the word "approval", while the reverse is rare.
    """
    for outcome, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            begin, finish = _sentence_bounds(text, match.start(), match.end())
            if _HEDGE_RE.search(text[begin:match.start()]):
                continue  # "if the FDA approves...", "following receipt of a complete response letter..."
            sentence = re.sub(r"\s+", " ", text[begin:finish]).strip()
            return outcome, sentence[:400]
    return None


async def find_outcome_evidence(
    client: httpx.AsyncClient,
    cik: str,
    catalyst_date: date,
    today: Optional[date] = None,
) -> Optional[OutcomeEvidence]:
    """
    Searches the company's 8-K / 6-K filings from OUTCOME_LOOKBACK_DAYS
    before the catalyst date (the FDA sometimes decides early) up to
    today. Of the filings opened that state a decision, returns the
    EARLIEST-filed one: a company repeats an approval in every later
    filing (seen live — a quarterly results release two weeks after the
    fact), and the first announcement is the one whose date is the
    decision date. None if no filing states one — which before the date
    is the normal case, and after it means the company hasn't filed yet
    or phrased it in a way this doesn't recognise.

    At most OUTCOME_MAX_FILINGS_PER_CATALYST filings are opened, those
    filed nearest the catalyst date. Raises httpx.HTTPError if the
    search itself can't be reached.
    """
    from aegis_bioquant.ingestion.fda_scraper import (
        EDGAR_FTS_URL,
        _fetch_filing_text,
        _filing_doc_url,
        _get_with_retry,
    )

    today = today or date.today()
    params = {
        "q": '"Complete Response Letter" OR "approval" OR "approved" OR "PDUFA"',
        "forms": "8-K,6-K",
        "ciks": f"{int(cik):010d}",
        "dateRange": "custom",
        "startdt": (catalyst_date - timedelta(days=c.OUTCOME_LOOKBACK_DAYS)).isoformat(),
        "enddt": today.isoformat(),
    }
    resp = await _get_with_retry(client, f"{EDGAR_FTS_URL}?{urlencode(params)}")
    resp.raise_for_status()
    dated = []
    for hit in resp.json().get("hits", {}).get("hits", []):
        try:
            dated.append((date.fromisoformat(hit.get("_source", {}).get("file_date", "")), hit))
        except ValueError:
            continue
    # Open the filings nearest the catalyst date, not the newest ones. The
    # announcement is made within days of the decision; for a date months
    # back, the newest filings are all later quarterly reports that only
    # repeat it (seen live: several outcomes "dated" to an earnings release
    # weeks after the fact, and one read off an executive's biography).
    dated.sort(key=lambda pair: abs((pair[0] - catalyst_date).days))

    earliest: Optional[OutcomeEvidence] = None
    for filed, hit in dated[: c.OUTCOME_MAX_FILINGS_PER_CATALYST]:
        url = _filing_doc_url(cik, hit["_id"])
        try:
            text = await _fetch_filing_text(client, url)
        except httpx.HTTPError:
            continue
        found = classify_filing_text(text)
        if found is not None and (earliest is None or filed < earliest.decision_date):
            outcome, sentence = found
            earliest = OutcomeEvidence(outcome=outcome, decision_date=filed, filing_url=url, sentence=sentence)
    return earliest
