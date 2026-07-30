"""The single error type raised when collection cannot honestly continue."""

from __future__ import annotations


class CollectError(Exception):
    """A guard failed, or the API returned something the schema cannot hold.

    Raised rather than logged whenever continuing would publish a number that is
    wrong in a way no later check could detect.
    """
