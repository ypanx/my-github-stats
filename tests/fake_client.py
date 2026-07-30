"""A stand-in for the GitHub client, so collection can be tested offline.

The real client is exercised separately. What this exists for is the layer above
it: the collection paths hold most of the checks that catch a silently wrong
result, and none of them can be reached without something to answer a query.
"""

from __future__ import annotations

from typing import Any


class FakeClient:
    """Answers queries from a prepared script.

    GraphQL responses are queued per label prefix, so a test names the call it is
    answering rather than relying on call order. REST responses are queued in
    order, since the file fetch makes many identical calls.
    """

    def __init__(self) -> None:
        self.graphql_responses: dict[str, list[Any]] = {}
        self.rest_responses: list[Any] = []
        self.graphql_calls: list[tuple[str, dict[str, Any]]] = []
        self.rest_calls_made: list[tuple[str, dict[str, Any] | None]] = []
        self.graphql_points = 0
        self.rest_calls = 0
        self.retries = 0

    def queue_graphql(self, label_prefix: str, *responses: Any) -> None:
        self.graphql_responses.setdefault(label_prefix, []).extend(responses)

    def queue_rest(self, *responses: Any) -> None:
        self.rest_responses.extend(responses)

    def graphql(self, query: str, variables: dict[str, Any], label: str) -> Any:
        self.graphql_calls.append((label, variables))
        for prefix, queued in self.graphql_responses.items():
            if label.startswith(prefix) and queued:
                return queued.pop(0)
        raise AssertionError(f"no queued response for {label!r}")

    def rest(self, path: str, params: dict[str, Any] | None = None,
             label: str = "") -> Any:
        self.rest_calls_made.append((path, params))
        self.rest_calls += 1
        if not self.rest_responses:
            raise AssertionError(f"no queued response for {path!r}")
        return self.rest_responses.pop(0)

    def budget(self) -> str:
        return "fake"


def viewer_response(login: str = "someone") -> dict:
    return {"viewer": {"login": login, "id": "MDQ6VXNlcjE="}}


def contributions_response(repositories: list[tuple[str, int]],
                           total: int | None = None) -> dict:
    return {"viewer": {"contributionsCollection": {
        "totalCommitContributions": total if total is not None
        else sum(count for _, count in repositories),
        "commitContributionsByRepository": [
            {"repository": {"nameWithOwner": name},
             "contributions": {"totalCount": count}}
            for name, count in repositories],
    }}}


def history_response(commits: list[dict], has_next: bool = False,
                     cursor: str | None = None, branch: str = "main") -> dict:
    return {"repository": {"defaultBranchRef": {"name": branch, "target": {
        "history": {"totalCount": len(commits),
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": commits}}}}}


def commit_node(oid: str, *, parents: int = 1, additions: int = 10,
                deletions: int = 5, date: str = "2026-01-15T10:00:00Z") -> dict:
    return {"oid": oid, "committedDate": date, "additions": additions,
            "deletions": deletions, "changedFilesIfAvailable": 1,
            "parents": {"totalCount": parents}}


def commit_files_response(filenames: list[str], *, additions: int = 1,
                          deletions: int = 0, total: int | None = None,
                          patch: bool = True) -> dict:
    files = []
    for filename in filenames:
        entry = {"filename": filename, "additions": additions,
                 "deletions": deletions, "status": "modified"}
        if patch:
            entry["patch"] = "@@"
        files.append(entry)
    return {"stats": {"total": total if total is not None
                      else len(filenames) * (additions + deletions)},
            "files": files}


def search_response(nodes: list[dict], *, matches: int | None = None,
                    has_next: bool = False, cursor: str | None = None) -> dict:
    return {"search": {"issueCount": matches if matches is not None else len(nodes),
                       "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                       "nodes": nodes}}


def pull_request_node(number: int, *, repo: str = "owner/repo",
                      created: str = "2026-01-15T10:00:00Z",
                      merged: str | None = None, state: str = "MERGED",
                      author: str = "someone") -> dict:
    return {"number": number, "createdAt": created, "mergedAt": merged,
            "state": state, "author": {"login": author},
            "repository": {"nameWithOwner": repo}}


def reviewed_node(number: int, reviews: list[dict], *, repo: str = "owner/repo",
                  author: str = "colleague", has_next: bool = False) -> dict:
    return {"number": number, "author": {"login": author},
            "repository": {"nameWithOwner": repo},
            "reviews": {"totalCount": len(reviews),
                        "pageInfo": {"hasNextPage": has_next},
                        "nodes": reviews}}


def review(state: str = "APPROVED", *, submitted: str = "2026-01-15T10:00:00Z",
           body: str = "", inline: int = 0, author: str = "someone") -> dict:
    return {"state": state, "submittedAt": submitted, "body": body,
            "author": {"login": author}, "comments": {"totalCount": inline}}


def commented_node(number: int, comments: list[dict], *, repo: str = "owner/repo",
                   author: str = "colleague", has_next: bool = False,
                   cursor: str | None = None) -> dict:
    return {"number": number, "author": {"login": author},
            "repository": {"nameWithOwner": repo},
            "comments": {"totalCount": len(comments),
                         "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                         "nodes": comments}}


def comment(*, author: str = "someone", created: str = "2026-01-15T10:00:00Z") -> dict:
    return {"author": {"login": author}, "createdAt": created}
