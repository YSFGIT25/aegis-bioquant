"""
telemetry/alerts.py
--------------------
Phase E: Notification & Telemetry.

STATUS: BUILT. Tested end-to-end against a local HTTP server standing in
for Telegram / Discord / a dashboard. NOT yet tested against the real
Telegram or Discord services: no bot token or webhook URL is configured
in this environment, and nothing here invents one. The request shapes
follow each service's documented API (Telegram Bot API sendMessage;
Discord webhook "content" message), but the first real send is still an
unverified step — expect to confirm it once credentials exist.

WHERE THIS FILE COMES FROM: the manual describes Phase E in prose but
never assigns it a file in the System Directory Structure. This
telemetry/ package is that missing home.

WHAT IT DOES:
  1. build_alert_payload()  — packs the arbitrage signal, options
     metrics and clinical scoring breakdown into one validated JSON
     schema (AlertPayload).
  2. dispatch_alert()       — pushes it, concurrently, to two kinds of
     recipient. OPERATOR channels, configured in settings: Telegram (as
     text), Discord (as text), and a generic dashboard webhook (the raw
     JSON payload). And every CUSTOMER destination in the database
     (core/accounts.py): each user's own Telegram chat or Discord
     webhook. Each customer delivery is logged, and a destination that
     fails repeatedly is switched off.
  3. notify_if_actionable() — the pipeline's entry point: decides
     whether to alert at all, and stops repeat scans from re-sending
     the same alert.
  4. notify_due_exits()     — Phase D's "automated flag to close": for
     every alert already sent, when one of its dated exit triggers
     arrives (14 days before a PDUFA date, 5 before an AdCom), sends an
     exit reminder to the same recipients, once.

WHAT EXIT REMINDERS CAN'T COVER: the manual's third exit — take profit
if the option gains 50% within 10 days — is not sent. That needs to
know which option was bought and at what price, and this system never
places or tracks a trade; it only knows it raised a signal. The alert
itself states the rule so the reader can apply it.

ALERT POLICY, stated plainly:
  - Only actionable signals alert. NO_SIGNAL is the normal result for
    most candidates and never sends anything.
  - The same (ticker, catalyst date, signal) alerts at most once per
    settings.ALERT_COOLDOWN_SECONDS. Without this, a scan scheduled
    hourly would re-send an identical alert every hour for the whole
    entry window. The signal is part of the key, so a candidate whose
    signal CHANGES alerts again immediately. Built on core/cache.py's
    lockout: the lock is simply left to expire instead of released.
  - If no channel accepted the alert, the cooldown is released so the
    next scan tries again. If only SOME channels failed, the cooldown
    stays (re-sending would duplicate the alert on the channels that
    worked) — the failed channel is reported, not retried.
  - If the cooldown store itself is unreachable, the alert is sent
    anyway: a duplicate alert costs less than a missed one.

SECRETS: a Telegram bot token and a Discord webhook URL are both
credentials that live inside the request URL. httpx puts the URL in its
exception messages, so failures are reported here as a status code or an
exception class name only — never the exception text.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional, Sequence

import httpx
from pydantic import BaseModel

from aegis_bioquant.config import constants as c
from aegis_bioquant.config import settings
from aegis_bioquant.core.accounts import (
    CHANNEL_DISCORD,
    CHANNEL_TELEGRAM,
    Destination,
    get_active_destinations,
    record_deliveries,
)
from aegis_bioquant.core.cache import acquire_lock, release_lock
from aegis_bioquant.logic.phase_a import format_market_cap, market_cap_ok, price_in_range
from aegis_bioquant.core.database import (
    claim_exit_notice,
    get_recent_alerts,
    release_exit_notice,
    save_alert_once,
)

SCHEMA_VERSION = 1

TELEGRAM_MAX_CHARS = 4096   # Bot API sendMessage limit
DISCORD_MAX_CHARS = 2000    # webhook "content" limit
# Discord shows a webhook message under the webhook's own name and picture
# unless the message sets them. Set here, so every Discord alert comes from
# the product whatever the customer called their webhook. The picture is
# served by the website (web/app.py, /logo.png).
DISCORD_SENDER_NAME = "FDAOdds"
_HTTP_TIMEOUT_SECONDS = 10.0


# --------------------------------------------------------------------------
# The unified JSON schema
# --------------------------------------------------------------------------

class CatalystBlock(BaseModel):
    type: str
    date: date
    days_until: int  # the countdown, as of the payload's as_of date


class ScoreAdjustmentBlock(BaseModel):
    label: str
    delta: float
    reason: str


class ClinicalBlock(BaseModel):
    nct_id: str
    p_fundamental: float
    base_rate: float
    data_completeness: str
    adjustments: list[ScoreAdjustmentBlock]


class LiquidityChecks(BaseModel):
    """Phase A's constraints, evaluated for display. Reported as-is — a
    False here does not by itself mean the pipeline rejected the ticker."""

    spot_price_in_range: bool
    open_interest_ok: bool
    daily_volume_ok: bool
    market_cap_ok: bool = True   # default keeps alerts stored before this field existed readable


class MarketBlock(BaseModel):
    spot_price: float
    market_cap: Optional[float] = None
    event_implied_move: Optional[float] = None       # see metrics.event_implied_move
    event_move_confidence: Optional[str] = None
    iv_rank_estimated: bool = False                   # iv_rank is the price-history estimate, not true IV Rank
    p_market: Optional[float]
    p_market_confidence: Optional[str]
    iv_rank: Optional[float]
    open_interest: int
    daily_options_volume: int
    liquidity_checks: LiquidityChecks


class SignalBlock(BaseModel):
    arbitrage_signal: str
    divergence_delta: Optional[float]   # recorded only — no rule reads it (logic/matrix_matcher.py)
    notes: str = ""                     # the facts behind the signal, in one line


class ExitTriggerBlock(BaseModel):
    label: str
    active: bool
    trigger_date: Optional[date]
    description: str


class ExecutionBlock(BaseModel):
    hold_state: str
    use_options: bool
    allocation_pct: float
    allocation_dollars: float
    kelly_fraction: Optional[float]
    trailing_stop_pct: Optional[float]
    exit_triggers: list[ExitTriggerBlock]
    notes: str


class AlertPayload(BaseModel):
    schema_version: int = SCHEMA_VERSION
    generated_at: datetime
    ticker: str
    catalyst: CatalystBlock
    signal: SignalBlock
    clinical: ClinicalBlock
    market: MarketBlock
    execution: ExecutionBlock


def build_alert_payload(
    result,
    as_of: Optional[date] = None,
    generated_at: Optional[datetime] = None,
) -> AlertPayload:
    """Packs one pipeline.PipelineResult into the unified schema."""
    cand, fs, plan = result.candidate, result.fundamental_score, result.execution_plan
    as_of = as_of or date.today()

    return AlertPayload(
        generated_at=generated_at or datetime.now(timezone.utc),
        ticker=cand.ticker,
        catalyst=CatalystBlock(
            type=cand.catalyst_type,
            date=cand.catalyst_date,
            days_until=(cand.catalyst_date - as_of).days,
        ),
        signal=SignalBlock(
            arbitrage_signal=cand.arbitrage_signal,
            divergence_delta=cand.divergence_delta,
            notes=cand.signal_notes,
        ),
        clinical=ClinicalBlock(
            nct_id=fs.nct_id,
            p_fundamental=fs.p_fundamental,
            base_rate=fs.base_rate,
            data_completeness=fs.data_completeness,
            adjustments=[
                ScoreAdjustmentBlock(label=a.label, delta=a.delta, reason=a.reason)
                for a in fs.adjustments
            ],
        ),
        market=MarketBlock(
            spot_price=cand.spot_price,
            market_cap=cand.market_cap,
            event_implied_move=cand.event_implied_move,
            event_move_confidence=cand.event_move_confidence,
            iv_rank_estimated=cand.iv_rank_estimated,
            p_market=cand.p_market,
            p_market_confidence=cand.p_market_confidence,
            iv_rank=cand.iv_rank,
            open_interest=cand.open_interest,
            daily_options_volume=cand.daily_options_volume,
            liquidity_checks=LiquidityChecks(
                spot_price_in_range=price_in_range(cand.spot_price),
                open_interest_ok=cand.open_interest > c.MIN_OPEN_INTEREST,
                daily_volume_ok=cand.daily_options_volume > c.MIN_DAILY_OPTIONS_VOLUME,
                market_cap_ok=market_cap_ok(cand.market_cap),
            ),
        ),
        execution=ExecutionBlock(
            hold_state=plan.hold_state,
            use_options=plan.use_options,
            allocation_pct=plan.allocation_pct,
            allocation_dollars=plan.allocation_dollars,
            kelly_fraction=plan.kelly_fraction,
            trailing_stop_pct=plan.trailing_stop_pct,
            exit_triggers=[
                ExitTriggerBlock(
                    label=t.label,
                    active=t.active,
                    trigger_date=t.trigger_date,
                    description=t.description,
                )
                for t in plan.exit_triggers
            ],
            notes=plan.notes,
        ),
    )


# --------------------------------------------------------------------------
# Human-readable layout (Telegram / Discord)
# --------------------------------------------------------------------------

# The same words the dashboard uses for each verdict (telemetry/dashboard.py).
_HEADLINES = {
    c.SIGNAL_LONG_RUN_UP_ALPHA: "BUY SETUP",
    c.SIGNAL_LONG_TERM_EQUITY_ACCUMULATION: "STRONG EVIDENCE, STOCK ONLY",
    c.SIGNAL_STRUCTURAL_COLLAPSE_AVOID: "AVOID",
}
_EVENT_NAMES = {"PDUFA": "FDA decision", "ADCOM": "FDA advisory committee meeting"}
_FOOTER = ("Experimental: FDAOdds' forecasts are not yet proven; the track record is on the dashboard. "
           "Information only, not financial advice.")
_MIN_REASON_CHARS = 160   # an evidence summary is never shortened below this


def _long_date(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


def _mark(ok: bool) -> str:
    return "\u2713" if ok else "\u2717"


def _event_name(payload: AlertPayload) -> str:
    return _EVENT_NAMES.get(payload.catalyst.type, payload.catalyst.type)


def _headline(signal: str) -> str:
    return _HEADLINES.get(signal, signal.replace("_", " "))


def _dashboard_line() -> list[str]:
    if not settings.PUBLIC_BASE_URL:
        return []
    return [f"Full details: {settings.PUBLIC_BASE_URL.rstrip('/')}/dashboard"]


def _check_lines(payload: AlertPayload) -> list[str]:
    """The dashboard's three checks. Each is two lines: the result, then
    a sentence saying what is being measured and the level a buy setup
    needs — a bare "92 of 100" told a reader nothing."""
    clin, mkt = payload.clinical, payload.market
    need_p = c.LONG_ALPHA_P_FUNDAMENTAL_MIN
    checks = [
        f"{_mark(clin.p_fundamental >= need_p)} Trial evidence: {clin.p_fundamental * 100:.0f} out of 100\n"
        f"How strong the case for approval looks, judged from the trial results. "
        f"{need_p * 100:.0f} or more counts as strong."
    ]

    move, need_move = mkt.event_implied_move, c.EVENT_MATTERS_MIN_MOVE
    if move is None:
        reading = "could not be measured today"
    elif move < 0.005:
        reading = "none"
    else:
        reading = f"about {move:.0%}"
    checks.append(
        f"{_mark(move is not None and move >= need_move)} Expected move: {reading}\n"
        f"How big a jump or drop traders are betting on for this event, read from options prices. "
        f"We look for at least {need_move:.0%}."
        + ("" if move is None or mkt.event_move_confidence == "GOOD" else " Today's reading is a rough one.")
    )

    cheap = c.IV_RANK_UNDERPRICED_THRESHOLD
    if mkt.iv_rank is None:
        reading = "not known yet"
    else:
        word = ("cheap" if mkt.iv_rank < cheap
                else "expensive" if mkt.iv_rank > c.EQUITY_ACCUMULATION_IV_RANK_OVERPRICED else "middling")
        reading = f"{word} ({mkt.iv_rank:.0f} out of 100)"
    checks.append(
        f"{_mark(mkt.iv_rank is not None and mkt.iv_rank < cheap)} Options cost: {reading}\n"
        f"How pricey this stock's options are today against its own past year: 0 is the cheapest, "
        f"100 the dearest. We look for under {cheap}."
        + (" Estimated from the stock's price history." if mkt.iv_rank_estimated else "")
    )
    return checks


def _cut_longest(texts: list[str], by: int) -> list[str]:
    """Shortens the longest of `texts` by `by` characters (never below a
    sentence's worth), ending it at a word with "..."."""
    if by <= 0 or not texts:
        return texts
    i = max(range(len(texts)), key=lambda k: len(texts[k]))
    keep = max(_MIN_REASON_CHARS, len(texts[i]) - by - 3)
    if keep >= len(texts[i]):
        return texts
    return texts[:i] + [texts[i][:keep].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."] + texts[i + 1:]


def _evidence_lines(clin: ClinicalBlock, cut: int = 0) -> list[str]:
    """`cut`: characters to take out of the free-text reasons, for a
    service with a short message limit (see format_alert_text)."""
    lines = ["Why the evidence score"]
    if clin.data_completeness in ("EVIDENCE_REVIEW", "CLAUDE_REVIEW"):
        lines += _cut_longest([adj.reason for adj in clin.adjustments if adj.reason], cut)
        lines.append("The score is an AI model's reading of the company's filings and published results, "
                     "not a measured probability.")
    else:
        lines.append(f"Starts at {clin.base_rate * 100:.0f}, the usual approval rate at this stage.")
        lines += [f"{adj.delta * 100:+.0f}: {adj.reason}" for adj in clin.adjustments]
        if clin.data_completeness == "BASELINE_NO_TRIAL":
            lines.append("Baseline score: no specific trial has been confirmed for this decision.")
        elif clin.data_completeness != "FULL":
            lines.append("Baseline score: the trial has not published results in the registry.")
    if clin.nct_id.upper().startswith("NCT"):
        lines.append(f"Trial on record: {clin.nct_id} (clinicaltrials.gov)")
    return lines


def _reason_lines(payload: AlertPayload) -> list[str]:
    sig, mkt = payload.signal.arbitrage_signal, payload.market
    if sig == c.SIGNAL_LONG_RUN_UP_ALPHA:
        why = ("All three checks pass inside the entry window: strong evidence, an event the market expects "
               "to move the stock, and options that are cheap for this stock.")
    elif sig == c.SIGNAL_LONG_TERM_EQUITY_ACCUMULATION:
        dear = mkt.iv_rank is not None and mkt.iv_rank > c.EQUITY_ACCUMULATION_IV_RANK_OVERPRICED
        thin = mkt.open_interest < c.EQUITY_ACCUMULATION_MIN_OPEN_INTEREST
        problem = " and ".join(w for w, on in (("expensive", dear), ("thinly traded", thin)) if on) or "not attractive"
        why = (f"The evidence is very strong ({c.EQUITY_ACCUMULATION_P_FUNDAMENTAL_MIN * 100:.0f} or more), "
               f"but options on this stock are {problem}. That makes it a case for the shares, not for options.")
    elif sig == c.SIGNAL_STRUCTURAL_COLLAPSE_AVOID:
        why = "Weak evidence going into an event the market expects to be big, with options already expensive."
    else:
        return []
    return ["Why you are getting this", why]


def _plan_lines(payload: AlertPayload) -> list[str]:
    """What the system's own rules would do. Sizes are a share of an
    account, never a dollar figure: the dollars in the payload are worked
    out on a notional account, not the reader's."""
    ex, event = payload.execution, _event_name(payload)
    if ex.hold_state == "SHORT_TERM_RUN_UP":
        lines = ["- Options, bought now and sold before the event", f"- Size: at most {ex.allocation_pct:.1%} of an account"]
        for trig in ex.exit_triggers:
            if trig.active and trig.trigger_date is not None:
                before = (payload.catalyst.date - trig.trigger_date).days
                lines.append(f"- Exit: close all of it by {_long_date(trig.trigger_date)}, "
                             f"{_days(before)} before the {event}")
        lines.append(f"- Take the profit early if the options gain more than {c.PROFIT_TARGET_PCT:.0%} "
                     f"within {c.PROFIT_TARGET_WINDOW_DAYS} days")
    elif ex.hold_state == "LONG_TERM_HOLD":
        lines = ["- Shares, not options", f"- Size: at most {ex.allocation_pct:.1%} of an account"]
        if ex.trailing_stop_pct is not None:
            lines.append(f"- Stop: sell if the price falls {ex.trailing_stop_pct:.0%} from its highest point")
        lines.append(f"- Held through the {event}, not sold before it")
    else:
        return []
    return ["The system's plan, for reference"] + lines


def format_alert_text(payload: AlertPayload, max_chars: Optional[int] = None) -> str:
    """
    The message a customer reads, in the dashboard's words: the verdict,
    the three checks, why, the plan, what to do.

    `max_chars`: the receiving service's message limit. A message over it
    has its evidence summary shortened, so that what gets cut is the
    middle of a paragraph and never the plan, the "what to do" line or
    the not-advice line at the end.

    Plain text on purpose — no Markdown/HTML parse mode. Free-text fields
    (the evidence summary) can contain characters either markup would
    choke on, and a rejected message is a missed alert.
    """
    text = _alert_text(payload, 0)
    if max_chars is not None and len(text) > max_chars:
        text = _alert_text(payload, len(text) - max_chars)
    return text


def _alert_text(payload: AlertPayload, cut: int) -> str:
    cat, mkt, sig = payload.catalyst, payload.market, payload.signal.arbitrage_signal
    avoid = sig == c.SIGNAL_STRUCTURAL_COLLAPSE_AVOID

    blocks = [
        [
            f"{_headline(sig)}: {payload.ticker}",
            f"{_event_name(payload)} on {_long_date(cat.date)}, in {_days(cat.days_until)}",
            f"Stock ${mkt.spot_price:,.2f}, company value {format_market_cap(mkt.market_cap)}",
        ],
        ["The three checks (\u2713 passes, \u2717 does not)"],
        *[[check] for check in _check_lines(payload)],
        _evidence_lines(payload.clinical, cut),
        _reason_lines(payload),
        _plan_lines(payload),
        [
            "What to do: nothing. This is a warning, not a trade idea." if avoid
            else "What to do: read the reasons and the company's own filings, then decide for yourself."
        ] + _dashboard_line() + [_FOOTER],
    ]
    return "\n\n".join("\n".join(block) for block in blocks if block)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

@dataclass
class DispatchOutcome:
    channel: str   # "telegram" | "discord" | "dashboard"
    ok: bool
    detail: str    # "HTTP 200", "HTTP 500", "ConnectTimeout" — never a URL, see module docstring
    # Both None for an operator channel (one configured in settings);
    # both set for a customer's destination (core/accounts.py).
    user_id: Optional[int] = None
    destination_id: Optional[int] = None


# Customer fan-out is capped at this many requests in flight. Telegram
# allows a bot roughly 30 messages a second across all chats; staying
# well under that keeps a large fan-out from getting the bot throttled.
_MAX_CONCURRENT_SENDS = 10


def configured_channels() -> list[str]:
    """The OPERATOR's channels — the ones set in settings. Customers'
    destinations are separate and live in the database."""
    channels = []
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        channels.append("telegram")
    if settings.DISCORD_WEBHOOK_URL:
        channels.append("discord")
    if settings.DASHBOARD_WEBHOOK_URL:
        channels.append("dashboard")
    return channels


async def _post(
    client: httpx.AsyncClient,
    limit: asyncio.Semaphore,
    channel: str,
    url: str,
    body: dict,
    destination: Optional[Destination] = None,
) -> DispatchOutcome:
    user_id = destination.user_id if destination else None
    destination_id = destination.id if destination else None
    async with limit:
        try:
            response = await client.post(url, json=body)
        except Exception as exc:  # noqa: BLE001 — one dead channel must not block the others
            return DispatchOutcome(channel, False, type(exc).__name__, user_id, destination_id)
    return DispatchOutcome(
        channel, response.is_success, f"HTTP {response.status_code}", user_id, destination_id
    )


def _telegram_send_url() -> str:
    return f"{settings.TELEGRAM_API_BASE_URL}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"


def _discord_body(text: str) -> dict:
    body = {"content": text, "username": DISCORD_SENDER_NAME}
    if settings.PUBLIC_BASE_URL:
        body["avatar_url"] = settings.PUBLIC_BASE_URL.rstrip("/") + "/logo.png"
    return body


async def dispatch_alert(
    payload: AlertPayload,
    destinations: Sequence[Destination] = (),
) -> list[DispatchOutcome]:
    """
    Pushes to every operator channel configured in settings, plus each
    customer destination passed in. Never raises for a delivery failure —
    each attempt comes back as a DispatchOutcome. Returns [] if there is
    nowhere to send.

    A customer's Telegram destination is sent with the operator's bot
    token; the caller must not pass Telegram destinations when no token
    is configured (notify_if_actionable() filters them out).
    """
    return await _dispatch_text(
        format_alert_text(payload), payload.model_dump(mode="json"), destinations,
        discord_text=format_alert_text(payload, max_chars=DISCORD_MAX_CHARS),
    )


async def _dispatch_text(
    text: str,
    dashboard_body: dict,
    destinations: Sequence[Destination] = (),
    discord_text: Optional[str] = None,
) -> list[DispatchOutcome]:
    """Sends `text` to every Telegram / Discord recipient and
    `dashboard_body` to the dashboard webhook. Shared by alerts and exit
    reminders. `discord_text`: a version already fitted to Discord's
    shorter limit; without one, `text` is cut at the limit."""
    channels = configured_channels()
    if not channels and not destinations:
        return []

    telegram_text = _truncate(text, TELEGRAM_MAX_CHARS)
    discord_text = _truncate(discord_text or text, DISCORD_MAX_CHARS)
    limit = asyncio.Semaphore(_MAX_CONCURRENT_SENDS)

    # follow_redirects stays off (httpx's default): a destination address
    # must never be able to bounce this server to some other URL.
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        sends = []
        if "telegram" in channels:
            sends.append(_post(
                client, limit, "telegram", _telegram_send_url(),
                {"chat_id": settings.TELEGRAM_CHAT_ID, "text": telegram_text},
            ))
        if "discord" in channels:
            sends.append(_post(
                client, limit, "discord", settings.DISCORD_WEBHOOK_URL, _discord_body(discord_text),
            ))
        if "dashboard" in channels:
            sends.append(_post(
                client, limit, "dashboard", settings.DASHBOARD_WEBHOOK_URL, dashboard_body,
            ))
        for dest in destinations:
            if dest.channel == CHANNEL_TELEGRAM:
                sends.append(_post(
                    client, limit, CHANNEL_TELEGRAM, _telegram_send_url(),
                    {"chat_id": dest.address, "text": telegram_text}, dest,
                ))
            elif dest.channel == CHANNEL_DISCORD:
                sends.append(_post(
                    client, limit, CHANNEL_DISCORD, dest.address, _discord_body(discord_text), dest,
                ))
        return list(await asyncio.gather(*sends))


# --------------------------------------------------------------------------
# Pipeline entry point
# --------------------------------------------------------------------------

@dataclass
class AlertResult:
    ticker: str
    signal: str
    status: str  # NOT_ACTIONABLE | NO_CHANNELS | SUPPRESSED_COOLDOWN | SENT | PARTIAL | FAILED
    outcomes: list[DispatchOutcome] = field(default_factory=list)
    payload: Optional[AlertPayload] = None
    # Things that went wrong around the send without stopping it — e.g.
    # the customer list couldn't be read. Free of secrets and addresses.
    warnings: list[str] = field(default_factory=list)


async def _deliverable_destinations(warnings: list[str]) -> list[Destination]:
    """Customer destinations that can actually be sent to right now.
    Problems are appended to `warnings` rather than raised: operator
    channels can still be alerted when the customer list can't be read."""
    destinations: list[Destination] = []
    try:
        destinations = await get_active_destinations()
    except Exception as exc:  # noqa: BLE001 — see docstring
        warnings.append(f"customer destinations could not be read ({type(exc).__name__}); customers not alerted")

    if not settings.TELEGRAM_BOT_TOKEN:
        skipped = sum(1 for d in destinations if d.channel == CHANNEL_TELEGRAM)
        if skipped:
            warnings.append(f"{skipped} customer Telegram destination(s) skipped: no bot token configured")
            destinations = [d for d in destinations if d.channel != CHANNEL_TELEGRAM]
    return destinations


def alert_cooldown_key(ticker: str, catalyst_date: date, signal: str) -> str:
    return f"alert:{ticker.upper()}:{catalyst_date.isoformat()}:{signal}"


async def notify_if_actionable(result, as_of: Optional[date] = None) -> AlertResult:
    """
    Sends an alert for one pipeline.PipelineResult if policy says to —
    see ALERT POLICY in the module docstring. Requires init_db() to have
    been called (the cooldown lives in the database).
    """
    cand = result.candidate
    signal = cand.arbitrage_signal

    if signal == c.SIGNAL_NONE:
        return AlertResult(cand.ticker, signal, "NOT_ACTIONABLE")

    payload = build_alert_payload(result, as_of=as_of)
    warnings: list[str] = []

    # Record it for the dashboards first, whatever happens to delivery
    # below — the dashboard reads this table directly. Stored at most
    # once per cooldown, so repeat scans don't pile up copies.
    try:
        await save_alert_once(
            cand.ticker, signal, cand.catalyst_date, payload.model_dump(mode="json"),
            within_seconds=settings.ALERT_COOLDOWN_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — still worth sending
        warnings.append(f"alert not saved for the dashboard ({type(exc).__name__})")

    destinations = await _deliverable_destinations(warnings)

    if not configured_channels() and not destinations:
        return AlertResult(cand.ticker, signal, "NO_CHANNELS", payload=payload, warnings=warnings)

    key = alert_cooldown_key(cand.ticker, cand.catalyst_date, signal)
    token: Optional[str] = None
    try:
        token = await acquire_lock(key, ttl_seconds=settings.ALERT_COOLDOWN_SECONDS)
    except Exception:  # noqa: BLE001 — cooldown store down: send anyway, see ALERT POLICY
        pass
    else:
        if token is None:
            return AlertResult(cand.ticker, signal, "SUPPRESSED_COOLDOWN", payload=payload, warnings=warnings)

    outcomes = await dispatch_alert(payload, destinations)
    delivered = sum(1 for o in outcomes if o.ok)

    customer_outcomes = [o for o in outcomes if o.destination_id is not None]
    try:
        switched_off = await record_deliveries(customer_outcomes, cand.ticker, signal, cand.catalyst_date)
        if switched_off:
            warnings.append(f"{switched_off} customer destination(s) switched off after repeated failures")
    except Exception as exc:  # noqa: BLE001 — the alert already went out; only the log was lost
        warnings.append(f"customer deliveries not logged ({type(exc).__name__})")

    if delivered == 0:
        if token is not None:
            try:
                await release_lock(key, token)  # nothing went out — let the next scan retry
            except Exception:  # noqa: BLE001 — worst case the retry waits out the cooldown
                pass
        status = "FAILED"
    else:
        status = "SENT" if delivered == len(outcomes) else "PARTIAL"

    return AlertResult(cand.ticker, signal, status, outcomes=outcomes, payload=payload, warnings=warnings)


# --------------------------------------------------------------------------
# Phase D exit reminders
# --------------------------------------------------------------------------

@dataclass
class ExitNotice:
    ticker: str
    label: str           # the exit trigger's label, e.g. "PDUFA_EXIT"
    trigger_date: date
    status: str          # SENT | PARTIAL | FAILED | NO_CHANNELS
    outcomes: list[DispatchOutcome] = field(default_factory=list)


def format_exit_text(payload: AlertPayload, label: str, trigger_date: date, as_of: date) -> str:
    cat, event = payload.catalyst, _event_name(payload)
    days_left = (cat.date - as_of).days
    return "\n\n".join([
        f"EXIT REMINDER: {payload.ticker}\n"
        f"The {event} on {_long_date(cat.date)} is {_days(days_left)} away.",
        f"The plan in the earlier \"{_headline(payload.signal.arbitrage_signal).capitalize()}\" alert was to close "
        f"all of the position by {_long_date(trigger_date)}, before the {event}. That date has arrived.",
        "\n".join(["This is a reminder of that plan, not a new assessment of the stock."] + _dashboard_line() + [_FOOTER]),
    ])


async def notify_due_exits(as_of: Optional[date] = None) -> list[ExitNotice]:
    """
    Sends an exit reminder for every stored alert with a dated, active
    exit trigger that has arrived and whose catalyst hasn't passed yet.
    "Arrived" means on or after the trigger date, so a scan missed on the
    exact day still sends it the next day. Each reminder is sent once
    (a permanent marker, claimed before sending); if it reaches nobody
    the marker is released so the next scan retries.

    Requires init_db(). Returns one ExitNotice per reminder attempted
    this call — reminders already sent earlier are not listed.
    """
    as_of = as_of or date.today()
    notices: list[ExitNotice] = []
    seen: set[str] = set()

    for record in await get_recent_alerts(limit=500):
        try:
            payload = AlertPayload.model_validate(record.payload)
        except Exception:  # noqa: BLE001 — a row from an older schema: skip, don't break reminders
            continue
        if payload.catalyst.date < as_of:
            continue  # the event has happened; an exit reminder is moot

        for trigger in payload.execution.exit_triggers:
            if not trigger.active or trigger.trigger_date is None or trigger.trigger_date > as_of:
                continue
            key = (
                f"exit:{payload.ticker}:{payload.catalyst.date.isoformat()}:"
                f"{payload.signal.arbitrage_signal}:{trigger.label}"
            )
            if key in seen:
                continue  # the same alert stored again after a cooldown
            seen.add(key)
            if not await claim_exit_notice(key):
                continue  # already sent on an earlier scan

            warnings: list[str] = []
            destinations = await _deliverable_destinations(warnings)
            outcomes = await _dispatch_text(
                format_exit_text(payload, trigger.label, trigger.trigger_date, as_of),
                {
                    "type": "exit_reminder",
                    "ticker": payload.ticker,
                    "catalyst": payload.catalyst.model_dump(mode="json"),
                    "arbitrage_signal": payload.signal.arbitrage_signal,
                    "exit_label": trigger.label,
                    "exit_date": trigger.trigger_date.isoformat(),
                },
                destinations,
            )
            delivered = sum(1 for o in outcomes if o.ok)
            if not outcomes:
                status = "NO_CHANNELS"
            elif delivered == 0:
                status = "FAILED"
            else:
                status = "SENT" if delivered == len(outcomes) else "PARTIAL"
            if delivered == 0:
                await release_exit_notice(key)  # nobody was told — try again next scan

            try:
                await record_deliveries(
                    [o for o in outcomes if o.destination_id is not None],
                    payload.ticker, f"EXIT:{trigger.label}", payload.catalyst.date,
                )
            except Exception:  # noqa: BLE001 — the reminder already went out; only the log was lost
                pass
            notices.append(ExitNotice(payload.ticker, trigger.label, trigger.trigger_date, status, outcomes))

    return notices
