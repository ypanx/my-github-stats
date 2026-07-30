"""Dates, windows, and the slicing of searches.

Everything here is pure arithmetic over dates, and every function is used by
more than one collection path. The chunking in particular is worth its tests:
an off-by-one in a search slice drops or duplicates a day's events without any
error being raised.
"""

from __future__ import annotations

import datetime as dt

from collector.constants import CHUNK_DAYS, CONTRIB_MAX_DAYS
from collector.errors import CollectError

UTC = dt.timezone.utc


def parse_timestamp(value: str) -> dt.datetime:
    """Parse an ISO-8601 timestamp, tolerating the trailing Z the API returns."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def utc_day(value: str) -> dt.date:
    """The UTC calendar day a timestamp falls on.

    Buckets are UTC rather than any local timezone so that the same input always
    produces the same output, wherever collection happens to run.
    """
    return parse_timestamp(value).astimezone(UTC).date()


def in_window(value: str | None, since: dt.date, until: dt.date) -> bool:
    """Whether a timestamp falls inside the window, inclusive of both ends."""
    if not value:
        return False
    return since <= utc_day(value) <= until


def window_bounds(since: dt.date, until: dt.date) -> tuple[str, str]:
    """The window as a pair of ISO-8601 instants, shared by every query.

    One pair of bounds serves repository enumeration and commit history alike,
    and that sharing is deliberate. When the two disagreed, a repository whose
    only activity fell in the gap was never enumerated, so all of its commits
    disappeared with nothing reporting a problem. Deriving both from here makes
    that impossible rather than merely unlikely.

    The end is capped at the present because the API cannot see the future. That
    discards nothing, unlike capping at a fixed span from the start.
    """
    start = dt.datetime.combine(since, dt.time.min, tzinfo=UTC)
    requested = dt.datetime.combine(until, dt.time.max, tzinfo=UTC).replace(microsecond=0)
    now = dt.datetime.now(UTC).replace(microsecond=0)
    end = min(requested, now)
    return _iso(start), _iso(end)


def require_window_fits(since: dt.date, until: dt.date) -> None:
    """Reject a window the contributions connection cannot answer in one query.

    Both ends of a window are inclusive, so a window covering a full year plus
    one day spans slightly more than a year and is refused. Moving the start
    forward by a day makes it fit, which is why the horizon counts dates rather
    than elapsed days.
    """
    dates = (until - since).days + 1
    if dates > CONTRIB_MAX_DAYS:
        suggested = until - dt.timedelta(days=CONTRIB_MAX_DAYS - 1)
        raise CollectError(
            f"the window {since}..{until} covers {dates} dates, and the "
            f"contributions API accepts at most {CONTRIB_MAX_DAYS}. Move the "
            f"start forward to {suggested}.")


def resolve_window(since: dt.date | None, until: dt.date | None,
                   horizon: int) -> tuple[dt.date, dt.date]:
    """Either the window given explicitly, or the trailing `horizon` dates.

    The horizon counts dates and not elapsed days, so a horizon of a full year
    fits inside a single contributions query.
    """
    if since and until:
        return since, until
    end = dt.datetime.now(UTC).date()
    return end - dt.timedelta(days=horizon - 1), end


def chunk_window(since: dt.date, until: dt.date,
                 days: int = CHUNK_DAYS) -> list[tuple[dt.date, dt.date]]:
    """Tile a window into consecutive slices of at most `days` each.

    Search date ranges are inclusive at both ends, so each slice begins the day
    after the previous one ended. A shared boundary day would count every event
    on it twice and a gap would drop them, and neither mistake raises anything.
    """
    if days < 1:
        raise ValueError(f"a slice must be at least one day, got {days}")
    if until < since:
        raise ValueError(f"the window ends before it starts: {since}..{until}")

    slices: list[tuple[dt.date, dt.date]] = []
    start = since
    while start <= until:
        end = min(start + dt.timedelta(days=days - 1), until)
        slices.append((start, end))
        start = end + dt.timedelta(days=1)
    return slices


def created_partitions(since: dt.date, until: dt.date, days: int = CHUNK_DAYS
                       ) -> list[tuple[dt.date | None, dt.date | None]]:
    """Disjoint creation-date slices covering every pull request that can matter.

    Searches for pull requests one has reviewed or commented on must be bounded
    by when they were last updated, and that bound has to stay open-ended, which
    means it cannot be used to slice. Creation date can: it never changes, so
    slices by creation date are disjoint by construction.

    The leading slice is open at the start, catching pull requests created before
    the window but touched inside it. No trailing slice is needed, because a pull
    request created after the window ends cannot carry an event inside it.
    """
    slices: list[tuple[dt.date | None, dt.date | None]] = [(None, since)]
    slices.extend(chunk_window(since, until, days))
    return slices


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
