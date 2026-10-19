import asyncio, csv, os, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "analysis"))
from datetime import date, datetime, timedelta, timezone

import numpy as np
from sqlalchemy import func, select

from aegis_bioquant.core import database as db
import run_analysis as A

# Made-up data only. Nothing here comes from the real record.

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def at(day, hour=19):
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)

def score(day, p, hour=19, version=1, fp=A.FINGERPRINT, basis="CLAUDE", signal="NO_SIGNAL"):
    return A.Score(at(day, hour), p, version, fp, basis, signal, True)

def outcome(ticker, decided, result="APPROVED", status="CONFIRMED", confirmed=None, catalyst=None, change=None):
    return db.CatalystOutcomeRecord(ticker=ticker, event_type="PDUFA", catalyst_date=catalyst or decided, status=status,
                                    outcome=result, decision_date=decided, confirmed_at=confirmed or at(decided + timedelta(days=2)),
                                    price_change_pct=change)

PRIMARY = A.RUNS["primary"]
D = date

def selection_cases():
    outcomes, scores, ann = [], {}, {}
    def add(o, s, announced=None):
        outcomes.append(o); scores[(o.ticker, o.catalyst_date)] = s
        if announced: ann[(o.ticker, o.catalyst_date)] = announced
    # 26 hours before the announcement counts; 2 hours before does not
    add(outcome("AAA", D(2027, 1, 10)), [score(D(2027, 1, 8), 0.80), score(D(2027, 1, 9), 0.91), score(D(2027, 1, 10), 0.10)],
        at(D(2027, 1, 10), 21))
    # exactly at the boundary: 23 hours before is out, 47 hours before is in
    add(outcome("BBB", D(2027, 2, 10), "CRL"), [score(D(2027, 2, 8), 0.70), score(D(2027, 2, 9), 0.50)], at(D(2027, 2, 10), 18))
    add(outcome("CCC", D(2026, 11, 1)), [score(D(2026, 10, 1), 0.9, version=None)], at(D(2026, 11, 1), 20))
    add(outcome("DDD", D(2027, 3, 1), "WITHDRAWN"), [score(D(2027, 2, 1), 0.9)])
    add(outcome("EEE", D(2027, 3, 2), "DELAYED"), [score(D(2027, 2, 1), 0.9)])
    add(outcome("FFF", D(2027, 3, 3), "INVALID"), [score(D(2027, 2, 1), 0.9)])
    add(outcome("GGG", D(2027, 3, 4), status="SUGGESTED"), [score(D(2027, 2, 1), 0.9)], at(D(2027, 3, 4), 20))
    add(outcome("HHH", D(2027, 10, 20)), [score(D(2027, 10, 1), 0.9)], at(D(2027, 10, 20), 20))
    add(outcome("III", D(2027, 10, 1), confirmed=at(D(2027, 11, 1), 1)), [score(D(2027, 9, 1), 0.9)], at(D(2027, 10, 1), 20))
    add(outcome("JJJ", D(2027, 4, 1)), [score(D(2027, 3, 1), 0.9)])
    # the last eligible score carries another fingerprint
    add(outcome("KKK", D(2027, 5, 1)), [score(D(2027, 4, 1), 0.60), score(D(2027, 4, 29), 0.95, fp="deadbeef0000")], at(D(2027, 5, 1), 20))
    # decided early, before its entry window opened (PDUFA 2027-08-30, window 2027-07-26)
    add(outcome("LLL", D(2027, 6, 1), catalyst=D(2027, 8, 30)), [score(D(2027, 5, 1), 0.85)], at(D(2027, 6, 1), 12))
    # secondary = first score on or after the window opens (PDUFA 2027-03-20, window 2027-02-13)
    add(outcome("MMM", D(2027, 3, 20)), [score(D(2027, 1, 1) + timedelta(days=7 * k), 0.5 + k / 100) for k in range(11)],
        at(D(2027, 3, 20), 21))
    # window opened before the record began: secondary = first version-1 score
    add(outcome("NNN", D(2026, 11, 5)), [score(D(2026, 10, 5), 0.3, version=None), score(D(2026, 10, 12), 0.77),
                                         score(D(2026, 10, 20), 0.88)], at(D(2026, 11, 5), 20))
    add(outcome("OOO", D(2027, 6, 2)), [], at(D(2027, 6, 2), 20))
    record = A.Record([o for o in outcomes if o.status == "CONFIRMED"], scores, {}, set())
    return record, ann

