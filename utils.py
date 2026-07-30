"""Everything between the API client and the two output files.

Dates and search slicing, the classification tables, folding file records into
per-language totals, assembling and splitting the payload, the figures the cards
derive, and the two checks that run before anything is written.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import math
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import yaml

from constants import (
    CHUNK_DAYS,
    CONTRIB_MAX_DAYS,
    LANGUAGES_PATH,
    MAX_DROP,
    MIN_COMPARABLE,
    PUBLISHED_FIELDS,
    REVIEW_STATES,
    SECTIONS,
)

UTC = dt.timezone.utc


class CollectError(Exception):
    """A run cannot honestly continue."""


# --------------------------------------------------------------------------- #
# Dates, windows, and the slicing of searches
# --------------------------------------------------------------------------- #

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
    return bool(value) and since <= utc_day(value) <= until


def window_bounds(since: dt.date, until: dt.date) -> tuple[str, str]:
    """The window as a pair of ISO-8601 instants, shared by every query.

    One pair serves repository enumeration and commit history alike, and that
    sharing is deliberate. When the two disagreed, a repository whose only
    activity fell in the gap was never enumerated, so all of its commits
    disappeared with nothing reporting a problem.

    The end is capped at the present because the API cannot see the future.
    """
    start = dt.datetime.combine(since, dt.time.min, tzinfo=UTC)
    requested = dt.datetime.combine(until, dt.time.max, tzinfo=UTC).replace(microsecond=0)
    end = min(requested, dt.datetime.now(UTC).replace(microsecond=0))
    return _iso(start), _iso(end)


def require_window_fits(since: dt.date, until: dt.date) -> None:
    """Reject a window the contributions connection cannot answer in one query.

    Both ends are inclusive, so a window covering a year plus one day spans
    slightly more than a year and is refused. That is why the horizon counts
    dates rather than elapsed days.

    An inverted window is refused here too. It used to pass this check, because a
    negative span is not greater than the maximum, and then fail several minutes
    later inside the first search — after a full history walk.
    """
    if until < since:
        raise CollectError(f"the window {since}..{until} ends before it starts")
    dates = (until - since).days + 1
    if dates > CONTRIB_MAX_DAYS:
        suggested = until - dt.timedelta(days=CONTRIB_MAX_DAYS - 1)
        raise CollectError(
            f"the window {since}..{until} covers {dates} dates, and the "
            f"contributions API accepts at most {CONTRIB_MAX_DAYS}. Move the "
            f"start forward to {suggested}.")


def resolve_window(since: dt.date | None, until: dt.date | None,
                   horizon: int) -> tuple[dt.date, dt.date]:
    """Either the window given explicitly, or the trailing `horizon` dates."""
    if since and until:
        return since, until
    end = dt.datetime.now(UTC).date()
    return end - dt.timedelta(days=horizon - 1), end


def chunk_window(since: dt.date, until: dt.date,
                 days: int = CHUNK_DAYS) -> list[tuple[dt.date, dt.date]]:
    """Tile a window into consecutive slices of at most `days` each.

    Search date ranges are inclusive at both ends, so each slice begins the day
    after the previous one ended. A shared boundary day counts every event on it
    twice and a gap drops them, and neither mistake raises anything.
    """
    if days < 1:
        raise ValueError(f"a slice must be at least one day, got {days}")
    if until < since:
        raise ValueError(f"the window ends before it starts: {since}..{until}")

    slices = []
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
    by when they were last updated, and that bound has to stay open-ended, so it
    cannot be used to slice. Creation date can: it never changes, so slices by
    creation date are disjoint by construction.

    The leading slice is open at the start, catching pull requests created before
    the window but touched inside it. No trailing slice is needed, because a pull
    request created after the window ends cannot carry an event inside it.
    """
    return [(None, since), *chunk_window(since, until, days)]


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Policy:
    """The tables `languages.yml` holds."""

    include_extensions: Mapping[str, str]
    include_filenames: Mapping[str, str]
    exclude_extensions: frozenset[str]
    exclude_filenames: frozenset[str]
    exclude_directories: frozenset[str]
    exclude_globs: tuple[str, ...]


