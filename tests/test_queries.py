"""Search strings and the slicing they depend on.

The properties asserted here are the ones whose absence is invisible: a query
scoped to a repository still returns plausible results, and a bounded update
range still returns most of what it should.
"""

from __future__ import annotations

import re

import datetime as dt

import pytest

import queries
from utils import created_partitions

D = dt.date
SINCE, UNTIL = D(2025, 7, 30), D(2026, 7, 29)


def every_query(login: str = "someone") -> list[str]:
    built = [queries.authored_query(login, SINCE, UNTIL)]
    for kind in ("reviewed", "commented"):
        for lower, upper in created_partitions(SINCE, UNTIL):
            built.append(queries.touched_query(kind, login, SINCE, lower, upper))
    return built


class TestAccountWide:
    def test_no_query_is_scoped_to_a_repository(self):
        """Every metric covers the whole account. A repository filter would
        silently narrow the entire dataset and nothing would look wrong."""
        for query in every_query():
            assert "repo:" not in query, query

    def test_no_query_document_hardcodes_an_account_or_repository(self):
        """The account is resolved at runtime, so no committed file names it.

        Asserted as "every qualifier takes a variable" rather than "no qualifier is
        hardcoded". The weaker form passed for a document with no qualifier at all,
        which is most of them, so it would not have caught `author:someone`.
        """
        documents = [queries.VIEWER, queries.CONTRIBUTIONS, queries.COMMIT_HISTORY,
                     queries.AUTHORED_PULL_REQUESTS, queries.REVIEWED_PULL_REQUESTS,
                     queries.COMMENTED_PULL_REQUESTS, queries.COMMENT_PAGE]
        # Excludes `$owner: String!`, which is a variable declaration rather than a
        # value being supplied to a field.
        qualifier = re.compile(
            r"(?<!\$)\b(owner|name|author|repo|login|commenter|reviewed-by)"
            r"\s*:\s*(\S+)")
        for document in documents:
            for field, value in qualifier.findall(document):
                assert value.startswith(("$", "{")), f"{field}: {value}"


class TestUpdateBound:
    def test_the_update_bound_is_always_open_ended(self):
        """A bounded update range is only safe when it ends now. Otherwise a pull
        request whose in-window activity was followed by later changes falls
        outside the upper bound and disappears entirely."""
        for kind in ("reviewed", "commented"):
            for lower, upper in created_partitions(SINCE, UNTIL):
                query = queries.touched_query(kind, "someone", SINCE, lower, upper)
                assert f"updated:>={SINCE}" in query
                assert ".." not in query.split("updated:")[1].split()[0]

    def test_creation_dates_are_bounded_on_both_sides(self):
        """Creation never changes, so a two-sided range is safe and slices
        disjointly."""
        query = queries.authored_query("someone", SINCE, UNTIL)
        assert f"created:{SINCE}..{UNTIL}" in query
        assert "updated:" not in query


class TestSlicing:
    def test_the_leading_slice_excludes_the_first_date(self):
        """The leading slice and the first tiled slice must not both claim it."""
        parts = created_partitions(SINCE, UNTIL)
        leading = queries.touched_query("reviewed", "me", SINCE, *parts[0])
        first = queries.touched_query("reviewed", "me", SINCE, *parts[1])
        assert f"created:<{SINCE}" in leading
        assert f"created:{SINCE}.." in first

    def test_an_open_ended_lower_bound_is_expressible(self):
        query = queries.touched_query("reviewed", "me", SINCE, SINCE, None)
        assert f"created:>={SINCE}" in query

    def test_an_unbounded_slice_carries_no_creation_filter(self):
        query = queries.touched_query("reviewed", "me", SINCE, None, None)
        assert "created:" not in query


class TestFields:
    def test_each_kind_searches_the_right_field(self):
        assert "reviewed-by:me" in queries.touched_query("reviewed", "me", SINCE, None, None)
        assert "commenter:me" in queries.touched_query("commented", "me", SINCE, None, None)
        assert "author:me" in queries.authored_query("me", SINCE, UNTIL)

    def test_an_unrecognized_kind_is_an_error(self):
        with pytest.raises(KeyError):
            queries.touched_query("approved", "me", SINCE, None, None)

    def test_reviews_request_a_count_of_inline_comments_only(self):
        """Selecting the comments themselves adds a level of nesting, and GraphQL
        charges for what a query could return, so the cost multiplies."""
        assert "comments { totalCount }" in queries.REVIEWED_PULL_REQUESTS

    def test_commit_history_requests_parent_counts(self):
        """Without these, merge commits cannot be told apart and their lines are
        counted twice."""
        assert "parents { totalCount }" in queries.COMMIT_HISTORY

    def test_the_repository_limit_is_always_passed_explicitly(self):
        """Its default is far lower than the maximum."""
        assert "maxRepositories: $maxRepos" in queries.CONTRIBUTIONS
