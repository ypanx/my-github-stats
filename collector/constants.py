"""Tunables, measured API limits, and the shape of the published files.

Most limits here were established by observation rather than taken from
documentation, and each one fails silently when exceeded, so none should be
changed without re-measuring.
"""

from __future__ import annotations

from pathlib import Path

DATA_DIR = Path("data")

#: Aggregated and safe to publish.
STATS_PATH = DATA_DIR / "stats.json"

#: The same records with repository attribution added. Never committed.
LOCAL_PATH = DATA_DIR / "stats.local.json"

# --------------------------------------------------------------------------- #
# API limits
# --------------------------------------------------------------------------- #

#: Search results are capped, so a query matching more than this cannot be
#: paginated to the end. The match count itself is not capped, which is what
#: makes it a usable detector for the situation.
SEARCH_CAP = 1000

#: Search page size. This must divide the cap exactly: a page straddling the
#: thousandth result comes back empty with no error, silently losing the tail.
PAGE_SIZE = 25

#: Commit history rejects a larger page with an excessive-pagination error.
HISTORY_PAGE = 100

#: The most repositories the contributions connection will return. Its default is
#: far lower, so it must always be passed explicitly. There is no pagination
#: behind it, so receiving exactly this many cannot be told apart from being
#: truncated.
MAX_REPOSITORIES = 100

#: Files per page when reading one commit. This is a page size, not a limit on
#: how many files a commit may have; treating it as a limit undercounts badly.
REST_FILE_PAGE = 300

#: Beyond this many file records a commit cannot be read to completion, and no
#: further pages exist to ask for.
REST_FILE_CEILING = 3000

#: The contributions connection rejects a span longer than one year.
CONTRIB_MAX_DAYS = 365

#: Days per search slice, small enough that no slice approaches the result cap.
CHUNK_DAYS = 91

#: Reviews requested per pull request. The observed maximum on any single pull
#: request is well under this, and truncation is asserted rather than assumed, so
#: the headroom can stay modest. GraphQL charges for what a query could return
#: and this figure sits inside a page of pull requests, so it multiplies.
REVIEW_PAGE = 30

#: Workers for the per-commit file fetch. Fetching sequentially takes minutes.
REST_WORKERS = 6

# --------------------------------------------------------------------------- #
# Guard thresholds
# --------------------------------------------------------------------------- #

#: The day-over-day guard exists to catch a collapse, not drift. When
#: authorization to an organization lapses, the API returns a successful response
#: carrying almost no data — a fall of well over ninety percent. A bound at half
#: catches that with room to spare while staying impossible for a rolling window
#: to reach honestly, so it never blocks a good run.
MAX_DROP = 0.50

#: Below this, a proportion says nothing useful: a tenth of three is less than
#: one, so any movement at all reads as a collapse. Small metrics are skipped
#: rather than given individual thresholds.
MIN_COMPARABLE = 50

#: The share of a repository's own credited contributions below which the number
#: of commits actually walked is implausible. Two effects legitimately widen that
#: gap: a co-authored commit credits every author while history returns only the
#: primary one, and contributions are bucketed in the account's own timezone
#: rather than in UTC, which shifts commits across the window edges. Both scale
#: with volume, so the bound is a proportion rather than a count.
MIN_WALKED_SHARE = 0.5

#: Tolerated shortfall between per-file line counts and the totals a commit
#: reports for itself. GitHub occasionally declines to produce a diff for a file
#: while still counting its lines at the commit level. That cannot be recovered,
#: so it is measured and bounded rather than fixed.
MAX_SHORTFALL = 0.005

# --------------------------------------------------------------------------- #
# Published shape
# --------------------------------------------------------------------------- #

#: Review states that count as a submitted review. A pending review is a draft
#: visible only to its author, and is excluded everywhere.
REVIEW_STATES = ("APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED")

#: The sections of both output files, in order.
#:
#: Every section holds individual records dated by UTC day, and none holds a
#: per-date or per-period summary. Any such view is derived by whatever reads the
#: file, which is what lets a reader choose a window the collector never
#: anticipated, and it keeps every published date on one definition of a day.
#:
#: The reviews section is named for envelopes rather than reviews because it
#: holds every review event, including those on one's own pull requests. Its
#: length is therefore not the number of reviews given, and the deliberately
#: awkward name exists to stop a reader reaching for the obvious and being wrong.
SECTIONS = ("commits", "prs", "review_envelopes", "comments")
