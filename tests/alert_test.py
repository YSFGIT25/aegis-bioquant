import asyncio, json, sys, threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from aegis_bioquant.config import settings
from aegis_bioquant.core.database import init_db
from aegis_bioquant.core.cache import is_locked
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan
from aegis_bioquant.telemetry import alerts as A

received = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received.append((self.path, body))
        self.send_response(500 if self.path.startswith("/fail") else 200)
        self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"
TOKEN = "123456:SECRET-TOKEN-xyz"

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def make(ticker, pf, pm, oi=4512, iv=None, reason="primary endpoint strongly significant (p~0.0001)"):
    cand = Candidate(ticker=ticker, catalyst_type="PDUFA", catalyst_date=date(2026, 11, 14), spot_price=7.42,
                     open_interest=oi, daily_options_volume=2002, p_fundamental=pf, p_market=pm,
                     event_implied_move=(0.25 if pm is not None else None), event_move_confidence=("GOOD" if pm is not None else None),
                     iv_rank=(None if pm is None else (20.0 if pf >= 0.75 else 70.0)),
                     p_market_confidence="GOOD" if pm is not None else "LOW_CONFIDENCE_WIDE_SPREAD")
    evaluate_candidate(cand)
    fs = FundamentalScore(nct_id="NCT05767346", base_rate=0.83, p_fundamental=pf, adjustments=[
        ScoreAdjustment("primary_endpoint_significance", 0.05, reason),
        ScoreAdjustment("safety_mortality_imbalance", -0.05, "1 more death(s) on treatment arm than control")])
    return SimpleNamespace(candidate=cand, fundamental_score=fs, market_snapshot=None,
                           execution_plan=build_execution_plan(cand, total_cash=100_000.0))

def set_channels(tg=True, discord="/discord", dash="/dash", base=BASE):
    settings.TELEGRAM_BOT_TOKEN = TOKEN if tg else None
    settings.TELEGRAM_CHAT_ID = "-100200300" if tg else None
    settings.TELEGRAM_API_BASE_URL = base
    settings.DISCORD_WEBHOOK_URL = base + discord if discord else None
    settings.DASHBOARD_WEBHOOK_URL = base + dash if dash else None

AS_OF = date(2026, 10, 5)

async def main():
    await init_db()
    long_alpha = make("CYTK", 0.83, 0.40)
    check("fixture is LONG_RUN_UP_ALPHA", long_alpha.candidate.arbitrage_signal == "LONG_RUN_UP_ALPHA")

    set_channels()
    r = await A.notify_if_actionable(make("NOSG", 0.83, None), as_of=AS_OF)
    check("NO_SIGNAL -> NOT_ACTIONABLE, nothing sent", r.status == "NOT_ACTIONABLE" and not received)

    set_channels(tg=False, discord=None, dash=None)
    r = await A.notify_if_actionable(long_alpha, as_of=AS_OF)
    key = A.alert_cooldown_key("CYTK", date(2026, 11, 14), "LONG_RUN_UP_ALPHA")
    check("no channels -> NO_CHANNELS, nothing sent, cooldown not consumed",
          r.status == "NO_CHANNELS" and not received and not await is_locked(key))

    set_channels()
    r = await A.notify_if_actionable(long_alpha, as_of=AS_OF)
    check("all channels -> SENT", r.status == "SENT" and len(r.outcomes) == 3 and all(o.ok for o in r.outcomes))
    by_path = dict(received)
    tg = by_path[f"/bot{TOKEN}/sendMessage"]
    check("telegram request shape", tg["chat_id"] == "-100200300" and tg["text"].startswith("BUY SETUP: "))
    check("discord request shape", by_path["/discord"]["content"] == tg["text"])
    check("discord alerts are sent under the product name", by_path["/discord"]["username"] == "FDAOdds")
    back = A.AlertPayload.model_validate(by_path["/dash"])
    check("dashboard JSON round-trips through the schema",
          back.ticker == "CYTK" and back.catalyst.days_until == 40 and back.signal.divergence_delta is not None
          and abs(back.signal.divergence_delta - 0.43) < 1e-9 and back.execution.use_options
          and back.market.liquidity_checks.spot_price_in_range and len(back.clinical.adjustments) == 2)
    print("----- alert text -----"); print(tg["text"]); print("----------------------")
    print("----- dashboard JSON (keys) -----"); print({k: (list(v) if isinstance(v, dict) else v) for k, v in by_path["/dash"].items()})

    n = len(received)
    r = await A.notify_if_actionable(long_alpha, as_of=AS_OF)
    check("repeat -> SUPPRESSED_COOLDOWN, nothing sent", r.status == "SUPPRESSED_COOLDOWN" and len(received) == n)

    eq = make("CYTK", 0.90, None, oi=1200)
    check("fixture is LONG_TERM_EQUITY_ACCUMULATION", eq.candidate.arbitrage_signal == "LONG_TERM_EQUITY_ACCUMULATION")
    r = await A.notify_if_actionable(eq, as_of=AS_OF)
    check("same ticker, changed signal -> alerts again", r.status == "SENT" and len(received) == n + 3)
    print("----- equity-track text -----"); print(received[-1][1].get("text") or [b for p, b in received[-3:] if "text" in b][0]["text"]); print("----------------------")

    avoid = make("SHRT", 0.35, 0.80)
    check("fixture is STRUCTURAL_COLLAPSE_AVOID", avoid.candidate.arbitrage_signal == "STRUCTURAL_COLLAPSE_AVOID")
    set_channels(discord="/fail/discord")
    r = await A.notify_if_actionable(avoid, as_of=AS_OF)
    d = {o.channel: o for o in r.outcomes}
    check("one channel 500 -> PARTIAL, others delivered",
          r.status == "PARTIAL" and not d["discord"].ok and d["discord"].detail == "HTTP 500" and d["telegram"].ok and d["dashboard"].ok)
    check("PARTIAL keeps cooldown", await is_locked(A.alert_cooldown_key("SHRT", date(2026, 11, 14), "STRUCTURAL_COLLAPSE_AVOID")))

    dead = make("DEAD", 0.83, 0.40)
    dkey = A.alert_cooldown_key("DEAD", date(2026, 11, 14), "LONG_RUN_UP_ALPHA")
    set_channels(base="http://127.0.0.1:9")  # nothing listens here
    r = await A.notify_if_actionable(dead, as_of=AS_OF)
    details = " ".join(o.detail for o in r.outcomes)
    print("failure details:", [(o.channel, o.detail) for o in r.outcomes])
    check("all channels unreachable -> FAILED", r.status == "FAILED" and len(r.outcomes) == 3)
    check("failure details leak no token/URL", "SECRET" not in details and "http" not in details.lower().replace("http ", ""))
    check("FAILED releases cooldown", not await is_locked(dkey))
    set_channels()
    r = await A.notify_if_actionable(dead, as_of=AS_OF)
    check("retry after outage -> SENT", r.status == "SENT")

    n = len(received)
    huge = make("LONG", 0.83, 0.40, reason="x" * 9000 + " *_[markdown]_* <b>html</b> & émoji 🚀")
    r = await A.notify_if_actionable(huge, as_of=AS_OF)
    sent = dict(received[n:])
    check("oversized text truncated to each service's limit",
          r.status == "SENT" and len(sent[f"/bot{TOKEN}/sendMessage"]["text"]) == 4096 and len(sent["/discord"]["content"]) == 2000)
    check("dashboard keeps the full untruncated reason", len(sent["/dash"]["clinical"]["adjustments"][0]["reason"]) > 9000)

asyncio.run(main())
print("ALL PASSED")
