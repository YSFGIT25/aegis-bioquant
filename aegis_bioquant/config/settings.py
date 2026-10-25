"""
config/settings.py
-------------------
API Keys, Database URLs, and runtime thresholds for FDAOdds.

Every value is read from the environment once, at start-up; each has a
comment saying which module uses it. Secrets (API keys, bot tokens,
webhook URLs) are never written in source.
"""

import os

# Required by ingestion/fda_scraper.py's EDGAR full-text search adapter.
# SEC's Fair Access policy requires a descriptive User-Agent with a real
# contact email on every automated request to sec.gov/efts.sec.gov — set
# this to your own email before running the PDUFA ingestion path.
# (Deliberately read from the environment rather than hardcoded, so no
# personal email address ends up committed to source control.)
SEC_EDGAR_CONTACT_EMAIL = os.environ.get("SEC_EDGAR_CONTACT_EMAIL")

# Required by core/database.py. Defaults to a local SQLite file — no
# server, no setup, fully real and testable today. This environment has
# no Postgres/Redis available to develop or verify against (checked:
# no docker, no psql, no redis-cli), so core/database.py is built on
# SQLAlchemy's async engine against SQLite rather than writing
# asyncpg-specific code that could never actually be run here. Point
# this at a real Postgres URL later ("postgresql+asyncpg://...") and
# core/database.py's ORM-level code needs no changes — only raw-SQL
# Postgres-specific features (not used here) would require care.
def _async_database_url(url: str) -> str:
    """Hosts like Railway hand out a plain "postgresql://..." (or the
    older "postgres://...") URL. SQLAlchemy's async engine needs the
    driver named explicitly, so rewrite those to use asyncpg."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


DATABASE_URL = _async_database_url(
    os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./aegis_bioquant.db")
)

# The Phase A filters (logic/phase_a.py): company value at or under the
# size ceiling, share price at or above the floor, and front-month
# options with open interest > 2000 and daily volume > 200 (the manual says 500; see constants.MIN_DAILY_OPTIONS_VOLUME).
# On by default because the manual requires them. Set to 0 to
# score every in-window candidate regardless — the thresholds themselves
# live in config/constants.py.
ENFORCE_PHASE_A_FILTERS = os.environ.get("ENFORCE_PHASE_A_FILTERS", "1") != "0"

# Claude's second opinion on each drug's evidence (engines/clinical_nlp/
# llm_review.py). Off unless a key is set. The key is a secret: set it in
# the environment (on Railway, as a service variable), never in a file.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None
LLM_REVIEW_MODEL = os.environ.get("LLM_REVIEW_MODEL", "claude-opus-5-5")
# Naming the drug from one excerpt is easy work; it goes to a small model
# to keep the bill down. The judgement itself stays on LLM_REVIEW_MODEL.
LLM_IDENTIFY_MODEL = os.environ.get("LLM_IDENTIFY_MODEL", "claude-haiku-4-5-20251001")
# When the company's filings say too little about a trial's results, let
# the model search the web for them. On by default; each search is billed.
LLM_REVIEW_WEB_SEARCH = os.environ.get("LLM_REVIEW_WEB_SEARCH", "1") != "0"

# Score candidates and take option readings only while the US market is
# open (ingestion/market_data_feed.us_market_open). A scan run outside
# those hours still refreshes the catalyst calendar. On by default; set
# to 0 only to exercise the pipeline off-hours, knowing the option data
# it then reads is not real.
REQUIRE_MARKET_HOURS = os.environ.get("REQUIRE_MARKET_HOURS", "1") != "0"

# Required by core/cache.py. How long a per-ticker pipeline lockout
# survives if the run holding it crashes without releasing it. Must be
# comfortably longer than one candidate's full pipeline run (seconds, in
# practice) — see core/cache.py's docstring.
PIPELINE_LOCK_TTL_SECONDS = float(os.environ.get("PIPELINE_LOCK_TTL_SECONDS", "900"))

# Used by telemetry/alerts.py (Phase E). Every channel is optional: a
# channel with nothing set here is simply not sent to, and with none set
# no alert leaves the machine. All of these are credentials — the bot
# token and both webhook URLs grant posting rights to whoever holds them
# — so they come from the environment only, never from source.
#   Telegram needs BOTH a bot token (from @BotFather) and the chat/channel
#   id to post into; Telegram has no single "webhook URL" for sending.
#   The bot token also delivers every CUSTOMER's Telegram alerts (to the
#   chat each one linked — see core/accounts.py); TELEGRAM_CHAT_ID is
#   only the operator's own optional channel. TELEGRAM_BOT_USERNAME (the
#   bot's @name, without the @) is needed to build customers' connect links.
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API_BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL", "https://api.telegram.org")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
# Any endpoint that accepts a JSON POST — receives the full AlertPayload.
DASHBOARD_WEBHOOK_URL = os.environ.get("DASHBOARD_WEBHOOK_URL")

# Used by web/app.py (the hosted site).
#   SESSION_COOKIE_SECURE: the login cookie is only sent over https.
#   Leave on wherever the site is served over https; set to 0 only for
#   running the site locally over plain http.
#   TELEGRAM_WEBHOOK_SECRET: a random string given to Telegram when the
#   bot's webhook is registered; Telegram sends it back with every
#   update, which is how the site knows an update really is from Telegram.
#   ADMIN_EMAILS: comma-separated emails of the accounts allowed to use
#   the review page, where the trial behind a catalyst is confirmed. That
#   choice changes what is scored for every customer, so it is not open
#   to ordinary accounts. ORDER MATTERS: create the account first, then
#   list its email here. An address listed here cannot be signed up
#   afterwards — otherwise, while email verification is off, anyone could
#   register the owner's address and become an administrator.
ADMIN_EMAILS = frozenset(
    e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()
)
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")

# Used by core/email.py (address verification and password reset). All
# three must be set for the site to send email; with any missing, those
# features stay off and the site works as it did without them.
#   RESEND_API_KEY   credential for the email service (resend.com)
#   EMAIL_FROM       the sender, e.g. "FDAOdds <accounts@fdaodds.com>".
#                    The service only sends from a domain you have verified with it.
#   PUBLIC_BASE_URL  the site's own address, e.g. "https://yourdomain.com" —
#                    what emailed links point at (see core/email.py for why
#                    this is configured rather than detected).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_FROM = os.environ.get("EMAIL_FROM")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")
EMAIL_API_BASE_URL = os.environ.get("EMAIL_API_BASE_URL", "https://api.resend.com")

# Where telemetry/dashboard.py listens. Loopback by default on purpose:
# the dashboard has no login, and its webhook accepts alerts from anyone
# who can reach it — so it should only be reachable from this machine.
# To send alerts to it, set
#   DASHBOARD_WEBHOOK_URL=http://127.0.0.1:8765/webhook
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))

# How long the same (ticker, catalyst date, signal) stays quiet after an
# alert goes out — see telemetry/alerts.py's ALERT POLICY.
ALERT_COOLDOWN_SECONDS = float(os.environ.get("ALERT_COOLDOWN_SECONDS", "86400"))

# Used by telemetry/status.py: after every scheduled scan the scanner sends
# a short "scan OK / scan FAILED" report to this one Telegram chat (the
# operator's own), through TELEGRAM_BOT_TOKEN. Unset = no report.
STATUS_TELEGRAM_CHAT_ID = os.environ.get("STATUS_TELEGRAM_CHAT_ID")

# Used by core/snapshots.py: once a week the scanner uploads a dated copy of
# the study's tables to the OSF project, so the record's state on each date
# is held by a third party. OSF_TOKEN is a personal access token from
# osf.io/settings/tokens with the osf.full_write scope — a credential, so it
# comes from the environment only. Unset = no snapshots.
OSF_TOKEN = os.environ.get("OSF_TOKEN") or None
OSF_PROJECT_ID = os.environ.get("OSF_PROJECT_ID", "zfg9b")
OSF_FILES_BASE_URL = os.environ.get("OSF_FILES_BASE_URL", "https://files.osf.io/v1")
SNAPSHOT_WEEKDAY = int(os.environ.get("SNAPSHOT_WEEKDAY", "6"))   # 0 = Monday ... 6 = Sunday (UTC)
