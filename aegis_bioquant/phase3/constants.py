"""
phase3/constants.py
--------------------
The numbers the Phase 3 forecasts run on. Part of the Phase 3 rules
fingerprint (phase3/rules.py): changing one is a rule change.
"""

# Starting point for every trial. BIO / Informa / QLS, "Clinical
# Development Success Rates 2011-2020" (2021): about 58% of Phase 3
# programs move on to an FDA submission. TO CONFIRM against the report
# itself before the Phase 3 rules are registered.
P3_BASE_RATE = 0.58

# Which trials are forecast, by their estimated primary completion date.
P3_LOOKAHEAD_DAYS = 90       # up to this far ahead (the registry feed's own horizon)
P3_LOOKBACK_DAYS = 180       # and this long after: results come weeks to months later
P3_MIN_PRICE = 2.0           # share price floor, the same as the FDA-decision track

# Reviews. Each one is a paid model call with web search, so they are rationed.
P3_MAX_REVIEWS_PER_SCAN = 12      # new or refreshed reviews per daily scan
P3_REVIEW_RECHECK_DAYS = 7        # redo a review this old if the company has filed about the drug since
P3_REVIEW_MAX_AGE_DAYS = 14       # redo it anyway at this age: news and commentary move without filings
P3_MAX_WEB_SEARCHES = 6           # searches per review (the team and expert views need more than the FDA reviews)
P3_REVIEW_MAX_TOKENS = 16000      # room for the model's reasoning plus the answer
P3_REVIEW_EFFORT = "medium"       # the model's default; the FDA-decision reviews run at it too

# Recording results (phase3/outcomes.py).
P3_OUTCOME_GIVE_UP_DAYS = 365     # stop looking for a result this long after the estimated completion
P3_OUTCOME_MAX_READS_PER_SCAN = 20  # filings the model reads per scan; the rest wait for the next day

# Estimated price of a review, for the cost line in each scan's report.
# Claude Opus 5.5 list prices; web search is billed per search.
P3_PRICE_INPUT_PER_MTOK = 4.00
P3_PRICE_OUTPUT_PER_MTOK = 20.00
P3_PRICE_CACHE_READ_PER_MTOK = 0.20
P3_PRICE_PER_SEARCH = 0.01
