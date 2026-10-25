"""
web/app.py
-----------
The hosted, customer-facing site.

STATUS: BUILT and tested (sign-up, login, dashboard, alert settings,
Telegram webhook). No payments yet: every account can see the dashboard
and receive alerts.

RUN IT:
    uvicorn aegis_bioquant.web.app:app --port 8000
Locally over plain http, also set SESSION_COOKIE_SECURE=0, or the
browser may refuse to send the login cookie back.

PAGES:
    /                 landing page
    /signup, /login   account creation and login; POST /logout
    /verify           the link in the "confirm your email" message
    /forgot, /reset   request a password-reset email; choose a new password
    /dashboard        the signals dashboard, login required
    /settings         connect / remove Telegram and Discord, login required
    /review           administrators only: confirm which trial a catalyst is about
    /review/outcomes  administrators only: record what the FDA decided
    /telegram/webhook where Telegram delivers "/start <code>" messages
    /healthz          for the host's health check

SECURITY MODEL, stated plainly:
  - Login state is a random session token in an HttpOnly cookie
    (web/sessions.py); JavaScript on the page cannot read it.
  - Every POST from a logged-in page must carry that session's CSRF
    token, and any POST whose Origin header names another site is
    refused — so another website cannot submit our forms for a visitor.
  - The Telegram webhook only accepts requests carrying the secret
    Telegram was given when the webhook was registered. With no secret
    configured the route does not exist (404).
  - Responses forbid framing and restrict scripts to this site's own
    files (Content-Security-Policy).
  - Email verification and password reset exist only once email
    sending is configured (core/email.py). Until then an account's
    email is whatever was typed at sign-up and a forgotten password
    cannot be recovered. Once configured, an account must confirm its
    address before it can connect an alert channel.
  - The password-reset form answers the same way whether or not the
    address has an account, and sends its email after the response has
    gone out, so neither the wording nor the response time reveals who
    has an account. Reset and verification emails are capped per
    address / account per hour.
  - /review is limited to accounts listed in settings.ADMIN_EMAILS (and,
    once email is configured, with a confirmed address). To anyone else
    the page answers 404, as if it didn't exist. An address on that list
    can't be signed up, so the list can't be claimed by a stranger.
  - NOT built: per-IP rate limits, sign-up abuse protection (captcha).
"""

from __future__ import annotations

import asyncio
import hmac
import re
from datetime import date, timedelta
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from aegis_bioquant.config import settings
from aegis_bioquant.core import accounts
from aegis_bioquant.core import email as mail
from aegis_bioquant.core.accounts import (
    CHANNEL_DISCORD,
    CHANNEL_TELEGRAM,
    PURPOSE_RESET,
    PURPOSE_VERIFY,
    AccountError,
)
from aegis_bioquant.config import constants
from aegis_bioquant.core.database import (
    OUTCOME_VALUES,
    confirm_outcome,
    dispose_db,
    get_catalysts_in_entry_window,
    get_catalysts_needing_outcome,
    get_outcome_rows,
    unconfirm_outcome,
    get_trial_overrides,
    init_db,
    remove_trial_override,
    set_trial_override,
)
from aegis_bioquant.ingestion import clinical_trials_api
from aegis_bioquant.ingestion.company_tickers import normalize_company_name
from aegis_bioquant.telemetry.dashboard import DASHBOARD_SCRIPT, build_state
from aegis_bioquant.web import sessions
from aegis_bioquant.web import pages
from aegis_bioquant.web.pages import render

SESSION_COOKIE = "aegis_session"

EMAILS_PER_HOUR = 3  # per account (verification) and per address (reset)

# Tiers the scan scores without anyone looking (pipeline.py holds the
# authoritative list; duplicated here rather than imported because
# importing the pipeline drags the whole scanner into the web process).
_AUTO_SCORED_TIERS = ("STUDY_TRIAL_NAME", "ACRONYM")
_NCT_RE = re.compile(r"^NCT\d{8}$")

_OUTCOME_LABELS = {
    "APPROVED": "Approved",
    "CRL": "Rejected (complete response letter)",
    "DELAYED": "Decision date moved by the FDA",
    "WITHDRAWN": "Application withdrawn",
    "INVALID": "Not a real decision date (misread or superseded)",
}