def main():
    # ---------------- measures ----------------
    p, o = np.array([0.9, 0.5]), np.array([1.0, 0.0])
    check("Brier score by hand: ((0.9-1)^2 + (0.5-0)^2)/2 = 0.13", abs(A.brier(p, o) - 0.13) < 1e-12)
    check("AUC: approvals all above CRLs = 1, all below = 0, ties count half",
          A.auc(np.array([.9, .8, .3]), np.array([1., 1., 0.])) == 1.0
          and A.auc(np.array([.2, .9]), np.array([1., 0.])) == 0.0
          and A.auc(np.array([.5, .5]), np.array([1., 0.])) == 0.5)
    check("AUC is undefined without both outcomes", A.auc(np.array([.9, .8]), np.array([1., 1.])) is None)
    check("log loss clips scores to 0.01-0.99", abs(A.log_loss(np.array([1.0]), np.array([0.0])) + np.log(0.01)) < 1e-12)
    rng = np.random.default_rng(1)
    p, o = rng.uniform(0.3, 1, 200), (rng.uniform(size=200) < 0.83).astype(float)
    dec = A.decomposition(p, o)
    check("reliability - resolution + uncertainty + within-band adds up to the Brier score",
          abs(dec["reliability"] - dec["resolution"] + dec["uncertainty"] + dec["within_band"] - A.brier(p, o)) < 1e-12)
    check("bands are under 0.60, 0.60-0.80, 0.80 or more",
          [n for _, n, _, _ in A.bands(np.array([0.59, 0.6, 0.79, 0.8, 1.0]), np.ones(5))] == [1, 2, 2])

    sizes = A.company_bootstrap(["X", "X", "Y"], lambda idx: float(len(idx)))
    seen = {len(np.concatenate([np.flatnonzero(np.array(["X", "X", "Y"]) == n) for n in pair])) for pair in (("X", "X"), ("X", "Y"), ("Y", "Y"))}
    check("the bootstrap draws companies, not decisions: a company's two decisions move together", seen == {2, 3, 4} and sizes[1] == 4.0)
    check("the bootstrap is repeatable (fixed seed)",
          A.company_bootstrap(list("ABCDE"), lambda idx: float(idx.sum())) == A.company_bootstrap(list("ABCDE"), lambda idx: float(idx.sum())))

    # ---------------- choosing the prediction ----------------
    record, ann = selection_cases()
    sel = A.select_decisions(record, ann, PRIMARY)
    got = {d.ticker: d for d in sel.decisions}
    reasons = {r: items for r, items in sel.excluded.items()}
    check("the primary score is the last one at least 24 hours before the announcement", got["AAA"].primary.p == 0.91)
    check("a score 23 hours before the announcement is left out", got["BBB"].primary.p == 0.70)
    check("scores from before 2026-10-12 never count", reasons.get("only scores from before 2026-10-12") == ["CCC 2026-11-01"])
    check("WITHDRAWN, DELAYED and INVALID are excluded and counted",
          all(reasons.get(f"outcome {k}") == [v] for k, v in (("WITHDRAWN", "DDD 2027-03-01"), ("DELAYED", "EEE 2027-03-02"), ("INVALID", "FFF 2027-03-03"))))
    check("an unconfirmed (SUGGESTED) outcome is never read", "GGG" not in got and not any("GGG" in i for v in reasons.values() for i in v))
    check("decisions after 2027-10-12 are outside the primary run", "HHH" not in got and not any("HHH" in i for v in reasons.values() for i in v))
    check("a decision confirmed after 2027-10-31 is excluded", reasons.get("confirmed after 2027-10-31") == ["III 2027-10-01"])
    check("a decision with no announcement time is listed, not guessed", reasons.get("no announcement time in announcements.csv") == ["JJJ 2027-04-01"])
    check("a never-scored catalyst is counted", reasons.get("never scored") == ["OOO 2027-06-02"])
    check("a score with another fingerprint is flagged", sel.off_fingerprint == ["KKK 2027-05-01 2027-04-29 19:00 deadbeef0000"] and got["KKK"].primary.p == 0.95)
    clean = {d.ticker: d for d in A.select_decisions(record, ann, PRIMARY, keep=lambda s: s.fingerprint == A.FINGERPRINT).decisions}
    check("... and the rerun without it uses the earlier score", clean["KKK"].primary.p == 0.60)
    check("the secondary score is the first one in the entry window", round(got["MMM"].secondary.p, 9) == 0.57)
    check("decided before its window opened: no secondary score", got["LLL"].secondary is None)
    check("window open before the record began: the secondary is the first version-1 score", got["NNN"].secondary.p == 0.77)
    check("a last score 31 days before the decision is flagged as stale", got["LLL"].stale and not got["AAA"].stale)
    check("the interim run stops at decisions dated 2027-03-31",
          {d.ticker for d in A.select_decisions(record, ann, A.RUNS["interim"]).decisions} == {"AAA", "BBB", "MMM", "NNN"})

    # ---------------- verdicts ----------------
    def sample(n_ok, n_crl, p_ok, p_crl):
        outs, scs, an = [], {}, {}
        for k in range(n_ok + n_crl):
            t, day = f"T{k:02d}", D(2027, 1, 1) + timedelta(days=5 * k)
            outs.append(outcome(t, day, "APPROVED" if k < n_ok else "CRL", change=0.1 if k < n_ok else -0.5))
            scs[(t, day)] = [score(day - timedelta(days=3), p_ok if k < n_ok else p_crl, signal="LONG_RUN_UP_ALPHA" if k % 2 else "NO_SIGNAL")]
            an[(t, day)] = at(day, 20)
        return A.Record(outs, scs, {}, set()), an
    def verdict(rec, an):
        return next(l for l in A.build_report(rec, an, PRIMARY, D(2027, 11, 5))[0] if l.startswith("  H1:"))
    check("a score that tells approvals from CRLs is SUPPORTED", "SUPPORTED: the whole" in verdict(*sample(40, 6, 0.95, 0.2)))
    check("an uninformative, overconfident score is NOT SUPPORTED", "NOT SUPPORTED" in verdict(*sample(40, 6, 0.99, 0.99)))
    lines = A.build_report(*sample(40, 3, 0.95, 0.2), PRIMARY, D(2027, 11, 5))[0]
    check("fewer than 4 CRLs is INCONCLUSIVE whatever the gap, and points to the 2028 rerun",
          any("INCONCLUSIVE: 3 CRLs" in l and "2028-10-12" in l for l in lines))
    check("... and H2 is not tested", any("H2" in l for l in lines) and any("not tested" in l for l in lines) and not any("AUC =" in l for l in lines))
    check("the interim report runs no test", not any("H1:" in l or "gap =" in l for l in A.build_report(*sample(40, 6, 0.95, 0.2), A.RUNS["interim"], D(2027, 4, 20))[0]))

    # ---------------- against a database ----------------
    asyncio.run(_db_part())
    _cli_part()

