"""
ingestion/market_data_feed.py
--------------------------------
Options chain & underlying quote ingestor (Phase B, Engine 2 input).

Uses yfinance — free, no auth, verified live against a real ticker (CYTK,
the same test case fda_scraper.py and clinical_trials_api.py used; its
PDUFA date of 2026-11-14 sits neatly between two real expirations:
2026-10-16 and 2026-11-20).

SCOPE, matching the directory structure's own line for this module
("Options chain & underlying quote ingestor"): this module fetches and
validates CURRENT snapshots — spot quote, and the full option chain for
whichever expirations bound a given catalyst date. It does NOT compute
IV Rank, Greeks, or market-implied probability — that's
engines/options_quant/{greeks,metrics}.py's job, operating on the
structured output of this module.

IV HISTORY: the manual's IV Rank formula needs a 52-WEEK HISTORY of
implied volatility. yfinance (free) only exposes a CURRENT options-chain
snapshot per call — no historical IV time series; that is normally a
paid data product (theta-data, ORATS, CBOE DataShop). So the history is
built here instead: fetch_iv_reference() takes one reading, the scan
calls it daily for every tracked ticker and stores the result
(pipeline.record_iv_snapshots). It is a bootstrap — IV Rank only becomes
available for a ticker once enough daily readings have accumulated.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

OptionType = Literal["CALL", "PUT"]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Quote(BaseModel):
    """Current underlying snapshot."""

    ticker: str
    last_price: float
    previous_close: Optional[float] = None
    market_cap: Optional[float] = None
    currency: Optional[str] = None
    fetched_at: datetime


class OptionContract(BaseModel):
    contract_symbol: str
    strike: float
    option_type: OptionType
    last_price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    implied_volatility: Optional[float] = None
    in_the_money: Optional[bool] = None

    @property
    def has_quoted_market(self) -> bool:
        """True if there's an actual two-sided market, not just a stale last trade."""
        return bool(self.bid and self.ask and self.bid > 0 and self.ask > 0)

    @property
    def bid_ask_spread_pct(self) -> Optional[float]:
        """Spread as a % of mid — a direct read on execution feasibility."""
        if not self.has_quoted_market:
            return None
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return None
        return round((self.ask - self.bid) / mid, 4)


class OptionChainSnapshot(BaseModel):
    ticker: str
    expiration: date
    calls: list[OptionContract] = Field(default_factory=list)
    puts: list[OptionContract] = Field(default_factory=list)
    fetched_at: datetime

    @property
    def total_open_interest(self) -> int:
        return sum((c.open_interest or 0) for c in self.calls + self.puts)

    @property
    def total_volume(self) -> int:
        return sum((c.volume or 0) for c in self.calls + self.puts)

    def atm_straddle(self, spot: float) -> Optional[tuple[OptionContract, OptionContract]]:
        """Nearest-strike call+put to spot — the basis for a straddle-implied move."""
        if not self.calls or not self.puts:
            return None
        nearest_call = min(self.calls, key=lambda c: abs(c.strike - spot))
        nearest_put = min(self.puts, key=lambda p: abs(p.strike - spot))
        if nearest_call.strike != nearest_put.strike:
            # Strikes aren't aligned 1:1 on every chain; fall back to the put
            # at the same strike as the chosen call, if one exists.
            same_strike_put = next((p for p in self.puts if p.strike == nearest_call.strike), None)
            if same_strike_put:
                nearest_put = same_strike_put
        return nearest_call, nearest_put


