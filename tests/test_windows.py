"""Date and window arithmetic.

These are the cheapest tests in the suite and cover the mistakes that are hardest
to notice: a search slice that drops or duplicates a day raises nothing at all.
"""

from __future__ import annotations

import datetime as dt

import pytest

from collector import windows
from collector.constants import CONTRIB_MAX_DAYS
from collector.errors import CollectError

D = dt.date


class TestChunkWindow:
    def test_slices_tile_the_window_without_gap_or_overlap(self):
        since, until = D(2025, 7, 30), D(2026, 7, 29)
        slices = windows.chunk_window(since, until)
        assert slices[0][0] == since
        assert slices[-1][1] == until
        for (_, end), (next_start, _) in zip(slices, slices[1:]):
            assert next_start == end + dt.timedelta(days=1)

    def test_every_date_appears_exactly_once(self):
        since, until = D(2025, 7, 30), D(2026, 7, 29)
        seen: list[D] = []
        for start, end in windows.chunk_window(since, until):
            day = start
            while day <= end:
                seen.append(day)
                day += dt.timedelta(days=1)
        assert len(seen) == len(set(seen))
        assert len(seen) == (until - since).days + 1

    def test_a_single_date_is_one_slice(self):
        day = D(2026, 1, 1)
        assert windows.chunk_window(day, day) == [(day, day)]

    def test_a_short_window_is_not_extended_past_its_end(self):
        assert windows.chunk_window(D(2026, 1, 1), D(2026, 1, 10)) == [
            (D(2026, 1, 1), D(2026, 1, 10))]

    @pytest.mark.parametrize("since,until,days", [
        (D(2026, 1, 2), D(2026, 1, 1), 91),
        (D(2026, 1, 1), D(2026, 1, 2), 0),
    ])
    def test_impossible_arguments_raise(self, since, until, days):
        with pytest.raises(ValueError):
            windows.chunk_window(since, until, days)


class TestCreatedPartitions:
    def test_the_first_slice_is_open_at_the_start(self):
        """Pull requests created before the window but touched inside it must
        still be found, and slicing only within the window would drop them."""
        parts = windows.created_partitions(D(2025, 7, 30), D(2026, 7, 29))
        assert parts[0] == (None, D(2025, 7, 30))

    def test_the_bounded_slices_tile_the_window(self):
        since, until = D(2025, 7, 30), D(2026, 7, 29)
        bounded = [p for p in windows.created_partitions(since, until) if p[0]]
        assert bounded[0][0] == since
        assert bounded[-1][1] == until
        for (_, end), (next_start, _) in zip(bounded, bounded[1:]):
            assert next_start == end + dt.timedelta(days=1)


class TestWindowBounds:
    def test_the_end_covers_the_whole_final_date(self):
        start, end = windows.window_bounds(D(2025, 7, 30), D(2026, 1, 31))
        assert start == "2025-07-30T00:00:00Z"
        assert end == "2026-01-31T23:59:59Z"

    def test_the_future_is_never_requested(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        _, end = windows.window_bounds(today - dt.timedelta(days=10), today)
        assert windows.parse_timestamp(end) <= dt.datetime.now(dt.timezone.utc)

    def test_the_same_window_always_produces_the_same_bounds(self):
        """Enumeration and history share these bounds. When they diverged, a
        repository active only in the gap was never enumerated at all."""
        since, until = D(2025, 7, 30), D(2026, 1, 31)
        assert windows.window_bounds(since, until) == windows.window_bounds(since, until)


class TestWindowFits:
    def test_a_window_longer_than_a_year_is_refused(self):
        with pytest.raises(CollectError, match="366 dates"):
            windows.require_window_fits(D(2025, 7, 29), D(2026, 7, 29))

    def test_the_error_names_the_date_that_would_work(self):
        with pytest.raises(CollectError, match="2025-07-30"):
            windows.require_window_fits(D(2025, 7, 29), D(2026, 7, 29))

    def test_a_window_of_exactly_a_year_of_dates_is_accepted(self):
        windows.require_window_fits(D(2025, 7, 30), D(2026, 7, 29))


class TestResolveWindow:
    def test_an_explicit_window_is_used_unchanged(self):
        since, until = D(2025, 7, 30), D(2026, 7, 29)
        assert windows.resolve_window(since, until, 365) == (since, until)

    def test_the_horizon_counts_dates_and_always_fits(self):
        since, until = windows.resolve_window(None, None, CONTRIB_MAX_DAYS)
        assert until == dt.datetime.now(dt.timezone.utc).date()
        assert (until - since).days + 1 == CONTRIB_MAX_DAYS
        windows.require_window_fits(since, until)


class TestTimestamps:
    def test_days_are_bucketed_in_utc(self):
        assert windows.utc_day("2026-01-01T23:30:00Z") == D(2026, 1, 1)
        assert windows.utc_day("2026-01-02T02:30:00+08:00") == D(2026, 1, 1)

    def test_the_window_includes_both_of_its_ends(self):
        since, until = D(2026, 1, 1), D(2026, 1, 31)
        assert windows.in_window("2026-01-01T00:00:00Z", since, until)
        assert windows.in_window("2026-01-31T23:59:59Z", since, until)
        assert not windows.in_window("2025-12-31T23:59:59Z", since, until)
        assert not windows.in_window("2026-02-01T00:00:00Z", since, until)

    def test_a_missing_timestamp_is_outside_every_window(self):
        assert not windows.in_window(None, D(2026, 1, 1), D(2026, 1, 31))
