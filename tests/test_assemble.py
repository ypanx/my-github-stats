"""Building the two payloads.

The publishable payload is produced by removing fields from the attributed one,
so the pair cannot disagree about anything they share. These tests hold that
property, and the ordering property that keeps repository structure out of the
published file.
"""

from __future__ import annotations

import json

from collector.assemble import (
    SORT_KEYS,
    build_commit_records,
    build_payload,
    sort_sections,
    strip_attribution,
)
from collector.constants import SECTIONS

FORBIDDEN_KEYS = {"repo", "path", "author", "login", "user", "oid", "sha",
                  "number", "url"}


def walk(node, visit):
    if isinstance(node, dict):
        for key, value in node.items():
            visit(key, value)
            walk(value, visit)
    elif isinstance(node, list):
        for item in node:
            walk(item, visit)


class TestCommitRecords:
    def test_paths_are_classified_and_then_discarded(self):
        commits = [{"oid": "a1", "committedDate": "2026-01-02T10:00:00Z",
                    "repo": "owner/private"}]
        files = {"a1": {"records": [
            {"path": "src/app.py", "additions": 10, "deletions": 2},
            {"path": "data/big.json", "additions": 500, "deletions": 0},
            {"path": "docs/readme.md", "additions": 4, "deletions": 1},
        ]}}
        records = build_commit_records(commits, files)
        assert records[0]["date"] == "2026-01-02"
        assert records[0]["languages"] == {
            "Markdown": {"additions": 4, "deletions": 1},
            "Python": {"additions": 10, "deletions": 2},
        }
        assert "src/app.py" not in json.dumps(records)

    def test_the_date_is_the_utc_day_not_the_reported_prefix(self):
        """Truncating the timestamp instead of converting it would put a commit on
        the wrong day whenever the two disagree."""
        commits = [{"oid": "a1", "committedDate": "2026-01-03T02:30:00+08:00",
                    "repo": "owner/private"}]
        files = {"a1": {"records": [{"path": "a.py", "additions": 1, "deletions": 0}]}}
        assert build_commit_records(commits, files)[0]["date"] == "2026-01-02"

    def test_a_commit_of_only_ignored_files_is_still_a_commit(self):
        """It happened, and dropping it would undercount the commit total."""
        commits = [{"oid": "a1", "committedDate": "2026-01-02T10:00:00Z",
                    "repo": "owner/private"}]
        files = {"a1": {"records": [{"path": "x.json", "additions": 9, "deletions": 0}]}}
        records = build_commit_records(commits, files)
        assert len(records) == 1
        assert records[0]["languages"] == {}


class TestStripping:
    def test_every_repository_name_is_removed(self, payload):
        published = strip_attribution(payload)
        assert "private" not in json.dumps(published)
        for section in SECTIONS:
            for record in published[section]:
                assert "repo" not in record

    def test_private_fields_are_removed(self, payload):
        """The mechanism is kept even with nothing currently using it, so a future
        derived field defaults to private rather than published by accident."""
        payload["prs"][0]["_scratch"] = "internal"
        published = strip_attribution(payload)
        assert all(not key.startswith("_") for pr in published["prs"] for key in pr)

    def test_everything_else_survives_unchanged(self, payload):
        published = strip_attribution(payload)
        assert len(published["commits"]) == len(payload["commits"])
        assert [c["languages"] for c in published["commits"]] == \
            [c["languages"] for c in payload["commits"]]
        assert published["window"] == payload["window"]

    def test_the_published_payload_carries_no_identifying_key(self, payload):
        published = strip_attribution(payload)
        seen: list[str] = []
        walk(published, lambda key, _: seen.append(key))
        assert FORBIDDEN_KEYS.isdisjoint(seen)

    def test_no_published_string_looks_like_a_path(self, payload):
        published = strip_attribution(payload)
        strings: list[str] = []
        walk(published, lambda _, value: strings.append(value)
             if isinstance(value, str) else None)
        assert all("/" not in value for value in strings)

    def test_the_time_to_merge_is_published_as_a_duration(self):
        """Dates alone cannot express it, because a large share of pull requests
        merge the day they are opened. A duration reveals how long something took
        without revealing when the work happened."""
        payload = {"window": {}, "commits": [], "review_envelopes": [],
                   "comments": [],
                   "prs": [{"created": "2026-01-02", "merged": "2026-01-03",
                            "state": "MERGED", "cycle_hours": 41.25,
                            "repo": "owner/private"}]}
        published = strip_attribution(payload)
        assert published["prs"][0]["cycle_hours"] == 41.25
        assert "T" not in json.dumps(published["prs"])


