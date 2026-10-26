"""
phase3/
--------
Forecasts for Phase 3 trial results: the chance each upcoming Phase 3
trial meets its primary endpoint. A separate track from the FDA-decision
forecasts, with its own rules, its own fingerprint and its own tables.

WHY A SEPARATE PACKAGE: the FDA-decision forecasts are a pre-registered
study (OSF e6rcu, rules v1 since 2026-10-12) whose fingerprint covers
config/constants.py, pipeline.py, logic/ and engines/. Nothing here
changes those files, so that study's record is untouched. This package
only imports from them.

WHAT IT DOES, once a day after the main scan (entrypoint.after_scan, when
settings.PHASE3_FORECASTS is on):
  1. Takes the Phase 3 trials the main scan already found on
     ClinicalTrials.gov (catalyst_events, event_type PHASE_3_READOUT)
     whose estimated completion date is from P3_LOOKBACK_DAYS ago to
     P3_LOOKAHEAD_DAYS ahead. Results usually come weeks to months after
     that date, so trials keep being scored after it passes.
  2. Drops stocks under P3_MIN_PRICE, the same screen as the FDA track.
  3. Has Claude review each trial (review.py): the science, who is behind
     it, what credible independent people say, and warning signs, from
     the company's filings, the registry and a web search.
  4. Stores one score per trial per day (store.py), stamped with the
     Phase 3 rules version and fingerprint (rules.py).

NOT YET BUILT (later steps): recording outcomes, the trade filter and
alerts, the dashboard tab. Until the Phase 3 rules are registered on OSF,
RULES_VERSION is 0 and every score is a trial run.
"""
