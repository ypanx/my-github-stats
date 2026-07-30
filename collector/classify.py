"""Deciding what language a file is, from its path alone.

GitHub reports which files a commit touched and how many lines each gained and
lost, and nothing about what those files are. Language attribution is therefore
entirely local, computed from the path, after which the path is discarded.

Nothing here touches the network or the filesystem, and the result depends only
on the path and the policy, so the whole module is fast and deterministic.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Literal

from identify import identify

from collector.policy import Policy, policy

#: Where a file record ends up. The four values partition every record, so a
#: total is always the sum of exactly these four.
Status = Literal["counted", "ignored", "unknown", "binary"]

STATUSES: tuple[Status, ...] = ("counted", "ignored", "unknown", "binary")

#: Tags describing encoding rather than language. A file whose tags are only
#: these is unclassifiable, and they never supply a display name.
GENERIC_TAGS = frozenset({"text", "binary"})


def classify(path: str) -> tuple[str | None, Status]:
    """Return the display name and status for a path.

    The name is None for anything but a counted file. Returning a raw tag for an
    ignored file is how repository metadata once became a language.

    The order of the checks below is load-bearing:

    1. Binary files are excluded in code rather than by policy, because GitHub
       cannot diff them and reports no lines either way.
    2. Generated and vendored paths are excluded next. Tags cannot express this:
       a generated Go file really is Go, and what disqualifies it is where it
       came from.
    3. Extensions come before the residual check further down. Some lockfiles
       carry no tags at all, so without an extension rule they would be counted
       as unclassifiable and consume the threshold that exists to flag genuinely
       unrecognized types.
    4. A rescue lets a hand-written format survive an ignored tag it shares with
       genuine data.
    5. Ignoring tests the whole tag set. Choosing one tag arbitrarily and testing
       only that is how an ignored image type once became a counted language.
    6. What is left with no tag beyond the generic ones is unclassifiable.
    """
    rules = policy()
    # The filename variant is required rather than preferred: the path variant
    # inspects the filesystem, and none of these files exist locally.
    tags = frozenset(identify.tags_from_filename(path))

    if "binary" in tags:
        return None, "binary"
    if _path_ignored(path, rules):
        return None, "ignored"
    if extension(path) in rules.ignore_extensions:
        return None, "ignored"
    if not tags & rules.count_anyway and tags & rules.ignore:
        return None, "ignored"
    if not tags - GENERIC_TAGS:
        return None, "unknown"
    return _display_name(tags, rules), "counted"


def extension(path: str) -> str:
    """The final dot-suffix of a basename, lower-cased, or empty if there is none.

    Lower-cased to match the tagging library, which does the same, so that an
    uppercase extension is not treated as a different type.

    A dotfile yields its name without the leading dot, which is what lets the
    unclassifiable breakdown group dotfiles sensibly.
    """
    _, dot, suffix = os.path.basename(path).rpartition(".")
    return suffix.lower() if dot else ""


def unknown_type(path: str) -> str:
    """A grouping key for an unclassifiable file: its extension, else its name.

    Unclassifiable files have no useful tag by definition, so the only thing left
    to group them by is the name.
    """
    return extension(path) or os.path.basename(path)


def _path_ignored(path: str, rules: Policy) -> bool:
    """Whether a path is generated or vendored rather than written.

    Directories match on a whole path component, so nesting depth is irrelevant
    and a name that merely contains the word does not match. Globs match the
    basename.
    """
    if any(part in rules.ignore_directories for part in PurePosixPath(path).parts):
        return True
    basename = os.path.basename(path)
    return any(fnmatch(basename, pattern) for pattern in rules.ignore_globs)


def _display_name(tags: frozenset[str], rules: Policy) -> str:
    """Choose a display name from a file's surviving tags.

    Three groups of tags are held back, in order:

    Generic tags never name anything, since the encoding is not the language.
    Ignored tags never name anything either, so that a rescued notebook is named
    for being a notebook rather than for the data format it is stored in. Demoted
    tags name the tool that reads a file rather than the language, and lose to
    anything else — but are still used when nothing else survives, which is what
    keeps a plain text file named as text rather than left unclassified.

    Whatever remains is resolved alphabetically. That is arbitrary, but harmless
    once markers are demoted, because the remaining ties are between tags for the
    same language rather than between a language and a marker.
    """
    candidates = tags - GENERIC_TAGS - rules.ignore
    preferred = candidates - rules.demote
    pool = preferred or candidates or (tags - GENERIC_TAGS)
    tag = min(pool)
    return rules.display.get(tag, tag.capitalize())