_TABLES = ("include_extensions", "include_filenames", "exclude_extensions",
           "exclude_filenames", "exclude_directories", "exclude_globs")


def load_policy(path: str | os.PathLike[str] | None = None) -> Policy:
    """Read `languages.yml`. Pure, with no caching."""
    path = Path(path) if path is not None else LANGUAGES_PATH
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or any(table not in raw for table in _TABLES):
        raise CollectError(f"{path}: expected a mapping holding {list(_TABLES)}")
    return Policy(
        include_extensions=dict(raw["include_extensions"]),
        include_filenames=dict(raw["include_filenames"]),
        exclude_extensions=frozenset(raw["exclude_extensions"]),
        exclude_filenames=frozenset(raw["exclude_filenames"]),
        exclude_directories=frozenset(raw["exclude_directories"]),
        exclude_globs=tuple(raw["exclude_globs"]),
    )


_cached: Policy | None = None


def policy() -> Policy:
    """The tables, loaded once. Classification runs per file record."""
    global _cached
    if _cached is None:
        _cached = load_policy()
    return _cached


def set_policy(value: Policy | None) -> None:
    """Replace the cached tables, or pass None to restore lazy loading."""
    global _cached
    _cached = value


def extension(path: str) -> str:
    _, dot, suffix = os.path.basename(path).rpartition(".")
    return suffix.lower() if dot else ""


def classify(path: str) -> str | None:
    """The language a path counts as, or None if it does not count.

    Order matters twice. Exclusions run first, so `service.pb.go` loses although
    `go` is included. Filenames beat extensions, so `.flake8` is reachable at
    all, its extension being `flake8`.
    """
    rules = policy()
    basename = os.path.basename(path)

    if any(part in rules.exclude_directories for part in PurePosixPath(path).parts):
        return None
    # Matched case-sensitively on every platform. `fnmatch` folds case on Windows,
    # which would make `license` match `LICENSE*` there and nowhere else.
    if any(fnmatch.fnmatchcase(basename, pattern) for pattern in rules.exclude_globs):
        return None
    if basename in rules.exclude_filenames or extension(path) in rules.exclude_extensions:
        return None
    return (rules.include_filenames.get(basename)
            or rules.include_extensions.get(extension(path)))


