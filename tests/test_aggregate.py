"""Accumulating classified records into language totals.

The conservation checks here compare the buckets against totals taken straight
from the input. Comparing them against a total derived from the buckets always
agrees, however wrong the buckets are, which is a mistake this suite has already
had to catch once.
"""

from __future__ import annotations

import json

from collector.aggregate import (
    DENOMINATOR_BASIS,
    aggregate,
    check_unknown_share,
    identify_version,
    tag_disposition,
)
from collector.classify import STATUSES

RECORDS = [
    {"path": "a.py", "additions": 10, "deletions": 4},
    {"path": "b.py", "additions": 1, "deletions": 0},
    {"path": "c.go", "additions": 7, "deletions": 3},
    {"path": "README.md", "additions": 5, "deletions": 5},
    {"path": "data.json", "additions": 100, "deletions": 100},
    {"path": "logo.svg", "additions": 50, "deletions": 0},
    {"path": "poetry.lock", "additions": 30, "deletions": 0},
    {"path": "settings.cfg", "additions": 1, "deletions": 1},
    {"path": "CODEOWNERS", "additions": 2, "deletions": 0},
    {"path": "logo.png", "additions": 0, "deletions": 0},
    {"path": "empty.py", "additions": 0, "deletions": 0},
]


class TestPartition:
    def test_every_record_lands_in_exactly_one_bucket(self):
        totals = aggregate(RECORDS)
        assert set(totals.records) == set(STATUSES)
        assert totals.total_records == len(RECORDS) == totals.input_records

    def test_no_lines_are_lost_or_double_counted(self):
        totals = aggregate(RECORDS)
        expected = sum(r["additions"] + r["deletions"] for r in RECORDS)
        assert totals.total_lines == expected == totals.input_lines

    def test_binary_files_contribute_nothing(self):
        totals = aggregate(RECORDS)
        assert totals.records["binary"] == 1
        assert totals.lines["binary"] == 0

    def test_a_record_with_no_changes_still_counts_as_a_record(self):
        totals = aggregate(RECORDS)
        assert totals.records["counted"] == 5
        assert totals.churn("Python") == 15


class TestLanguages:
    def test_additions_and_deletions_are_kept_apart(self):
        """Collapsing them during collection would make the choice permanent, and
        whether a card shows churn or net change belongs to rendering."""
        totals = aggregate(RECORDS)
        assert totals.languages["Python"] == {"additions": 11, "deletions": 4}
        assert totals.languages["Go"] == {"additions": 7, "deletions": 3}
        assert totals.languages["Markdown"] == {"additions": 5, "deletions": 5}

    def test_the_language_table_sums_to_the_counted_lines(self):
        totals = aggregate(RECORDS)
        assert sum(totals.churn(n) for n in totals.languages) == totals.lines["counted"]

    def test_additions_and_deletions_match_the_counted_bucket(self):
        """Checked separately rather than as a sum, because summing them first
        would let the two being swapped inside a language cancel out."""
        totals = aggregate(RECORDS)
        assert sum(v["additions"] for v in totals.languages.values()) == \
            totals.additions["counted"]
        assert sum(v["deletions"] for v in totals.languages.values()) == \
            totals.deletions["counted"]

    def test_ignored_and_unclassified_types_never_appear(self):
        totals = aggregate(RECORDS)
        assert not {"Image", "Gitattributes", "Json", "Xml", "Svg"} & set(totals.languages)


class TestTagTable:
    def test_ignored_tags_are_reported_too(self):
        """This table is what reveals a data type quietly counting as a language,
        which no total can show because the totals reconcile either way."""
        totals = aggregate(RECORDS)
        assert totals.tag_lines["json"] == 200
        assert totals.tag_lines["xml"] == 50
        assert totals.tag_lines["python"] == 15

    def test_outcomes_describe_what_happened_not_what_a_tag_means_alone(self):
        """An image tag looks like a language in isolation. What matters is that
        files carrying it were ignored because of another tag entirely."""
        totals = aggregate(RECORDS)
        assert totals.tag_outcomes["svg"] == {"ignored": 1}
        assert totals.tag_outcomes["image"] == {"binary": 1, "ignored": 1}
        assert totals.tag_outcomes["python"] == {"counted as Python": 3}

    def test_a_tag_resolving_two_ways_shows_both(self):
        totals = aggregate(RECORDS + [{"path": "notes.txt", "additions": 3,
                                       "deletions": 0}])
        assert totals.tag_outcomes["plain-text"] == {
            "counted as Markdown": 1, "counted as Text": 1}

    def test_dispositions_are_described(self):
        assert tag_disposition("json") == "ignored by policy"
        assert tag_disposition("text") == "generic"
        assert tag_disposition("plain-text") == "demoted for naming"
        assert tag_disposition("python") == ""


class TestResiduals:
    def test_residuals_are_grouped_by_type(self):
        totals = aggregate(RECORDS)
        assert totals.unknown_types == {
            "CODEOWNERS": {"records": 1, "lines": 2},
            "cfg": {"records": 1, "lines": 2},
        }

    def test_example_paths_are_collected_for_triage(self):
        totals = aggregate(RECORDS)
        assert totals.unknown_examples["cfg"] == ["settings.cfg"]

    def test_the_number_of_examples_is_bounded(self):
        records = [{"path": f"f{i}.cfg", "additions": 1, "deletions": 0}
                   for i in range(10)]
        totals = aggregate(records, examples=2)
        assert len(totals.unknown_examples["cfg"]) == 2


class TestUnknownShare:
    def test_the_denominator_is_named_alongside_the_number(self):
        """The same count yields different proportions on different bases, so the
        basis travels with it."""
        totals = aggregate(RECORDS)
        share = check_unknown_share(totals)
        # Compared against the input tally rather than against the buckets the
        # share was computed from, so the assertion can actually fail.
        assert share.denominator == totals.input_lines
        assert share.unknown_lines == 4
        assert share.basis == DENOMINATOR_BASIS

    def test_an_acceptable_share_passes(self):
        records = [{"path": "a.py", "additions": 10_000, "deletions": 0},
                   {"path": "a.cfg", "additions": 1, "deletions": 0}]
        assert check_unknown_share(aggregate(records)).ok

    def test_too_much_unclassifiable_content_fails(self):
        records = [{"path": "a.py", "additions": 50, "deletions": 0},
                   {"path": "a.cfg", "additions": 50, "deletions": 0}]
        share = check_unknown_share(aggregate(records))
        assert share.share == 0.5
        assert not share.ok

    def test_an_empty_input_does_not_divide_by_zero(self):
        assert check_unknown_share(aggregate([])).share == 0.0


class TestDeterminism:
    def test_the_same_input_produces_identical_output(self):
        first = aggregate(RECORDS)
        second = aggregate(list(reversed(RECORDS)))
        assert json.dumps(first.languages) == json.dumps(second.languages)
        assert list(first.tag_lines) == list(second.tag_lines)

    def test_the_tagging_library_version_is_available(self):
        """Its tag tables change between releases, which moves the breakdown."""
        assert identify_version().count(".") >= 1
