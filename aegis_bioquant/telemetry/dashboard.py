"""
telemetry/dashboard.py
-----------------------
Phase E: the "operational UI dashboard" — a small local web app that is
the receiving end of telemetry/alerts.py's dashboard channel.

STATUS: BUILT and tested (webhook -> database -> page, in a real browser).

RUN IT (from the project root, so it opens the same database file the
scan writes to):
    python -m aegis_bioquant.telemetry.dashboard
then open http://127.0.0.1:8765. A scan run against the same database
shows up here by itself — the scan saves its alerts and results to the
database and this page reads them. POST /webhook is only needed to
receive alerts from a scan that writes to a DIFFERENT database.

This is the local, single-user, no-login viewer. The hosted site for
customers (web/app.py) shows the same page behind a login.

WHAT IT SHOWS (the look and the script are in telemetry/dashboard_ui.py):
  - Headline numbers  decisions tracked, in entry window, active signals,
                      next decision, track record
  - Upcoming          ONE row per upcoming FDA decision: three checks
    decisions         (evidence, market move, options price) and a
                      verdict, best first; a row opens to explain itself
  - Signals           the alerts raised
  - Track record      recorded FDA decisions and the stock's reaction
  - Phase 3 watchlist trials nearing completion (never scored)
  - How to read this  the handful of terms, in plain words

build_state() assembles all of it from the tables the scan writes. Its
"catalysts" list is what the page's main view draws; the older keys
(predictions, catalysts_in_window, event_moves, ...) are still returned
because tests and other code read them.

NO LOGIN, BY DESIGN LIMITATION, stated plainly: there is no
authentication here. The webhook stores whatever valid AlertPayload it
is sent, and the page shows it to whoever opens it. That is acceptable
only because settings.DASHBOARD_HOST defaults to 127.0.0.1 (this machine
only). Do not bind it to a public address as-is.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from aegis_bioquant.config import constants, settings
from aegis_bioquant.core.database import (
    dispose_db,
    get_catalysts_in_entry_window,
    get_iv_history_days,
    get_last_scan_time,
    get_upcoming_catalysts,
    get_latest_event_moves,
    get_latest_predictions,
    get_latest_screened,
    get_outcome_rows,
    get_phase3_watchlist,
    get_recent_alerts,
    get_track_record,
    get_trial_overrides,
    get_llm_reviews,
    init_db,
    save_alert_once,
)
from aegis_bioquant.ingestion.market_data_feed import us_market_open
from aegis_bioquant.telemetry.alerts import AlertPayload
from aegis_bioquant.telemetry.dashboard_ui import (
    DASHBOARD_SCRIPT,
    DASHBOARD_SECTIONS,
    FAVICON_LINK,
    FONT_LINKS,
    LOGO_SVG,
    PAGE_STYLE,
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await init_db()
    yield
    await dispose_db()


app = FastAPI(title="FDAOdds Dashboard", lifespan=_lifespan, docs_url=None, redoc_url=None)


def _utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """The database hands timestamps back without a timezone; they were
    written as UTC, so label them as such for the browser."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@app.post("/webhook")
async def receive_alert(payload: AlertPayload) -> dict:
    """telemetry/alerts.py's dashboard channel posts here. FastAPI
    validates the body against AlertPayload before this runs — anything
    that isn't a well-formed alert is rejected with a 422, not stored."""
    # save_alert_once, not save_alert: a scan writing to this same
    # database has already stored the alert itself (telemetry/alerts.py),
    # so the copy arriving here must not create a second row.
    alert_id = await save_alert_once(
        ticker=payload.ticker,
        arbitrage_signal=payload.signal.arbitrage_signal,
        catalyst_date=payload.catalyst.date,
        payload=payload.model_dump(mode="json"),
        within_seconds=settings.ALERT_COOLDOWN_SECONDS,
    )
    return {"stored": alert_id is not None, "id": alert_id}


