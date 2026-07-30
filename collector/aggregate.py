"""Accumulating classified file records into language totals.

Additions and deletions are kept apart throughout. Collapsing them into a single
churn figure during collection would make that choice permanent, and whether a
card shows churn or net change belongs to rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from typing import Any, Iterable, Mapping

from identify import identify

from collector.classify import GENERIC_TAGS, STATUSES, classify, unknown_type
from collector.policy import policy

#: What the unclassifiable share is measured against, named alongside the number
#: because several plausible denominators exist and they differ enough to matter.
DENOMINATOR_BASIS = "sum of per-file records (additions + deletions)"


@dataclass(frozen=True)
class Aggregate:
    """Everything one pass over a set of file records produces."""

    #: Display name to its additions and deletions, kept separate.
    languages: dict[str, dict[str, int]]

    #: Status to the number of records that landed in it.
    records: dict[str, int]

    #: Status to lines, and the same split into additions and deletions, so that
    #: swapping the two inside a language can be detected.
    lines: dict[str, int]
    additions: dict[str, int]
    deletions: dict[str, int]

    #: Every tag seen, with how many records and lines carried it, and what
    #: actually became of those records. The last of these matters: a tag looks
    #: like one thing in isolation and can resolve to something else entirely
    #: once the whole tag set is considered.
    tag_records: dict[str, int]
    tag_lines: dict[str, int]
    tag_outcomes: dict[str, dict[str, int]]

    #: Unclassifiable records grouped by type, with example paths held back for
    #: verbose output only.
    unknown_types: dict[str, dict[str, int]]
    unknown_examples: dict[str, list[str]]

    #: Totals taken straight from the input, before any bucketing. The
    #: conservation checks compare the buckets against these; comparing the
    #: buckets against a total derived from the buckets would always agree.
    input_records: int
    input_lines: int

    @property
    def total_records(self) -> int:
        return sum(self.records.values())

    @property
    def total_lines(self) -> int:
        return sum(self.lines.values())

    def churn(self, language: str) -> int:
        counts = self.languages[language]
        return counts["additions"] + counts["deletions"]


@dataclass(frozen=True)
class UnknownShare:
    """The unclassifiable share, its denominator, and whether it is acceptable."""

    unknown_lines: int
    denominator: int
    basis: str
    share: float
    threshold: float
    ok: bool


def aggregate(records: Iterable[Mapping[str, Any]], *, examples: int = 3) -> Aggregate:
    """Classify records and accumulate them.

    Every returned mapping is rebuilt in sorted order, so two passes over the
    same input produce identical output.
    """
    languages: dict[str, dict[str, int]] = {}
    record_counts = dict.fromkeys(STATUSES, 0)
    line_counts = dict.fromkeys(STATUSES, 0)
    addition_counts = dict.fromkeys(STATUSES, 0)
    deletion_counts = dict.fromkeys(STATUSES, 0)
    tag_records: dict[str, int] = {}
    tag_lines: dict[str, int] = {}
    tag_outcomes: dict[str, dict[str, int]] = {}
    unknown: dict[str, dict[str, int]] = {}
    unknown_paths: dict[str, list[str]] = {}
    input_records = 0
    input_lines = 0

    for record in records:
        path = str(record["path"])
        additions = int(record["additions"])
        deletions = int(record["deletions"])
        churn = additions + deletions
        input_records += 1
        input_lines += churn

        name, status = classify(path)
        outcome = f"counted as {name}" if status == "counted" else status
        for tag in identify.tags_from_filename(path):
            tag_records[tag] = tag_records.get(tag, 0) + 1
            tag_lines[tag] = tag_lines.get(tag, 0) + churn
            seen = tag_outcomes.setdefault(tag, {})
            seen[outcome] = seen.get(outcome, 0) + 1

        record_counts[status] += 1
        line_counts[status] += churn
        addition_counts[status] += additions
        deletion_counts[status] += deletions

        if status == "counted":
            assert name is not None
            bucket = languages.setdefault(name, {"additions": 0, "deletions": 0})
            bucket["additions"] += additions
            bucket["deletions"] += deletions
        elif status == "unknown":
            key = unknown_type(path)
            slot = unknown.setdefault(key, {"records": 0, "lines": 0})
            slot["records"] += 1
            slot["lines"] += churn
            paths = unknown_paths.setdefault(key, [])
            if len(paths) < examples:
                paths.append(path)

    return Aggregate(
        languages={name: languages[name] for name in sorted(languages)},
        records=record_counts,
        lines=line_counts,
        additions=addition_counts,
        deletions=deletion_counts,
        tag_records={tag: tag_records[tag] for tag in sorted(tag_records)},
        tag_lines={tag: tag_lines[tag] for tag in sorted(tag_lines)},
        tag_outcomes={
            tag: dict(sorted(tag_outcomes[tag].items(), key=lambda kv: (-kv[1], kv[0])))
            for tag in sorted(tag_outcomes)
        },
        unknown_types={key: unknown[key] for key in sorted(unknown)},
        unknown_examples={key: sorted(unknown_paths[key]) for key in sorted(unknown_paths)},
        input_records=input_records,
        input_lines=input_lines,
    )


def check_unknown_share(totals: Aggregate) -> UnknownShare:
    """Measure the unclassifiable share against the per-file record total.

    The denominator is deliberately the sum of what the classifier could actually
    see. Using the totals commits report for themselves would quietly loosen the
    threshold, because those are slightly larger.
    """
    denominator = totals.total_lines
    unknown_lines = totals.lines["unknown"]
    share = unknown_lines / denominator if denominator else 0.0
    threshold = policy().unknown_threshold
    return UnknownShare(
        unknown_lines=unknown_lines,
        denominator=denominator,
        basis=DENOMINATOR_BASIS,
        share=share,
        threshold=threshold,
        ok=share <= threshold,
    )


def identify_version() -> str:
    """The version of the tagging library.

    Recorded because its tag tables change between releases, and a change there
    moves the language breakdown without anything else having been touched.
    """
    return metadata.version("identify")


def tag_disposition(tag: str) -> str:
    """How the policy regards a tag in isolation, for the diagnostic table."""
    rules = policy()
    if tag in rules.ignore:
        return "ignored by policy"
    if tag in GENERIC_TAGS:
        return "generic"
    if tag in rules.demote:
        return "demoted for naming"
    return ""
