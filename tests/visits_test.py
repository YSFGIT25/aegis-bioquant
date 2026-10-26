"""The site's own visitor count: what is counted, what is stored, who can see it. No network."""
import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

os.environ["SESSION_COOKIE_SECURE"] = "0"
import httpx

from aegis_bioquant.config import settings
from aegis_bioquant.core import database as db
from aegis_bioquant.web import visits as V
from aegis_bioquant.web.app import app

failed = []
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0 Safari/537.36"


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failed.append(name)


def client(ua=BROWSER):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 1234)),
                             base_url="http://testserver", headers={"user-agent": ua})


def report(path="/", referrer="", query=""):
    return json.dumps({"p": path, "r": referrer, "q": query})


async def rows():
    factory = db.get_session_factory()
    async with factory() as session:
        return list(await session.scalars(db.select(V.VisitRecord)))


async def main():
    await db.init_db()
    settings.ADMIN_EMAILS = frozenset()

    async with client() as anon:
        page = (await anon.get("/")).text
        check("every page loads the counting script", '<script src="/static/v.js" defer></script>' in page)
        r = await anon.get("/static/v.js")
        check("script served from this site, cacheable", r.status_code == 200 and "javascript" in r.headers["content-type"]
              and "sendBeacon" in r.text and "max-age" in r.headers["cache-control"])
        r = await anon.post("/v", content=report("/", "https://t.co/abc", "?utm_source=X&x=1"))
        check("a report is answered 204", r.status_code == 204)
        await anon.post("/v", content=report("/signup", "http://testserver/"))
        await anon.post("/v", content=report("/wp-admin"))
        await anon.post("/v", content=b"not json")
        await anon.post("/v", content=b"x" * 5000)
    stored = await rows()
    check("two page views counted; unknown pages and junk ignored", len(stored) == 2)
    first = next(r for r in stored if r.path == "/")
    check("from X: host name only, utm_source cleaned", first.referrer == "t.co" and first.source == "x" and not first.member)
    check("moving between pages is 'internal'", next(r for r in stored if r.path == "/signup").referrer == "internal")
    check("one visitor code for the same visitor that day", len({r.visitor for r in stored}) == 1)
    columns = {c.name for c in V.VisitRecord.__table__.columns}
    check("no IP address, user agent or full URL stored",
          not columns & {"ip", "user_agent", "url", "referrer_url"} and "203.0.113.7" not in str([vars(r) for r in stored]))

    async with client("Googlebot/2.1 (+http://www.google.com/bot.html)") as bot, client("") as blank:
        await bot.post("/v", content=report("/"))
        await blank.post("/v", content=report("/"))
    check("bots and blank user agents not counted", len(await rows()) == 2)

    async with client() as other:
        await other.post("/v", content=report("/", "https://www.google.com/search?q=fdaodds"))
    check("a referrer's full address is cut to its host", (await rows())[-1].referrer == "google.com")

    # A different browser on the same address is a different visitor.
    async with client(BROWSER.replace("150", "151")) as phone:
        await phone.post("/v", content=report("/"))
    check("different browser -> different visitor", len({r.visitor for r in await rows()}) == 2)

    # The key changes daily and old keys are deleted.
    k1 = await V._key_for(date.today())
    k2 = await V._key_for(date.today() + timedelta(days=1))
    factory = db.get_session_factory()
    async with factory() as session:
        keys = list(await session.scalars(db.select(V.VisitKeyRecord)))
    check("a new key each day; yesterday's is deleted", k1 != k2 and len(keys) == 1 and keys[0].key == k2)

    # Admins aren't counted, and only admins see the page.
    async with client() as owner, client() as user, client() as anon:
        # As on the real site: the owner's account exists before it is listed as an administrator.
        await owner.post("/signup", data={"email": "owner@test.invalid", "password": "correct horse battery"})
        settings.ADMIN_EMAILS = frozenset({"owner@test.invalid"})
        before = len(await rows())
        await owner.post("/v", content=report("/dashboard"))
        check("an administrator's visits are not counted", len(await rows()) == before)
        r = await owner.get("/review/visitors")
        check("administrator sees the Visitors page", r.status_code == 200 and "Where visitors came from" in r.text
              and "t.co" in r.text and "google.com" in r.text)
        # Same address and browser all along except the second browser: 2 visitors, 4 page views, 1 account so far.
        check("totals shown: visitors, page views, new accounts", "<td>2</td><td>4</td><td>1</td>" in r.text)
        await user.post("/signup", data={"email": "bob@test.invalid", "password": "correct horse battery"})
        await user.post("/v", content=report("/settings"))
        check("a logged-in visitor is counted as a member", (await rows())[-1].member is True)
        check("anyone else gets 404", (await user.get("/review/visitors")).status_code == 404)
        check("logged out -> login", (await anon.get("/review/visitors")).status_code == 303)

    s = await V.summary(date.today())
    check("summary: new accounts counted per day", s["signups"] == 2 and s["days"][0]["signups"] == 2)

    await db.dispose_db()
    print(f"\n{'ALL PASSED' if not failed else str(len(failed)) + ' FAILED'}")
    sys.exit(1 if failed else 0)


asyncio.run(main())
