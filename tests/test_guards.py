"""The checks that must pass before anything is written.

Each guard exists because of a failure that produces a plausible wrong number
rather than an error, so each test here describes a way the run could have been
quietly wrong.
"""

from __future__ import annotations

import json

from collector import guards
from collector.aggregate import aggregate
from collector.assemble import strip_attribution


def reconciliation_case(records, additions, deletions, *, undiffable=0, complete=True):
    commits = [{"oid": "a1", "additions": additions, "deletions": deletions,
                "repo": "owner/private"}]
    files = {"a1": {"records": records, "complete": complete,
                    "reported_total": additions + deletions, "undiffable": undiffable}}
    return guards.check_line_reconciliation(commits, files)


class TestNonZero:
    def test_healthy_data_passes(self, payload):
        assert guards.check_non_zero(strip_attribution(payload), 512) == []

    def test_a_plausible_small_number_is_caught(self, payload):
        """When authorization lapses the API returns a successful response
        carrying only personal repositories, so the failure looks like a small
        number rather than zero and a single emptiness check would pass it."""
        assert guards.check_non_zero(strip_attribution(payload), 0)

    def test_an_empty_run_names_the_commits_specifically(self, payload):
        """The arm cannot fire on its own, since lines are derived from commits,
        but it names which quantity was empty and that is what makes the failure
        readable rather than merely present."""
        payload["commits"] = []
        problems = guards.check_non_zero(strip_attribution(payload), 512)
        assert any("no commits were collected" == problem for problem in problems)

    def test_no_lines_is_caught(self, payload):
        for commit in payload["commits"]:
            commit["languages"] = {}
        problems = guards.check_non_zero(strip_attribution(payload), 512)
        assert any("lines" in problem for problem in problems)


class TestUnknownLines:
    def test_an_acceptable_share_passes(self):
        records = [{"path": "a.py", "additions": 1000, "deletions": 0},
                   {"path": "a.cfg", "additions": 1, "deletions": 0}]
        problems, share = guards.check_unknown_lines(records)
        assert problems == []
        assert share.denominator == 1001

    def test_too_much_unclassifiable_content_fails(self):
        records = [{"path": "a.py", "additions": 50, "deletions": 0},
                   {"path": "a.cfg", "additions": 50, "deletions": 0}]
        problems, share = guards.check_unknown_lines(records)
        assert problems and share.share == 0.5

    def test_the_threshold_can_be_tightened_for_a_dry_run(self):
        records = [{"path": "a.py", "additions": 10_000, "deletions": 0},
                   {"path": "a.cfg", "additions": 1, "deletions": 0}]
        assert guards.check_unknown_lines(records)[0] == []
        assert guards.check_unknown_lines(records, 0.00001)[0]


class TestLineReconciliation:
    def test_matching_counts_pass(self):
        problems, stats = reconciliation_case(
            [{"path": "a.py", "additions": 10, "deletions": 5}], 10, 5)
        assert problems == []
        assert stats["shortfall"] == 0

    def test_counting_more_than_the_commit_reports_is_fatal(self):
        """That means a page was read twice, which is recoverable and therefore
        must not be tolerated."""
        problems, _ = reconciliation_case(
            [{"path": "a.py", "additions": 100, "deletions": 0}], 10, 0)
        assert problems and "twice" in problems[0]

    def test_a_small_shortfall_is_tolerated(self):
        """GitHub occasionally declines to diff a file while still counting its
        lines at the commit level, which cannot be recovered here."""
        problems, stats = reconciliation_case(
            [{"path": "a.py", "additions": 10_000, "deletions": 0}], 10_010, 0,
            undiffable=1)
        assert problems == []
        assert stats["shortfall"] == 10
        assert stats["undiffable"] == 1

    def test_a_large_shortfall_fails(self):
        problems, _ = reconciliation_case(
            [{"path": "a.py", "additions": 100, "deletions": 0}], 1_000, 0)
        assert problems and "short" in problems[0]

    def test_a_commit_that_could_not_be_read_is_left_out(self):
        """Including it would consume the whole tolerance with a gap that is
        already reported separately."""
        commits = [{"oid": "big", "additions": 90_000, "deletions": 0, "repo": "owner/p"},
                   {"oid": "ok", "additions": 10, "deletions": 5, "repo": "owner/p"}]
        files = {"big": {"records": [{"path": "a.py", "additions": 1, "deletions": 0}],
                         "complete": False, "undiffable": 0},
                 "ok": {"records": [{"path": "b.py", "additions": 10, "deletions": 5}],
                        "complete": True, "undiffable": 0}}
        problems, stats = guards.check_line_reconciliation(commits, files)
        assert problems == []
        assert stats["excluded"] == 1
        assert stats["record_lines"] == 15


