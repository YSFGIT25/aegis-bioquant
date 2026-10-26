# Notes for the paper

Facts about the data that the paper's methods and limitations sections
should state. None of them changes the pre-registered analysis; each is
recorded here so it is not forgotten by the time the paper is written
(after the primary analysis, November 2027). Written 2026-10-25. The
first scored decision (IONS) had already resolved on 2026-10-20; see
"The first scored decision came before the change" below. The paper
draft itself is a Claude Doc:
https://claude.ai/artifact/AQ2QbtfBauCy7DqgDmcffu

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

**Outcomes and announcement times.** Changed on 2026-10-25, five days
after the first scored decision had resolved (logged in the plan's
change log and on OSF): no
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

**The automatic rules checked on the trial period** (not
pre-registered; run 2026-10-25, after the method was adopted). The
rules (`core/auto_confirm.decide`), run with live openFDA data on the 18
outcomes the author had confirmed by hand before 2026-10-12 (17
approvals, 1 CRL; decisions 2026-06-17 to 2026-09-25, from the OSF
snapshot of 2026-10-25), recorded 12, and all 12 agreed with the
author. The 6 not recorded were all approvals: two partners'
applications (SPRO = GSK's Utebzi; ZYME = Jazz's Ziihera), one bought
company's (MNKD = Furoscix ReadyFlow, listed under scPharmaceuticals,
NDA209988), and three with no Drugs@FDA record, consistent with
CBER-regulated products (REPL; RARE twice). The snapshot holds no drug
names for these past decisions, so the drug-name rule could not help;
in live use it might match the first three, never the CBER ones.

**The registration's answers on existing data were wrong.** The OSF form
(2026-10-23) said no scored decision had resolved and IONS was due
2026-10-26; IONS had been approved 2026-10-20. Corrected by a formal OSF
registration update (text: `analysis/osf-registration-update-2026-10-26.md`),
which also brings the outcome method up to date. In the paper, report the
primary H1 result with and without IONS, labelled as not pre-registered.

**Plan edits around the registration** (from the Claude Code session
logs, which timestamp every edit made to the plan doc). No edit between
2026-10-19 04:02 UTC and 2026-10-23 17:45 UTC (revision 23 at both
times, so no browser edits either). At 17:45 UTC on 2026-10-23, about
20 minutes before registering, three wording edits (revision 23 to 26):
the author's confirmation of the primary and interim dates, both
already in the plan, and the analysis commit time corrected to 23:55 US
Eastern. The registered PDF is revision 26. The author did not know of
the IONS approval when registering; the registration session's records
never mention it.

**Models.** `LLM_REVIEW_MODEL` = claude-opus-5-5 and
`LLM_IDENTIFY_MODEL` = claude-haiku-4-5-20251001, confirmed on
2026-10-25 because the code's defaults reproduce the live fingerprint
a185ea010c25, which includes both names. Every review in the 2026-10-25
snapshot (35 current, 21 earlier versions) was written by
claude-opus-5-5.

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

**The base rate is an anchor, not a measurement.** 0.83 was set in the
code from literature said to cite 80-90% first-cycle approval. The
sources found count eventual approval in that range (BIO, Informa and
QLS 2021: 90.6%, n = 1,453, counting resubmissions). The FDA's own
analysis (fda.gov/media/194858, FY 2008-2024) puts the median
first-cycle approval rate for original NDAs/BLAs at about 64% (about
77% eventually approved), a first-cycle CRL rate of about 29% since
2011, and 7% for efficacy supplements by FY 2024.

**Unrecordable outcomes lean towards approvals.** An approval needs a
matching Drugs@FDA record; a CRL needs only the absence of one. CBER
products and unmatched partner or bought-company applications therefore
drop out when approved but not when rejected, which can make CRLs look
more common than they are (6 of 18 in the trial-period check, all
approvals). Report every unrecorded scored decision with its reason.

**Snapshots began on 2026-10-25.** For earlier decisions, including
IONS, only the database's own timestamps show the scores came first.

**Name.** The software was renamed from Aegis-BioQuant to FDAOdds on
2026-10-24 (visible name only; code, repository and fingerprint
unchanged). Use one name in the paper, with "(formerly …)" once.

**AI assistance.** Much of the code was written with an AI coding
assistant (Claude), as the OSF registration states. Say so in the paper
too, in one plain sentence.
