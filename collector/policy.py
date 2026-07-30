"""Loading and validating the classification policy.

The policy decides which files count towards the language breakdown and what
each language is called. It lives in YAML because it encodes judgements that get
revisited, not logic.

Validation is strict. Every failure mode of this file is silent: a mistyped key
would disable a rule, and the resulting numbers would still add up perfectly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policy.yml"


class PolicyError(Exception):
    """The policy file is malformed, or carries a key that must not exist."""


@dataclass(frozen=True)
class Policy:
    """Every rule the classifier consults, already validated."""

    #: Tag to display name, for the cases where capitalizing the tag is wrong.
    display: Mapping[str, str]

    #: Tags naming the tool that reads a file rather than the language it is
    #: written in. These lose to any other tag when choosing a display name.
    demote: frozenset[str]

    #: Tags whose files are recognized but should not count as authored work.
    ignore: frozenset[str]

    #: Tags that rescue a file from `ignore`, for formats stored as data but
    #: written by hand.
    count_anyway: frozenset[str]

    #: Extensions to ignore regardless of tags, lower-cased.
    ignore_extensions: frozenset[str]

    #: Directory names that mark generated or vendored content, matched against
    #: any component of a path.
    ignore_directories: frozenset[str]

    #: Basename globs that mark generated content.
    ignore_globs: tuple[str, ...]

    #: The share of unclassifiable lines above which a run is not trustworthy.
    unknown_threshold: float


_FIELD_TYPES = {
    "display": dict,
    "demote": list,
    "ignore": list,
    "count_anyway": list,
    "ignore_extensions": list,
    "ignore_directories": list,
    "ignore_globs": list,
    "unknown_threshold": (int, float),
}

_STRING_LISTS = ("demote", "ignore", "count_anyway", "ignore_extensions",
                 "ignore_directories", "ignore_globs")


def load_policy(path: str | os.PathLike[str] | None = None) -> Policy:
    """Read and validate a policy file. Pure, with no caching."""
    path = Path(path) if path is not None else DEFAULT_POLICY_PATH
    raw = yaml.safe_load(path.read_text())

    if not isinstance(raw, dict):
        raise PolicyError(f"{path}: expected a mapping at the top level")

    # Unclassifiable files are a residual, not a category. Anything worth naming
    # belongs under `ignore` or `display`, and a key here would imply otherwise.
    if "unknown" in raw:
        raise PolicyError(
            f"{path}: `unknown` is a residual rather than a configurable "
            "category. Put the type under `ignore` or `display` instead.")

    if unexpected := sorted(set(raw) - set(_FIELD_TYPES)):
        raise PolicyError(f"{path}: unrecognized key(s): {', '.join(unexpected)}")
    if missing := sorted(set(_FIELD_TYPES) - set(raw)):
        raise PolicyError(f"{path}: missing key(s): {', '.join(missing)}")

    for key, expected in _FIELD_TYPES.items():
        if not isinstance(raw[key], expected) or isinstance(raw[key], bool):
            raise PolicyError(f"{path}: `{key}` has the wrong type")

    display = raw["display"]
    if bad := sorted(key for key, value in display.items()
                     if not isinstance(key, str) or not isinstance(value, str)):
        raise PolicyError(f"{path}: `display` must map strings to strings: {bad}")
    for key in _STRING_LISTS:
        if bad := [item for item in raw[key] if not isinstance(item, str)]:
            raise PolicyError(f"{path}: `{key}` must hold strings: {bad}")

    threshold = float(raw["unknown_threshold"])
    if not 0.0 <= threshold <= 1.0:
        raise PolicyError(f"{path}: `unknown_threshold` must be a fraction of one")

    # A tag in both lists is a contradiction: one says discard the file, the
    # other says keep it. Rejecting it beats letting evaluation order decide.
    if overlap := sorted(set(raw["ignore"]) & set(raw["count_anyway"])):
        raise PolicyError(
            f"{path}: tag(s) in both `ignore` and `count_anyway`: {overlap}. A "
            "rescue applies to a file whose other tag is ignored, so a tag "
            "cannot rescue itself.")

    return Policy(
        display=dict(display),
        demote=frozenset(raw["demote"]),
        ignore=frozenset(raw["ignore"]),
        count_anyway=frozenset(raw["count_anyway"]),
        # Extensions are lower-cased on both sides of the comparison because the
        # tagging library lower-cases them, and a case-sensitive rule here made
        # this the only case-sensitive rule in the classifier.
        ignore_extensions=frozenset(item.lower() for item in raw["ignore_extensions"]),
        ignore_directories=frozenset(raw["ignore_directories"]),
        ignore_globs=tuple(raw["ignore_globs"]),
        unknown_threshold=threshold,
    )


_cached: Policy | None = None


def policy() -> Policy:
    """The policy, loaded once and reused.

    Loading is cached because classification runs once per file record and there
    are thousands of them. The path is resolved relative to this package rather
    than the working directory, so it does not matter where a run is started.
    """
    global _cached
    if _cached is None:
        _cached = load_policy()
    return _cached


def set_policy(value: Policy | None) -> None:
    """Replace the cached policy, or pass None to restore lazy loading."""
    global _cached
    _cached = value
