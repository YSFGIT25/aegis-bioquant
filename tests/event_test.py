import asyncio, math, sys
from datetime import date, datetime, timedelta, timezone

from aegis_bioquant import pipeline as P
from aegis_bioquant.config import constants as c, settings
from aegis_bioquant.core import database as db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.engines.options_quant import metrics as M
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.ingestion.market_data_feed import MarketSnapshot, OptionChainSnapshot, OptionContract, Quote
from aegis_bioquant.telemetry import alerts as AL, dashboard as D
from types import SimpleNamespace

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
CAT = TODAY + timedelta(days=40)
real_sleep = asyncio.sleep
async def no_sleep(_): await real_sleep(0)
P.asyncio.sleep = no_sleep

def chain(days, iv, spot=10.0, quoted=True, oi=5000, vol=600):
    bid, ask = (0.98, 1.02) if quoted else (0.0, 0.0)
    mk = lambda kind, k, b, a: OptionContract(contract_symbol=f"{kind}{k}{days}", strike=k, option_type=kind, bid=b, ask=a,
                                               open_interest=oi, volume=vol, implied_volatility=iv)
    # calls at 9.5 / 10.5 priced so P(S > spot) = 0.40
    return OptionChainSnapshot(ticker="T", expiration=TODAY + timedelta(days=days), fetched_at=NOW,
                               calls=[mk("CALL", 10.0, bid, ask), mk("CALL", 9.5, 0.98 if quoted else 0, 1.02 if quoted else 0), mk("CALL", 10.5, 0.58 if quoted else 0, 0.62 if quoted else 0)],
                               puts=[mk("PUT", 10.0, bid, ask)])

def move(before, after, cat=CAT):
    return M.event_implied_move(before, after, 10.0, cat, TODAY)