_NOTICES = {
    "outcome_saved": ("ok", "Outcome recorded."),
    "outcome_undone": ("ok", "Outcome removed. The catalyst is waiting for an outcome again."),
    "outcome_bad": ("error", "Choose an outcome and a decision date that is not in the future."),
    "outcome_no_event": ("error", "That catalyst isn't one that is waiting for an outcome."),
    "trial_confirmed": ("ok", "Trial confirmed. It will be scored at the next scan."),
    "trial_removed": ("ok", "Confirmation removed. The catalyst is back to needing review."),
    "trial_bad_id": ("error", "That isn't a trial ID. It should look like NCT01234567."),
    "trial_none": ("error", "Select a trial or enter a trial ID."),
    "trial_unknown": ("error", "The trial registry has no trial with that ID."),
    "trial_unreachable": ("error", "The trial registry couldn't be reached to check that ID. Try again."),
    "trial_no_event": ("error", "That catalyst is no longer in an entry window."),
    "verify_sent": ("ok", "Confirmation email sent. Check your inbox."),
    "verify_limited": ("error", "Too many confirmation emails requested. Try again in an hour."),
    "verify_first": ("error", "Confirm your email address before connecting an alert channel."),
    "password_reset": ("ok", "Password changed. Log in with your new password."),
    "discord_saved": ("ok", "Discord webhook saved. Alerts will be posted there."),
    "discord_removed": ("ok", "Discord webhook removed."),
    "telegram_removed": ("ok", "Telegram disconnected."),
    "telegram_unavailable": ("error", "Telegram alerts aren't available yet."),
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await init_db()
    yield
    await dispose_db()


app = FastAPI(title="FDAOdds", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _security(request: Request, call_next):
    # Refuse a form POST sent from another website. Browsers attach the
    # Origin header to cross-site POSTs; Telegram's server-to-server
    # webhook has none and is authenticated by its own secret instead.
    if request.method == "POST" and request.url.path != "/telegram/webhook":
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-site request refused.", status_code=403)

    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        # fonts.googleapis.com / fonts.gstatic.com: the site's two typefaces.
        # Nothing else is loaded from another origin.
        "default-src 'self'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://t.me"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path != "/static/dashboard.js":
        response.headers["Cache-Control"] = "no-store"
    return response


async def _session(request: Request) -> Optional[sessions.Session]:
    return await sessions.get_session(request.cookies.get(SESSION_COOKIE))


def _csrf_ok(session: sessions.Session, submitted: str) -> bool:
    return hmac.compare_digest(session.csrf_token, submitted or "")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)  # 303: the browser follows with a GET


def _verify_needed(session: Optional[sessions.Session]) -> bool:
    """True when this account still has to confirm its email. Never true
    while email sending isn't configured — nobody could confirm."""
    return session is not None and mail.email_enabled() and not session.email_verified


def _is_admin(session: Optional[sessions.Session]) -> bool:
    if session is None or session.email not in settings.ADMIN_EMAILS:
        return False
    # Once addresses can be confirmed, an admin's must be.
    return session.email_verified or not mail.email_enabled()


pages.set_admin_check(_is_admin)


def _notice(code: str) -> Optional[dict]:
    """A fixed message for a ?msg= code. Unknown codes show nothing, so
    the query string can never put arbitrary text on a page."""
    found = _NOTICES.get(code)
    return {"kind": found[0], "text": found[1]} if found else None


def _message_page(session, heading: str, text: str, link_url: str, link_text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        render(
            "message", session=session, here="", verify_needed=False,
            heading=heading, text=text, link_url=link_url, link_text=link_text,
        ),
        status_code=status,
    )


async def _send_verification(user_id: int, address: str) -> None:
    token = await accounts.create_email_token(user_id, PURPOSE_VERIFY)
    await mail.send_verification_email(address, token)


async def _send_reset_if_account(address: str) -> None:
    """Runs after the response has been sent — see SECURITY MODEL."""
    if not await sessions.allow_action(f"reset:{accounts.normalize_email(address)}", EMAILS_PER_HOUR, 3600):
        return
    user = await accounts.get_user_by_email(address)
    if user is None:
        return
    token = await accounts.create_email_token(user.id, PURPOSE_RESET)
    await mail.send_password_reset_email(user.email, token)


def _start_session(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=sessions.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


# --------------------------------------------------------------------------
# Public pages
# --------------------------------------------------------------------------

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/static/dashboard.js")
async def dashboard_js() -> Response:
    return Response(DASHBOARD_SCRIPT, media_type="text/javascript")


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> str:
    session = await _session(request)
    return render("landing", session=session, here="home", verify_needed=_verify_needed(session))


def _auth_page(
    action: str, email: str = "", error: Optional[str] = None, status: int = 200, msg: str = ""
) -> HTMLResponse:
    return HTMLResponse(
        render(
            "auth",
            session=None,
            verify_needed=False,
            here="login" if action == "/login" else "signup",
            heading="Log in" if action == "/login" else "Create account",
            action=action,
            email=email,
            error=error,
            notice=_notice(msg),
            reset_available=mail.email_enabled(),
            password_min=accounts.PASSWORD_MIN_LENGTH,
        ),
        status_code=status,
    )


@app.get("/signup")
async def signup_form(request: Request):
    if await _session(request):
        return _redirect("/dashboard")
    return _auth_page("/signup")


@app.post("/signup")
async def signup(background: BackgroundTasks, email: str = Form(""), password: str = Form("")):
    # An administrator address that has no account yet is not open for
    # sign-up (see settings.ADMIN_EMAILS). Same wording as a taken
    # address, so the response doesn't reveal which addresses are admins.
    if accounts.normalize_email(email) in settings.ADMIN_EMAILS:
        return _auth_page("/signup", email=email, status=400,
                          error="An account with that email already exists.")
    try:
        user = await accounts.create_user(email, password)
    except AccountError as exc:
        return _auth_page("/signup", email=email, error=str(exc), status=400)
    if mail.email_enabled():
        # After the response: the account exists either way, and a slow or
        # failing email service shouldn't hold up or break sign-up. The
        # banner on every page offers "Send it again".
        background.add_task(_send_verification, user.id, user.email)
    response = _redirect("/settings")  # straight to connecting an alert channel
    _start_session(response, await sessions.create_session(user.id))
    return response


@app.get("/login")
async def login_form(request: Request, msg: str = ""):
    if await _session(request):
        return _redirect("/dashboard")
    return _auth_page("/login", msg=msg)


@app.post("/login")
async def login(email: str = Form(""), password: str = Form("")):
    if not await sessions.login_allowed(email):
        return _auth_page(
            "/login", email=email, status=429,
            error="Too many failed attempts for this email. Wait 15 minutes and try again.",
        )
    user = await accounts.authenticate(email, password)
    if user is None:
        await sessions.record_login_failure(email)
        return _auth_page("/login", email=email, error="Email or password is incorrect.", status=401)
    await sessions.clear_login_failures(email)
    response = _redirect("/dashboard")
    _start_session(response, await sessions.create_session(user.id))
    return response


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    session = await _session(request)
    if session and _csrf_ok(session, csrf_token):
        await sessions.delete_session(request.cookies.get(SESSION_COOKIE))
        response = _redirect("/")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
    return _redirect("/")


# --------------------------------------------------------------------------
# Email verification and password reset
# --------------------------------------------------------------------------

@app.get("/verify")
async def verify_email(request: Request, token: str = ""):
    session = await _session(request)
    user_id = await accounts.consume_email_token(token, PURPOSE_VERIFY)
    if user_id is None:
        return _message_page(
            session, "This link has expired",
            "Confirmation links work once and for 24 hours. Log in and use \"Send it again\" to get a new one.",
            "/settings" if session else "/login", "Continue", status=400,
        )
    await accounts.mark_email_verified(user_id)
    return _message_page(
        session, "Email confirmed",
        "Your email address is confirmed. You can now connect Telegram or Discord.",
        "/settings" if session else "/login", "Alert settings" if session else "Log in",
    )


@app.post("/verify/resend")
async def resend_verification(request: Request, csrf_token: str = Form("")):
    session = await _settings_post(request, csrf_token)
    if session is None:
        return _redirect("/login")
    if not _verify_needed(session):
        return _redirect("/settings")
    if not await sessions.allow_action(f"verify:{session.user_id}", EMAILS_PER_HOUR, 3600):
        return _redirect("/settings?msg=verify_limited")
    await _send_verification(session.user_id, session.email)
    return _redirect("/settings?msg=verify_sent")


@app.get("/forgot")
async def forgot_form(request: Request):
    session = await _session(request)
    if not mail.email_enabled():
        return _message_page(
            session, "Password reset isn't available yet",
            "This site can't send email yet, so passwords can't be reset.", "/login", "Back to log in",
        )
    return HTMLResponse(render("forgot", session=session, here="login", verify_needed=False))


@app.post("/forgot")
async def forgot(request: Request, background: BackgroundTasks, email: str = Form("")):
    session = await _session(request)
    if mail.email_enabled():
        background.add_task(_send_reset_if_account, email)
    return _message_page(
        session, "Check your email",
        "If that address has an account, a link to choose a new password is on its way. It works for 1 hour.",
        "/login", "Back to log in",
    )


def _reset_expired(session) -> HTMLResponse:
    return _message_page(
        session, "This link has expired",
        "Password reset links work once and for 1 hour. Request a new one.",
        "/forgot", "Request a new link", status=400,
    )


@app.get("/reset")
async def reset_form(request: Request, token: str = ""):
    session = await _session(request)
    if await accounts.peek_email_token(token, PURPOSE_RESET) is None:
        return _reset_expired(session)
    return HTMLResponse(
        render(
            "reset", session=session, here="login", verify_needed=False,
            token=token, error=None, password_min=accounts.PASSWORD_MIN_LENGTH,
        )
    )


@app.post("/reset")
async def reset_password(request: Request, token: str = Form(""), password: str = Form("")):
    session = await _session(request)
    # Check the new password BEFORE using the link up, so a too-short
    # password gets an error and another try rather than a dead link.
    try:
        accounts.validate_password(password)
    except AccountError as exc:
        if await accounts.peek_email_token(token, PURPOSE_RESET) is None:
            return _reset_expired(session)
        return HTMLResponse(
            render(
                "reset", session=session, here="login", verify_needed=False,
                token=token, error=str(exc), password_min=accounts.PASSWORD_MIN_LENGTH,
            ),
            status_code=400,
        )
    user_id = await accounts.consume_email_token(token, PURPOSE_RESET)
    if user_id is None:
        return _reset_expired(session)

    await accounts.set_password(user_id, password)
    await sessions.delete_sessions_for_user(user_id)  # log out everywhere, including an attacker
    await accounts.mark_email_verified(user_id)        # using the emailed link proves the address
    user = await accounts.get_user(user_id)
    if user is not None:
        await sessions.clear_login_failures(user.email)
    response = _redirect("/login?msg=password_reset")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------
# Logged-in pages
# --------------------------------------------------------------------------

@app.get("/dashboard")
async def dashboard(request: Request):
    session = await _session(request)
    if session is None:
        return _redirect("/login")
    destinations = await accounts.get_destinations(session.user_id)
    return HTMLResponse(
        render(
            "dashboard",
            session=session,
            verify_needed=_verify_needed(session),
            here="dashboard",
            has_destination=any(d.enabled for d in destinations),
        )
    )


@app.get("/api/state")
async def api_state(request: Request):
    if await _session(request) is None:
        return JSONResponse({"error": "not logged in"}, status_code=401)
    return await build_state()


def _discord_label(url: str) -> str:
    """Enough of the webhook to recognise which one it is, without
    putting the secret half of the URL back on a page."""
    match = re.search(r"/webhooks/(\d+)/", url)
    return f"…{match.group(1)[-6:]}" if match else "…"


@app.get("/settings")
async def settings_page(request: Request, msg: str = ""):
    session = await _session(request)
    if session is None:
        return _redirect("/login")
    return await _settings_response(session, _notice(msg))


async def _settings_response(session: sessions.Session, notice: Optional[dict], status: int = 200) -> HTMLResponse:
    by_channel = {d.channel: d for d in await accounts.get_destinations(session.user_id)}
    discord = by_channel.get(CHANNEL_DISCORD)
    return HTMLResponse(
        render(
            "settings",
            session=session,
            verify_needed=_verify_needed(session),
            here="settings",
            telegram=by_channel.get(CHANNEL_TELEGRAM),
            telegram_available=bool(settings.TELEGRAM_BOT_USERNAME),
            discord=discord,
            discord_label=_discord_label(discord.address) if discord else "",
            notice=notice,
        ),
        status_code=status,
    )


async def _settings_post(request: Request, csrf_token: str) -> Optional[sessions.Session]:
    """The session for a settings form POST, or None if the request isn't
    a logged-in user's own form submission."""
    session = await _session(request)
    if session is None or not _csrf_ok(session, csrf_token):
        return None
    return session


@app.post("/settings/discord")
async def save_discord(request: Request, csrf_token: str = Form(""), webhook_url: str = Form("")):
    session = await _settings_post(request, csrf_token)
    if session is None:
        return _redirect("/login")
    if _verify_needed(session):
        return _redirect("/settings?msg=verify_first")
    try:
        await accounts.set_discord_webhook(session.user_id, webhook_url)
    except AccountError as exc:
        return await _settings_response(session, {"kind": "error", "text": str(exc)}, status=400)
    return _redirect("/settings?msg=discord_saved")


@app.post("/settings/discord/remove")
async def remove_discord(request: Request, csrf_token: str = Form("")):
    session = await _settings_post(request, csrf_token)
    if session is None:
        return _redirect("/login")
    await accounts.remove_destination(session.user_id, CHANNEL_DISCORD)
    return _redirect("/settings?msg=discord_removed")


@app.post("/settings/telegram/connect")
async def connect_telegram(request: Request, csrf_token: str = Form("")):
    session = await _settings_post(request, csrf_token)
    if session is None:
        return _redirect("/login")
    if _verify_needed(session):
        return _redirect("/settings?msg=verify_first")
    try:
        link = await accounts.create_telegram_link(session.user_id)
    except AccountError:
        return _redirect("/settings?msg=telegram_unavailable")
    return _redirect(link)  # https://t.me/<bot>?start=<one-time code>


@app.post("/settings/telegram/remove")
async def remove_telegram(request: Request, csrf_token: str = Form("")):
    session = await _settings_post(request, csrf_token)
    if session is None:
        return _redirect("/login")
    await accounts.remove_destination(session.user_id, CHANNEL_TELEGRAM)
    return _redirect("/settings?msg=telegram_removed")


# --------------------------------------------------------------------------
# Review (administrators): confirm which trial a catalyst is about
# --------------------------------------------------------------------------

def _not_found() -> PlainTextResponse:
    return PlainTextResponse("Not Found", status_code=404)


async def _review_items() -> list[dict]:
    """Every catalyst in its entry window that has a ticker, with its
    current trial match, any confirmation, and — where it still needs a
    decision — the company's trials to choose from."""
    events = [e for e in await get_catalysts_in_entry_window() if e.ticker]
    overrides = await get_trial_overrides()

    async def options_for(event):
        if not event.company_name:
            return [], False
        try:
            return await clinical_trials_api.list_trial_options(
                normalize_company_name(event.company_name), event.drug_or_trial_candidate
            ), False
        except Exception:  # noqa: BLE001 — the page still works with a typed ID
            return [], True

    confirmed = [overrides.get((e.ticker, e.event_type, e.start_date)) for e in events]
    lookups = await asyncio.gather(*(
        options_for(e) if c is None else asyncio.sleep(0, result=([], False))
        for e, c in zip(events, confirmed)
    ))
    return [
        {
            "ticker": e.ticker,
            "event_type": e.event_type,
            "catalyst_date": e.start_date.isoformat(),
            "company_name": e.company_name,
            "candidate": e.drug_or_trial_candidate,
            "source_url": e.source_url if e.source_url.startswith("https://") else None,
            "current_nct": e.resolved_nct_id,
            "auto": e.resolution_tier in _AUTO_SCORED_TIERS,
            "confirmed": c,
            "options": options,
            "lookup_failed": failed,
        }
        for e, c, (options, failed) in zip(events, confirmed, lookups)
    ]


@app.get("/review")
async def review_page(request: Request, msg: str = ""):
    session = await _session(request)
    if session is None:
        return _redirect("/login")
    if not _is_admin(session):
        return _not_found()
    return HTMLResponse(
        render(
            "review", session=session, here="review", verify_needed=_verify_needed(session),
            items=await _review_items(), notice=_notice(msg),
        )
    )


async def _review_post(request: Request, csrf_token: str, ticker: str, event_type: str, catalyst_date: str):
    """(session, parsed date) for an administrator's own form POST about a
    catalyst that is really in its entry window — or an error response."""
    session = await _session(request)
    if session is None or not _csrf_ok(session, csrf_token):
        return _redirect("/login"), None
    if not _is_admin(session):
        return _not_found(), None
    try:
        when = date.fromisoformat(catalyst_date)
    except ValueError:
        return _redirect("/review?msg=trial_no_event"), None
    # The form's hidden fields are only a pointer: the catalyst must exist.
    in_window = {(e.ticker, e.event_type, e.start_date) for e in await get_catalysts_in_entry_window()}
    if (ticker, event_type, when) not in in_window:
        return _redirect("/review?msg=trial_no_event"), None
    return session, when


@app.post("/review/confirm")
async def review_confirm(
    request: Request, csrf_token: str = Form(""), ticker: str = Form(""), event_type: str = Form(""),
    catalyst_date: str = Form(""), nct_choice: str = Form(""), nct_manual: str = Form(""),
):
    session, when = await _review_post(request, csrf_token, ticker, event_type, catalyst_date)
    if when is None:
        return session  # the error response

    nct_id = (nct_manual.strip() or nct_choice.strip()).upper()
    if not nct_id:
        return _redirect("/review?msg=trial_none")
    if not _NCT_RE.match(nct_id):
        return _redirect("/review?msg=trial_bad_id")
    # Check the ID against the registry itself — a typo'd ID would
    # otherwise only surface as a failed scan the next day.
    try:
        trial = await clinical_trials_api.fetch_trial_option(nct_id)
    except Exception:  # noqa: BLE001
        return _redirect("/review?msg=trial_unreachable")
    if trial is None:
        return _redirect("/review?msg=trial_unknown")

    title = (f"{trial.acronym}: " if trial.acronym else "") + trial.brief_title
    await set_trial_override(ticker, event_type, when, nct_id, title[:300], confirmed_by=session.user_id)
    return _redirect("/review?msg=trial_confirmed")


@app.post("/review/remove")
async def review_remove(
    request: Request, csrf_token: str = Form(""), ticker: str = Form(""), event_type: str = Form(""),
    catalyst_date: str = Form(""),
):
    session, when = await _review_post(request, csrf_token, ticker, event_type, catalyst_date)
    if when is None:
        return session
    await remove_trial_override(ticker, event_type, when)
    return _redirect("/review?msg=trial_removed")


# --------------------------------------------------------------------------
# Review (administrators): record what the FDA decided
# --------------------------------------------------------------------------

async def _outcome_candidates() -> dict:
    """PDUFA catalysts that may have an outcome recorded, keyed by (ticker, date)."""
    events = await get_catalysts_needing_outcome(
        date.today(), constants.OUTCOME_LOOKBACK_DAYS, constants.OUTCOME_GIVE_UP_DAYS
    )
    return {(e.ticker, e.start_date): e for e in events}


@app.get("/review/outcomes")
async def outcomes_page(request: Request, msg: str = ""):
    session = await _session(request)
    if session is None:
        return _redirect("/login")
    if not _is_admin(session):
        return _not_found()

    today = date.today()
    rows = await get_outcome_rows()
    items = []
    for (ticker, when), event in sorted((await _outcome_candidates()).items(), key=lambda kv: kv[0][1], reverse=True):
        row = rows.get((ticker, "PDUFA", when))
        suggested = row is not None and row.status == "SUGGESTED"
        # Before its date a catalyst is only listed if a filing already
        # appears to state a decision (the FDA sometimes acts early).
        if when > today and not suggested:
            continue
        items.append(
            {
                "ticker": ticker,
                "catalyst_date": when.isoformat(),
                "company_name": event.company_name,
                "candidate": event.drug_or_trial_candidate,
                "source_url": event.source_url if event.source_url.startswith("https://") else None,
                "days_past": (today - when).days,
                "checked": row is not None,
                "suggestion": {
                    "outcome": row.outcome,
                    "decision_date": row.decision_date.isoformat() if row.decision_date else when.isoformat(),
                    "text": row.evidence_text or "",
                    "url": row.evidence_url if (row.evidence_url or "").startswith("https://") else None,
                } if suggested else None,
            }
        )
    recorded = sorted(
        (r for r in rows.values() if r.status == "CONFIRMED"),
        key=lambda r: (r.decision_date or r.catalyst_date), reverse=True,
    )[:100]
    return HTMLResponse(
        render(
            "outcomes", session=session, here="review", verify_needed=_verify_needed(session),
            items=items, recorded=recorded, labels=_OUTCOME_LABELS, outcome_values=OUTCOME_VALUES,
            today=today.isoformat(), notice=_notice(msg),
        )
    )


async def _outcome_post(request: Request, csrf_token: str, catalyst_date: str):
    """(session, parsed catalyst date) for an administrator's own form
    POST, or (error response, None)."""
    session = await _session(request)
    if session is None or not _csrf_ok(session, csrf_token):
        return _redirect("/login"), None
    if not _is_admin(session):
        return _not_found(), None
    try:
        return session, date.fromisoformat(catalyst_date)
    except ValueError:
        return _redirect("/review/outcomes?msg=outcome_no_event"), None


@app.post("/review/outcomes/confirm")
async def outcome_confirm(
    request: Request, csrf_token: str = Form(""), ticker: str = Form(""), catalyst_date: str = Form(""),
    outcome: str = Form(""), decision_date: str = Form(""),
):
    session, when = await _outcome_post(request, csrf_token, catalyst_date)
    if when is None:
        return session  # the error response
    # The form's hidden fields are only a pointer: the catalyst must be
    # one the system knows and that is still open.
    if (ticker, when) not in await _outcome_candidates():
        return _redirect("/review/outcomes?msg=outcome_no_event")
    try:
        decided = date.fromisoformat(decision_date)
    except ValueError:
        return _redirect("/review/outcomes?msg=outcome_bad")
    earliest = when - timedelta(days=constants.OUTCOME_LOOKBACK_DAYS * 3)
    if outcome not in OUTCOME_VALUES or decided > date.today() or decided < earliest:
        return _redirect("/review/outcomes?msg=outcome_bad")

    await confirm_outcome(ticker, "PDUFA", when, outcome, decided, confirmed_by=session.user_id)
    return _redirect("/review/outcomes?msg=outcome_saved")


@app.post("/review/outcomes/undo")
async def outcome_undo(
    request: Request, csrf_token: str = Form(""), ticker: str = Form(""), catalyst_date: str = Form(""),
):
    session, when = await _outcome_post(request, csrf_token, catalyst_date)
    if when is None:
        return session
    row = (await get_outcome_rows()).get((ticker, "PDUFA", when))
    if row is None or row.status != "CONFIRMED":
        return _redirect("/review/outcomes?msg=outcome_no_event")
    await unconfirm_outcome(ticker, "PDUFA", when)
    return _redirect("/review/outcomes?msg=outcome_undone")


# --------------------------------------------------------------------------
# Telegram webhook
# --------------------------------------------------------------------------

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    secret = settings.TELEGRAM_WEBHOOK_SECRET
    if not secret or not settings.TELEGRAM_BOT_TOKEN:
        return PlainTextResponse("Not found", status_code=404)
    sent = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(sent, secret):
        return PlainTextResponse("Forbidden", status_code=403)

    try:
        update = await request.json()
    except Exception:  # noqa: BLE001 — not JSON: nothing to act on
        return {"ok": True}

    reply = await accounts.handle_telegram_update(update)
    if reply is not None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{settings.TELEGRAM_API_BASE_URL}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": reply.chat_id, "text": reply.text},
                )
        except Exception:  # noqa: BLE001 — the link itself already succeeded or failed; only the reply was lost
            pass
    # Always 200: any other status makes Telegram re-deliver the same update.
    return {"ok": True}
