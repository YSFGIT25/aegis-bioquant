import asyncio, json, os, re, sys, threading
os.environ["SESSION_COOKIE_SECURE"] = "0"
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from sqlalchemy import select
from aegis_bioquant.config import settings
from aegis_bioquant.core import accounts as AC
from aegis_bioquant.core import database as db
from aegis_bioquant.web.app import app, SESSION_COOKIE

sent = []          # (authorization header, body)
mail_status = [200]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/emails" and mail_status[0] == 200:
            sent.append((self.headers.get("Authorization"), body))
        self.send_response(mail_status[0]); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond: sys.exit(1)

def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

def link_in(body, path):
    m = re.search(r"https://fdaodds\.example" + path + r"\?token=([A-Za-z0-9_%-]+)", body["text"])
    return m.group(1) if m else None

HOOK = "https://discord.com/api/webhooks/123456789012345678/abc"
PW = "correct horse battery"

async def uid(email):
    return (await AC.get_user_by_email(email)).id

async def main():
    await db.init_db()

    # ---------------- email NOT configured: site behaves as before ----------------
    settings.RESEND_API_KEY = settings.EMAIL_FROM = settings.PUBLIC_BASE_URL = None
    async with client() as c:
        r = await c.post("/signup", data={"email": "early@test.invalid", "password": PW})
        page = (await c.get("/settings")).text
        check("email off: sign-up works, no confirmation demanded, nothing sent",
              r.status_code == 303 and "Confirm your email" not in page and not sent)
        r = await c.post("/settings/discord", data={"webhook_url": HOOK, "csrf_token": csrf(page)})
        check("email off: unverified account can still connect a channel", r.headers["location"] == "/settings?msg=discord_saved")
        check("email off: no forgot-password link on the login page", "Forgot password" not in (await client().get("/login")).text)
        r = await c.get("/forgot")
        check("email off: reset page says it isn't available", "isn't available yet" in r.text.replace("&#39;", "'"))
        r = await c.post("/forgot", data={"email": "early@test.invalid"})
        check("email off: reset request sends nothing", r.status_code == 200 and not sent)

    # ---------------- email configured ----------------
    settings.RESEND_API_KEY, settings.EMAIL_FROM = "re_SECRETKEY", "FDAOdds <accounts@fdaodds.example>"
    settings.PUBLIC_BASE_URL, settings.EMAIL_API_BASE_URL = "https://fdaodds.example", f"http://127.0.0.1:{srv.server_port}"

    async with client() as a, client() as a2, client() as anon:
        r = await a.post("/signup", data={"email": "alice@test.invalid", "password": PW})
        check("sign-up sends exactly one confirmation email", r.status_code == 303 and len(sent) == 1)
        auth, body = sent[-1]
        tok = link_in(body, "/verify")
        check("email goes to the new address, from the configured sender, with the API key as a bearer token",
              body["to"] == ["alice@test.invalid"] and body["from"] == "FDAOdds <accounts@fdaodds.example>" and auth == "Bearer re_SECRETKEY" and "Confirm" in body["subject"])
        check("the link points at the configured site address and appears in both text and HTML", tok and f"https://fdaodds.example/verify?token={tok}" in body["html"])
        async with db.get_session_factory()() as s:
            stored = list(await s.scalars(select(db.EmailTokenRecord.token_hash)))
        check("the token is stored only as a hash", tok not in stored and len(stored[0]) == 64)

        page = (await a.get("/settings")).text
        tok_a = csrf(page)
        check("unverified: banner shown, connect controls replaced", "Confirm your email address to connect alert channels" in page
              and "Confirm your email address to connect Discord" in page and 'name="webhook_url"' not in page)
        check("unverified: dashboard still viewable", (await a.get("/dashboard")).status_code == 200)
        r = await a.post("/settings/discord", data={"webhook_url": HOOK, "csrf_token": tok_a})
        check("unverified: saving a webhook refused", r.headers["location"] == "/settings?msg=verify_first" and not await AC.get_destinations(await uid("alice@test.invalid")))
        settings.TELEGRAM_BOT_USERNAME = "FDAOddsTestBot"
        r = await a.post("/settings/telegram/connect", data={"csrf_token": tok_a})
        check("unverified: Telegram connect refused", r.headers["location"] == "/settings?msg=verify_first")

        r = await anon.get("/verify?token=not-a-real-token")
        check("bogus confirmation link -> expired page", r.status_code == 400 and "expired" in r.text)
        r = await anon.get(f"/reset?token={tok}")
        check("a confirmation token is not accepted as a reset token", r.status_code == 400)
        r = await anon.get(f"/verify?token={tok}")
        check("confirmation link works (even from a browser that isn't logged in)", r.status_code == 200 and "Email confirmed" in r.text)
        check("confirmation link is single-use", (await anon.get(f"/verify?token={tok}")).status_code == 400)
        page = (await a.get("/settings")).text
        check("verified: banner gone, controls back", "Confirm your email address" not in page and 'name="webhook_url"' in page)
        r = await a.post("/settings/discord", data={"webhook_url": HOOK, "csrf_token": tok_a})
        check("verified: webhook saved", r.headers["location"] == "/settings?msg=discord_saved")
        check("verified: resend does nothing more", (await a.post("/verify/resend", data={"csrf_token": tok_a})).headers["location"] == "/settings" and len(sent) == 1)

        # ---- resend + limits ----
        async with client() as b:
            await b.post("/signup", data={"email": "bob@test.invalid", "password": PW})
            first = link_in(sent[-1][1], "/verify")
            tok_b = csrf((await b.get("/settings")).text)
            r = await b.post("/verify/resend", data={})
            check("resend without CSRF token refused", r.headers["location"] == "/login" and len(sent) == 2)
            r = await b.post("/verify/resend", data={"csrf_token": tok_b})
            second = link_in(sent[-1][1], "/verify")
            check("resend sends a new link", r.headers["location"] == "/settings?msg=verify_sent" and len(sent) == 3 and second != first)
            check("the older link stops working once a newer one is sent", (await anon.get(f"/verify?token={first}")).status_code == 400)
            await b.post("/verify/resend", data={"csrf_token": tok_b}); await b.post("/verify/resend", data={"csrf_token": tok_b})
            n = len(sent)
            r = await b.post("/verify/resend", data={"csrf_token": tok_b})
            check("4th resend in an hour is refused and sends nothing", r.headers["location"] == "/settings?msg=verify_limited" and len(sent) == n)
            check("limit notice shown", "Too many confirmation emails" in (await b.get("/settings?msg=verify_limited")).text)

        # ---- forgot password ----
        check("login page now offers Forgot password", 'href="/forgot"' in (await anon.get("/login")).text)
        n = len(sent)
        r_unknown = await anon.post("/forgot", data={"email": "nobody@test.invalid"})
        r_known = await anon.post("/forgot", data={"email": "ALICE@test.invalid"})
        check("same page whether or not the address has an account", r_unknown.status_code == r_known.status_code == 200 and r_unknown.text == r_known.text and "If that address has an account" in r_known.text)
        check("only the real account gets an email", len(sent) == n + 1 and sent[-1][1]["to"] == ["alice@test.invalid"] and "Reset" in sent[-1][1]["subject"])
        rtok_old = link_in(sent[-1][1], "/reset")
        r = await anon.post("/forgot", data={"email": "alice@test.invalid"}, headers={"Host": "evil.example"})
        rtok = link_in(sent[-1][1], "/reset")
        check("a forged Host header cannot change where the emailed link points", rtok and "evil.example" not in json.dumps(sent[-1][1]))
        check("a newer reset link cancels the older one", (await anon.get(f"/reset?token={rtok_old}")).status_code == 400)
        await anon.post("/forgot", data={"email": "alice@test.invalid"})
        rtok = link_in(sent[-1][1], "/reset")
        n = len(sent)
        await anon.post("/forgot", data={"email": "alice@test.invalid"})
        check("4th reset request for one address in an hour sends nothing", len(sent) == n)

        # ---- reset ----
        check("a reset token is not accepted as a confirmation token", (await anon.get(f"/verify?token={rtok}")).status_code == 400)
        r = await anon.get(f"/reset?token={rtok}")
        check("reset link opens the new-password form", r.status_code == 200 and f'name="token" value="{rtok}"' in r.text)
        check("opening the form does not use the link up", (await anon.get(f"/reset?token={rtok}")).status_code == 200)
        r = await anon.post("/reset", data={"token": rtok, "password": "short"})
        check("too-short new password -> error, link still usable", r.status_code == 400 and "Password must be" in r.text and (await anon.get(f"/reset?token={rtok}")).status_code == 200)
        r = await anon.post("/reset", data={"token": "bogus", "password": "a brand new password"})
        check("bogus token cannot set a password", r.status_code == 400 and (await AC.authenticate("alice@test.invalid", PW)) is not None)

        await a2.post("/login", data={"email": "alice@test.invalid", "password": PW})
        check("(alice is logged in on two browsers)", (await a.get("/dashboard")).status_code == 200 and (await a2.get("/dashboard")).status_code == 200)
        for _ in range(8):
            await anon.post("/login", data={"email": "alice@test.invalid", "password": "guess guess guess"})
        r = await anon.post("/reset", data={"token": rtok, "password": "a brand new password"})
        check("reset succeeds -> back to login with a notice", r.status_code == 303 and r.headers["location"] == "/login?msg=password_reset"
              and "Password changed" in (await anon.get("/login?msg=password_reset")).text)
        check("every existing session is logged out", (await a.get("/dashboard")).status_code == 303 and (await a2.get("/dashboard")).status_code == 303)
        check("old password no longer works", await AC.authenticate("alice@test.invalid", PW) is None)
        r = await anon.post("/login", data={"email": "alice@test.invalid", "password": "a brand new password"})
        check("new password works, and the earlier lockout is cleared", r.status_code == 303 and r.headers["location"] == "/dashboard")
        r = await anon.post("/reset", data={"token": rtok, "password": "yet another password"})
        check("reset link is single-use", r.status_code == 400 and await AC.authenticate("alice@test.invalid", "a brand new password") is not None)

        # reset proves the address: bob (unverified) resets and becomes verified
        async with db.get_session_factory()() as s:   # clear bob's verify-email quota; reset has its own
            pass
        await anon.post("/forgot", data={"email": "bob@test.invalid"})
        btok = link_in(sent[-1][1], "/reset")
        await anon.post("/reset", data={"token": btok, "password": "bob picks a new one"})
        check("resetting through the emailed link also confirms the address", (await AC.get_user_by_email("bob@test.invalid")).email_verified_at is not None)

        # ---- email service failing ----
        mail_status[0] = 500
        async with client() as d:
            r = await d.post("/signup", data={"email": "dave@test.invalid", "password": PW})
            check("email service returning errors: sign-up still succeeds", r.status_code == 303 and (await d.get("/dashboard")).status_code == 200)
            r = await d.post("/forgot", data={"email": "dave@test.invalid"})
            check("email service returning errors: reset request still answers normally", r.status_code == 200 and "If that address has an account" in r.text)
        settings.EMAIL_API_BASE_URL = "http://127.0.0.1:9"
        async with client() as e:
            r = await e.post("/signup", data={"email": "erin@test.invalid", "password": PW})
            check("email service unreachable: sign-up still succeeds", r.status_code == 303)
    await db.dispose_db()

asyncio.run(main())
print("ALL PASSED")
