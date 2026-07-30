"""Keeping identifying detail out of messages that reach public logs.

Error messages are the awkward case. The success paths deliberately hide
repository names, paths and commit hashes unless verbose output is asked for, but
an exception carries whatever the underlying library put in it — a transport
failure from the HTTP layer, for instance, quotes the full request URL, and that
URL contains the repository owner and name.

Since workflow logs on a public repository are public, anything heading for a
message goes through here first.
"""

from __future__ import annotations

import re

#: A run identifies a commit by a short prefix, which is enough to look one up
#: locally while not being the full hash.
SHORT_HASH_LENGTH = 8

_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")
_REPO_PATH = re.compile(r"/repos/[^/\s]+/[^/\s?]+")
_OWNER_NAME = re.compile(r"\b[\w.-]+/[\w.-]+\b")


def short_hash(oid: str | None) -> str:
    """A commit hash trimmed to a prefix that is not the whole thing."""
    return (oid or "unknown")[:SHORT_HASH_LENGTH]


def scrub(text: str) -> str:
    """Remove repository paths and long hashes from arbitrary text.

    Applied to messages originating outside this package, where there is no way
    to know in advance what detail they carry.
    """
    text = _REPO_PATH.sub("/repos/<repository>", text)
    text = _OWNER_NAME.sub("<repository>", text)
    return _HASH.sub(lambda match: match.group()[:SHORT_HASH_LENGTH], text)