async def main():
    # ---------------- the measure ----------------
    m = move(chain(20, 0.50), chain(45, 0.50))
    check("no event priced (same volatility before and after) -> 0, not the ordinary daily swing", m.move == 0.0 and m.event_share == 0.0 and m.confidence == "GOOD")
    m = move(chain(20, 0.60), chain(45, 0.50))
    check("later expiry cheaper than the earlier one -> 0, never negative", m.move == 0.0)
    m = move(chain(20, 0.89), chain(45, 1.10))
    expect = math.sqrt((1.10**2 - 0.89**2) * 45 / 365)
    check(f"SMMT-like inputs (0.89 -> 1.10 over 45 days) -> about {expect:.1%}", abs(m.move - expect) < 1e-4 and 0.22 < m.move < 0.24)
    check("event share = event variance / everything the later expiry prices", abs(m.event_share - (1.10**2 - 0.89**2) / 1.10**2) < 1e-3)
    check("a bigger gap between the two expiries means a bigger event", move(chain(20, 0.89), chain(45, 1.40)).move > m.move)
    check("the same gap priced over more days is a bigger event", move(chain(20, 0.89), chain(90, 1.10)).move > m.move)
    check("baseline expiry only days away -> flagged", move(chain(5, 0.89), chain(45, 1.10)).confidence == "LOW_CONFIDENCE_SHORT_BASELINE")
    check("baseline expiry right before the catalyst -> flagged (the FDA may act early)", move(chain(38, 0.89), chain(45, 1.10), cat=TODAY + timedelta(days=40)).confidence == "LOW_CONFIDENCE_BASELINE_NEAR_EVENT")
    check("no expiry before the catalyst -> no reading", move(None, chain(45, 1.10)) is None and move(chain(20, 0.89), None) is None)
    check("no live quotes on one side -> no reading", move(chain(20, 0.89, quoted=False), chain(45, 1.10)) is None and move(chain(20, 0.89), chain(45, 1.10, quoted=False)) is None)
    check("junk volatility on one side -> no reading", move(chain(20, 0.00001), chain(45, 1.10)) is None)
    check("expiries in the wrong order -> no reading", move(chain(45, 0.89), chain(20, 1.10)) is None)

    # ---------------- inside a scored candidate ----------------
    snap = MarketSnapshot(ticker="T", catalyst_date=CAT, quote=Quote(ticker="T", last_price=10.0, market_cap=3e9, fetched_at=NOW),
                          expiry_before_catalyst=chain(20, 0.89), expiry_after_catalyst=chain(45, 1.10), fetched_at=NOW)
    async def fake_snapshot(ticker, catalyst_date):
        if ticker == "NOQUOTE": return snap.model_copy(update={"expiry_before_catalyst": chain(20, 0.89, quoted=False)})
        if ticker == "DOWN": raise RuntimeError("data source down")
        return snap
    async def fake_protocol(nct): return SimpleNamespace(nct_id=nct)
    P.fetch_market_snapshot, P.fetch_study_protocol = fake_snapshot, fake_protocol
    P.score = lambda protocol: FundamentalScore(nct_id=protocol.nct_id, base_rate=0.83, p_fundamental=0.83, adjustments=[ScoreAdjustment("x", 0.0, "t")])
    settings.ENFORCE_PHASE_A_FILTERS = True
    r0 = await P.run_candidate_pipeline("T", "NCT1", "PDUFA", CAT)
    check("a scored candidate carries the event-implied move", abs(r0.candidate.event_implied_move - expect) < 1e-4 and r0.candidate.event_move_confidence == "GOOD")
    check("the old market number alone (0.40, which used to mean 'buy') no longer makes a signal", abs(r0.candidate.p_market - 0.40) < 1e-6 and r0.candidate.arbitrage_signal == "NO_SIGNAL")
    r = await P.run_candidate_pipeline("T", "NCT1", "PDUFA", CAT, iv_rank=18.0)
    check("strong score + event that matters + cheap premium -> signal", r.candidate.arbitrage_signal == "LONG_RUN_UP_ALPHA")
    from types import SimpleNamespace as _NS
    rc = await P.run_candidate_pipeline("T", "NCT1", "PDUFA", CAT, iv_rank=18.0, claude_review=_NS(score=35, summary="Advisers voted against it.", evidence_quality="ADEQUATE"))
    check("Claude's read replaces the clinical score the rules act on; the point score is kept for comparison",
          rc.candidate.p_fundamental == 0.35 and rc.candidate.evidence_basis == "CLAUDE" and rc.candidate.point_score == 0.83
          and rc.candidate.arbitrage_signal == "NO_SIGNAL" and rc.fundamental_score.data_completeness == "EVIDENCE_REVIEW")
    check("...and the alert payload explains the score with Claude's summary", "Advisers voted against it." in str(AL.build_alert_payload(rc).model_dump())
          and "AI model's reading" in AL.format_alert_text(AL.build_alert_payload(rc)) and "laude" not in AL.format_alert_text(AL.build_alert_payload(rc)) and "laude" not in str(AL.build_alert_payload(rc).model_dump()))
    rt = await P.run_candidate_pipeline("T", "NCT1", "PDUFA", CAT, iv_rank=18.0, claude_review=_NS(score=90, summary="s", evidence_quality="THIN"))
    check("a thin review still drives, marked as thin", rt.candidate.p_fundamental == 0.90 and rt.candidate.evidence_basis == "CLAUDE_THIN" and rt.candidate.arbitrage_signal == "LONG_RUN_UP_ALPHA")
    check("no review -> the point system as before", r.candidate.evidence_basis != "CLAUDE" and r.candidate.point_score is None)
    report = P._format_report(r)
    check("report shows the reading, the reason, and that the old number is unused",
          "Event-implied move: 22.7% (GOOD)" in report and "Why: clinical score 0.83; event move priced 23%; IV Rank 18" in report and "recorded only, no rule reads it" in report)
    payload = AL.build_alert_payload(r)
    text = AL.format_alert_text(payload)
    check("alert states the size of move priced for the event, the IV Rank, and nothing about the retired number",
          "\u2713 Expected move: about 23%\nHow big a jump or drop traders are betting on" in text and "We look for at least 10%." in text and "\u2713 Options cost: cheap (18 out of 100)" in text and "We look for under 30." in text and "Trial evidence: 83 out of 100\nHow strong the case for approval looks" in text and "estimate" not in text and text.startswith("BUY SETUP: ") and "LONG_RUN_UP" not in text and "P_Fundamental" not in text and "$" not in text.split("The three checks")[1] and "Information only, not financial advice." in text and "P_Market" not in text and "Divergence" not in text
          and payload.market.event_implied_move == r.candidate.event_implied_move and payload.signal.notes.startswith("clinical score 0.83"))
    long = AL.build_alert_payload(rc); long.clinical.adjustments[0].reason = "The trial met its main goal. " * 60
    full, short = AL.format_alert_text(long), AL.format_alert_text(long, max_chars=AL.DISCORD_MAX_CHARS)
    check("a message over a service's limit loses the middle of the evidence summary, never its closing lines",
          len(full) > 2000 >= len(short) > 1900 and short.endswith("Information only, not financial advice.") and "What to do:" in short
          and "..." in short and "..." not in full and AL.format_alert_text(payload, max_chars=2000) == text)
    old = payload.model_dump(mode="json"); old["market"].pop("event_implied_move"); old["market"].pop("event_move_confidence"); old["signal"].pop("notes")
    check("an alert stored before this change still reads back", "\u2717 Expected move: could not be measured today" in AL.format_alert_text(AL.AlertPayload.model_validate(old)))

    # ---------------- daily recording for every upcoming catalyst ----------------
    await db.reset_db()
    ev = lambda t, kind="PDUFA", d=CAT: CatalystEvent(event_type=kind, ticker=t, title="x", start_date=d, source_url=f"https://example.invalid/{t}{kind}{d}",
                                                     raw_source="SEC_EDGAR_FTS", horizon_status="OUTSIDE_WINDOW")
    events = [ev("AAA"), ev("AAA", d=CAT + timedelta(days=30)), ev("ADC", kind="ADCOM"), ev("NOQUOTE"), ev("DOWN"), ev(None),
              ev("PAST", d=TODAY - timedelta(days=2)), ev("TODAY", d=TODAY), ev("FAR", d=TODAY + timedelta(days=c.EVENT_MOVE_LOOKAHEAD_DAYS + 5)),
              ev("P3", kind="PHASE_3_READOUT")]
    saved = await P.record_event_moves(events, TODAY)
    check("one reading per upcoming catalyst (a company with two dates gets two); past, today's, far-off, no-ticker and watch-list rows skipped", saved == (3, 5))
    await P.record_event_moves(events, TODAY)
    latest = await db.get_latest_event_moves()
    check("a second scan the same day replaces the reading rather than adding one", len(latest) == 3)
    check("stored soonest catalyst first, with the inputs kept", [(m.ticker, m.event_type) for m in latest] == [("AAA", "PDUFA"), ("ADC", "ADCOM"), ("AAA", "PDUFA")]
          and abs(latest[0].event_move - expect) < 1e-4 and (latest[0].baseline_iv, latest[0].post_event_iv) == (0.89, 1.10))
    await db.save_event_move_snapshot("AAA", "PDUFA", CAT, TODAY - timedelta(days=1), 0.10, 0.8, 0.9, 0.2, "GOOD", 10.0)
    latest = await db.get_latest_event_moves()
    check("history is kept per day; the newest reading is the one shown", len(latest) == 3 and abs(next(m for m in latest if m.ticker == "AAA" and m.catalyst_date == CAT).event_move - expect) < 1e-4)
    st = await D.build_state()
    check("dashboard data carries the readings", len(st["event_moves"]) == 3 and st["event_moves"][0]["ticker"] == "AAA" and st["event_moves"][0]["confidence"] == "GOOD")
    await db.dispose_db()

    # ---------------- an existing database gains the new prediction column ----------------
    import sqlite3, os
    path = sys.argv[1]
    if os.path.exists(path): os.remove(path)
    con = sqlite3.connect(path)
    con.execute("create table predictions (id integer primary key, ticker varchar, nct_id varchar, catalyst_type varchar, catalyst_date date, p_fundamental float, fundamental_breakdown json, fundamental_base_rate float, spot_price float, p_market float, p_market_confidence varchar, iv_rank float, open_interest integer, daily_volume integer, divergence_delta float, arbitrage_signal varchar, hold_state varchar, use_options integer, allocation_pct float, allocation_dollars float, trailing_stop_pct float, scanned_at datetime, actual_fda_outcome varchar, actual_price_change_pct float, outcome_recorded_at datetime)")
    con.execute("create table users (id integer primary key, email varchar, password_hash varchar, created_at datetime, stripe_customer_id varchar, subscription_status varchar, email_verified_at datetime)")
    con.commit(); con.close()
    settings.DATABASE_URL = f"sqlite+aiosqlite:///{path}"
    await db.init_db(); await db.init_db()
    cols = [r[1] for r in sqlite3.connect(path).execute("pragma table_info(predictions)")]
    await db.save_prediction(r)
    check("older database: column added on start-up, and a prediction saves with the new value", "event_implied_move" in cols
          and abs((await db.get_predictions_for_ticker("T"))[0].event_implied_move - expect) < 1e-4)
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
