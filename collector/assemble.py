"""Turning collected records into the two published payloads.

One payload is built, sorted, and then stripped to produce the second, so the two
files cannot drift apart in content or in order.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from collector.aggregate import aggregate
from collector.constants import SECTIONS
from collector.windows import utc_day

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

    A commit whose files are all ignored still produces a record, with an empty
    language map. It happened, and dropping it would undercount the commit total.
    """
    records = []
    for commit in commits:
        totals = aggregate(files[commit["oid"]]["records"])
        records.append({
            "date": utc_day(commit["committedDate"]).isoformat(),
            "languages": totals.languages,
            "repo": commit["repo"],
        })
    return records


def build_payload(window: dict[str, str], commits: list[dict[str, Any]],
                  pull_requests: list[dict[str, Any]],
                  envelopes: list[dict[str, Any]],
                  comments: list[dict[str, Any]]) -> dict[str, Any]:
    """The attributed payload, sorted and ready to be stripped for publication."""
    payload = {
        "window": window,
        "commits": commits,
        "prs": pull_requests,
        "review_envelopes": envelopes,
        "comments": comments,
    }
    return sort_sections(payload)


def sort_sections(payload: dict[str, Any]) -> dict[str, Any]:
    """Order every section by published content, in place.

    Called before the payload is split, so both files share one order and remain
    comparable record for record.
    """
    for section, key in SORT_KEYS.items():
        payload[section].sort(key=key)
    return payload


def strip_attribution(payload: dict[str, Any]) -> dict[str, Any]:
    """The publishable view: repository names and private fields removed.

    Produced by removal from the attributed payload rather than assembled
    separately, so the two cannot disagree about anything they share.

    Deep-copied rather than shared by reference. Nothing mutates either payload
    today, but sharing nested objects would mean a later change to the published
    one silently rewriting the attributed file too.
    """
    stripped: dict[str, Any] = {"window": dict(payload["window"])}
    for section in SECTIONS:
        stripped[section] = [
            deepcopy({key: value for key, value in record.items()
                      if key != "repo" and not key.startswith("_")})
            for record in payload[section]
        ]
    return stripped