class MarketSnapshot(BaseModel):
    """Top-level output: everything Engine 2 needs for one ticker/catalyst pair."""

    ticker: str
    catalyst_date: date
    quote: Quote
    expiry_before_catalyst: Optional[OptionChainSnapshot] = None
    expiry_after_catalyst: Optional[OptionChainSnapshot] = None
    fetched_at: datetime


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _clean_number(value) -> Optional[float]:
    """yfinance surfaces missing data as NaN, not None — pydantic chokes on NaN."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _clean_int(value) -> Optional[int]:
    f = _clean_number(value)
    return None if f is None else int(f)


def _row_to_contract(row, option_type: OptionType) -> OptionContract:
    return OptionContract(
        contract_symbol=row["contractSymbol"],
        strike=float(row["strike"]),
        option_type=option_type,
        last_price=_clean_number(row.get("lastPrice")),
        bid=_clean_number(row.get("bid")),
        ask=_clean_number(row.get("ask")),
        volume=_clean_int(row.get("volume")),
        open_interest=_clean_int(row.get("openInterest")),
        implied_volatility=_clean_number(row.get("impliedVolatility")),
        in_the_money=bool(row.get("inTheMoney")) if row.get("inTheMoney") is not None else None,
    )


def nearest_bounding_expirations(
    available: list[str], catalyst_date: date
) -> tuple[Optional[date], Optional[date]]:
    """
    Given the ticker's available expiration strings (yfinance format
    'YYYY-MM-DD') and a catalyst date, returns (nearest_before, nearest_at_or_after) —
    the expiration cycles immediately bounding the catalyst, per the manual's
    Engine 2 spec. Either side can be None if no expiration exists there.
    """
    parsed = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in available)

    before = max((d for d in parsed if d < catalyst_date), default=None)
    at_or_after = min((d for d in parsed if d >= catalyst_date), default=None)
    return before, at_or_after


# --------------------------------------------------------------------------
# Public API (sync yfinance calls wrapped in asyncio.to_thread for a
# consistent async interface across the ingestion layer)
# --------------------------------------------------------------------------

def _fetch_quote_sync(ticker: str) -> Quote:
    import yfinance as yf

    t = yf.Ticker(ticker)
    info = t.fast_info

    return Quote(
        ticker=ticker,
        last_price=float(info["lastPrice"]),
        previous_close=_clean_number(info.get("previousClose")),
        market_cap=_clean_number(info.get("marketCap")),
        currency=info.get("currency"),
        fetched_at=datetime.now(timezone.utc),
    )


def _fetch_expirations_sync(ticker: str) -> list[str]:
    import yfinance as yf

    return list(yf.Ticker(ticker).options)


def _fetch_chain_sync(ticker: str, expiration: date) -> OptionChainSnapshot:
    import yfinance as yf

    chain = yf.Ticker(ticker).option_chain(expiration.isoformat())
    return OptionChainSnapshot(
        ticker=ticker,
        expiration=expiration,
        calls=[_row_to_contract(row, "CALL") for _, row in chain.calls.iterrows()],
        puts=[_row_to_contract(row, "PUT") for _, row in chain.puts.iterrows()],
        fetched_at=datetime.now(timezone.utc),
    )


async def fetch_quote(ticker: str) -> Quote:
    return await asyncio.to_thread(_fetch_quote_sync, ticker)


async def fetch_option_expirations(ticker: str) -> list[date]:
    raw = await asyncio.to_thread(_fetch_expirations_sync, ticker)
    return sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in raw)


async def fetch_option_chain(ticker: str, expiration: date) -> OptionChainSnapshot:
    return await asyncio.to_thread(_fetch_chain_sync, ticker, expiration)


async def fetch_market_snapshot(ticker: str, catalyst_date: date) -> MarketSnapshot:
    """
    Top-level entry point: fetches the quote plus the option chains for
    the expiration cycles immediately bounding catalyst_date, per the
    manual's Engine 2 spec (Phase B, step 1).
    """
    quote_task = asyncio.create_task(fetch_quote(ticker))
    raw_expirations = await asyncio.to_thread(_fetch_expirations_sync, ticker)

    before, after = nearest_bounding_expirations(raw_expirations, catalyst_date)

    chain_before_task = asyncio.create_task(fetch_option_chain(ticker, before)) if before else None
    chain_after_task = asyncio.create_task(fetch_option_chain(ticker, after)) if after else None

    quote = await quote_task
    chain_before = await chain_before_task if chain_before_task else None
    chain_after = await chain_after_task if chain_after_task else None

    return MarketSnapshot(
        ticker=ticker,
        catalyst_date=catalyst_date,
        quote=quote,
        expiry_before_catalyst=chain_before,
        expiry_after_catalyst=chain_after,
        fetched_at=datetime.now(timezone.utc),
    )


async def fetch_iv_reference(
    ticker: str, min_days_to_expiry: int
) -> Optional[tuple[Quote, OptionChainSnapshot]]:
    """
    The quote plus one option chain to take a daily IV reading from: the
    first expiration at least `min_days_to_expiry` away (the last listed
    one if none is that far out). Using a consistent "about a month out"
    expiry keeps day-to-day readings comparable. None if the ticker has
    no listed options.
    """
    expirations = await fetch_option_expirations(ticker)
    if not expirations:
        return None
    cutoff = date.today() + timedelta(days=min_days_to_expiry)
    expiry = next((d for d in expirations if d >= cutoff), expirations[-1])
    quote, chain = await asyncio.gather(fetch_quote(ticker), fetch_option_chain(ticker, expiry))
    return quote, chain


def _fetch_daily_closes_sync(ticker: str, start: date, end: date) -> list[tuple[date, float]]:
    import yfinance as yf

    history = yf.Ticker(ticker).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), auto_adjust=False)
    closes = []
    for stamp, close in history["Close"].items():
        value = _clean_number(close)
        if value is not None:
            closes.append((stamp.date(), value))
    return closes


async def fetch_daily_closes(ticker: str, start: date, end: date) -> list[tuple[date, float]]:
    """(day, closing price) for each trading day from `start` to `end`
    inclusive, oldest first. Historical closes do not depend on the
    market being open now."""
    return await asyncio.to_thread(_fetch_daily_closes_sync, ticker, start, end)


def reaction_closes(closes: list[tuple[date, float]], decision_date: date) -> Optional[tuple[float, float]]:
    """
    (close on the last trading day BEFORE the decision date, close on the
    first trading day AFTER it), or None if either is missing — the
    usual reason being that the day after hasn't happened yet.

    The decision day itself is skipped on purpose. A decision announced
    before the open moves that day's close; one announced after the
    close moves the next day's. Measuring from the day before to the day
    after catches the reaction either way, at the cost of including one
    extra day of ordinary movement.
    """
    before = [price for day, price in closes if day < decision_date]
    after = [price for day, price in closes if day > decision_date]
    if not before or not after:
        return None
    return before[-1], after[0]


def us_market_open(now: Optional[datetime] = None) -> bool:
    """
    True during the regular US options session: Monday to Friday,
    9:30 AM to 4:00 PM New York time.

    Everything this system reads from an option chain is only real
    inside that window. Outside it the data source returns zero or stale
    bids and asks, junk implied volatility, and open interest that is
    blanked or partial (seen live for one stock: 13,224 at midday, 1,015
    at midnight). A score, a liquidity check or a volatility reading
    taken then is wrong, and a volatility reading is also STORED and
    feeds IV Rank for a year.

    Market holidays are not known to this function: on a holiday it
    answers True and the quotes are simply stale. That is about nine
    days a year, and the readings' own live-quote checks reject most of
    what a closed market returns.
    """
    from zoneinfo import ZoneInfo

    eastern = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if eastern.weekday() >= 5:
        return False
    minutes = eastern.hour * 60 + eastern.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


# --------------------------------------------------------------------------
# Phase A liquidity gate support (raw aggregation only — the manual's
# actual pass/fail eviction decision belongs to pipeline.py, not here)
# --------------------------------------------------------------------------

@dataclass
class LiquiditySummary:
    expiration: date
    total_open_interest: int
    total_volume: int


def front_month_liquidity(snapshot: MarketSnapshot) -> Optional[LiquiditySummary]:
    """
    Open interest and volume summed over the two expirations bounding the
    catalyst — what the manual's Phase A liquidity check (OI > 2000,
    daily volume > 500 "across front-month contracts") is run against.
    Returns None if neither chain was available (e.g. no listed options
    at all). `expiration` in the result is the more heavily held of the
    two, for reference.

    WHY BOTH EXPIRATIONS, NOT ONE: the question the filter asks is "can
    a position around this catalyst be opened and closed?", and trading
    around a catalyst is spread over the expiry before it and the one
    after. Two single-expiry readings were tried first and both misread
    real stocks:
      - the NEAREST expiry is often a freshly listed weekly almost nobody
        holds — SMMT, a liquid name, showed open interest of 3;
      - the MOST-HELD expiry alone undercounts the day's trading — CYTK
        showed 470 contracts on one expiry and 406 on the other at
        midday, failing a 500-a-day test on either while trading 876.

    The volume figure is the session's volume SO FAR when read during
    market hours, not a full day — which is why the scan runs late in
    the session (see entrypoint.py).
    """
    candidates = [c for c in (snapshot.expiry_before_catalyst, snapshot.expiry_after_catalyst) if c]
    if not candidates:
        return None

    front = max(candidates, key=lambda c: c.total_open_interest)
    return LiquiditySummary(
        expiration=front.expiration,
        total_open_interest=sum(c.total_open_interest for c in candidates),
        total_volume=sum(c.total_volume for c in candidates),
    )
