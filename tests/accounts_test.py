import asyncio, json, sys, threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from sqlalchemy import select, func, delete

from aegis_bioquant.config import settings
from aegis_bioquant.core import accounts as AC
from aegis_bioquant.core import database as db
from aegis_bioquant.engines.clinical_nlp.classifier import FundamentalScore, ScoreAdjustment
from aegis_bioquant.logic.matrix_matcher import Candidate, evaluate_candidate
from aegis_bioquant.logic.risk_manager import build_execution_plan
from aegis_bioquant.telemetry import alerts as A

received = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(404 if self.path.startswith("/fail") else 200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"
BOT = "999:SECRET-BOT-TOKEN"

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

async def err(coro):
    try: await coro
    except AC.AccountError as e: return e.code
    return None

def make(ticker):
    cand = Candidate(ticker=ticker, catalyst_type="PDUFA", catalyst_date=date(2026, 11, 14), spot_price=7.42,
                     open_interest=4512, daily_options_volume=2002, p_fundamental=0.83, p_market=0.40, p_market_confidence="GOOD", event_implied_move=0.25, event_move_confidence="GOOD", iv_rank=20.0)
    evaluate_candidate(cand)
    fs = FundamentalScore(nct_id="NCT05767346", base_rate=0.83, p_fundamental=0.83,
                          adjustments=[ScoreAdjustment("primary_endpoint_significance", 0.05, "test")])
    return SimpleNamespace(candidate=cand, fundamental_score=fs, market_snapshot=None,
                           execution_plan=build_execution_plan(cand, total_cash=100_000.0))

async def cleanup():
    f = db.get_session_factory()
    async with f() as s:
        ids = list(await s.scalars(select(db.UserRecord.id).where(db.UserRecord.email.like("%@test.invalid"))))
        for model in (db.AlertDeliveryRecord, db.AlertDestinationRecord, db.TelegramLinkTokenRecord):
            await s.execute(delete(model).where(model.user_id.in_(ids)))
        await s.execute(delete(db.UserRecord).where(db.UserRecord.id.in_(ids)))
        await s.execute(delete(db.PipelineLockRecord).where(db.PipelineLockRecord.key.like("alert:ZZ%")))
        await s.commit()
    return len(ids)

async def count(model, *where):
    f = db.get_session_factory()
    async with f() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where))

