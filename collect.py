"""Collecting a window from the GitHub API and writing the two output files.

One command produces both files. Both checks run before either file is touched,
so a failed run leaves the previous pair exactly as it found them.

Build logs for a public repository are public and permanent, so the default output
is counts. `--verbose` adds the account login and the per-repository breakdown.
Error messages are not gated by it and go through `scrub` instead, because an
exception carries whatever the library that raised it put in the text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import queries
from client import (
    GitHubClient,
    GitHubError,
    scrub,
    short_hash,
    token_from_environment,
)
from constants import (
    HISTORY_PAGE,
    LOCAL_PATH,
    MAX_REPOSITORIES,
    PAGE_SIZE,
    REST_FILE_CEILING,
    REST_FILE_PAGE,
    REST_WORKERS,
    REVIEW_PAGE,
    REVIEW_STATES,
    SEARCH_CAP,
    STATS_PATH,
)
from utils import (
    CollectError,
    build_commit_records,
    build_payload,
    check_day_over_day,
    check_non_zero,
    chunk_window,
    created_partitions,
    in_window,
    parse_timestamp,
    require_window_fits,
    resolve_window,
    strip_attribution,
    total_lines,
    utc_day,
    window_bounds,
    write_atomic,
)

UTC = dt.timezone.utc


def is_merge_commit(commit: dict[str, Any]) -> bool:
    """Whether a commit has more than one parent.

    A merge commit's diff against its first parent is the entire merged change,
    so counting it duplicates the branch commits that history already returned. A
    merge is also attributed to whoever performed it, so counting merges would
    credit the merger with the author's work.

    A commit arriving without parent information is an error rather than a
    non-merge, since assuming the latter would silently include it.
    """
    parents = commit.get("parents")
    if not isinstance(parents, dict) or "totalCount" not in parents:
        raise CollectError(
            f"commit {short_hash(commit.get('oid'))} arrived without parent "
            "information")
    return int(parents["totalCount"]) > 1


class Collector:
    """Fetches every record for one window.

    Each method returns plain records and raises on anything it cannot honestly
    report. Assembling them into the published shape happens in `utils`.
    """

    def __init__(self, client: GitHubClient, since: dt.date, until: dt.date, *,
                 verbose: bool = False, log=print) -> None:
        self.client = client
        self.since = since
        self.until = until
        self.verbose = verbose
        self.log = log
        self.viewer: dict[str, str] = {}
        self.notes: list[str] = []

    @property
    def login(self) -> str:
        return self.viewer["login"]

    # -- identity ---------------------------------------------------------- #

    def resolve_viewer(self) -> dict[str, str]:
        """Look up the account being measured.

        The login comes from the API rather than from configuration, so no
        committed file needs to name the account. The node identifier is what
        commit history requires to filter by author.
        """
        self.viewer = self.client.graphql(queries.VIEWER, {}, "viewer")["viewer"]
        self.log("  account resolved from the API"
                 + (f" ({self.login})" if self.verbose else ""))
        return self.viewer

    # -- repositories ------------------------------------------------------- #

    def enumerate_repositories(self) -> dict[str, Any]:
        """The repositories the account has commit contributions in.

        The account-wide contribution total comes back alongside the list because
        commit history can only be requested one repository at a time, and a run
        that read no history at all would otherwise look like a quiet window
        rather than like a failure. The total is what a later check compares
        against to tell those two apart.
        """
        start, end = window_bounds(self.since, self.until)
        collection = self.client.graphql(
            queries.CONTRIBUTIONS,
            {"from": start, "to": end, "maxRepos": MAX_REPOSITORIES},
            "contributions")["viewer"]["contributionsCollection"]

        entries = collection["commitContributionsByRepository"]
        if len(entries) == MAX_REPOSITORIES:
            raise CollectError(
                f"exactly {MAX_REPOSITORIES} repositories were returned, which "
                "cannot be told apart from the list having been truncated, and "
                "there is no pagination behind it")

        repositories = [
            {"name_with_owner": entry["repository"]["nameWithOwner"],
             "contributions": entry["contributions"]["totalCount"]}
            for entry in sorted(entries,
                                key=lambda e: -e["contributions"]["totalCount"])
        ]
        total = collection["totalCommitContributions"]
        self.log(f"  {len(repositories)} repositories, {total} commit "
                 "contributions")
        return {"repositories": repositories, "total_commit_contributions": total}

    # -- commits ------------------------------------------------------------ #

    def walk_commits(self, repositories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Commits authored by the account on each repository's default branch.

        Keyed by hash while collecting, because a commit reachable from two
        repositories would otherwise be counted twice in the line totals while
        the per-commit file fetch, which is keyed by hash, counted it once.
        """
        since, until = window_bounds(self.since, self.until)
        commits: dict[str, dict[str, Any]] = {}
        merges_skipped = 0
        duplicates = 0

        for entry in repositories:
            owner, _, name = entry["name_with_owner"].partition("/")
            cursor: str | None = None
            kept = 0
            while True:
                data = self.client.graphql(queries.COMMIT_HISTORY, {
                    "owner": owner, "name": name, "authorId": self.viewer["id"],
                    "since": since, "until": until,
                    "page": HISTORY_PAGE, "cursor": cursor}, "commit history")
                ref = data["repository"].get("defaultBranchRef")
                if not ref or not ref.get("target"):
                    break
                history = ref["target"]["history"]
                for node in history["nodes"]:
                    if is_merge_commit(node):
                        merges_skipped += 1
                        continue
                    if node["oid"] in commits:
                        duplicates += 1
                        continue
                    kept += 1
                    commits[node["oid"]] = dict(node, repo=entry["name_with_owner"])
                if not history["pageInfo"]["hasNextPage"]:
                    break
                cursor = history["pageInfo"]["endCursor"]

            if self.verbose:
                self.log(f"    {entry['name_with_owner']:<45} {kept:>5} commits "
                         f"of {entry['contributions']} contributions")

        summary = f"  {len(commits)} commits authored, {merges_skipped} merges skipped"
        if duplicates:
            summary += f", {duplicates} reachable from more than one repository"
            self.notes.append(
                f"{duplicates} commit(s) reachable from more than one repository, "
                "counted once")
        self.log(summary)
        self.notes.append(f"merge commits skipped: {merges_skipped}")
        return list(commits.values())

    # -- per-commit files --------------------------------------------------- #

    def commit_files(self, repo: str, sha: str) -> dict[str, Any]:
        """Every file record for one commit.

        Pages until a short page arrives, which is the only proof that no further
        files exist. The page size is not a limit on how many files a commit may
        have, and treating it as one undercounts.
        """
        owner, _, name = repo.partition("/")
        records: list[dict[str, Any]] = []
        undiffable = 0
        page = 1
        complete = False

        while True:
            payload = self.client.rest(
                f"/repos/{owner}/{name}/commits/{sha}",
                params={"page": page, "per_page": REST_FILE_PAGE},
                label="commit files")
            files = payload.get("files") or []
            for entry in files:
                additions = entry.get("additions", 0)
                deletions = entry.get("deletions", 0)
                # No line counts and no patch, on a status that must have changed
                # something, means GitHub declined to diff the file rather than
                # that the file is empty. Its lines cannot be recovered, so they
                # are counted here and reported as a note.
                if (additions + deletions == 0 and "patch" not in entry
                        and entry.get("status") != "renamed"):
                    undiffable += 1
                records.append({"path": entry["filename"],
                                "additions": additions, "deletions": deletions})

            if len(files) < REST_FILE_PAGE:
                complete = True
                break
            page += 1
            # Strictly greater, because a commit with exactly the ceiling many
            # files fills its last page exactly and needs one more empty page to
            # prove it was read to the end.
            if len(records) > REST_FILE_CEILING:
                break

        return {"records": records, "complete": complete, "undiffable": undiffable}

    def fetch_all_files(self, commits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Fetch file records for every commit, several at a time.

        A commit that still fails after retries fails the whole run. Skipping it
        would undercount the line totals in a way no guard could detect.
        """
        started = time.monotonic()
        results: dict[str, dict[str, Any]] = {}
        failures: list[str] = []

        with ThreadPoolExecutor(max_workers=REST_WORKERS) as pool:
            pending = {pool.submit(self.commit_files, commit["repo"], commit["oid"]): commit
                       for commit in commits}
            for done, future in enumerate(as_completed(pending), start=1):
                commit = pending[future]
                try:
                    results[commit["oid"]] = future.result()
                except Exception as error:                       # noqa: BLE001
                    failures.append(f"{short_hash(commit['oid'])}: {scrub(str(error))}")
                if done % 100 == 0:
                    self.log(f"    {done}/{len(commits)} commits read")

        if failures:
            raise CollectError(
                f"{len(failures)} commit(s) could not be read after retries, so "
                f"the run would undercount: {failures[:3]}")

        # A commit above the ceiling is a limit of the API rather than a fault
        # here, and no further pages exist to fetch. Failing would block every run
        # until that commit left the window a year later, so this warns instead.
        incomplete = [sha for sha, result in results.items() if not result["complete"]]
        if incomplete:
            self.notes.append(
                f"{len(incomplete)} commit(s) exceeded {REST_FILE_CEILING} file "
                "records, so their lines are undercounted")

        # Lines GitHub would not diff. Reported because nothing else can see them:
        # they are simply absent from the totals, and a growing number of them
        # shrinks the line count with no other signal.
        if undiffable := sum(result["undiffable"] for result in results.values()):
            self.notes.append(
                f"{undiffable} file record(s) had no diff available, so their lines "
                "are missing from the totals")

        total = sum(len(result["records"]) for result in results.values())
        self.log(f"  {total} file records in {time.monotonic() - started:.0f}s")
        return results

    # -- searches ----------------------------------------------------------- #

    def search(self, document: str, query: str, label: str,
               extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Page through one search, refusing a result set that cannot be read.

        The reported match count is not subject to the result cap even though the
        results are, which is what makes it a trustworthy detector rather than a
        self-fulfilling one.
        """
        cursor: str | None = None
        nodes: list[dict[str, Any]] = []
        matches = 0

        while True:
            variables = {"q": query, "page": PAGE_SIZE, "cursor": cursor}
            variables.update(extra or {})
            result = self.client.graphql(document, variables, label)["search"]
            matches = result["issueCount"]
            if matches > SEARCH_CAP:
                raise CollectError(
                    f"{label}: {matches} matches exceeds the {SEARCH_CAP} that "
                    "can be read, so results would be silently truncated. "
                    "Shortening the slice length subdivides the slices inside "
                    "the window, but not the leading slice, which is open at the "
                    "start and needs an explicit earlier boundary.")
            nodes.extend(node for node in result["nodes"] if node)
            if not result["pageInfo"]["hasNextPage"]:
                break
            cursor = result["pageInfo"]["endCursor"]

        if len(nodes) != matches:
            raise CollectError(
                f"{label}: read {len(nodes)} of {matches} matches, so pagination "
                "stopped early")
        return nodes

    def search_touched(self, kind: str, document: str,
                       extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Every pull request the account reviewed or commented on."""
        seen: dict[tuple[str, int], dict[str, Any]] = {}
        for lower, upper in created_partitions(self.since, self.until):
            query = queries.touched_query(kind, self.login, self.since, lower, upper)
            nodes = self.search(document, query, f"{kind} [{lower}..{upper}]", extra)
            for node in nodes:
                seen[(node["repository"]["nameWithOwner"], node["number"])] = node
            self.log(f"    created {str(lower or 'earlier'):<12}"
                     f"..{str(upper or 'end'):<12} {len(nodes):>5} pull requests")
        self.log(f"  {len(seen)} distinct pull requests {kind}")
        return list(seen.values())

    def collect_pull_requests(self) -> list[dict[str, Any]]:
        """Pull requests opened by the account inside the window.

        The time to merge is recorded as a duration in hours rather than as
        timestamps. Dates alone cannot express it, because a large share of pull
        requests are merged the same day they are opened and a difference of
        dates reads as zero for every one of them. A duration says how long
        something took without revealing when the work happened.
        """
        seen: dict[tuple[str, int], dict[str, Any]] = {}
        for start, end in chunk_window(self.since, self.until):
            query = queries.authored_query(self.login, start, end)
            for node in self.search(queries.AUTHORED_PULL_REQUESTS, query,
                                    f"authored [{start}..{end}]"):
                seen[(node["repository"]["nameWithOwner"], node["number"])] = node

        pull_requests = []
        for node in seen.values():
            if not in_window(node["createdAt"], self.since, self.until):
                continue
            merged_at = node.get("mergedAt")
            pull_requests.append({
                "created": utc_day(node["createdAt"]).isoformat(),
                "merged": utc_day(merged_at).isoformat() if merged_at else None,
                "state": node["state"],
                "cycle_hours": round(
                    (parse_timestamp(merged_at)
                     - parse_timestamp(node["createdAt"])).total_seconds() / 3600, 2
                ) if merged_at else None,
                "repo": node["repository"]["nameWithOwner"],
            })

        merged = sum(1 for pr in pull_requests if pr["merged"])
        self.log(f"  {len(pull_requests)} pull requests opened, {merged} merged")
        return pull_requests

    def collect_review_envelopes(self) -> list[dict[str, Any]]:
        """Every review the account submitted inside the window.

        Reviews on the account's own pull requests are kept and flagged rather
        than dropped. They are not reviews given — counting them would mean being
        reviewed more made one look more active — but they do carry inline
        comments that are genuine work, so the distinction is recorded and the
        decision left to whatever renders the result.
        """
        pull_requests = self.search_touched(
            "reviewed", queries.REVIEWED_PULL_REQUESTS,
            {"login": self.login, "reviewPage": REVIEW_PAGE})

        envelopes: list[dict[str, Any]] = []
        truncated = 0
        deepest = 0
        foreign: Counter[str] = Counter()

        for pull_request in pull_requests:
            reviews = pull_request.get("reviews") or {}
            deepest = max(deepest, reviews.get("totalCount", 0))
            if (reviews.get("pageInfo") or {}).get("hasNextPage"):
                truncated += 1
            author = (pull_request.get("author") or {}).get("login")

            for review in reviews.get("nodes") or []:
                reviewer = (review.get("author") or {}).get("login")
                if reviewer != self.login:
                    foreign[reviewer or "unknown"] += 1
                # A pending review is an unsubmitted draft, visible only to its
                # author and carrying no submission time.
                if review["state"] == "PENDING":
                    continue
                # Checked before the window filter, so an unfamiliar state is
                # caught on every review the API returns rather than only on
                # those that happen to fall inside the window.
                if review["state"] not in REVIEW_STATES:
                    raise CollectError(f"unexpected review state {review['state']!r}")
                if not in_window(review.get("submittedAt"), self.since, self.until):
                    continue
                envelopes.append({
                    "date": utc_day(review["submittedAt"]).isoformat(),
                    "state": review["state"],
                    "inline_count": (review.get("comments") or {}).get("totalCount", 0),
                    "has_body": bool((review.get("body") or "").strip()),
                    "on_own_pr": author == self.login,
                    "repo": pull_request["repository"]["nameWithOwner"],
                })

        if foreign:
            raise CollectError(
                f"the review author filter returned {sum(foreign.values())} "
                "review(s) by other people, so it cannot be trusted and each "
                "pull request would need to be fetched separately")
        if truncated:
            raise CollectError(
                f"{truncated} pull request(s) carried more than {REVIEW_PAGE} "
                "reviews by this account, so reviews were truncated")
        if deepest > REVIEW_PAGE * 0.8:
            self.notes.append(
                f"the busiest pull request carried {deepest} reviews against a "
                f"page of {REVIEW_PAGE}, so the headroom is thin")

        given = sum(1 for envelope in envelopes if not envelope["on_own_pr"])
        self.log(f"  {len(envelopes)} review envelopes: {given} given, "
                 f"{len(envelopes) - given} on the account's own pull requests")
        return envelopes

    def collect_comments(self, envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Conversational comments from the API, plus inline comments per review.

        The two kinds are distinct object types and cannot overlap. Inline
        comments exist only as a count against each review, so one record is
        emitted per counted comment and dated to the review that carried it,
        which is correct because the two are submitted together.

        Conversational comments run slightly low at the trailing edge, which is
        accepted: the search index that finds them lags, and a later run picks
        them up while their timestamps are still inside the window.
        """
        pull_requests = self.search_touched("commented", queries.COMMENTED_PULL_REQUESTS)
        records: list[dict[str, Any]] = []
        fetched = 0
        needs_paging: list[tuple[dict[str, Any], str]] = []

        def absorb(pull_request: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
            nonlocal fetched
            author = (pull_request.get("author") or {}).get("login")
            for comment in nodes:
                fetched += 1
                if (comment.get("author") or {}).get("login") != self.login:
                    continue
                if not in_window(comment.get("createdAt"), self.since, self.until):
                    continue
                records.append({
                    "date": utc_day(comment["createdAt"]).isoformat(),
                    "kind": "conversational",
                    "on_own_pr": author == self.login,
                    "repo": pull_request["repository"]["nameWithOwner"],
                })

        for pull_request in pull_requests:
            comments = pull_request.get("comments") or {}
            absorb(pull_request, comments.get("nodes") or [])
            if (comments.get("pageInfo") or {}).get("hasNextPage"):
                needs_paging.append((pull_request, comments["pageInfo"]["endCursor"]))

        if needs_paging:
            self.log(f"    reading further comment pages for "
                     f"{len(needs_paging)} pull request(s)")
        for pull_request, cursor in needs_paging:
            owner, _, name = pull_request["repository"]["nameWithOwner"].partition("/")
            while cursor:
                page = self.client.graphql(queries.COMMENT_PAGE, {
                    "owner": owner, "name": name, "number": pull_request["number"],
                    "cursor": cursor},
                    "comment page")["repository"]["pullRequest"]["comments"]
                absorb(pull_request, page["nodes"] or [])
                cursor = (page["pageInfo"]["endCursor"]
                          if page["pageInfo"]["hasNextPage"] else None)

        conversational = len(records)
        for envelope in envelopes:
            for _ in range(envelope["inline_count"]):
                records.append({"date": envelope["date"], "kind": "inline",
                                "on_own_pr": envelope["on_own_pr"],
                                "repo": envelope["repo"]})

        self.log(f"  {conversational} conversational comments from {fetched} read, "
                 f"{len(records) - conversational} inline")
        return records


# --------------------------------------------------------------------------- #
# Orchestration and the command line
# --------------------------------------------------------------------------- #

def collect(args: argparse.Namespace) -> int:
    """Fetch a window, check it, and write both files if it holds up."""
    since, until = resolve_window(args.since, args.until, args.horizon)
    require_window_fits(since, until)

    client = GitHubClient(token_from_environment())
    collector = Collector(client, since, until, verbose=args.verbose)

    dates = (until - since).days + 1
    print(f"collecting {since} to {until}, {dates} dates")
    if not args.verbose:
        print("  the account login and per-repository counts are hidden; "
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

    problems = check_non_zero(published, enumerated["total_commit_contributions"])
    drop_problems, skipped = check_day_over_day(published, STATS_PATH)
    problems += drop_problems

    print("\nchecks")
    print(f"  commit contributions  {enumerated['total_commit_contributions']:,}")
    print(f"  commits and lines     {len(published['commits']):,} and "
          f"{total_lines(published['commits']):,}")
    print(f"  against previous run  "
          f"{'skipped, ' + skipped if skipped else 'compared'}")
    for note in collector.notes:
        print(f"  note                  {note}")

    if problems:
        # Flushed first: stdout is block-buffered when piped, as it is in a
        # workflow log, so without this the failure appears above the run it
        # describes.
        sys.stdout.flush()
        print("\nchecks failed, so nothing was written and the previous files are "
              "untouched", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("  all checks passed")

    write_atomic(STATS_PATH, published)
    write_atomic(LOCAL_PATH, attributed)
    print(f"\nwrote {STATS_PATH} and {LOCAL_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="collect.py",
        description="Collect GitHub review and contribution statistics.")
    parser.add_argument("--since", type=dt.date.fromisoformat,
                        help="first date of the window, inclusive")
    parser.add_argument("--until", type=dt.date.fromisoformat,
                        help="last date of the window, inclusive")
    parser.add_argument("--horizon", type=int, default=365,
                        help="number of trailing dates to cover when no explicit "
                             "window is given (default: 365)")
    parser.add_argument("--verbose", action="store_true",
                        help="show the account login and the per-repository "
                             "breakdown; never use this where logs are public")
    args = parser.parse_args(argv)

    if bool(args.since) != bool(args.until):
        parser.error("--since and --until must be given together")

    try:
        return collect(args)
    except (CollectError, GitHubError) as error:
        print(f"\nfailed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