def fold_languages(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Additions and deletions per language over one commit's file records.

    Additions and deletions are kept apart. Collapsing them into a single churn
    figure here would make that choice permanent, and whether a card shows churn
    or net change belongs to rendering.
    """
    totals: dict[str, dict[str, int]] = {}
    for record in records:
        name = classify(str(record["path"]))
        if name is None:
            continue
        bucket = totals.setdefault(name, {"additions": 0, "deletions": 0})
        bucket["additions"] += int(record["additions"])
        bucket["deletions"] += int(record["deletions"])
    return {name: totals[name] for name in sorted(totals)}


# --------------------------------------------------------------------------- #
# Assembling the two payloads
# --------------------------------------------------------------------------- #

#: Sort keys that depend only on published content, so that record order carries
#: no information the records do not already contain.
#:
#: Records arrive grouped by repository, and that ordering leaked structure the
#: aggregated file is meant to withhold: the points at which the date stopped
#: descending marked the repository boundaries, letting a reader infer how the
#: commits were distributed. Sorting on content removes that without needing
#: randomness, which would make runs irreproducible.
SORT_KEYS = {
    "commits": lambda record: (record["date"],
                               json.dumps(record["languages"], sort_keys=True)),
    "prs": lambda record: (record["created"], record["merged"] or "", record["state"],
                           record["cycle_hours"] if record["cycle_hours"] is not None
                           else -1.0),
    "review_envelopes": lambda record: (record["date"], record["state"],
                                        record["inline_count"], record["has_body"],
                                        record["on_own_pr"]),
    "comments": lambda record: (record["date"], record["kind"], record["on_own_pr"]),
}


def build_commit_records(commits: list[dict[str, Any]],
                         files: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per commit: the day it landed and its per-language line counts.

    Classification happens here and the paths are discarded immediately, so they
    never reach either output file.

    A commit whose files are all excluded still produces a record, with an empty
    language map. It happened, and dropping it would undercount the commit total.
    """
    return [{"date": utc_day(commit["committedDate"]).isoformat(),
             "languages": fold_languages(files[commit["oid"]]["records"]),
             "repo": commit["repo"]}
            for commit in commits]


def sort_sections(payload: dict[str, Any]) -> dict[str, Any]:
    """Order every section by published content, in place.

    Called before the payload is split, so both files share one order and remain
    comparable record for record.
    """
    for section, key in SORT_KEYS.items():
        payload[section].sort(key=key)
    return payload


def build_payload(window: dict[str, str], commits: list[dict[str, Any]],
                  pull_requests: list[dict[str, Any]],
                  envelopes: list[dict[str, Any]],
                  comments: list[dict[str, Any]]) -> dict[str, Any]:
    """The attributed payload, sorted and ready to be stripped for publication."""
    return sort_sections({"window": window, "commits": commits,
                          "prs": pull_requests, "review_envelopes": envelopes,
                          "comments": comments})


def strip_attribution(payload: dict[str, Any]) -> dict[str, Any]:
    """The publishable view, keeping only the fields named in `PUBLISHED_FIELDS`.

    Derived from the attributed payload rather than assembled separately, so the two
    cannot disagree about anything they share. Deep-copied so a later change to one
    cannot rewrite the other.

    An allow-list rather than a deny-list. Dropping `repo` by name would publish any
    field added to a record later, and the file is served publicly, so the default
    for something unrecognized has to be to withhold it.
    """
    stripped: dict[str, Any] = {"window": dict(payload["window"])}
    for section in SECTIONS:
        keep = PUBLISHED_FIELDS[section]
        stripped[section] = [
            deepcopy({key: record[key] for key in keep if key in record})
            for record in payload[section]
        ]
    return stripped


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write via a temporary file and a rename, so a crash cannot truncate.

    The rename is only atomic within a single filesystem, which is why the
    temporary file sits beside its target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


# --------------------------------------------------------------------------- #
# Figures derived from a payload
# --------------------------------------------------------------------------- #

def nearest_rank(values: list[float], proportion: float) -> float:
    """The nearest-rank percentile: sort, then take one specific element.

    Pinned to a definition because "median" is ambiguous. Interpolating between
    the two middle values of an even-length list gives a different answer in the
    first decimal place, which is the precision being reported.
    """
    if not values:
        raise ValueError("cannot take a percentile of nothing")
    if not 0 < proportion <= 1:
        raise ValueError(f"a percentile must fall in (0, 1], got {proportion}")
    ordered = sorted(values)
    index = min(max(math.ceil(proportion * len(ordered)) - 1, 0), len(ordered) - 1)
    return ordered[index]


def total_lines(commits: list[dict[str, Any]]) -> int:
    """Total churn across every counted language."""
    return sum(counts["additions"] + counts["deletions"]
               for commit in commits
               for counts in commit["languages"].values())


def language_totals(commits: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Additions and deletions per language, across all commits."""
    totals: dict[str, dict[str, int]] = {}
    for commit in commits:
        for name, counts in commit["languages"].items():
            bucket = totals.setdefault(name, {"additions": 0, "deletions": 0})
            bucket["additions"] += counts["additions"]
            bucket["deletions"] += counts["deletions"]
    return {name: totals[name] for name in sorted(totals)}


def cycle_hours(pull_requests: list[dict[str, Any]]) -> list[float]:
    """Time to merge for every pull request that was merged."""
    return [pr["cycle_hours"] for pr in pull_requests
            if pr.get("cycle_hours") is not None]


def reviews_given(envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reviews of other people's pull requests.

    Reviews of one's own are excluded because they are not reviews given; their
    inline comments are counted among the comments instead.
    """
    return [envelope for envelope in envelopes if not envelope["on_own_pr"]]


def active_dates(payload: dict[str, Any]) -> set[str]:
    """Every date on which any collected record says something happened.

    Taken as the union of the dates the payload already publishes, so this counts
    the same activity as every other figure beside it and inherits the same
    definition of a day.
    """
    return ({commit["date"] for commit in payload["commits"]}
            | {pr["created"] for pr in payload["prs"]}
            | {envelope["date"] for envelope in payload["review_envelopes"]}
            | {comment["date"] for comment in payload["comments"]})


def headline_metrics(payload: dict[str, Any]) -> dict[str, int]:
    """The integer counts the cards show and the day-over-day check watches.

    Every value is derived from the payload itself, so no baseline needs storing
    and nothing here names a repository or an account.

    There is no count of distinct languages. Cardinality is the wrong measure for
    a set that small: two rarely-touched languages ageing out of the window reads
    as a collapse while representing a rounding error in the line totals.
    """
    envelopes = payload["review_envelopes"]
    comments = payload["comments"]
    given = reviews_given(envelopes)
    return {
        "commits": len(payload["commits"]),
        "lines": total_lines(payload["commits"]),
        "prs_opened": len(payload["prs"]),
        "prs_merged": sum(1 for pr in payload["prs"] if pr["merged"]),
        "reviews_given": len(given),
        "review_envelopes": len(envelopes),
        # Per state as well as in total, because the four states collapse into one
        # number: a fault that relabelled every approval would move no total, yet
        # approvals are what gets reported.
        **{f"reviews_{state.lower()}": sum(1 for r in given if r["state"] == state)
           for state in REVIEW_STATES},
        "reviews_substantive": sum(1 for r in given
                                   if r["has_body"] or r["inline_count"] > 0),
        "comments_inline": sum(1 for c in comments if c["kind"] == "inline"),
        "comments_conversational": sum(1 for c in comments
                                       if c["kind"] == "conversational"),
        # Counting distinct dates is sound where counting distinct languages is
        # not. A date is a unit of activity in its own right rather than a proxy
        # for volume.
        "active_days": len(active_dates(payload)),
    }


# --------------------------------------------------------------------------- #
# The two checks that run before anything is written
# --------------------------------------------------------------------------- #

def check_non_zero(payload: dict[str, Any], total_contributions: int) -> list[str]:
    """Confirm the run actually saw data.

    When authorization to an organization lapses the API returns a successful
    response carrying only the account's personal repositories, so the failure
    looks like a small but plausible number rather than zero. A single check for
    emptiness would wave that through, which is why this is a conjunction.
    """
    problems = []
    if total_contributions <= 0:
        problems.append(f"the API reported {total_contributions} commit contributions")
    metrics = headline_metrics(payload)
    if metrics["commits"] <= 0:
        problems.append("no commits were collected")
    if metrics["lines"] <= 0:
        problems.append("no lines were counted")
    return problems


def check_day_over_day(payload: dict[str, Any],
                       previous_path: Path) -> tuple[list[str], str | None]:
    """Compare against the previous run, and refuse a collapse.

    This is the check that actually catches lapsed authorization, because the API
    answers successfully with a plausibly small number rather than with an error.
    Confirmed in practice on 2026-07-30: a token with the right scopes but no
    organization authorization returned twelve figures down 99 to 100%.

    A rolling window cannot honestly lose half its content in a day, which is what
    makes the bound safe. It reports a skip rather than a failure when there is
    nothing to compare against, so a first run is unguarded — collect once by hand
    before enabling a schedule.
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

    try:
        before = headline_metrics(previous)
    except (KeyError, TypeError):
        # Section names alone do not prove the records still have the same fields.
        return [], "the previous file predates this schema"
    after = headline_metrics(payload)
    problems = []
    for name, was in before.items():
        # Either too small for a proportion to mean anything, or more than half of
        # it has gone. One rule, with no per-metric tuning.
        if was < MIN_COMPARABLE:
            continue
        if (now := after[name]) < was * (1 - MAX_DROP):
            problems.append(f"{name} fell {(was - now) / was:.0%}, "
                            f"from {was:,} to {now:,}")
    return problems, None


def _window_span(window: dict[str, Any]) -> int | None:
    try:
        return (dt.date.fromisoformat(window["to"])
                - dt.date.fromisoformat(window["from"])).days + 1
    except (KeyError, TypeError, ValueError):
        return None
