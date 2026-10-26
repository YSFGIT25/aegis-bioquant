# OSF registration update, drafted 2026-10-26

For https://osf.io/e6rcu, submitted by the author through OSF's "Update"
on the registration. Each section below is one answer of the original
registration (2026-10-23) that changes, with the replacement text to
paste. Answers not listed here are unchanged.

## Justification (OSF asks for one)

Correction and update. (1) The description and the answer on existing
data said that no scored FDA decision had resolved and that the first,
IONS, was due 2026-10-26. That was wrong: the FDA had approved IONS
(bepirovirsen, GSK's NDA221276) on 2026-10-20 and Ionis announced it on
2026-10-21, before this registration. The author did not know; the
stored outcomes had not been checked. The analysis code (analysis-v1,
committed 2026-10-18) predates that decision and is unchanged. (2) On
2026-10-25 the way outcomes are recorded changed: instead of the author
confirming each outcome, an outcome counts only when the company's SEC
filing and the FDA's public records agree. Both changes are dated in the
analysis plan's change log. The primary analysis will also be reported
without IONS, labelled as not pre-registered.

## Description

FDAOdds (formerly Aegis-BioQuant) is software that scans upcoming FDA
decision dates (PDUFA dates) for small and mid-sized US biotech
companies and stores a probability of approval for each, produced by an
AI model's reading of company filings. Since 2026-10-12 every stored
score is stamped with rules version 1 and code fingerprint a185ea010c25.
This registration fixes how those predictions will be judged against the
FDA outcomes. The analysis code was fixed on 2026-10-18, before any
scored decision resolved; this registration was made on 2026-10-23,
after the first scored decision (IONS) had been approved on 2026-10-20,
which the author did not know at the time (see the update of
2026-10-26). No trades are placed; all positions the system prints are
paper positions.

Code: https://github.com/YSFGIT25/aegis-bioquant. Rules version 1 is the
commit tagged rules-v1 (e190f87). The analysis script is
analysis/run_analysis.py, tagged analysis-v1 (cd50d87), committed
2026-10-18 23:55 US Eastern.

## Explanation of existing data (foreknowledge)

The predictor data exist: version-1 scores have been stored daily since
2026-10-12 and the author has seen them on the system's dashboard. One
outcome also existed at registration, unknown to the author: the first
scored decision, IONS, was approved on 2026-10-20 and announced on
2026-10-21; the original answer here said no scored decision had been
announced, which was wrong. The system had logged a suggested outcome
for it on 2026-10-22, which the author had not looked at. The analysis
code was fixed on 2026-10-18, before that decision, and the plan's text
was not edited between 2026-10-19 and the day of registration except for
three wording edits that changed no definition, hypothesis, threshold or
date. IONS is APPROVED under every method described here, and the
primary result will also be reported without it.

Steps taken to limit influence: the analysis is fixed in code
(analysis/run_analysis.py, tag analysis-v1, commit cd50d87, pushed to
GitHub before registration) and refuses to run the primary analysis
before 2027-11-01; the rules that produce the scores are fixed by
version and fingerprint (a185ea010c25, tag rules-v1); scores stored
before 2026-10-12 are a trial run and excluded; outcomes are recorded by
fixed rules with no human judgement (from 2026-10-25); the interim
report is descriptive only and cannot change the plan or the stopping
date; any later change is listed, dated and justified in a change log
and reported in the paper.

## Study design (first paragraph after the PDF reference: replace the announcement definition)

The system re-scores each upcoming PDUFA catalyst daily and stores a
probability of approval (p_fundamental). For each FDA decision, one
prediction is taken: the last version-1 score whose scanned_at is at
least 24 hours before the decision's first public announcement, not the
scheduled PDUFA date. Since 2026-10-25 the announcement time is set by
rule: the earlier of the SEC acceptance time of the company's filing and
midnight US Eastern at the start of the FDA's action date (or the
filing's decision date, if earlier). Early decisions happen (PHAR's came
43 days before its scheduled date) and companies often announce in the
evening; the 24-hour gap keeps any score that could have read the news
out of the sample. A secondary score is the first eligible score on or
after the day the entry window opens, 35 days before the scheduled date.

## Data collection procedures (replace the "Outcomes" paragraph)

Outcomes: from 2026-10-25, recorded automatically with no human
judgement (code: core/auto_confirm.py). The system's outcome finder reads
the company's own SEC filing (8-K, or 6-K for foreign companies) and
suggests an outcome; it counts only when the FDA's public records
(openFDA) agree. APPROVED: Drugs@FDA lists an approval of an original
application or an efficacy supplement for the same company or the same
drug (as named by the catalyst or by its evidence review), with an
action date within 3 days of the filing's decision date. CRL: Drugs@FDA
lists no matching approval from 3 days before to 14 days after the
decision date, and either the FDA's published complete response letter
to that company is dated within 3 days of it, or Drugs@FDA's data has
been updated at least 14 days after it. Anything else stays unrecorded
and is excluded, with the count and reasons reported. The decision date
is the FDA's action date when one is found. Nobody confirms, changes or
overrides an outcome by hand; an outcome later found wrong is not
corrected in the primary analysis, and the result is repeated without
it. Each recorded outcome is published with the filing and the FDA
record it rests on. Partial approvals (narrower label, or approval with
a required follow-up study) count as APPROVED and are listed. Before
2026-10-25 the author confirmed outcomes by reading the filing.

## Measured variables (replace the "Outcome" line)

- Outcome: APPROVED (1) or CRL (0), recorded automatically when the
  company's SEC filing and the FDA's public records agree.

## Inclusion and exclusion (replace the "Included" line and add one exclusion)

Included: PDUFA catalysts with at least one score stamped
rules_version = 1 stored at least 24 hours before the decision's first
public announcement, with an outcome recorded by the rules above.

Add to "Excluded, with the count reported": decisions whose outcome the
rules cannot record (for example products regulated by CBER, which are
not in Drugs@FDA, or an application under a partner's or another
company's name), listed with the filing's reading and the reason. These
exclude approvals more often than CRLs.

## Additional information (replace the "Known limitations" paragraph)

Known limitations: few rejections (about 1 CRL in 10 decisions, so wide
intervals on every rejection result); one market and one period (small
and mid-sized US biotechs over one year); the commercial AI model
behind the evidence score may be updated under the same name, an
undetectable source of drift within version 1; results describe only
catalysts that pass the system's filters, and a candidate whose options
fail the liquidity filter is stored only if its evidence score is at
least 0.90 (clarified 2026-10-24); paper positions only, so H4 ignores
costs, slippage and whether options could actually be bought; the first
scored decision resolved before this registration (see the explanation
of existing data); outcomes the automatic rules cannot record are more
often approvals than CRLs.

Add at the end: The software has been published under the name FDAOdds
since 2026-10-24; the code, repository, tags and fingerprint are
unchanged.
