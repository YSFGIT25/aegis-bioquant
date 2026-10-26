"""
phase3/rules.py
----------------
Which rules produced a stored Phase 3 score: the same idea as
config/rules.py, kept separate so a change to the Phase 3 forecasts can
never touch the FDA-decision study's version or fingerprint.

  RULES_VERSION   0 until the Phase 3 rules are registered on OSF: every
                  score stored under 0 is a trial run. Raised by hand
                  whenever a change alters what would be scored.
  fingerprint()   a hash of every other file in this package plus the
                  review model, so an unlogged change still shows in the
                  data.

HOW TO CHANGE A RULE: as in config/rules.py. Make the change; if it
changes behaviour raise RULES_VERSION and add a RULES_LOG entry; set
RECORDED_FINGERPRINT to the new fingerprint(); for a new version tag the
commit phase3-rules-vN.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from aegis_bioquant.config import settings
from aegis_bioquant.config.rules import _code_tokens

RULES_VERSION = 0

RULES_LOG: list[tuple[int, str, str]] = [
    (0, "2026-10-26",
     "Trial run, not registered. Every Phase 3 trial of a listed company with an estimated primary completion "
     "date from 180 days ago to 90 days ahead, share price $2+, is reviewed by an AI model (filings, the trial "
     "registry and a web search: the science, who is behind the trial, independent expert views, warning signs), "
     "starting from a 58% base rate. One score per trial per day. Results recorded automatically from the "
     "company's 8-K/6-K filings: MET only if every primary endpoint was met with statistical significance (or the "
     "trial was stopped early for efficacy); anything else NOT_MET; ended for non-efficacy reasons STOPPED, not "
     "graded. Announcement time: midnight US Eastern on the filing date; the last score 24h before it is graded."),
]

RECORDED_FINGERPRINT = "288a5c9cfdef"

_HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def fingerprint() -> str:
    digest = hashlib.sha256()
    # This file is left out, as config/rules.py is from the study's
    # fingerprint: it records the fingerprint, so it can't be part of it.
    for path in sorted(p for p in _HERE.glob("*.py") if p.name != "rules.py"):
        digest.update(path.name.encode())
        digest.update("\x00".join(_code_tokens(path.read_text(encoding="utf-8"))).encode())
    digest.update(repr(settings.LLM_REVIEW_MODEL).encode())
    return digest.hexdigest()[:12]


def unlogged_change() -> bool:
    return fingerprint() != RECORDED_FINGERPRINT


def stamp() -> dict:
    return {"rules_version": RULES_VERSION, "rules_fingerprint": fingerprint()}
