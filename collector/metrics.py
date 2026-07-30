"""Figures derived from a published payload.

Nothing here is precomputed into the output files. Every total, share and
percentile is derived on demand, which is what lets a dashboard offer time
windows the collector never anticipated.
"""

from __future__ import annotations

import math
from typing import Any

from collector.constants import REVIEW_STATES


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
    definition of a day. A per-date count supplied by the API instead covers a
    different set of activities and buckets them in the account's own timezone,
    which leaves it impossible to reconcile against anything else in the file.
    """
    dates = {commit["date"] for commit in payload["commits"]}
    dates |= {pull_request["created"] for pull_request in payload["prs"]}
    dates |= {envelope["date"] for envelope in payload["review_envelopes"]}
    dates |= {comment["date"] for comment in payload["comments"]}
    return dates


def headline_metrics(payload: dict[str, Any]) -> dict[str, int]:
    """The integer counts the day-over-day comparison watches.

    Every value is derived from the payload itself, so no baseline needs to be
    stored anywhere and nothing here names a repository or an account.

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
        # for volume, and a year of them is numerous enough that a fall in the
        # count means work stopped rather than that a thinly represented member
        # of a small set happened to leave the window.
        "active_days": len(active_dates(payload)),
    }
