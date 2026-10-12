"""
logic/phase_a.py
-----------------
Phase A's "Liquidity & Asset Structure Constraints": is this company the
kind this system trades, and can a position in it be opened and closed?

Pure checks over numbers already fetched — no network, no database — so
the pipeline (which evicts on them) and the alert layout (which displays
them) apply exactly the same rules.

HOW THIS DIFFERS FROM THE MANUAL, by the owner's decisions on 2026-10-06:
the manual isolates small companies with a share-price band of $2-$10.
Share price says little about company size, and enforced literally the
band removed every confidently matched candidate the scan found. So:
  - SIZE is now measured directly: company value (market capitalisation)
    at or under constants.MARKET_CAP_MAX. Any drug maker or biotech under
    that ceiling is in scope; the large pharmaceutical companies, where
    one approval barely moves the stock, are out.
  - PRICE keeps only the manual's $2 floor (a guard against penny
    stocks). The $10 ceiling is gone — with size measured directly it
    only served to exclude mid-sized biotechs with a high share price.
The option-liquidity thresholds are the manual's, unchanged.

MISSING DATA IS NOT A FAILURE: a company value the data source didn't
return, or open interest it blanked overnight, is treated as unknown
rather than as zero. Evicting on a gap in the feed would silently drop
real candidates.
"""

from __future__ import annotations

from typing import Optional

from aegis_bioquant.config import constants as c


def price_in_range(spot: float) -> bool:
    if spot < c.SPOT_PRICE_MIN:
        return False
    return c.SPOT_PRICE_MAX is None or spot <= c.SPOT_PRICE_MAX


def price_range_text() -> str:
    if c.SPOT_PRICE_MAX is None:
        return f"${c.SPOT_PRICE_MIN:.0f} minimum"
    return f"${c.SPOT_PRICE_MIN:.0f}-${c.SPOT_PRICE_MAX:.0f}"


def market_cap_ok(market_cap: Optional[float]) -> bool:
    """True when the company is within the size ceiling — or when its
    value is unknown (see MISSING DATA above)."""
    return market_cap is None or market_cap <= c.MARKET_CAP_MAX


def format_market_cap(market_cap: Optional[float]) -> str:
    if market_cap is None:
        return "unknown"
    if market_cap >= 1e9:
        return f"${market_cap / 1e9:,.1f}B"
    return f"${market_cap / 1e6:,.0f}M"


def phase_a_failures(
    spot: float, open_interest: int, volume: int, market_cap: Optional[float] = None
) -> tuple[list[str], list[str]]:
    """
    The constraints a candidate fails, in plain language, as
    (company failures, option-liquidity failures). Both empty = passes.

    The two lists are kept apart because the pipeline treats them
    differently: a company failure (too big, penny stock) evicts at once;
    thin options evict only if the candidate also fails to qualify for
    the stock-only track.
    """
    company: list[str] = []
    liquidity: list[str] = []

    if not price_in_range(spot):
        company.append(f"stock price ${spot:,.2f} is outside the allowed range ({price_range_text()})")
    if not market_cap_ok(market_cap):
        company.append(
            f"company value {format_market_cap(market_cap)} is above the "
            f"{format_market_cap(c.MARKET_CAP_MAX)} ceiling"
        )

    # Open interest of exactly 0 across chains that traded today is not a
    # real reading — the data source blanks open interest for part of the
    # night (seen live: 4,512 one evening, 0 at 2:30 AM, with the same
    # 2,002 volume). Treat that as unknown; a genuinely dead chain has no
    # volume either and fails the next check.
    open_interest_unknown = open_interest == 0 and volume > 0
    if not open_interest_unknown and not open_interest > c.MIN_OPEN_INTEREST:
        liquidity.append(f"too few of its options are held open ({open_interest:,} contracts; more than {c.MIN_OPEN_INTEREST:,} needed)")
    if not volume > c.MIN_DAILY_OPTIONS_VOLUME:
        liquidity.append(f"too few of its options traded today ({volume:,} contracts; more than {c.MIN_DAILY_OPTIONS_VOLUME:,} needed)")
    return company, liquidity
