import asyncio, json, sys, threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from aegis_bioquant import pipeline as P
from aegis_bioquant.config import settings as _settings
_settings.REQUIRE_MARKET_HOURS = False   # these tests run at any hour
async def _no_quote(ticker): raise RuntimeError('tests stay off the network')
P.fetch_quote = _no_quote
from aegis_bioquant.config import constants as c, settings
from aegis_bioquant.core import accounts as AC, database as db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.engines.options_quant import metrics as M
from aegis_bioquant.ingestion.fda_scraper import CatalystEvent
from aegis_bioquant.ingestion.market_data_feed import MarketSnapshot, OptionChainSnapshot, OptionContract, Quote
from aegis_bioquant.telemetry import alerts as A, dashboard as D

received = []
fail_all = [False]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received.append((self.path, body))
        self.send_response(500 if fail_all[0] else 200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
CATALYST = TODAY + timedelta(days=40)

def contract(kind, strike, bid, ask, oi, vol, iv=0.9):
    return OptionContract(contract_symbol=f"X{kind}{strike}", strike=strike, option_type=kind, bid=bid, ask=ask,
                          open_interest=oi, volume=vol, implied_volatility=iv)

def snapshot(spot, oi_each, vol_each, cap=None):
    # calls at strike-1 / strike+1 around spot priced so P(S > spot) = 0.40 with tight spreads
    lo, hi = round(spot) - 0.5 if spot < 20 else spot - 2.5, round(spot) + 0.5 if spot < 20 else spot + 2.5
    gap = hi - lo
    calls = [contract("CALL", lo, 0.98, 1.02, oi_each, vol_each), contract("CALL", hi, 1.0 - 0.4 * gap - 0.02, 1.0 - 0.4 * gap + 0.02, oi_each, vol_each)]
    puts = [contract("PUT", lo, 0.50, 0.54, oi_each, vol_each), contract("PUT", hi, 0.90, 0.94, oi_each, vol_each)]
    chain = OptionChainSnapshot(ticker="T", expiration=CATALYST + timedelta(days=6), calls=calls, puts=puts, fetched_at=NOW)
    # a pre-event expiry at lower volatility (0.6 vs 0.9) => the options price a ~24% extra move for the event.
    # It carries no open interest or volume, so the liquidity totals asserted below are unchanged.
    before = OptionChainSnapshot(ticker="T", expiration=CATALYST - timedelta(days=25), fetched_at=NOW,
                                 calls=[contract("CALL", lo, 0.98, 1.02, 0, 0, iv=0.6)], puts=[contract("PUT", lo, 0.50, 0.54, 0, 0, iv=0.6)])
    return MarketSnapshot(ticker="T", catalyst_date=CATALYST, quote=Quote(ticker="T", last_price=spot, market_cap=cap, fetched_at=NOW),
                          expiry_before_catalyst=before, expiry_after_catalyst=chain, fetched_at=NOW)

state = {"snap": None, "p_fund": 0.83, "protocol_calls": 0}
async def fake_snapshot(ticker, catalyst_date): return state["snap"]
async def fake_protocol(nct_id): state["protocol_calls"] += 1; return SimpleNamespace(nct_id=nct_id)
def fake_score(protocol):
    return FundamentalScore(nct_id=protocol.nct_id, base_rate=0.83, p_fundamental=state["p_fund"],
                            adjustments=[ScoreAdjustment("primary_endpoint_significance", 0.0, "test")])
P.fetch_market_snapshot, P.fetch_study_protocol, P.score = fake_snapshot, fake_protocol, fake_score

async def run(spot, oi_each, vol_each, p_fund, cap=None, **kw):
    state.update(snap=snapshot(spot, oi_each, vol_each, cap), p_fund=p_fund, protocol_calls=0)
    kw.setdefault("iv_rank", 20.0)   # premium cheap, unless a test says otherwise
    try:
        return await P.run_candidate_pipeline("T", "NCT1", "PDUFA", CATALYST, **kw)
    except P.PhaseAEviction as out:
        return out

async def main():
    await db.reset_db()
    settings.ENFORCE_PHASE_A_FILTERS = True

    # ---------------- Phase A filters ----------------
    r = await run(162.70, 1200, 600, 0.83, cap=348e9)
    check("a $348B company -> screened out, with the reason", isinstance(r, P.PhaseAEviction) and r.reasons == ["company value $348.0B is above the $20.0B ceiling"])
    check("...and before any clinical data was fetched", state["protocol_calls"] == 0)
    r = await run(7.40, 1200, 600, 0.83)
    check("price, open interest and volume all pass -> scored normally", isinstance(r, P.PipelineResult) and r.candidate.arbitrage_signal == "LONG_RUN_UP_ALPHA"
          and r.candidate.open_interest == 4800 and abs(r.candidate.p_market - 0.40) < 1e-6 and 0.23 < r.candidate.event_implied_move < 0.25)
    r = await run(7.40, 1200, 600, 0.83, iv_rank=None)
    check("the same candidate with no IV Rank yet -> scored, but no signal, and it says why",
          isinstance(r, P.PipelineResult) and r.candidate.arbitrage_signal == "NO_SIGNAL" and "IV Rank not available yet" in r.candidate.signal_notes)
    r = await run(7.40, 1200, 600, 0.83, iv_rank=55.0)
    check("...and with premium that isn't cheap -> no signal", r.candidate.arbitrage_signal == "NO_SIGNAL" and "premium not cheap" in r.candidate.signal_notes)
    r = await run(7.40, 300, 600, 0.83)
    check("thin options + ordinary clinical score -> screened out after scoring", isinstance(r, P.PhaseAEviction) and state["protocol_calls"] == 1
          and r.reasons == ["too few of its options are held open (1,200 contracts; more than 2,000 needed)", "its evidence score of 83 is below the 90 needed to be covered on its shares alone"])
    r = await run(7.40, 300, 600, 0.90)
    check("thin options + exceptional clinical score -> kept for the stock-only track", isinstance(r, P.PipelineResult) and r.candidate.arbitrage_signal == "LONG_TERM_EQUITY_ACCUMULATION")
    r = await run(7.40, 1200, 40, 0.83)
    check("low option volume alone also screens out", isinstance(r, P.PhaseAEviction) and "too few of its options traded today (160 contracts; more than 200 needed)" in r.reasons[0])
    r = await run(1.50, 1200, 600, 0.95)
    check("price below $2 -> screened out even with a top clinical score", isinstance(r, P.PhaseAEviction))
    r = await run(162.70, 1200, 600, 0.83, cap=348e9, enforce_phase_a=False)
    check("filters switched off -> the same candidate is scored", isinstance(r, P.PipelineResult))
    r = await run(2.0, 1200, 600, 0.83, enforce_phase_a=True); r2 = await run(10.0, 1200, 600, 0.83)
    check("$2.00 and $10.00 exactly are inside the range", isinstance(r, P.PipelineResult) and isinstance(r2, P.PipelineResult))
    r = await run(61.37, 1200, 600, 0.83, cap=8.5e9); r2 = await run(174.0, 1200, 600, 0.83, cap=9.1e9)
    check("mid-sized companies are scored whatever the share price ($61, $174)", isinstance(r, P.PipelineResult) and isinstance(r2, P.PipelineResult) and r.candidate.market_cap == 8.5e9)
    r = await run(30.0, 1200, 600, 0.83, cap=20e9); r2 = await run(30.0, 1200, 600, 0.83, cap=20.1e9); r3 = await run(30.0, 1200, 600, 0.83, cap=None)
    check("exactly $20B is inside; just over is out; an unknown value is not treated as too large", isinstance(r, P.PipelineResult) and isinstance(r2, P.PhaseAEviction) and isinstance(r3, P.PipelineResult))

    # ---------------- IV reading + IV Rank ----------------
    ch = snapshot(7.4, 1200, 600).expiry_after_catalyst
    check("ATM IV = average of the at-the-money call and put", M.atm_implied_volatility(ch, 7.4) == 0.9)
    ch.calls[0].implied_volatility = 0.00001; ch.calls[1].implied_volatility = 0.00001
    ch.puts[0].implied_volatility = 0.7; ch.puts[1].implied_volatility = 0.7
    check("junk IV on one leg is ignored", M.atm_implied_volatility(ch, 7.4) == 0.7)
    for leg in ch.calls + ch.puts: leg.bid = 0.0
    check("no real quote on either leg -> no reading", M.atm_implied_volatility(ch, 7.4) is None)
    hist = [0.5 + 0.01 * i for i in range(30)]          # 0.50 .. 0.79
    check("29 readings -> IV Rank unknown", M.iv_rank_from_history(0.6, hist[:29]) is None)
    check("30 readings -> IV Rank computed (0.50..0.79, now 0.645 = 50)", M.iv_rank_from_history(0.645, hist) == 50.0)
    check("at the low / high of the range -> 0 / 100", M.iv_rank_from_history(0.5, hist) == 0.0 and M.iv_rank_from_history(0.79, hist) == 100.0)

    for i, iv in enumerate(hist):
        await db.save_iv_snapshot("ZIV", TODAY - timedelta(days=29 - i), iv, CATALYST, 7.4)
    await db.save_iv_snapshot("ZIV", TODAY, 0.645, CATALYST, 7.4)          # same day again: replaces
    rank, n, est = await P._iv_rank_for("ZIV", TODAY)
    check("history stored per day, same-day reading replaced, rank read back", n == 30 and not est and rank == round((0.645 - 0.5) / (0.78 - 0.5) * 100, 2))
    await db.save_iv_snapshot("OLD", TODAY - timedelta(days=400), 0.2, CATALYST, 7.4)
    check("readings older than a year are outside the window", (await P._iv_rank_for("OLD", TODAY)) == (None, 0, False))
    rank, n, est = await P._iv_rank_for("ZIV", TODAY + timedelta(days=1))
    check("no reading today -> rank unknown (never ranks a stale value as current)", rank is None and n == 30 and not est)

    # ---- before 30 readings exist: the estimate from the stock's own price history ----
    await db.save_iv_snapshot("NEW", TODAY, 0.60, CATALYST, 7.4, hv_low=0.40, hv_high=0.80)
    check("few readings + a price-history range -> estimate, flagged as one", (await P._iv_rank_for("NEW", TODAY)) == (50.0, 1, True))
    await db.save_iv_snapshot("NORANGE", TODAY, 0.60, CATALYST, 7.4)
    check("few readings and no range -> unknown, not flagged", (await P._iv_rank_for("NORANGE", TODAY)) == (None, 1, False))
    check("estimate is clipped to 0..100", M.options_cost_estimate(0.2, 0.4, 0.8) == 0.0 and M.options_cost_estimate(1.5, 0.4, 0.8) == 100.0)
    check("estimate refuses missing or degenerate inputs", M.options_cost_estimate(None, 0.4, 0.8) is None and M.options_cost_estimate(0.6, 0.5, 0.5) is None and M.options_cost_estimate(0.6, None, None) is None)
    import math as _m
    flat = [10 * _m.exp(0.01 * ((-1) ** i)) for i in range(120)]
    lo, hi = M.realized_volatility_range(flat)
    check("price-history range: steady swings give one steady volatility", abs(lo - hi) < 0.01 and 0.25 < lo < 0.40)
    check("price-history range needs enough history", M.realized_volatility_range(flat[:40]) is None)

    asked = []
    async def fake_ref(ticker, min_days):
        asked.append(ticker)
        if ticker == "BAD": raise RuntimeError("yahoo down")
        if ticker == "NOOPT": return None
        s = snapshot(7.4, 1200, 600)
        return s.quote, s.expiry_after_catalyst
    P.fetch_iv_reference = fake_ref
    real_sleep = asyncio.sleep
    async def no_sleep(_): await real_sleep(0)
    P.asyncio.sleep = no_sleep
    ev = lambda t, kind="PDUFA", d=CATALYST, st="OUTSIDE_WINDOW": CatalystEvent(event_type=kind, ticker=t, title="x", start_date=d,
        source_url=f"https://example.invalid/{t}{kind}{d}", raw_source="SEC_EDGAR_FTS", horizon_status=st,
        resolved_nct_id="NCT1", resolution_tier="ACRONYM")
    events = [ev("AAA"), ev("AAA", d=CATALYST + timedelta(days=30)), ev("BAD"), ev("NOOPT"), ev("PAST", d=TODAY - timedelta(days=3)),
              ev("ADC", kind="ADCOM"), ev(None)]
    saved = await P.record_iv_snapshots(events, TODAY)
    check("IV readings: each ticker with an upcoming PDUFA date or AdCom once; past events and no-ticker rows skipped", sorted(asked) == ["AAA", "ADC", "BAD", "NOOPT"])
    check("a failing ticker and a ticker with no options don't stop the rest", saved == (2, 4) and len(await db.get_iv_history("AAA", TODAY)) == 1)

    # ---------------- scan level: screen-out recorded and shown ----------------
    async def fake_ingest(problems=None):
        return [ev("PRICEY", st="PDUFA_ENTRY_WINDOW"), ev("GOOD", st="PDUFA_ENTRY_WINDOW")]
    P.ingest_catalysts = fake_ingest
    snaps = {"PRICEY": snapshot(1.20, 1200, 600), "GOOD": snapshot(7.40, 1200, 600)}
    async def snap_by_ticker(ticker, catalyst_date): return snaps[ticker]
    P.fetch_market_snapshot = snap_by_ticker
    state["p_fund"] = 0.83
    settings.TELEGRAM_BOT_TOKEN = settings.TELEGRAM_CHAT_ID = settings.DISCORD_WEBHOOK_URL = None
    settings.DASHBOARD_WEBHOOK_URL = BASE + "/dash"
    # 30 earlier daily IV readings, all higher than today's 0.9, so today ranks as cheap (IV Rank 0)
    for t in ("GOOD", "GOOD2"):
        for i in range(1, 31):
            await db.save_iv_snapshot(t, TODAY - timedelta(days=i), 1.0 + 0.05 * i, CATALYST, 7.4)
    scan = await P.run_discovery_scan()
    report = P._format_discovery_report(scan)
    check("scan: one scored, one screened out, IV readings counted", len(scan.pipeline_results) == 1 and [o.event.ticker for o in scan.evicted] == ["PRICEY"] and scan.iv_snapshots == (2, 2))
    check("report lists the screen-out and its reason", "Screened out by Phase A filters: 1" in report and "PRICEY | " in report and "stock price $1.20 is outside the allowed range ($2 minimum)" in report)
    check("report shows the IV Rank and how much history is behind it", "IV_Rank: 0.0 (31 daily reading(s) on record; 30 needed)" in report)
    check("report says why the signal fired", "Why: clinical score 0.83; event move priced 24%; IV Rank 0" in report)
    st = await D.build_state()
    check("dashboard data includes the screen-out", [(x["ticker"], x["reasons"]) for x in st["screened"]] == [("PRICEY", ["stock price $1.20 is outside the allowed range ($2 minimum)"])])
    check("the scored candidate raised an alert", [a.status for a in scan.alerts] == ["SENT"] and received[-1][0] == "/dash")

    # ---------------- exit reminders ----------------
    exit_day = CATALYST - timedelta(days=c.PDUFA_EXIT_DAYS_PRIOR)
    u = await AC.create_user("exit@test.invalid", "correct horse battery")
    await AC._set_destination(u.id, "discord", BASE + "/discord/u")
    received.clear()
    check("before the exit date: nothing sent", await A.notify_due_exits(as_of=exit_day - timedelta(days=1)) == [] and not received)
    notices = await A.notify_due_exits(as_of=exit_day)
    check("on the exit date: one reminder, to the operator channel and the customer",
          [(n.ticker, n.label, n.trigger_date, n.status) for n in notices] == [("GOOD", "PDUFA_EXIT", exit_day, "SENT")] and sorted(p for p, _ in received) == ["/dash", "/discord/u"])
    text = [b for p, b in received if p == "/discord/u"][0]["content"]
    print("----- exit reminder -----"); print(text); print("-------------------------")
    check("reminder names the stock, the event and that it restates the earlier plan", text.startswith("EXIT REMINDER: GOOD") and "is 14 days away" in text and "_EXIT" not in text and "not a new assessment" in text)
    check("dashboard webhook gets a structured exit record", [b for p, b in received if p == "/dash"][0]["type"] == "exit_reminder")
    n = len(received)
    check("the reminder is sent once", await A.notify_due_exits(as_of=exit_day) == [] and await A.notify_due_exits(as_of=exit_day + timedelta(days=2)) == [] and len(received) == n)
    check("after the event has passed: nothing", await A.notify_due_exits(as_of=CATALYST + timedelta(days=1)) == [])

    # a second alert whose reminder fails the first time
    snaps["GOOD2"] = snapshot(7.40, 1200, 600)
    async def ingest2(problems=None): return [ev("GOOD2", st="PDUFA_ENTRY_WINDOW")]
    P.ingest_catalysts = ingest2
    await P.run_discovery_scan()
    fail_all[0] = True
    notices = await A.notify_due_exits(as_of=exit_day + timedelta(days=1))
    check("scan missed on the exact day -> still sent the next day; total failure reported", [(n.ticker, n.status) for n in notices] == [("GOOD2", "FAILED")])
    fail_all[0] = False
    notices = await A.notify_due_exits(as_of=exit_day + timedelta(days=1))
    check("a reminder that reached nobody is retried on the next scan", [(n.ticker, n.status) for n in notices] == [("GOOD2", "SENT")])

    # stock-only track has no dated exit
    snaps["EQ"] = snapshot(7.40, 300, 600); state["p_fund"] = 0.90
    async def ingest3(problems=None): return [ev("EQ", st="PDUFA_ENTRY_WINDOW")]
    P.ingest_catalysts = ingest3
    scan = await P.run_discovery_scan()
    check("(stock-only alert raised)", scan.pipeline_results[0].candidate.arbitrage_signal == "LONG_TERM_EQUITY_ACCUMULATION")
    check("stock-only track has no dated exit, so no reminder", await A.notify_due_exits(as_of=exit_day + timedelta(days=3)) == [])
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
