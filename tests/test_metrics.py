"""Figures derived from a payload.

Nothing is precomputed into the output files, so every one of these is computed
on demand and each is worth pinning.
"""

from __future__ import annotations

import pytest

from collector.metrics import (
    active_dates,
    cycle_hours,
    headline_metrics,
    language_totals,
    nearest_rank,
    reviews_given,
    total_lines,
)


class TestPercentile:
    def test_it_does_not_interpolate(self):
        """Interpolating between the two middle values of an even-length list
        gives a different answer in the first decimal place, which is the
        precision being reported."""
        assert nearest_rank([1, 2, 3, 4], 0.5) == 2
        assert nearest_rank([1, 2, 3, 4], 0.9) == 4

    def test_a_single_value_is_its_own_percentile(self):
        assert nearest_rank([5], 0.5) == 5

    @pytest.mark.parametrize("values,proportion", [([], 0.5), ([1], 0), ([1], 1.5)])
    def test_impossible_arguments_raise(self, values, proportion):
        with pytest.raises(ValueError):
            nearest_rank(values, proportion)


class TestLanguages:
    def test_totals_are_summed_per_language(self, payload):
        assert language_totals(payload["commits"]) == {
            "Go": {"additions": 7, "deletions": 3},
            "Python": {"additions": 10, "deletions": 5},
        }

    def test_lines_are_the_churn_across_every_language(self, payload):
        assert total_lines(payload["commits"]) == 25

    def test_the_language_table_and_the_line_total_agree(self, payload):
        totals = language_totals(payload["commits"])
        assert sum(v["additions"] + v["deletions"] for v in totals.values()) == \
            total_lines(payload["commits"])

    def test_a_language_touched_by_two_commits_is_summed(self):
        commits = [{"date": "2026-01-01",
                    "languages": {"Python": {"additions": 1, "deletions": 2}}},
                   {"date": "2026-01-02",
                    "languages": {"Python": {"additions": 10, "deletions": 20}}}]
        assert language_totals(commits) == {"Python": {"additions": 11, "deletions": 22}}


class TestReviews:
    def test_reviews_on_ones_own_pull_requests_are_not_reviews_given(self, payload):
        """Counting them would mean being reviewed more made one look more active."""
        given = reviews_given(payload["review_envelopes"])
        assert len(given) == 1
        assert all(not r["on_own_pr"] for r in given)

    def test_their_inline_comments_are_still_counted(self, payload):
        """They are reclassified rather than discarded, because the comments are
        genuine work."""
        inline = sum(1 for c in payload["comments"] if c["kind"] == "inline")
        assert inline == 1


class TestCycleTime:
    def test_only_merged_pull_requests_contribute(self, payload):
        assert cycle_hours(payload["prs"]) == [24.0]

    def test_it_is_derivable_from_the_payload_alone(self, payload):
        assert nearest_rank(cycle_hours(payload["prs"]), 0.5) == 24.0


class TestActiveDates:
    def test_it_is_the_union_of_every_kind_of_record(self, payload):
        """A date on which the only activity was opening a pull request is as
        active as one carrying a commit, so no single record type can define the
        set on its own."""
        assert active_dates(payload) == {"2026-01-02", "2026-01-03", "2026-01-04",
                                         "2026-01-05", "2026-01-06"}

    def test_a_date_reached_by_more_than_one_record_type_is_counted_once(self, payload):
        """Otherwise the figure would drift towards a count of records, which is
        already reported and means something different."""
        assert len(payload["commits"]) + len(payload["prs"]) \
            + len(payload["review_envelopes"]) + len(payload["comments"]) == 8
        assert len(active_dates(payload)) == 5

    def test_a_date_carrying_several_records_of_one_type_is_counted_once(self, payload):
        """The two comments share a date, and that date is also a review date."""
        assert [c["date"] for c in payload["comments"]] == ["2026-01-05", "2026-01-05"]
        assert "2026-01-05" in active_dates(payload)
        assert len(active_dates(payload)) == 5

    def test_it_counts_our_own_records_and_nothing_else(self, payload):
        """A per-date count from elsewhere covers a different set of activities
        and buckets them in another timezone, so honouring one would mean
        contradicting the records published beside it."""
        payload["contribution_calendar"] = [{"date": "2026-02-14", "count": 9},
                                            {"date": "2026-01-02", "count": 0}]
        assert active_dates(payload) == {"2026-01-02", "2026-01-03", "2026-01-04",
                                         "2026-01-05", "2026-01-06"}

    def test_a_payload_with_no_records_has_no_active_dates(self, payload):
        for section in ("commits", "prs", "review_envelopes", "comments"):
            payload[section] = []
        assert active_dates(payload) == set()


class TestHeadlineMetrics:
    def test_it_counts_what_it_claims_to(self, payload):
        metrics = headline_metrics(payload)
        assert metrics["commits"] == 2
        assert metrics["lines"] == 25
        assert metrics["prs_opened"] == 2
        assert metrics["prs_merged"] == 1
        assert metrics["review_envelopes"] == 2
        assert metrics["reviews_given"] == 1
        assert metrics["comments_inline"] == 1
        assert metrics["comments_conversational"] == 1
        assert metrics["active_days"] == 5

    def test_review_states_are_counted_individually(self, payload):
        """The four states collapse into one number, so a fault that relabelled
        every approval would move no total, yet approvals are what gets
        reported."""
        metrics = headline_metrics(payload)
        assert metrics["reviews_approved"] == 1
        assert metrics["reviews_commented"] == 0

    def test_the_states_partition_the_reviews_given(self, payload):
        metrics = headline_metrics(payload)
        by_state = sum(value for key, value in metrics.items()
                       if key.startswith("reviews_")
                       and key not in ("reviews_given", "reviews_substantive"))
        assert by_state == metrics["reviews_given"]

    def test_there_is_no_count_of_distinct_languages(self, payload):
        """Cardinality is the wrong measure for a set that small: two rarely
        touched languages ageing out reads as a collapse while representing a
        rounding error in the line totals."""
        assert "languages" not in headline_metrics(payload)

    def test_every_value_is_an_integer(self, payload):
        assert all(isinstance(value, int) for value in headline_metrics(payload).values())
