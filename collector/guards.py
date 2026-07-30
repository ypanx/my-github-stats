"""Checks that must pass before anything is written.

Every guard here exists because of a failure that produces a plausible wrong
number rather than an error. The API returns a successful response with less data
when authorization lapses; a search silently truncates at its result cap; a file
whose diff was declined still counts its lines at the commit level. None of these
raise anything, and each would publish quietly incorrect figures.

Because the output overwrites the previous run's and is published unattended,
refusing to write is always preferable to writing something wrong.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from collections import Counter
from pathlib import Path
from typing import Any

from collector.aggregate import Aggregate, UnknownShare, aggregate, check_unknown_share
from collector.classify import STATUSES
from collector.constants import (
    MAX_DROP,
    MAX_SHORTFALL,
    MIN_COMPARABLE,
    MIN_WALKED_SHARE,
    SECTIONS,
)
from collector.metrics import headline_metrics
from collector.redact import short_hash

def check_non_zero(payload: dict[str, Any], total_contributions: int) -> list[str]:
    """Confirm the run actually saw data.

    When authorization to an organization lapses the API returns a successful
    response carrying only the account's personal repositories, so the failure
    looks like a small but plausible number rather than zero. A single check for
    emptiness would wave that through, which is why this is a conjunction.
    """
    problems = []
    if total_contributions <= 0:
        problems.append(
            f"the API reported {total_contributions} commit contributions")
    metrics = headline_metrics(payload)
    if metrics["commits"] <= 0:
        problems.append("no commits were collected")
    if metrics["lines"] <= 0:
        problems.append("no lines were counted")
    return problems


def check_unknown_lines(records: list[dict[str, Any]],
                        override: float | None = None) -> tuple[list[str], UnknownShare]:
    """Confirm the share of unclassifiable lines is acceptable.

    The threshold is the alarm for file types nothing has decided about yet, so
    it is deliberately not something the collector re-derives: the denominator
    has several plausible definitions and choosing the wrong one loosens the
    check without appearing to.
    """
    share = check_unknown_share(aggregate(records))
    if override is not None:
        # Rebuilt rather than compared against separately, so the returned object
        # never reports a verdict different from the one acted on.
        share = replace(share, threshold=override, ok=share.share <= override)
    if share.ok:
        return [], share
    return [f"unclassifiable lines are {share.unknown_lines:,} of "
            f"{share.denominator:,}, {share.share:.4%}, above the "
            f"{share.threshold:.4%} threshold"], share


def check_line_reconciliation(commits: list[dict[str, Any]],
                              files: dict[str, dict[str, Any]]
                              ) -> tuple[list[str], dict[str, int]]:
    """Compare per-file line counts against what each commit reports for itself.

    Two failures look identical in a total and have opposite remedies, so they are
    separated. Per-file counts exceeding the commit total mean a page was read
    twice, which is a fault here and recoverable, so it is fatal. Falling short
    means GitHub declined to diff a file, which cannot be recovered and is
    therefore bounded instead.
    """
    over: list[str] = []
    shortfall = 0
    record_lines = 0
    commit_lines = 0
    undiffable = 0
    excluded = 0

    for commit in commits:
        result = files[commit["oid"]]
        # A commit that could not be read to the end is knowingly short, and
        # including it would consume the whole shortfall budget with a gap that is
        # already reported separately.
        if not result.get("complete", True):
            excluded += 1
            continue
        counted = sum(record["additions"] + record["deletions"]
                      for record in result["records"])
        reported = int(commit.get("additions", 0)) + int(commit.get("deletions", 0))
        record_lines += counted
        commit_lines += reported
        undiffable += result.get("undiffable", 0)
        if counted > reported:
            over.append(
                f"{short_hash(commit['oid'])} counted {counted} against {reported}")
        else:
            shortfall += reported - counted

    problems = []
    if over:
        problems.append(
            f"{len(over)} commit(s) counted more lines per file than the commit "
            f"itself reports, which means a page was read twice: {over[:3]}")
    if record_lines and shortfall > record_lines * MAX_SHORTFALL:
        problems.append(
            f"per-file counts fall {shortfall:,} lines short of the {commit_lines:,} "
            f"the commits report, {shortfall / commit_lines:.4%}, above the "
            f"{MAX_SHORTFALL:.2%} tolerance")

    return problems, {"record_lines": record_lines, "commit_lines": commit_lines,
                      "shortfall": shortfall, "undiffable": undiffable,
                      "excluded": excluded}


def check_repository_reconciliation(repositories: list[dict[str, Any]],
                                    commits: list[dict[str, Any]]) -> list[str]:
    """Compare contributions credited per repository against commits walked.

    This is what would catch a broken author filter or a truncated repository
    list, both of which lose commits in bulk. A repository with contributions but
    no walked commits is expected rather than wrong, since a co-authored commit
    credits every author while history returns only the primary one.

    Messages never name a repository, because they reach public build logs.
    """
    walked: Counter[str] = Counter(commit["repo"] for commit in commits)
    problems = []
    for entry in repositories:
        credited = entry["contributions"]
        found = walked.get(entry["name_with_owner"], 0)
        if found and credited and found < credited * MIN_WALKED_SHARE:
            problems.append(
                f"a repository yielded {found} commits against {credited} credited "
                "contributions, so more than half of its volume is missing")
    return problems


def check_day_over_day(payload: dict[str, Any],
                       previous_path: Path) -> tuple[list[str], str | None]:
    """Compare against the previously published file and catch a collapse.

    Returns the problems found and, separately, a reason the comparison was
    skipped. A skip is not a failure. When there is no previous file, or it cannot
    be read, or its schema or window length differs, the comparison is simply
    impossible — and failing instead would let a rename block publication
    indefinitely, since every subsequent run would meet the same stale file.

    A rolling window cannot honestly lose half its content in a day, which is what
    makes the threshold safe. It also catches what a fixed canary never could: a
    revoked scope, a truncated enumeration, a regression in this code.
    """
    if not previous_path.exists():
        return [], "no previous file"
    try:
        previous = json.loads(previous_path.read_text())
    except json.JSONDecodeError:
        return [], "the previous file is not valid JSON"
    if missing := [section for section in SECTIONS if section not in previous]:
        return [], f"the previous file predates this schema, lacking {missing[0]}"

    before_span = _window_span(previous.get("window") or {})
    after_span = _window_span(payload["window"])
    if before_span != after_span:
        return [], f"the window length changed, {before_span} to {after_span} dates"

    before = headline_metrics(previous)
    after = headline_metrics(payload)
    problems = []
    for name, was in before.items():
        # Either too small for a proportion to mean anything, or more than half
        # of it has gone. One rule, with no per-metric tuning.
        if was < MIN_COMPARABLE:
            continue
        now = after[name]
        if now < was * (1 - MAX_DROP):
            problems.append(f"{name} fell {(was - now) / was:.0%}, from {was:,} to {now:,}")
    return problems, None


def describe_totals(totals: Aggregate) -> str:
    """One line summarizing how file records were classified."""
    return " ".join(
        f"{status} {totals.records[status]:,}/{totals.lines[status]:,}"
        for status in STATUSES)


def _window_span(window: dict[str, Any]) -> int | None:
    try:
        return (dt.date.fromisoformat(window["to"])
                - dt.date.fromisoformat(window["from"])).days + 1
    except (KeyError, TypeError, ValueError):
        return None
