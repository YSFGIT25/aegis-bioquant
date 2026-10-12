"""
config/rules.py
-----------------
Which version of the rules produced a stored score.

The scores this system stores before each FDA decision are only a fair
test of it if one can tell, afterwards, which rules were in force when
each was made. A score made under one set of thresholds and a score made
under another are not the same experiment.

Two things are stamped on every stored prediction and evidence review:

  RULES_VERSION   a number, raised by hand whenever a change alters what
                  the system would score, flag or alert on. RULES_LOG
                  says, with a date, what each version changed.

  fingerprint()   a short hash of the code and settings that decide a
                  score (see _RULE_FILES). It changes whenever any of
                  them does, so a change that was made without raising
                  the version still shows in the stored data.

HOW TO CHANGE A RULE
  1. Make the change.
  2. If it changes behaviour: raise RULES_VERSION and add a RULES_LOG
     entry dated the day it is deployed.
  3. Set RECORDED_FINGERPRINT to the new fingerprint() — also for a
     change that alters no behaviour (a reworded message, a refactor),
     which needs no new version.
  4. Commit, and for a new version tag the commit: git tag rules-vN.
     The tag is how the exact code behind a version is found again.
Until step 3 is done, every scan reports "rules changed without being
logged" (unlogged_change()).
"""

from __future__ import annotations

import hashlib
import io
import tokenize
from functools import lru_cache
from pathlib import Path

from aegis_bioquant.config import settings

RULES_VERSION = 1

# (version, date it took effect, what changed). Newest last; never edit
# an old entry.
RULES_LOG: list[tuple[int, str, str]] = [
    (1, "2026-10-12",
     "First numbered version; scores stored before this date carry no version and are a trial run. "
     "Rules in force: the evidence score is an AI model's reading of the company's filings (the registry "
     "point system when there is no review). Buy setup: evidence 75+, expected move 10%+, options cost "
     "under 30. Stock-only alert: evidence 90+ on a review that is not thin, with options expensive "
     "(over 50) or thinly held (open interest under 2,000). Avoid: evidence 40 or under with an expected "
     "move of 10%+ and options cost over 50. Entry windows: 35-45 days before an FDA decision date, 55-70 before an advisory "
     "committee. Company value at most $20 billion, share price $2+, options open interest over 2,000, "
     "daily options volume over 200."),
]

# fingerprint() as of the last RULES_LOG entry (or the last change that
# altered no behaviour). See "HOW TO CHANGE A RULE" above.
RECORDED_FINGERPRINT = "a185ea010c25"

_PACKAGE = Path(__file__).resolve().parent.parent
# The code that decides a score, a verdict or an alert. Ingestion (which
# decisions are found) and the website (how they are shown) are left out.
_RULE_FILES = ("config/constants.py", "pipeline.py")
_RULE_DIRS = ("logic", "engines")
_SKIPPED = (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING)
_LINE_STARTS = (None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)


def _code_tokens(source: str) -> list[str]:
    """The source as its tokens, without comments, blank lines or
    docstrings, so that rewording an explanation is not a rule change."""
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type not in _SKIPPED]
    kept: list[str] = []
    for i, tok in enumerate(tokens):
        before = tokens[i - 1].type if i else None
        after = tokens[i + 1].type if i + 1 < len(tokens) else None
        if tok.type == tokenize.STRING and before in _LINE_STARTS and after == tokenize.NEWLINE:
            continue   # a string standing alone on a line: a docstring
        kept.append(tokenize.tok_name[tok.type] if tok.type in _LINE_STARTS[1:] else tok.string)
    return kept


@lru_cache(maxsize=1)
def fingerprint() -> str:
    paths = [_PACKAGE / name for name in _RULE_FILES]
    for folder in _RULE_DIRS:
        paths += sorted((_PACKAGE / folder).rglob("*.py"))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(_PACKAGE).as_posix().encode())
        digest.update("\x00".join(_code_tokens(path.read_text(encoding="utf-8"))).encode())
    # Set outside the code, on the host: which model judges the evidence.
    digest.update(repr((settings.LLM_REVIEW_MODEL, settings.LLM_IDENTIFY_MODEL, settings.LLM_REVIEW_WEB_SEARCH)).encode())
    return digest.hexdigest()[:12]


def unlogged_change() -> bool:
    """True when the rules differ from those last written up in RULES_LOG."""
    return fingerprint() != RECORDED_FINGERPRINT


def stamp() -> dict:
    """The two columns stored with every prediction and evidence review."""
    return {"rules_version": RULES_VERSION, "rules_fingerprint": fingerprint()}
