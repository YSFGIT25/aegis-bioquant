# Notes for the paper

Facts about the data that the paper's methods and limitations sections
should state. None of them changes the pre-registered analysis; each is
recorded here so it is not forgotten by the time the paper is written
(after the primary analysis, November 2027). Written 2026-10-25, before
any scored decision had resolved.

## Methods

**When a score was made.** Each stored score's `scanned_at` is the moment
its daily scan started. The evidence reviews behind the scores are written
during the scan, so a score can rest on material read a few minutes after
its timestamp (a scan takes about two minutes; at most 40 reviews are
written per scan). The analysis only uses scores stored at least 24 hours
before a decision's first public announcement, which covers this many
times over.

Suggested wording: *"Each prediction is timestamped at the start of the
daily scan that produced it. Because the evidence reviews are written
during the scan, the material behind a prediction can be up to the scan's
running time newer than its timestamp; the 24-hour gap between the
prediction used and the announcement exceeds that by far."*

**Market holidays.** The market-hours check knows weekdays and trading
hours but not exchange holidays (`ingestion/market_data_feed.us_market_open`).
On a US market holiday the scan runs and stores scores built on the
previous session's option quotes. The evidence score does not use option
data, so H1 and H2 are unaffected; the event-implied move (H3) and the
signal (H4) read on a holiday rest on stale quotes. When writing up H3 and
H4, list any primary score whose `scanned_at` falls on an exchange
holiday. The analysis plan's list of "weekdays with no version-1 score"
will usually not include holidays for this reason.

Suggested wording: *"Scans on US market holidays used the previous
session's option quotes; N of the predictions used for H3 and H4 fall on
such days and are marked."*

**Outcomes and announcement times.** Changed on 2026-10-25, before the
first scored decision (logged in the plan's change log and on OSF): no
person confirms outcomes. An outcome read from the company's SEC filing
is recorded only when the FDA's own records agree
(`core/auto_confirm.py`): for an approval, Drugs@FDA lists a matching
original application or efficacy supplement approved within 3 days of
the filing's decision date; for a CRL, Drugs@FDA lists no matching
approval from 3 days before to 14 days after it, and either the FDA's
published letter is found or Drugs@FDA's data has been updated at least
14 days past the decision. Anything else stays unrecorded and is
excluded. The announcement time is the earlier of the SEC acceptance time
and midnight US Eastern on the FDA's action date. Report: how many
decisions were recorded by each rule (`confirmation_source`), and, labelled
as not pre-registered, how many scored PDUFA decisions were never
recorded and why (biologics regulated by CBER are not in Drugs@FDA; a
partner's application may match neither name).

**The first scored decision came before the change.** IONS (catalyst
2026-10-26, bepirovirsen, application held by Ionis's partner GSK) was
approved on 2026-10-20 (Drugs@FDA NDA221276) and announced by Ionis on
2026-10-21; this was only noticed on 2026-10-25, while the automatic
method was being built. The plan's change-log entries of 2026-10-24 and
2026-10-25 that said no decision had resolved carry a dated correction.
The rule matching on the evidence review's drug name was added after
seeing this case (otherwise a partner's application never matches).
Its outcome is APPROVED under either method. Say all of this in the
paper. Scores from 2026-10-21 on read the approval news (the evidence
review cites it; p = 0.99); the 24-hour rule already keeps them out,
since the announcement bound is 2026-10-20 04:00 UTC.

Suggested wording: *"Outcomes were recorded without human judgement: a
decision counted only when the company's SEC filing and the FDA's own
public records agreed, under rules fixed before the first decision."*
Before running the analysis: `python analysis/export_announcements.py --osf`
writes them into `analysis/announcements.csv` from the newest snapshot.

**Software.** The analysis was run with the library versions in
`analysis/requirements.txt` (Python 3.13.5, numpy 2.3.4). Record the
versions actually used when the analysis runs.

**Record snapshots.** From 2026-10-25 the scanner uploads a dated copy of
the study's tables to the OSF project every week, with SHA-256 hashes in
a manifest (`core/snapshots.py`). Cite them as evidence that each
prediction existed before its outcome.

## Limitations

**Sample selection by option liquidity.** Logged in the plan's change log
and on OSF on 2026-10-24: candidates failing the option-liquidity filter
are stored only when they qualify for the stock-only track (score ≥ 0.90),
so thinly traded companies with lower scores are under-represented and
their inclusion depends on the score. Report, labelled as not
pre-registered, the number of PDUFA decisions screened out for liquidity
(`screened_candidates`) and how many never had a stored version-1 score.

**Name.** The software was renamed from Aegis-BioQuant to FDAOdds on
2026-10-24 (visible name only; code, repository and fingerprint
unchanged). Use one name in the paper, with "(formerly …)" once.

**AI assistance.** Much of the code was written with an AI coding
assistant (Claude), as the OSF registration states. Say so in the paper
too, in one plain sentence.
