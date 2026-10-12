"""
config/constants.py
---------------------
Catalyst mappings and filter constraints referenced by logic/matrix_matcher.py
and logic/risk_manager.py. Pure data — no logic.
"""

# --- Phase A: Catalyst horizon windows (days) ---
PDUFA_WINDOW_MIN_DAYS = 35
PDUFA_WINDOW_MAX_DAYS = 45

ADCOM_WINDOW_MIN_DAYS = 55
ADCOM_WINDOW_MAX_DAYS = 70

PHASE_3_STATUS_TAG = "WAIT_FOR_FLOW_SIGNAL"

# --- Phase A: Liquidity & asset structure constraints ---
# DEPARTURE FROM THE MANUAL, by the owner's decisions (2026-10-06) — see
# logic/phase_a.py for the reasoning. The manual's $2.00-$10.00 share-
# price band is replaced by a direct size test: company value at or
# under MARKET_CAP_MAX. The $2 price floor stays; there is no price
# ceiling (None).
SPOT_PRICE_MIN = 2.00
SPOT_PRICE_MAX = None
MARKET_CAP_MAX = 20_000_000_000     # $20 billion

MIN_OPEN_INTEREST = 2000
# The manual says 500. Lowered to 200 by the owner's decision (2026-10-09):
# on the first full scan, 500 screened out the companies with the largest
# event moves priced (MLYS at 483 contracts with a 36% move, VNDA at 287
# with 54%). The cost is wider bid-ask spreads on what now gets through.
MIN_DAILY_OPTIONS_VOLUME = 200

# --- Phase B, Engine 2: IV Rank thresholds ---
IV_RANK_UNDERPRICED_THRESHOLD = 30
IV_RANK_IDEAL_THRESHOLD = 20

# --- Phase B, Engine 2: self-built IV history (see core/database.py's
# IVSnapshotRecord and pipeline.record_iv_snapshots) ---
IV_HISTORY_LOOKBACK_DAYS = 365       # the manual's 52-week window
IV_RANK_MIN_READINGS = 30            # below this many daily readings, IV Rank stays unknown:
                                     # a "rank" over a few days says nothing about a year
IV_REFERENCE_MIN_DAYS_TO_EXPIRY = 21 # read IV from the first expiry at least this far out,
                                     # so the reading isn't dominated by a few days to expiry
IV_PLAUSIBLE_MIN = 0.01              # yfinance reports junk IV (0.00001, 50+) for contracts
IV_PLAUSIBLE_MAX = 10.0              # with no real quote — readings outside this are ignored

# --- Phase B, Engine 2: event-implied move (metrics.event_implied_move) ---
EVENT_MOVE_MIN_BASELINE_DAYS = 7        # a pre-event expiry closer than this gives a jumpy baseline
EVENT_MOVE_BASELINE_EVENT_GAP_DAYS = 3  # pre-event expiry this close to the catalyst may already hold event risk
EVENT_MOVE_LOOKAHEAD_DAYS = 150         # record the measure daily for catalysts up to this far ahead

# --- Phase C: Arbitrage Matrix thresholds ---
# The clinical thresholds are the manual's. The market side is NOT: the
# manual compares against a market-implied approval probability
# (P_Market <= 0.50 / >= 0.70), and that number proved unusable — see
# logic/matrix_matcher.py. By the owner's decision (2026-10-07) the
# market side is now two observable tests instead.
LONG_ALPHA_P_FUNDAMENTAL_MIN = 0.75
SHORT_ALPHA_P_FUNDAMENTAL_MAX = 0.40

# "The event matters to this company": the options price at least this
# much extra movement for the catalyst (metrics.event_implied_move).
# Set by judgement from one day of readings — on 2026-10-06 the readings
# ran from 0 (large companies, label expansions) through 9-16% (mid-sized
# companies) to 50%+ (single-product companies). Due a second look once
# a few weeks of daily readings exist.
EVENT_MATTERS_MIN_MOVE = 0.10

# "The premium is still cheap" for the long track is the manual's own
# test, IV_RANK_UNDERPRICED_THRESHOLD (< 30), defined above.
# "The premium is rich" for the avoid track mirrors it from the other
# side, at the same level the equity-accumulation rule already calls
# overpriced.
COLLAPSE_IV_RANK_RICH = 50

# Retired with the P_Market rules; kept so the old thresholds are on record.
LONG_ALPHA_P_MARKET_MAX = 0.50
SHORT_ALPHA_P_MARKET_MIN = 0.70

# --- Phase C (extension): Long-Term Equity Accumulation pivot ---
# Triggers when Engine 1 conviction is very high but Engine 2 shows the
# options structure is unfavorable (premium overpriced OR illiquid).
# 0.90, not the manual's 0.85 (the owner's decision, 2026-10-11). 0.85
# was written when clinical scores clustered at 0.83 and almost nothing
# reached it. With Claude's scores driving, 9 of 23 scored catalysts did
# on the first scan, and each would have become an alert in its window.
EQUITY_ACCUMULATION_P_FUNDAMENTAL_MIN = 0.90
EQUITY_ACCUMULATION_IV_RANK_OVERPRICED = 50     # IV_Rank > 50 => premium overpriced
EQUITY_ACCUMULATION_MIN_OPEN_INTEREST = 2000    # OI < 2000 => illiquid