def grouped_payload() -> dict:
    """Commits from two repositories, arriving grouped and sharing dates.

    Records really do arrive grouped by repository, and several land on the same
    day. A fixture whose records all carry one repository, or all carry distinct
    dates, cannot tell a content-based sort from one that merely happens to leave
    arrival order intact.
    """
    return {
        "window": {"from": "2026-01-01", "to": "2026-01-31", "generated_at": "z"},
        "commits": [
            {"date": "2026-01-10", "repo": "owner/big",
             "languages": {"Python": {"additions": 3, "deletions": 0}}},
            {"date": "2026-01-10", "repo": "owner/big",
             "languages": {"Python": {"additions": 1, "deletions": 0}}},
            {"date": "2026-01-10", "repo": "owner/small",
             "languages": {"Go": {"additions": 2, "deletions": 0}}},
            {"date": "2026-01-10", "repo": "owner/small",
             "languages": {"Go": {"additions": 4, "deletions": 0}}},
        ],
        "prs": [], "review_envelopes": [], "comments": [],
    }


class TestOrdering:
    def test_records_end_up_in_date_order(self, payload):
        """Records arrive grouped by repository, and the points at which the date
        stopped descending marked the repository boundaries, letting a reader infer
        how the commits were distributed."""
        sorted_payload = sort_sections(payload)
        dates = [c["date"] for c in sorted_payload["commits"]]
        assert dates == sorted(dates)

    def test_order_does_not_depend_on_which_repository_a_record_came_from(self, payload):
        """Otherwise the ordering itself becomes metadata."""
        other = json.loads(json.dumps(payload))
        for record in other["commits"]:
            record["repo"] = "owner/somewhere-else"
        assert strip_attribution(sort_sections(payload)) == \
            strip_attribution(sort_sections(other))

    def test_records_sharing_a_date_are_ordered_by_their_content(self):
        """Sorting on the date alone would leave same-date records in arrival
        order, and arrival order is grouped by repository. What is asserted here is
        the weaker but achievable property: order is a function of the published
        content. Where content happens to correlate with repository it will still
        correlate, but no information beyond the records themselves is added."""
        published = strip_attribution(sort_sections(grouped_payload()))
        keys = [json.dumps(c["languages"], sort_keys=True) for c in published["commits"]]
        assert keys == sorted(keys)
        assert len(set(keys)) == len(keys), "the fixture must not rely on ties"

    def test_reordering_the_input_cannot_change_the_output(self):
        """The stronger form of the property: any arrival order, one result."""
        import itertools
        results = set()
        base = grouped_payload()["commits"]
        for permutation in itertools.permutations(range(len(base))):
            candidate = grouped_payload()
            candidate["commits"] = [base[index] for index in permutation]
            results.add(json.dumps(strip_attribution(sort_sections(candidate))))
        assert len(results) == 1, "the sort is not a total order over content"

    def test_order_does_not_depend_on_the_order_records_arrived_in(self, payload):
        forward = strip_attribution(sort_sections(json.loads(json.dumps(payload))))
        reversed_payload = json.loads(json.dumps(payload))
        for section in SECTIONS:
            reversed_payload[section].reverse()
        assert strip_attribution(sort_sections(reversed_payload)) == forward

    def test_every_published_section_is_sorted(self):
        """A section left out would keep whatever grouping collection produced."""
        assert set(SORT_KEYS) == set(SECTIONS)

    def test_sorting_neither_adds_nor_drops_records(self, payload):
        before = {section: len(payload[section]) for section in SECTIONS}
        sort_sections(payload)
        assert {section: len(payload[section]) for section in SECTIONS} == before


class TestPayload:
    def test_it_is_built_sorted_and_complete(self, payload):
        built = build_payload(
            window=payload["window"], commits=payload["commits"],
            pull_requests=payload["prs"], envelopes=payload["review_envelopes"],
            comments=payload["comments"])
        assert set(built) == {"window", *SECTIONS}
        dates = [c["date"] for c in built["commits"]]
        assert dates == sorted(dates)

    def test_the_reviews_section_is_not_named_reviews(self, payload):
        """Its length is the number of review events rather than the number of
        reviews given, and the awkward name exists to stop a reader reaching for
        the obvious and being wrong."""
        published = strip_attribution(payload)
        assert "review_envelopes" in published
        assert "reviews" not in published
