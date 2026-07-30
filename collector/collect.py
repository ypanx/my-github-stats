"""Orchestration and the command line.

One command produces both output files. Every guard runs before either file is
touched, so a failed run leaves the previous pair exactly as it found them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from client import GitHubClient, GitHubError, token_from_environment
from collector import guards
from collector.aggregate import aggregate
from collector.assemble import build_commit_records, build_payload, strip_attribution
from collector.collector import Collector
from collector.constants import LOCAL_PATH, MAX_SHORTFALL, STATS_PATH
from collector.errors import CollectError
from collector.metrics import total_lines
from collector.report import print_classification, print_summary
from collector.windows import require_window_fits, resolve_window

UTC = dt.timezone.utc


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write via a temporary file and a rename, so a crash cannot truncate.

    The rename is only atomic within a single filesystem, which is why the
    temporary file sits beside its target rather than somewhere else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def collect(args: argparse.Namespace) -> int:
    """Fetch a window, check it, and write both files if it holds up."""
    since, until = resolve_window(args.since, args.until, args.horizon)
    require_window_fits(since, until)

    client = GitHubClient(token_from_environment())
    collector = Collector(client, since, until, verbose=args.verbose)

    dates = (until - since).days + 1
    print(f"collecting {since} to {until}, {dates} dates")
    if not args.verbose:
        print("  repository names, paths and hashes are hidden; "
              "use --verbose locally to show them")

    print("\nresolving the account")
    collector.resolve_viewer()

    print("enumerating repositories")
    enumerated = collector.enumerate_repositories()

    print("walking commit history")
    commits = collector.walk_commits(enumerated["repositories"])

    print("reading files per commit")
    files = collector.fetch_all_files(commits)

    print("searching pull requests opened")
    pull_requests = collector.collect_pull_requests()

    print("searching reviews, then comments")
    envelopes = collector.collect_review_envelopes()
    comments = collector.collect_comments(envelopes)

    generated_at = dt.datetime.now(UTC).replace(microsecond=0)
    attributed = build_payload(
        window={"from": since.isoformat(), "to": until.isoformat(),
                "generated_at": generated_at.isoformat().replace("+00:00", "Z")},
        commits=build_commit_records(commits, files),
        pull_requests=pull_requests,
        envelopes=envelopes,
        comments=comments,
    )
    published = strip_attribution(attributed)

    print(f"\ncost: {client.budget()}")

    file_records = [record for result in files.values() for record in result["records"]]
    problems: list[str] = []
    problems += guards.check_non_zero(published, enumerated["total_commit_contributions"])
    unknown_problems, share = guards.check_unknown_lines(file_records,
                                                        args.unknown_threshold)
    problems += unknown_problems
    reconciliation_problems, reconciliation = guards.check_line_reconciliation(commits, files)
    problems += reconciliation_problems
    problems += guards.check_repository_reconciliation(enumerated["repositories"], commits)
    drop_problems, skipped = guards.check_day_over_day(published, STATS_PATH)
    problems += drop_problems

    print("\nchecks")
    print(f"  commit contributions  {enumerated['total_commit_contributions']:,}")
    print(f"  commits and lines     {len(published['commits']):,} and "
          f"{total_lines(published['commits']):,}")
    print(f"  unclassifiable        {share.unknown_lines:,} of {share.denominator:,} "
          f"= {share.share:.4%}")
    print(f"  line reconciliation   {reconciliation['record_lines']:,} counted against "
          f"{reconciliation['commit_lines']:,} reported, short by "
          f"{reconciliation['shortfall']:,} "
          f"({reconciliation['shortfall'] / max(reconciliation['record_lines'], 1):.4%} "
          f"of a {MAX_SHORTFALL:.2%} tolerance)")
    print(f"  undiffable records    {reconciliation['undiffable']:,}")
    print(f"  against previous run  "
          f"{'skipped, ' + skipped if skipped else 'compared'}")
    for note in collector.notes:
        print(f"  note                  {note}")

    if problems:
        print("\nchecks failed, so nothing was written and the previous files are "
              "untouched", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("  all checks passed")

    write_atomic(STATS_PATH, published)
    write_atomic(LOCAL_PATH, attributed)
    print(f"\nwrote {STATS_PATH} and {LOCAL_PATH}")

    print_summary(attributed, title="derived figures")
    if args.classification:
        print_classification(aggregate(file_records), verbose=args.verbose)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collect",
        description="Collect GitHub review and contribution statistics.")
    parser.add_argument("--since", type=dt.date.fromisoformat,
                        help="first date of the window, inclusive")
    parser.add_argument("--until", type=dt.date.fromisoformat,
                        help="last date of the window, inclusive")
    parser.add_argument("--horizon", type=int, default=365,
                        help="number of trailing dates to cover when no explicit "
                             "window is given (default: 365)")
    parser.add_argument("--summary", metavar="FILE",
                        help="print derived figures from an existing file and exit")
    parser.add_argument("--classification", action="store_true",
                        help="also print the tag-level classification table")
    parser.add_argument("--unknown-threshold", type=float, default=None,
                        help="override the unclassifiable-line threshold")
    parser.add_argument("--verbose", action="store_true",
                        help="show repository names, paths and hashes; never use "
                             "this where logs are public")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.summary:
        print_summary(json.loads(Path(args.summary).read_text()),
                      title=f"derived figures from {args.summary}")
        return 0
    if bool(args.since) != bool(args.until):
        parser.error("--since and --until must be given together")

    try:
        return collect(args)
    except (CollectError, GitHubError) as error:
        print(f"\nfailed: {error}", file=sys.stderr)
        return 1
