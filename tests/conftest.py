"""Shared fixtures. Every test here is offline and touches no network.

One test file per implementation file:

    test_client.py     client.py
    test_queries.py    queries.py
    test_collect.py    collect.py
    test_utils.py      utils.py
    test_cards.py      cards.py

`constants.py` holds no logic, so nothing tests it directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils  # noqa: E402
from utils import Policy  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def committed_policy():
    """Run every test against the committed policy.

    The cache is cleared afterwards so that a test installing its own policy
    cannot influence any later one.
    """
    utils.set_policy(utils.load_policy())
    yield
    utils.set_policy(None)


@pytest.fixture
def make_policy():
    """Build a synthetic set of tables, for rules the committed file cannot express.

    The committed tables deliberately hold no contradictions, so the order between
    two rules that only disagree on a contradictory row can only be tested against
    an installed policy.
    """
    def build(**overrides) -> Policy:
        fields = {
            "include_extensions": {}, "include_filenames": {},
            "exclude_extensions": frozenset(), "exclude_filenames": frozenset(),
            "exclude_directories": frozenset(), "exclude_globs": (),
        }
        return Policy(**{**fields, **overrides})
    return build


@pytest.fixture
def payload() -> dict:
    """A small attributed payload, shaped exactly like a real one."""
    return {
        "window": {"from": "2026-01-01", "to": "2026-01-31",
                   "generated_at": "2026-02-01T00:00:00Z"},
        "commits": [
            {"date": "2026-01-03", "repo": "owner/private",
             "languages": {"Go": {"additions": 7, "deletions": 3}}},
            {"date": "2026-01-02", "repo": "owner/private",
             "languages": {"Python": {"additions": 10, "deletions": 5}}},
        ],
        "prs": [
            {"created": "2026-01-04", "merged": None, "state": "OPEN",
             "cycle_hours": None, "repo": "owner/private"},
            {"created": "2026-01-02", "merged": "2026-01-03", "state": "MERGED",
             "cycle_hours": 24.0, "repo": "owner/private"},
        ],
        "review_envelopes": [
            {"date": "2026-01-06", "state": "COMMENTED", "inline_count": 1,
             "has_body": False, "on_own_pr": True, "repo": "owner/private"},
            {"date": "2026-01-05", "state": "APPROVED", "inline_count": 2,
             "has_body": True, "on_own_pr": False, "repo": "owner/private"},
        ],
        "comments": [
            {"date": "2026-01-05", "kind": "inline", "on_own_pr": False,
             "repo": "owner/private"},
            {"date": "2026-01-05", "kind": "conversational", "on_own_pr": False,
             "repo": "owner/private"},
        ],
    }


@pytest.fixture
def bulk_payload():
    """A payload with realistic volume.

    The small payload is unusable for the day-over-day comparison, because a
    proportion of two records means nothing. This builds something large enough
    for the thresholds to be meaningful.
    """
    def build(commits: int = 467, lines: int = 250_000, approvals: int = 618):
        return {
            "window": {"from": "2025-07-30", "to": "2026-07-29",
                       "generated_at": "2026-07-29T12:00:00Z"},
            "commits": [
                {"date": "2026-01-02",
                 "languages": {"Python": {"additions": lines // max(commits, 1),
                                          "deletions": 0}}}
                for _ in range(commits)
            ],
            "prs": [{"created": "2026-01-02", "merged": "2026-01-03",
                     "state": "MERGED", "cycle_hours": 24.0} for _ in range(597)],
            "review_envelopes": [
                {"date": "2026-01-05", "state": "APPROVED", "inline_count": 1,
                 "has_body": True, "on_own_pr": False} for _ in range(approvals)
            ],
            "comments": [{"date": "2026-01-05", "kind": "inline",
                          "on_own_pr": False} for _ in range(654)],
        }
    return build