async def build_state() -> dict:
    """Everything the dashboard page shows, as JSON. Shared with the
    hosted site (web/app.py), which serves the same page behind a login."""
    alerts = await get_recent_alerts()
    predictions = await get_latest_predictions()
    catalysts = await get_catalysts_in_entry_window()
    screened = await get_latest_screened()
    phase3 = await get_phase3_watchlist()
    overrides = await get_trial_overrides()
    event_moves = await get_latest_event_moves()
    track_record = await get_track_record()

    today = date.today()
    # A catalyst whose outcome is already recorded is no longer upcoming,
    # even if its original date is still ahead: the FDA often decides
    # early, and a date can turn out to be moved or never real.
    decided = {(k[0], k[1], k[2]) for k, v in (await get_outcome_rows()).items() if v.status == "CONFIRMED"}
    upcoming = [e for e in await get_upcoming_catalysts(today) if (e.ticker, e.event_type, e.start_date) not in decided]
    rows = _catalyst_rows(upcoming, predictions, screened, overrides, event_moves, today)
    reviews = await get_llm_reviews()
    for row in rows:
        row["review"] = _claude_view(reviews.get((row["ticker"], row["event_type"], date.fromisoformat(row["date"]))))
        # Our evidence check passes but Claude, reading the company's own
        # announcements, puts approval well below the passing mark. No rule
        # acts on this; the page says so next to the verdict.
        # The review found the FDA has moved this decision. Until a reviewer
        # confirms it (and the new date gets its own row), the row must not
        # read as a setup for a date that is no longer real.
        if row["review"] and row["review"]["date_moved_to"]:
            row["status"], row["status_label"] = "late", "Date has moved"
            row["status_detail"] = (
                f"Our evidence review found the FDA has moved this decision to {_long_date(row['review']['date_moved_to'])}. "
                "The date shown is out of date and this row is being corrected."
            )
            row["status_action"] = "What to do: nothing. Do not act on the date shown here."
        row["review_drives"] = (row["evidence_basis"] or "").startswith("REVIEW")
        row["review_disagrees"] = bool(
            row["review"] and row["evidence"] is not None and not row["review_drives"]
            and row["evidence"] >= constants.LONG_ALPHA_P_FUNDAMENTAL_MIN
            and row["review"]["score"] < _CLAUDE_DISAGREES_BELOW
        )
    rows.sort(key=lambda r: (_STATUS_ORDER[r["status"]], -r["checks_passed"], r["days_until"], r["ticker"]))
    decisions_ahead = [r for r in rows if r["event_type"] == "PDUFA" and r["status_label"] != "Date has moved"]

    return {
        "today": today.isoformat(),
        # The main list: one row per upcoming catalyst, already worded for
        # display. The older keys below are kept — other code reads them.
        "catalysts": rows,
        "signal_labels": SIGNAL_LABELS,
        "summary": {
            "tracked": len(rows),
            "in_window": sum(1 for r in rows if r["phase"] == "window"),
            "signals": sum(1 for r in rows if r["status"] == "signal" or (r["status"] == "avoid" and r["phase"] == "window")),
            "setting_up": sum(1 for r in rows if r["status"] == "setup"),
            "close": sum(1 for r in rows if r["status"] == "close"),
            "scored": sum(1 for r in rows if r["evidence"] is not None),
            "next": min(decisions_ahead, key=lambda r: r["days_until"]) if decisions_ahead else None,
            "iv_history_days": await get_iv_history_days(),
            "iv_days_needed": constants.IV_RANK_MIN_READINGS,
            "last_scan": _utc_iso(await get_last_scan_time()),
            "market_open": us_market_open(),
        },
        "alerts": [
            {"id": a.id, "received_at": _utc_iso(a.received_at), "payload": _unbranded(a.payload)}
            for a in alerts
        ],
        "predictions": [
            {
                "ticker": p.ticker,
                "nct_id": p.nct_id,
                "catalyst_type": p.catalyst_type,
                "catalyst_date": p.catalyst_date.isoformat(),
                "spot_price": p.spot_price,
                "p_fundamental": p.p_fundamental,
                "p_market": p.p_market,
                "p_market_confidence": p.p_market_confidence,
                "event_implied_move": p.event_implied_move,
                "iv_rank": p.iv_rank,
                "signal_notes": p.signal_notes,
                "arbitrage_signal": p.arbitrage_signal,
                "hold_state": p.hold_state,
                "allocation_pct": p.allocation_pct,
                "scanned_at": _utc_iso(p.scanned_at),
            }
            for p in predictions
        ],
        "phase3_watch": [
            {
                "ticker": e.ticker,
                "company_name": e.company_name,
                "title": e.title,
                "trial": e.drug_or_trial_candidate,
                "nct_id": e.resolved_nct_id,
                "completion_date": e.start_date.isoformat(),
                "source_url": e.source_url,
            }
            for e in phase3
        ],
        "track_record": _track_record_state(track_record),
        "event_moves": [
            {
                "ticker": m.ticker,
                "event_type": m.event_type,
                "catalyst_date": m.catalyst_date.isoformat(),
                "event_move": m.event_move,
                "baseline_iv": m.baseline_iv,
                "post_event_iv": m.post_event_iv,
                "confidence": m.confidence,
                "as_of": m.as_of.isoformat(),
            }
            for m in event_moves
        ],
        "confirmed": [
            {
                "ticker": ticker,
                "catalyst_date": when.isoformat(),
                "nct_id": o.nct_id,
                "confirmed_at": _utc_iso(o.confirmed_at),
            }
            for (ticker, _event_type, when), o in overrides.items()
        ],
        "screened": [
            {
                "ticker": x.ticker,
                "catalyst_date": x.catalyst_date.isoformat(),
                "reasons": x.reasons,
                "scanned_at": _utc_iso(x.scanned_at),
            }
            for x in screened
        ],
        "catalysts_in_window": [
            {
                "ticker": e.ticker,
                "company_name": e.company_name,
                "event_type": e.event_type,
                "title": e.title,
                "start_date": e.start_date.isoformat(),
                "drug_or_trial_candidate": e.drug_or_trial_candidate,
                "resolved_nct_id": e.resolved_nct_id,
                "resolution_tier": e.resolution_tier,
                "source_url": e.source_url,
                "last_seen_at": _utc_iso(e.last_seen_at),
            }
            for e in catalysts
        ],
    }


