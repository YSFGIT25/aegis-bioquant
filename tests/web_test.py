import asyncio, json, os, re, sys, threading
os.environ["SESSION_COOKIE_SECURE"] = "0"
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from aegis_bioquant.config import settings
from aegis_bioquant.core import accounts as AC
from aegis_bioquant.core.database import dispose_db, init_db
from aegis_bioquant.web.app import app, SESSION_COOKIE

received = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
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

GOOD_HOOK = "https://discord.com/api/webhooks/123456789012345678/SECRETPART_abc-123"

async def main():
    await init_db()
    settings.TELEGRAM_BOT_USERNAME = settings.TELEGRAM_BOT_TOKEN = settings.TELEGRAM_WEBHOOK_SECRET = None
    settings.TELEGRAM_API_BASE_URL = f"http://127.0.0.1:{srv.server_port}"

    async with client() as a, client() as b, client() as anon:
        r = await anon.get("/")
        check("landing page loads with security headers", r.status_code == 200 and "Create account" in r.text
              and "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff")
        check("health check", (await anon.get("/healthz")).text == "ok")
        r = await anon.get("/static/dashboard.js")
        check("dashboard script served as a file", r.status_code == 200 and "javascript" in r.headers["content-type"] and "REFRESH_MS" in r.text)
        for path in ("/dashboard", "/settings"):
            r = await anon.get(path)
            check(f"{path} without login -> login page", r.status_code == 303 and r.headers["location"] == "/login")
        check("/api/state without login -> 401", (await anon.get("/api/state")).status_code == 401)

        # ---- sign-up ----
        r = await a.post("/signup", data={"email": "nope", "password": "correct horse battery"})
        check("sign-up: bad email -> error shown", r.status_code == 400 and "look like an email" in r.text)
        r = await a.post("/signup", data={"email": "alice@test.invalid", "password": "short"})
        check("sign-up: short password -> error shown, email kept in the form", r.status_code == 400 and "Password must be" in r.text and 'value="alice@test.invalid"' in r.text)
        r = await a.post("/signup", data={"email": "Alice@test.invalid", "password": "correct horse battery"})
        cookie = r.headers.get("set-cookie", "")
        check("sign-up: success -> logged in, sent to alert settings", r.status_code == 303 and r.headers["location"] == "/settings" and SESSION_COOKIE in a.cookies)
        check("session cookie is HttpOnly and SameSite=Lax", "httponly" in cookie.lower() and "samesite=lax" in cookie.lower())
        r = await anon.post("/signup", data={"email": "alice@test.invalid", "password": "another long password"})
        check("sign-up: email already used -> error", r.status_code == 400 and "already exists" in r.text)
        r = await b.post("/signup", data={"email": "<b>bob</b>@test.invalid", "password": "bob has a long password"})
        page = (await b.get("/settings")).text
        check("a user-typed email is escaped on the page", r.status_code == 303 and "<b>bob</b>" not in page and "&lt;b&gt;bob&lt;/b&gt;@test.invalid" in page)

        # ---- logged-in pages ----
        r = await a.get("/dashboard")
        check("dashboard loads for a logged-in user, with the connect prompt", r.status_code == 200 and 'id="alerts"' in r.text and "have not connected Telegram or Discord yet" in r.text and '/static/dashboard.js' in r.text)
        r = await a.get("/api/state")
        check("/api/state for a logged-in user", r.status_code == 200 and set(r.json()) >= {"alerts", "predictions", "catalysts_in_window", "today"})
        page = (await a.get("/settings")).text
        tok_a = csrf(page)
        check("settings page: nothing connected yet, Telegram unavailable without a bot", page.count("Not connected.") == 2 and "aren't available yet" in page)

        # ---- CSRF / cross-site ----
        r = await a.post("/settings/discord", data={"webhook_url": GOOD_HOOK})
        check("settings POST without CSRF token refused", r.status_code == 303 and r.headers["location"] == "/login" and not await AC.get_destinations(1))
        r = await a.post("/settings/discord", data={"webhook_url": GOOD_HOOK, "csrf_token": csrf((await b.get("/settings")).text)})
        check("settings POST with another user's CSRF token refused", r.headers["location"] == "/login" and not await AC.get_destinations(1))
        r = await a.post("/settings/discord", data={"webhook_url": GOOD_HOOK, "csrf_token": tok_a}, headers={"Origin": "https://evil.example"})
        check("POST from another site's Origin refused", r.status_code == 403 and not await AC.get_destinations(1))
        r = await anon.post("/login", data={"email": "x@test.invalid", "password": "whatever long"}, headers={"Origin": "null"})
        check("POST with a null Origin refused", r.status_code == 403)

        # ---- discord ----
        r = await a.post("/settings/discord", data={"webhook_url": "http://127.0.0.1:8765/webhook", "csrf_token": tok_a}, headers={"Origin": "http://testserver"})
        check("invalid webhook URL -> error shown, nothing saved", r.status_code == 400 and "isn&#39;t a Discord webhook URL" in r.text and not await AC.get_destinations(1))
        r = await a.post("/settings/discord", data={"webhook_url": GOOD_HOOK, "csrf_token": tok_a})
        check("valid webhook saved", r.status_code == 303 and r.headers["location"] == "/settings?msg=discord_saved")
        page = (await a.get("/settings?msg=discord_saved")).text
        check("settings shows it connected, without echoing the secret part of the URL", "Discord webhook saved" in page and "…345678" in page and "SECRETPART" not in page)
        check("dashboard prompt disappears once a channel is connected", "no alert channel connected" not in (await a.get("/dashboard")).text)
        check("unknown notice code shows nothing", "notice ok" not in (await a.get("/settings?msg=<script>alert(1)</script>")).text and "<script>alert" not in (await a.get("/settings?msg=<script>alert(1)</script>")).text)
        r = await b.post("/settings/discord/remove", data={"csrf_token": csrf((await b.get("/settings")).text)})
        check("one user's remove does not touch another user's webhook", r.status_code == 303 and len(await AC.get_destinations(1)) == 1)

        # ---- telegram ----
        r = await a.post("/settings/telegram/connect", data={"csrf_token": tok_a})
        check("Telegram connect without a bot configured -> friendly notice", r.headers["location"] == "/settings?msg=telegram_unavailable")
        r = await anon.post("/telegram/webhook", json={})
        check("Telegram webhook does not exist until configured", r.status_code == 404)
        settings.TELEGRAM_BOT_USERNAME, settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_WEBHOOK_SECRET = "FDAOddsTestBot", "1:BOT", "hook-secret"
        r = await a.post("/settings/telegram/connect", data={"csrf_token": tok_a})
        link = r.headers["location"]
        check("Telegram connect -> redirect to the bot with a one-time code", r.status_code == 303 and link.startswith("https://t.me/FDAOddsTestBot?start="))
        code = link.split("start=")[1]
        update = {"update_id": 1, "message": {"chat": {"id": 424242, "type": "private"}, "text": f"/start {code}"}}
        r = await anon.post("/telegram/webhook", json=update)
        check("Telegram webhook without the secret refused", r.status_code == 403)
        r = await anon.post("/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        check("Telegram webhook with a wrong secret refused", r.status_code == 403 and len(await AC.get_destinations(1)) == 1)
        r = await anon.post("/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})
        dests = {d.channel: d.address for d in await AC.get_destinations(1)}
        check("Telegram webhook with the secret links the chat", r.status_code == 200 and dests.get("telegram") == "424242")
        check("confirmation sent back to that chat through the bot", received and received[-1][0] == "/bot1:BOT/sendMessage" and received[-1][1]["chat_id"] == "424242" and received[-1][1]["text"].startswith("Connected"))
        r = await anon.post("/telegram/webhook", content=b"not json", headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})
        check("garbage body from Telegram is acknowledged, not an error", r.status_code == 200)
        page = (await a.get("/settings")).text
        check("settings shows Telegram connected", "Alerts are sent to your Telegram chat" in page and "Reconnect Telegram" in page and "424242" not in page)
        r = await a.post("/settings/telegram/remove", data={"csrf_token": tok_a})
        check("Telegram disconnect", r.headers["location"] == "/settings?msg=telegram_removed" and "telegram" not in {d.channel for d in await AC.get_destinations(1)})

        # ---- logout / login ----
        r = await a.post("/logout", data={})
        check("logout without CSRF token does nothing", (await a.get("/dashboard")).status_code == 200)
        old_cookie = a.cookies[SESSION_COOKIE]
        r = await a.post("/logout", data={"csrf_token": tok_a})
        check("logout ends the session", r.status_code == 303 and (await a.get("/dashboard")).status_code == 303)
        r = await anon.get("/dashboard", cookies={SESSION_COOKIE: old_cookie})
        check("the old cookie no longer works after logout", r.status_code == 303)
        r = await anon.get("/dashboard", cookies={SESSION_COOKIE: "forged-token"})
        check("a forged cookie is not a login", r.status_code == 303)
        r = await a.post("/login", data={"email": "alice@test.invalid", "password": "wrong password!!"})
        check("login: wrong password", r.status_code == 401 and "Email or password is incorrect" in r.text)
        r = await a.post("/login", data={"email": "nobody@test.invalid", "password": "wrong password!!"})
        check("login: unknown email gives the same message", r.status_code == 401 and "Email or password is incorrect" in r.text)
        r = await a.post("/login", data={"email": "ALICE@test.invalid", "password": "correct horse battery"})
        check("login: success -> dashboard", r.status_code == 303 and r.headers["location"] == "/dashboard" and (await a.get("/dashboard")).status_code == 200)
        check("login page redirects when already logged in", (await a.get("/login")).status_code == 303)
        for _ in range(8):
            await anon.post("/login", data={"email": "alice@test.invalid", "password": "guess guess guess"})
        r = await anon.post("/login", data={"email": "alice@test.invalid", "password": "correct horse battery"})
        check("after 8 wrong passwords the email is locked, even for the right password", r.status_code == 429 and "Too many failed attempts" in r.text)
        r = await anon.post("/login", data={"email": "<b>bob</b>@test.invalid", "password": "bob has a long password"})
        check("another account is unaffected by that lock", r.status_code == 303)
    await dispose_db()

asyncio.run(main())
print("ALL PASSED")