async def main():
    await db.init_db()
    await cleanup()
    settings.TELEGRAM_BOT_TOKEN = settings.TELEGRAM_CHAT_ID = settings.DISCORD_WEBHOOK_URL = settings.DASHBOARD_WEBHOOK_URL = None
    settings.TELEGRAM_API_BASE_URL = BASE

    # ---- accounts ----
    u1 = await AC.create_user("  Alice@Test.Invalid ", "correct horse battery")
    check("user created, email normalised, password hashed", u1.id and u1.email == "alice@test.invalid" and u1.password_hash.startswith("$argon2id$") and "correct horse" not in u1.password_hash)
    check("duplicate email (different case) refused", await err(AC.create_user("ALICE@test.invalid", "another password!")) == "EMAIL_TAKEN")
    check("invalid email refused", await err(AC.create_user("not-an-email", "correct horse battery")) == "INVALID_EMAIL")
    check("short password refused", await err(AC.create_user("short@test.invalid", "short")) == "WEAK_PASSWORD")
    check("oversized password refused", await err(AC.create_user("long@test.invalid", "x" * 5000)) == "WEAK_PASSWORD")
    check("login with right password", (await AC.authenticate("ALICE@test.invalid", "correct horse battery")).id == u1.id)
    check("login with wrong password refused", await AC.authenticate("alice@test.invalid", "wrong password!!") is None)
    check("login for unknown email refused", await AC.authenticate("nobody@test.invalid", "correct horse battery") is None)
    u2 = await AC.create_user("bob@test.invalid", "bob has a long password")
    u3 = await AC.create_user("carol@test.invalid", "carol has a long password")

    # ---- discord validation ----
    good = "https://discord.com/api/webhooks/123456789012345678/AbC-dEf_123"
    for url in (good, "https://discordapp.com/api/webhooks/1/abc", "https://canary.discord.com/api/webhooks/1/abc", good + "?wait=true"):
        check(f"accepts {url[:48]}", await err(AC.set_discord_webhook(u1.id, url)) is None)
    stored = (await AC.get_destinations(u1.id))[0].address
    check("query string stripped, one row per user per channel", stored == good and len(await AC.get_destinations(u1.id)) == 1)
    for url in ("http://discord.com/api/webhooks/1/abc", "https://discord.com.evil.example/api/webhooks/1/abc",
                "https://evil.example/api/webhooks/1/abc", "http://127.0.0.1:8765/webhook", "http://169.254.169.254/latest/meta-data",
                "https://discord.com/api/webhooks/1/abc/../../users/@me", "https://discord.com@evil.example/api/webhooks/1/abc",
                "https://discord.com/api/webhooks/1/abc\nhttps://evil.example", "file:///etc/passwd", ""):
        check(f"rejects {url[:52]!r}", await err(AC.set_discord_webhook(u1.id, url)) == "INVALID_DISCORD_WEBHOOK")
    check("rejected URLs did not overwrite the stored one", (await AC.get_destinations(u1.id))[0].address == good)

    # ---- telegram linking ----
    settings.TELEGRAM_BOT_USERNAME = None
    check("no bot username -> clear error", await err(AC.create_telegram_link(u1.id)) == "TELEGRAM_NOT_CONFIGURED")
    settings.TELEGRAM_BOT_USERNAME = "AegisTestBot"
    link1 = await AC.create_telegram_link(u1.id)
    link2 = await AC.create_telegram_link(u1.id)
    tok1, tok2 = link1.split("start=")[1], link2.split("start=")[1]
    check("link format", link2.startswith("https://t.me/AegisTestBot?start=") and len(tok2) == 32 and tok1 != tok2)
    upd = lambda text, chat=555001: {"update_id": 1, "message": {"chat": {"id": chat, "type": "private"}, "text": text}}
    r = await AC.handle_telegram_update(upd(f"/start {tok1}"))
    check("older code invalidated by a newer one", "expired" in r.text and await count(db.AlertDestinationRecord, db.AlertDestinationRecord.channel == "telegram", db.AlertDestinationRecord.user_id == u1.id) == 0)
    r = await AC.handle_telegram_update(upd(f"/start {tok2}"))
    tg = [d for d in await AC.get_destinations(u1.id) if d.channel == "telegram"]
    check("valid code links the chat to the right user", r.text.startswith("Connected") and r.chat_id == "555001" and len(tg) == 1 and tg[0].address == "555001")
    r = await AC.handle_telegram_update(upd(f"/start {tok2}", chat=666002))
    check("code is single-use (second chat cannot reuse it)", "expired" in r.text and await count(db.AlertDestinationRecord, db.AlertDestinationRecord.address == "666002") == 0)
    AC.TELEGRAM_LINK_TTL_SECONDS = -1
    tok3 = (await AC.create_telegram_link(u2.id)).split("start=")[1]
    AC.TELEGRAM_LINK_TTL_SECONDS = 900
    r = await AC.handle_telegram_update(upd(f"/start {tok3}", chat=777003))
    check("expired code refused", "expired" in r.text and not [d for d in await AC.get_destinations(u2.id) if d.channel == "telegram"])
    check("guessed code refused", "expired" in (await AC.handle_telegram_update(upd("/start AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))).text)
    check("/start with no code -> instructions", "Connect Telegram" in (await AC.handle_telegram_update(upd("/start"))).text)
    for junk in (upd("hello"), {}, {"message": None}, {"message": {"chat": {}, "text": "/start x"}}, {"edited_message": {}}, "nonsense", None):
        check(f"ignores {str(junk)[:40]}", await AC.handle_telegram_update(junk) is None)
    r = await AC.handle_telegram_update(upd("/stop@AegisTestBot"))
    check("/stop unlinks the chat", "off" in r.text and not [d for d in await AC.get_destinations(u1.id) if d.channel == "telegram"])
    check("/stop on an unlinked chat", "isn't connected" in (await AC.handle_telegram_update(upd("/stop"))).text)
    tok4 = (await AC.create_telegram_link(u1.id)).split("start=")[1]
    await AC.handle_telegram_update(upd(f"/start@AegisTestBot {tok4}"))
    check("relink works (group-style /start@Bot)", [d.address for d in await AC.get_destinations(u1.id) if d.channel == "telegram"] == ["555001"])

    # ---- fan-out ----   u1: telegram + discord(ok)   u2: discord(fails)   u3: nothing
    await AC._set_destination(u1.id, "discord", BASE + "/discord/u1")
    await AC._set_destination(u2.id, "discord", BASE + "/fail/u2")
    settings.TELEGRAM_BOT_TOKEN = None
    r = await A.notify_if_actionable(make("ZZT0"))
    check("no bot token: customer Telegram skipped with a warning, Discord still sent",
          r.status == "PARTIAL" and any("Telegram destination(s) skipped" in w for w in r.warnings) and sorted(p for p, _ in received) == ["/discord/u1", "/fail/u2"])
    received.clear()
    settings.TELEGRAM_BOT_TOKEN, settings.DASHBOARD_WEBHOOK_URL = BOT, BASE + "/dash"
    r = await A.notify_if_actionable(make("ZZT1"))
    by = {p: b for p, b in received}
    check("alert fanned out to operator dashboard + each customer destination",
          sorted(by) == sorted(["/dash", f"/bot{BOT}/sendMessage", "/discord/u1", "/fail/u2"]) and r.status == "PARTIAL")
    check("customer Telegram message goes to that customer's chat", by[f"/bot{BOT}/sendMessage"]["chat_id"] == "555001" and by[f"/bot{BOT}/sendMessage"]["text"].startswith("BUY SETUP: "))
    cust = sorted((o.user_id, o.channel, o.ok, o.detail) for o in r.outcomes if o.destination_id is not None)
    check("outcomes attribute each delivery to its user", cust == sorted([(u1.id, "discord", True, "HTTP 200"), (u1.id, "telegram", True, "HTTP 200"), (u2.id, "discord", False, "HTTP 404")]))
    check("deliveries logged", await count(db.AlertDeliveryRecord, db.AlertDeliveryRecord.ticker == "ZZT1") == 3
          and await count(db.AlertDeliveryRecord, db.AlertDeliveryRecord.ticker == "ZZT1", db.AlertDeliveryRecord.ok.is_(False)) == 1)
    n = len(received)
    r = await A.notify_if_actionable(make("ZZT1"))
    check("cooldown still stops repeats for everyone", r.status == "SUPPRESSED_COOLDOWN" and len(received) == n)

    # ---- auto switch-off after repeated failures (u2 has failed twice so far) ----
    warned = []
    for i in (2, 3, 4):
        warned += (await A.notify_if_actionable(make(f"ZZT{i}"))).warnings
    d2 = (await AC.get_destinations(u2.id))[0]
    check("destination switched off at the 5th consecutive failure, with a warning", d2.enabled is False and d2.consecutive_failures == 5 and warned == ["1 customer destination(s) switched off after repeated failures"])
    received.clear()
    r = await A.notify_if_actionable(make("ZZT5"))
    u1_dests = await AC.get_destinations(u1.id)
    check("switched-off destination is no longer attempted; healthy ones unaffected", r.status == "SENT" and not any(p.startswith("/fail") for p, _ in received) and all(d.consecutive_failures == 0 for d in u1_dests))
    await AC._set_destination(u2.id, "discord", BASE + "/discord/u2")
    d2 = (await AC.get_destinations(u2.id))[0]
    check("saving a new address switches it back on", d2.enabled is True and d2.consecutive_failures == 0)

    settings.DASHBOARD_WEBHOOK_URL = None
    received.clear()
    r = await A.notify_if_actionable(make("ZZT6"))
    check("customers are alerted even with no operator channel configured", r.status == "SENT" and len(received) == 3)
    check("removing a destination", await AC.remove_destination(u1.id, "discord") and not await AC.remove_destination(u3.id, "discord"))
    everything = " ".join([str(o.detail) for o in r.outcomes] + r.warnings)
    check("outcomes and warnings carry no token, address or email", "SECRET" not in everything and "555001" not in everything and "@" not in everything and "discord/u" not in everything)
    print("test users removed:", await cleanup())
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