class TestRepositoryReconciliation:
    def test_a_repository_with_no_authored_commits_is_expected(self):
        """A co-authored commit credits every author while history returns only
        the primary one, so contributions without commits is normal."""
        repositories = [{"name_with_owner": "owner/bot-output", "contributions": 12}]
        assert guards.check_repository_reconciliation(repositories, []) == []

    def test_losing_most_of_a_repository_is_caught(self):
        """This is what would catch a broken author filter or a truncated list,
        both of which lose commits in bulk."""
        repositories = [{"name_with_owner": "owner/main", "contributions": 450}]
        commits = [{"repo": "owner/main"} for _ in range(10)]
        problems = guards.check_repository_reconciliation(repositories, commits)
        assert problems

    def test_no_message_names_a_repository(self):
        """These messages reach public build logs."""
        repositories = [{"name_with_owner": "owner/main", "contributions": 450}]
        commits = [{"repo": "owner/main"} for _ in range(10)]
        problems = guards.check_repository_reconciliation(repositories, commits)
        assert all("owner/main" not in problem for problem in problems)

    def test_ordinary_drift_is_tolerated(self):
        repositories = [{"name_with_owner": "owner/main", "contributions": 452}]
        commits = [{"repo": "owner/main"} for _ in range(447)]
        assert guards.check_repository_reconciliation(repositories, commits) == []


class TestDayOverDay:
    def test_a_collapse_is_caught(self, tmp_path, bulk_payload):
        """When authorization lapses the commit count falls by more than ninety
        percent, which is what this threshold is calibrated for."""
        previous = tmp_path / "stats.json"
        previous.write_text(json.dumps(bulk_payload(commits=467)))
        problems, _ = guards.check_day_over_day(bulk_payload(commits=6), previous)
        assert any("commits" in problem for problem in problems)

    def test_ordinary_rolling_movement_passes(self, tmp_path, bulk_payload):
        """A rolling window turns over a small fraction of itself each day."""
        previous = tmp_path / "stats.json"
        previous.write_text(json.dumps(bulk_payload(commits=467)))
        problems, _ = guards.check_day_over_day(bulk_payload(commits=461), previous)
        assert problems == []

    def test_metrics_too_small_to_compare_are_skipped(self, tmp_path, bulk_payload):
        """A proportion of three says nothing, so a fall from three to one must
        not block a run."""
        previous = tmp_path / "stats.json"
        before = bulk_payload()
        before["review_envelopes"] = [
            {"date": "2026-01-05", "state": "DISMISSED", "inline_count": 0,
             "has_body": False, "on_own_pr": False}] * 3
        previous.write_text(json.dumps(before))
        after = json.loads(json.dumps(before))
        after["review_envelopes"] = after["review_envelopes"][2:]
        problems, _ = guards.check_day_over_day(after, previous)
        assert problems == []

    def test_large_per_state_counts_are_still_watched(self, tmp_path, bulk_payload):
        """Approvals are numerous enough for a proportion to mean something, and
        they are what gets reported."""
        previous = tmp_path / "stats.json"
        previous.write_text(json.dumps(bulk_payload(approvals=618)))
        after = bulk_payload(approvals=618)
        for record in after["review_envelopes"]:
            record["state"] = "COMMENTED"
        problems, _ = guards.check_day_over_day(after, previous)
        assert any("approved" in problem for problem in problems)

    def test_a_first_run_is_skipped_rather_than_failed(self, tmp_path, bulk_payload):
        problems, skipped = guards.check_day_over_day(bulk_payload(),
                                                      tmp_path / "absent.json")
        assert problems == []
        assert skipped == "no previous file"

    def test_a_schema_change_is_skipped_rather_than_failed(self, tmp_path, bulk_payload):
        """Failing would let a rename block publication indefinitely, since every
        later run would meet the same stale file."""
        previous = tmp_path / "stats.json"
        stale = bulk_payload()
        stale["reviews"] = stale.pop("review_envelopes")
        previous.write_text(json.dumps(stale))
        problems, skipped = guards.check_day_over_day(bulk_payload(), previous)
        assert problems == []
        assert skipped and "schema" in skipped

    def test_an_unreadable_previous_file_is_skipped(self, tmp_path, bulk_payload):
        previous = tmp_path / "stats.json"
        previous.write_text("{ not json")
        problems, skipped = guards.check_day_over_day(bulk_payload(), previous)
        assert problems == []
        assert skipped and "JSON" in skipped

    def test_a_different_window_length_is_skipped(self, tmp_path, bulk_payload):
        """A shorter window is a legitimate thing to ask for, and comparing it
        against a year would fail on nearly every metric."""
        previous = tmp_path / "stats.json"
        previous.write_text(json.dumps(bulk_payload()))
        month = bulk_payload(commits=8)
        month["window"] = {"from": "2026-06-30", "to": "2026-07-29",
                           "generated_at": "x"}
        problems, skipped = guards.check_day_over_day(month, previous)
        assert problems == []
        assert skipped and "window length" in skipped

    def test_a_real_comparison_reports_no_skip_reason(self, tmp_path, bulk_payload):
        previous = tmp_path / "stats.json"
        previous.write_text(json.dumps(bulk_payload(commits=467)))
        problems, skipped = guards.check_day_over_day(bulk_payload(commits=461), previous)
        assert problems == []
        assert skipped is None


class TestDescription:
    def test_the_classification_summary_covers_every_bucket(self):
        line = guards.describe_totals(aggregate(
            [{"path": "a.py", "additions": 1, "deletions": 0}]))
        for status in ("counted", "ignored", "unknown", "binary"):
            assert status in line