SIGNAL_LABELS = {
    "LONG_RUN_UP_ALPHA": "Buy setup",
    "LONG_TERM_EQUITY_ACCUMULATION": "Strong evidence, stock only",
    "STRUCTURAL_COLLAPSE_AVOID": "Avoid",
}
# Trial matches the scan scores without a person checking (pipeline.py
# holds the authoritative list; importing the pipeline here would pull
# the whole scanner into the web process).
_AUTO_SCORED_TIERS = ("STUDY_TRIAL_NAME", "ACRONYM", "CONFIRMED")
_ENTRY_WINDOWS = {
    "PDUFA": (constants.PDUFA_WINDOW_MIN_DAYS, constants.PDUFA_WINDOW_MAX_DAYS),
    "ADCOM": (constants.ADCOM_WINDOW_MIN_DAYS, constants.ADCOM_WINDOW_MAX_DAYS),
}
_STATUS_ORDER = {"signal": 0, "avoid": 0, "setup": 1, "stock": 2, "close": 2, "none": 3, "filtered": 4, "unscored": 5, "late": 6}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return (text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")) if text else ""


# What each verdict asks of the reader, in one sentence. A newcomer's first
# question about any row is "so what do I do with this?" — and for most
# rows the honest answer is "nothing", which is worth saying outright.
_ACTIONS = {
    "signal": "What to do: this is the situation the alerts exist for. Read the reasons here and the company's filing, then decide for yourself. It is information, not advice.",
    "avoid": "What to do: treat it as a warning, not a trade idea. Weak evidence going into a decision the market expects to be big.",
    "setup": "What to do: nothing yet. Keep an eye on it. If it still passes when its window opens you will be alerted.",
    "stock": "What to do: nothing yet. If the evidence is still this strong when its entry window opens, you get a stock-only alert.",
    "close": "What to do: nothing. It moves up the list by itself if the missing check starts to pass.",
    "none": "What to do: nothing. Most FDA decisions never become a setup.",
    "filtered": "What to do: ignore it. It is outside what this system covers.",
    "unscored": "What to do: nothing. Look again after the next daily scan.",
    "late": "What to do: nothing. The window for acting on this one has passed.",
}


_CLAUDE_DISAGREES_BELOW = 60   # our evidence check passes at 75; this far under it is worth a warning

_ENDPOINT_WORDS = {
    "MET": "The main trial met its primary goal.",
    "NOT_MET": "The main trial did not meet its primary goal.",
    "MIXED": "The main trial's results were mixed.",
    "SINGLE_ARM": "The main trial had no comparison group.",
    "UNKNOWN": "The filings do not say whether the main trial met its goal.",
}
_QUALITY_WORDS = {
    "STRONG": "The sources described the results in detail.",
    "ADEQUATE": "The sources described the results in outline.",
    "THIN": "Little has been published about the results, so the score stays close to 83, the usual approval rate.",
}


def _long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _unbranded(value):
    """A stored alert payload with the AI vendor's name taken out. Alerts
    saved on 2026-10-09 carry it in a label and a status code; nothing a
    customer's browser receives should name the vendor."""
    if isinstance(value, dict):
        return {k: _unbranded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unbranded(v) for v in value]
    if isinstance(value, str):
        return value.replace("Claude's read of the evidence", "Evidence review").replace("CLAUDE_REVIEW", "EVIDENCE_REVIEW")
    return value


def _whole(text: str) -> str:
    """A point that was cut off mid-sentence when stored is shown ending
    in "..." rather than stopping on a dangling word."""
    text = (text or "").strip()
    return text if not text or text[-1] in ".!?)'\"" else text + "..."


def _claude_view(review) -> Optional[dict]:
    """Claude's review for one row, worded for the page; None if there is
    none. See engines/clinical_nlp/llm_review.py. From the first scan
    after a review is written it is the evidence score the verdict uses."""
    if review is None or review.status != "REVIEWED" or review.score is None:
        return None
    return {
        "score": review.score,
        "drug": review.drug,
        "indication": review.indication,
        "application": review.application,
        "summary": review.summary,
        "endpoint": _ENDPOINT_WORDS.get(review.primary_endpoint or "UNKNOWN", _ENDPOINT_WORDS["UNKNOWN"]),
        "quality": _QUALITY_WORDS.get(review.evidence_quality or "THIN", _QUALITY_WORDS["THIN"]),
        "positives": [_whole(t) for t in (review.positives or [])],
        "risks": [_whole(t) for t in (review.risks or [])],
        "sources": [u for u in (review.sources or []) if isinstance(u, str) and u.startswith("https://")][:14],
        "reviewed_at": _utc_iso(review.reviewed_at),
        "date_moved_to": review.date_moved_to.isoformat() if review.date_moved_to else None,
    }


def _check(key: str, state: str, big: str, sub: str, missing: str = "") -> dict:
    return {"key": key, "state": state, "big": big, "sub": sub, "missing": missing}


def _checks(p, m) -> list[dict]:
    """
    The three things a buy setup needs, each as one plain answer. They are
    the LONG_RUN_UP_ALPHA conditions in logic/matrix_matcher.py, read from
    the same thresholds, so the page can never disagree with the rule.

    state: "pass", "soft" (passes, but on a stand-in: baseline evidence or
    the options-cost estimate), "fail", or "unknown" (no reading).
    `missing` is how a failed check is described when it is the one thing
    standing between a company and a setup.
    """
    # 1. Evidence
    if p is None:
        evidence = _check("evidence", "unknown", "Not scored", "waiting for the next scan", "it has not been scored")
    else:
        points = round(p.p_fundamental * 100)
        basis = p.evidence_basis or "TRIAL_RESULTS"
        if p.p_fundamental >= constants.LONG_ALPHA_P_FUNDAMENTAL_MIN:
            if basis == "CLAUDE":
                evidence = _check("evidence", "pass", "Strong", f"score {points}, from our review")
            elif basis == "CLAUDE_THIN":
                evidence = _check("evidence", "soft", "Typical", f"score {points}, little published")
            elif basis == "TRIAL_RESULTS":
                evidence = _check("evidence", "pass", "Strong", f"score {points}, from trial results")
            elif basis == "TRIAL_NO_RESULTS":
                evidence = _check("evidence", "soft", "Typical", f"score {points}, trial results not published")
            else:
                evidence = _check("evidence", "soft", "Typical", f"score {points}, no trial checked")
        elif p.p_fundamental <= constants.SHORT_ALPHA_P_FUNDAMENTAL_MAX:
            evidence = _check("evidence", "fail", "Weak", f"score {points}", "the trial evidence is weak")
        else:
            evidence = _check("evidence", "fail", "Mixed", f"score {points}", "the trial evidence is mixed")

    # 2. Does the market expect the decision to move the stock?
    value = p.event_implied_move if (p is not None and p.event_implied_move is not None) else (m.event_move if m else None)
    rough = bool(m) and m.confidence == "LOW_CONFIDENCE_SHORT_BASELINE"
    if value is None:
        move = _check("move", "unknown", "No reading", "needs live option prices", "there is no reading of the move the market expects")
    elif value >= constants.EVENT_MATTERS_MIN_MOVE and not rough:
        move = _check("move", "pass", f"±{value * 100:.0f}%", "a big move is expected")
    elif value >= constants.EVENT_MATTERS_MIN_MOVE:
        move = _check("move", "fail", f"±{value * 100:.0f}%", "rough reading, not counted", "the expected-move reading is too rough to rely on")
    elif value == 0:
        move = _check("move", "fail", "None", "market expects no extra move", "the market is not pricing any move on this decision")
    else:
        move = _check("move", "fail", f"±{value * 100:.0f}%", f"under the {constants.EVENT_MATTERS_MIN_MOVE:.0%} we look for", "the market expects only a small move")

    # 3. Are options still cheap?
    rank = p.iv_rank if p is not None else None
    if rank is None:
        cost = _check("cost", "unknown", "No reading", "needs live option prices", "there is no reading of what its options cost")
    else:
        note = f"{rank:.0f} of 100" + (", estimate" if p.iv_rank_estimated else "")
        if rank < constants.IV_RANK_UNDERPRICED_THRESHOLD:
            cost = _check("cost", "soft" if p.iv_rank_estimated else "pass", "Cheap", note)
        elif rank > constants.COLLAPSE_IV_RANK_RICH:
            cost = _check("cost", "fail", "Expensive", note, "its options are already expensive")
        else:
            cost = _check("cost", "fail", "Not cheap", note, "its options are no longer cheap")
    return [evidence, move, cost]


def _catalyst_rows(upcoming, predictions, screened, overrides, event_moves, today: date) -> list[dict]:
    """
    One display row per upcoming catalyst: three checks (see _checks) and
    a verdict, worded for a reader who has never heard of a PDUFA date.

    `status` is a short code the page styles and sorts by; `status_label`
    is the verdict shown and `status_detail` the sentence behind it:

      signal    passes the rule AND is inside its entry window (alerts go out)
      avoid     the avoid rule fired
      setup     passes the rule today, entry window not open yet
      close     two of the three checks pass
      none      scored, not a setup
      filtered  left out by the size / price / liquidity screen
      unscored  no score yet
      late      past its entry window

    Between a score and a screen-out for the same catalyst, whichever is
    more recent describes it now. Rows come back best first.
    """
    scored_by = {(p.ticker, p.catalyst_date): p for p in predictions}
    screened_by = {(x.ticker, x.catalyst_date): x for x in screened}
    moves_by = {(m.ticker, m.catalyst_date): m for m in event_moves}

    rows = []
    for e in upcoming:
        key = (e.ticker, e.start_date)
        days = (e.start_date - today).days
        low, high = _ENTRY_WINDOWS[e.event_type]
        phase = "upcoming" if days > high else ("window" if days >= low else "closed")

        p, x, m = scored_by.get(key), screened_by.get(key), moves_by.get(key)
        screened_last = x is not None and (p is None or _aware(x.scanned_at) > _aware(p.scanned_at))
        if screened_last:
            p = None
        checks = _checks(p, m)
        passed = [c for c in checks if c["state"] in ("pass", "soft")]
        failed = [c for c in checks if c not in passed]
        opens_in = days - high
        signal = p.arbitrage_signal if p is not None else "NO_SIGNAL"

        if screened_last:
            status = ("filtered", "Filtered out", "Left out because: " + _sentence("; ".join(x.reasons)))
        elif p is None and days > constants.EVENT_MOVE_LOOKAHEAD_DAYS:
            status = ("unscored", "Too far out", f"Scoring starts {constants.EVENT_MOVE_LOOKAHEAD_DAYS} days before the decision, "
                      f"in {days - constants.EVENT_MOVE_LOOKAHEAD_DAYS} day{'' if days - constants.EVENT_MOVE_LOOKAHEAD_DAYS == 1 else 's'}. "
                      "Options this far ahead say little about the decision.")
        elif p is None:
            status = ("unscored", "Not scored yet", "It will be scored at the next scan during US market hours.")
        elif phase == "closed":
            status = ("late", "Too late to enter", f"Fewer than {low} days remain before the decision. New positions are not opened this close to it.")
        elif signal == "STRUCTURAL_COLLAPSE_AVOID":
            status = ("avoid", "Avoid", "Weak trial evidence going into a decision the market expects to be big, with options "
                      "already expensive. This is the reverse of a buy setup.")
        elif signal == "LONG_TERM_EQUITY_ACCUMULATION" and phase == "window":
            status = ("signal", SIGNAL_LABELS[signal],
                      "The trial evidence is very strong (90 or more), but this company's options are expensive or barely traded, so it does not "
                      "pass the three checks. It is inside its entry window, so a stock-only alert goes out: the case is for the shares, not options.")
        elif signal != "NO_SIGNAL" and phase == "window":
            status = ("signal", SIGNAL_LABELS.get(signal, "Buy setup"),
                      f"All three checks pass and it is inside its entry window ({low} to {high} days before the decision). Alerts go out for this.")
        elif signal == "LONG_TERM_EQUITY_ACCUMULATION" and phase != "window":
            # A different rule from the three checks: strong evidence, but
            # the options are expensive or barely traded, so the rule points
            # at the shares instead. It must not read as "passes all checks".
            status = ("stock", "Strong evidence, stock only",
                      "The trial evidence is very strong (90 or more), but this company's options are expensive or barely traded, so it does not "
                      "pass the three checks. The case here is for the shares themselves, not options.")
        elif signal != "NO_SIGNAL":
            status = ("setup", "Setting up", f"It passes all the checks today. Its entry window opens in {opens_in} day{'' if opens_in == 1 else 's'}; "
                      "if it still passes then, it becomes a buy setup and alerts go out.")
        elif len(passed) == 2:
            status = ("close", "Close: 2 of 3", "One thing is missing: " + failed[0]["missing"] + ".")
        else:
            reasons = "; ".join(c["missing"] for c in failed if c["missing"])
            status = ("none", "No setup", _sentence("Not a setup because " + reasons) if reasons else "It does not meet the conditions for a setup.")

        # A check with no reading says why: "waiting for the next scan" is
        # wrong for a company that is left out or still too far ahead.
        why_blank = {"Filtered out": "left out, see the verdict", "Too far out": "too far ahead to measure"}.get(status[1])
        if why_blank:
            for check in checks:
                if check["state"] == "unknown":
                    check["sub"] = why_blank

        auto = bool(e.resolved_nct_id) and e.resolution_tier in _AUTO_SCORED_TIERS
        basis = (p.evidence_basis or "TRIAL_RESULTS") if p is not None else None
        if basis in ("CLAUDE", "CLAUDE_THIN"):
            trial_note = ("This score comes from our evidence review: an AI model's reading of what the company and others have published about the trial "
                          "and the FDA review. Its reasons and sources are below."
                          + (" Very little has been published, so it stays close to 83, the usual approval rate."
                             if basis == "CLAUDE_THIN" else "")
                          )
        elif basis == "BASELINE":
            trial_note = ("We could not tell with confidence which clinical trial this decision rests on, so the score is the "
                          "standard starting point: about 83 in 100 drugs that reach an FDA decision are approved. "
                          "Nothing specific to this drug has been checked.")
        elif basis == "TRIAL_NO_RESULTS":
            trial_note = ("The trial is known but has not published results in the public registry, so the score is close to "
                          "the standard starting point of 83.")
        elif e.resolution_tier == "CONFIRMED":
            trial_note = "Scored from the trial's published results. The trial was confirmed by a reviewer."
        elif auto:
            trial_note = "Scored from the trial's published results. The trial was matched automatically from the company's filing."
        elif e.resolved_nct_id:
            trial_note = "This trial is a best guess and has not been confirmed."
        else:
            trial_note = "No clinical trial has been matched to this event yet."

        rows.append(
            {
                "ticker": e.ticker,
                "company": e.company_name,
                "event_type": e.event_type,
                "event_label": "FDA decision" if e.event_type == "PDUFA" else "FDA advisory meeting",
                "date": e.start_date.isoformat(),
                "days_until": days,
                "phase": phase,
                "window_opens_in": opens_in if phase == "upcoming" else None,
                "status": status[0],
                "status_label": status[1],
                "status_detail": status[2],
                "status_action": _ACTIONS[status[0]],
                "checks": checks,
                "checks_passed": len(passed),
                "evidence": p.p_fundamental if p else None,
                # what the page is told; the vendor's name stays out of anything a customer's browser receives
                "evidence_basis": basis.replace("CLAUDE", "REVIEW") if basis else None,
                "iv_rank": p.iv_rank if p else None,
                "iv_rank_estimated": bool(p.iv_rank_estimated) if p else False,
                "event_move": m.event_move if m else None,
                "event_move_low_confidence": bool(m) and m.confidence != "GOOD",
                "drug": e.drug_or_trial_candidate,
                "nct_id": e.resolved_nct_id if (auto or p is None or basis != "BASELINE") else None,
                "trial_note": trial_note,
                "source_url": e.source_url,
            }
        )
    rows.sort(key=lambda r: (_STATUS_ORDER[r["status"]], -r["checks_passed"], r["days_until"], r["ticker"]))
    return rows


def _track_record_state(record: list[dict]) -> dict:
    """The confirmed FDA decisions plus the few summary numbers that can
    be stated honestly from them. `brier` is the mean squared gap between
    the clinical score and what happened (0 = perfect, 0.25 = a coin
    flip) over the decisions that HAD a score before the decision; with
    a handful of decisions it means very little, so the count is always
    shown beside it."""
    approved = sum(1 for r in record if r["outcome"] == "APPROVED")
    scored = [r for r in record if r["p_fundamental"] is not None]
    hit = lambda r: 1.0 if r["outcome"] == "APPROVED" else 0.0
    brier = sum((r["p_fundamental"] - hit(r)) ** 2 for r in scored) / len(scored) if scored else None
    # The same score for a flat guess of the base rate on the same
    # decisions: the comparison the study makes (H1).
    base_brier = sum((constants.FUNDAMENTAL_BASE_RATE_PRIOR - hit(r)) ** 2 for r in scored) / len(scored) if scored else None
    # "Called right" = leaned the way it went (50 or more on an approval,
    # under 50 on a rejection). Crude, and said so on the page.
    called = sum(1 for r in scored if (r["p_fundamental"] >= 0.5) == (r["outcome"] == "APPROVED"))
    return {
        "decisions": len(record),
        "approved": approved,
        "rejected": len(record) - approved,
        "scored": len(scored),
        "scored_approved": sum(1 for r in scored if r["outcome"] == "APPROVED"),
        "average_score": sum(r["p_fundamental"] for r in scored) / len(scored) if scored else None,
        "brier": brier,
        "base_brier": base_brier,
        "base_rate": constants.FUNDAMENTAL_BASE_RATE_PRIOR,
        "called": called,
        "rows": [
            {
                "ticker": r["ticker"],
                "catalyst_date": r["catalyst_date"].isoformat(),
                "decision_date": r["decision_date"].isoformat(),
                "outcome": r["outcome"],
                "price_change_pct": r["price_change_pct"],
                "p_fundamental": r["p_fundamental"],
                "signal": r["signal"],
                "event_move_priced": r["event_move_priced"],
            }
            for r in record
        ],
    }


@app.get("/api/state")
async def state() -> dict:
    return await build_state()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE


_PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FDAOdds</title>
{FONT_LINKS}
{FAVICON_LINK}
<style>{PAGE_STYLE}
  .localbar {{ display: flex; align-items: center; gap: 10px; max-width: 1240px; margin: 0 auto; padding: 16px 20px 0;
               font-weight: 700; font-size: 16px; letter-spacing: -0.01em; }}
</style>
</head>
<body>
<div class="localbar">{LOGO_SVG}<span>FDAOdds</span></div>
<main>
{DASHBOARD_SECTIONS}
</main>
<script>{DASHBOARD_SCRIPT}</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.DASHBOARD_HOST, port=settings.DASHBOARD_PORT, log_level="warning")
