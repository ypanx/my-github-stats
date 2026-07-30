"""Collection behaviour that can be checked without a network.

The merge-commit filter is the important case. The account being measured has no
merge commits at all, so a live run cannot distinguish a working filter from a
missing one and only the recorded fixture can.
"""

from __future__ import annotations

import json

import pytest

from collector.collector import is_merge_commit
from collector.errors import CollectError

from conftest import FIXTURES


def fixture_commits() -> list[dict]:
    return json.loads((FIXTURES / "merge_commits.json").read_text())["commits"]


class TestMergeFilter:
    def test_the_fixture_contains_both_kinds(self):
        commits = fixture_commits()
        assert any(c["expect_merge"] for c in commits)
        assert any(not c["expect_merge"] for c in commits)

    def test_every_recorded_commit_is_identified_correctly(self):
        for commit in fixture_commits():
            assert is_merge_commit(commit) is commit["expect_merge"], commit["oid"][:10]

    def test_merges_report_real_line_counts(self):
        """If a merge reported nothing, excluding it would be cosmetic. A merge's
        diff is the whole merged change, which is exactly the duplication being
        avoided."""
        merges = [c for c in fixture_commits() if c["expect_merge"]]
        assert merges
        for commit in merges:
            assert commit["additions"] + commit["deletions"] > 0

    def test_a_commit_without_parent_information_is_an_error(self):
        """Treating it as a non-merge would silently include it."""
        with pytest.raises(CollectError, match="parent"):
            is_merge_commit({"oid": "abc123"})
        with pytest.raises(CollectError):
            is_merge_commit({"oid": "abc123", "parents": {}})

    @pytest.mark.parametrize("count,expected", [(1, False), (2, True), (3, True)])
    def test_more_than_one_parent_makes_a_merge(self, count, expected):
        assert is_merge_commit({"oid": "x", "parents": {"totalCount": count}}) is expected