async def _db_part():
    await db.init_db()
    factory = db.get_session_factory()
    day = D(2027, 2, 1)
    async with factory() as s:
        s.add(db.CatalystOutcomeRecord(ticker="ZZZ", event_type="PDUFA", catalyst_date=day, status="CONFIRMED", outcome="APPROVED",
                                       decision_date=day, confirmed_at=at(day + timedelta(days=1)), price_change_pct=0.2))
        s.add(db.PredictionRecord(ticker="ZZZ", nct_id="NCT0", catalyst_type="PDUFA", catalyst_date=day, p_fundamental=0.88,
                                  fundamental_breakdown=[], fundamental_base_rate=0.83, spot_price=10.0, open_interest=1, daily_volume=1,
                                  arbitrage_signal="NO_SIGNAL", hold_state="X", use_options=0, allocation_pct=0, allocation_dollars=0,
                                  scanned_at=at(day - timedelta(days=2)), rules_version=1, rules_fingerprint=A.FINGERPRINT, evidence_basis="CLAUDE"))
        await s.commit()
    await db.dispose_db()

    record = await A.load_record()
    check("reads confirmed outcomes and version-1 scores from the database",
          len(record.outcomes) == 1 and record.scores[("ZZZ", day)][0].p == 0.88 and record.scores[("ZZZ", day)][0].scanned_at.tzinfo is not None)
    async with db.get_session_factory()() as s:
        counts = (await s.scalar(select(func.count()).select_from(db.PredictionRecord)),
                  await s.scalar(select(func.count()).select_from(db.CatalystOutcomeRecord)))
    await db.dispose_db()
    check("reading changes nothing", counts == (1, 1))

def _cli_part():
    with tempfile.TemporaryDirectory() as tmp:
        ann, out = pathlib.Path(tmp, "a.csv"), pathlib.Path(tmp, "out")
        check("the primary analysis refuses to run before its cut-off", A.main(["primary", "--announcements", str(ann), "--out", str(out)], today=D(2027, 10, 31)) == 2)
        ann.write_text("ticker,catalyst_date,announced_at_utc,source_url\n", encoding="utf-8")
        check("... and refuses while a decision has no announcement time", A.main(["primary", "--announcements", str(ann), "--out", str(out)], today=D(2027, 11, 1)) == 3)
        ann.write_text("ticker,catalyst_date,announced_at_utc,source_url\nZZZ,2027-02-01,2027-02-01T21:30:00Z,https://example.com/8k\n", encoding="utf-8")
        check("with every time recorded it runs", A.main(["primary", "--announcements", str(ann), "--out", str(out)], today=D(2027, 11, 1)) == 0)
        rows = list(csv.DictReader(pathlib.Path(out, "primary-2027-11-01.csv").open(encoding="utf-8")))
        check("one CSV row per decision, with the score that was used", len(rows) == 1 and rows[0]["primary_score"] == "0.88")
        ann.write_text("ticker,catalyst_date,announced_at_utc,source_url\nZZZ,2027-02-01,2027-02-01T21:30:00,x\n", encoding="utf-8")
        try:
            A.load_announcements(ann); ok = False
        except ValueError:
            ok = True
        check("an announcement time without a time zone is refused", ok)

main()