# --- Signal labels (set on candidate.arbitrage_signal) ---
SIGNAL_LONG_RUN_UP_ALPHA = "LONG_RUN_UP_ALPHA"
SIGNAL_STRUCTURAL_COLLAPSE_AVOID = "STRUCTURAL_COLLAPSE_AVOID"
SIGNAL_LONG_TERM_EQUITY_ACCUMULATION = "LONG_TERM_EQUITY_ACCUMULATION"
SIGNAL_NONE = "NO_SIGNAL"

# --- Phase D: Risk sizing & exit triggers (options track) ---
KELLY_FRACTION_CAP = 0.25          # conservative fractional-Kelly ceiling
MAX_POSITION_PCT_OF_CASH = 0.10    # hard binary-stop-loss bound on any single options trade

PDUFA_EXIT_DAYS_PRIOR = 14
ADCOM_EXIT_DAYS_PRIOR = 5
PROFIT_TARGET_PCT = 0.50
PROFIT_TARGET_WINDOW_DAYS = 10

# --- Phase D (extension): Long-Term Equity Accumulation track ---
EQUITY_ALLOCATION_MIN_PCT = 0.02   # flat 2% floor of total available cash
EQUITY_ALLOCATION_MAX_PCT = 0.05   # flat 5% ceiling of total available cash
EQUITY_TRAILING_STOP_PCT = 0.15    # macro tail-risk hedge, long-term hold

# --- Phase A (ingestion): SEC EDGAR full-text search (PDUFA source) ---
EDGAR_PDUFA_LOOKBACK_DAYS = 180     # how far back to search 8-Ks for PDUFA disclosures
EDGAR_MAX_FILINGS_PER_RUN = 250     # bound per-run network calls to EDGAR. The search returned
                                    # ~210 filings over 180 days when this was raised from 50
                                    # (2026-10-06); at 50, three in four were never opened.

# --- Phase A (ingestion): Phase 3 readout watch list (ClinicalTrials.gov) ---
PHASE3_LOOKAHEAD_DAYS = 90          # the manual's "upcoming quarter"
PHASE3_MAX_STUDIES = 600            # bound on registry rows read per run (~360 matched when built)

# --- Claude's second opinion on the evidence (engines/clinical_nlp/llm_review.py) ---
# A new date within this many days of the one an evidence review says the
# decision moved to is taken to be that decision (companies' own filings
# disagree by a day now and then).
MOVED_DATE_TOLERANCE_DAYS = 3
LLM_REVIEW_MAX_AGE_DAYS = 7              # after this long a review is redone IF the company has filed about the drug since
LLM_REVIEW_MAX_PER_SCAN = 40             # bounds one scan's API spend and its requests to the SEC
LLM_REVIEW_FILING_LOOKBACK_DAYS = 900    # how far back to look for the drug's trial results
LLM_REVIEW_MAX_RESULT_FILINGS = 4        # filings that report trial results (drug name + "primary endpoint")
LLM_REVIEW_MAX_RECENT_FILINGS = 4        # newest filings mentioning the drug (how the review is going)
LLM_REVIEW_MAX_CHARS_PER_FILING = 5000   # passages kept from one such filing
LLM_REVIEW_MAX_CHARS_ANNUAL = 10000      # ...and from the latest annual report, which describes results in full
LLM_REVIEW_MAX_CHARS = 40000             # passages sent to the model for one catalyst (~10k tokens)
LLM_REVIEW_MAX_WEB_SEARCHES = 5          # searches allowed when the filings say too little (billed per search)
LLM_REVIEW_MAX_CONTINUES = 3             # times an unfinished web search is resumed before giving up

# --- Outcome tracking (ingestion/outcome_finder.py, pipeline.update_outcomes) ---
OUTCOME_LOOKBACK_DAYS = 30               # look for a decision filing from this long before the date (early decisions)
OUTCOME_MAX_FILINGS_PER_CATALYST = 6     # newest filings opened per catalyst per check
OUTCOME_MAX_CHECKS_PER_SCAN = 15         # catalysts checked per scan; bounds the requests a backlog can cause
OUTCOME_RECHECK_DAYS = 2                 # an unresolved catalyst is looked up again after this long
OUTCOME_GIVE_UP_DAYS = 120               # ...until its date is this far in the past

# --- Phase B, Engine 1: Clinical NLP & Probability base rate ---
# By construction, every candidate classifier.py scores already has an
# FDA-ACCEPTED NDA/BLA with a PDUFA date (that's how it entered this
# system via fda_scraper.py) — a materially different and higher base
# rate than generic "Phase 3 success rate". Industry/FDA CDER literature
# commonly cites ~80-90% first-cycle approval among applications that
# reach PDUFA with an accepted filing; this constant is a documented,
# approximate anchor from that literature, NOT an empirically-tracked
# number this system has validated itself. See classifier.py's module
# docstring — replacing this with a real historical PDUFA-outcome
# database + calibration tracking is flagged there as the honest
# long-term fix, consistent with the calibration gap flagged early in
# this project's design discussion.
FUNDAMENTAL_BASE_RATE_PRIOR = 0.83
