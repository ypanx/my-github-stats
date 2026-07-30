"""GraphQL documents and the search strings that parameterize them.

No search here is ever scoped to a repository. Every metric covers the whole
account, and a repository filter would silently narrow the entire dataset.
Commit history is necessarily requested one repository at a time because the
schema offers no account-wide equivalent, but that is enumeration rather than
filtering, and the list of repositories comes from the API at runtime.
"""

from __future__ import annotations

import datetime as dt

VIEWER = "query { viewer { login id } }"

CONTRIBUTIONS = """
query($from: DateTime!, $to: DateTime!, $maxRepos: Int!) {
  rateLimit { cost remaining }
  viewer {
    login
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      commitContributionsByRepository(maxRepositories: $maxRepos) {
        repository { nameWithOwner }
        contributions { totalCount }
      }
    }
  }
}
"""

COMMIT_HISTORY = """
query($owner: String!, $name: String!, $authorId: ID!,
      $since: GitTimestamp!, $until: GitTimestamp!, $page: Int!, $cursor: String) {
  rateLimit { cost remaining }
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      name
      target {
        ... on Commit {
          history(first: $page, author: {id: $authorId},
                  since: $since, until: $until, after: $cursor) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              committedDate
              additions
              deletions
              changedFilesIfAvailable
              parents { totalCount }
            }
          }
        }
      }
    }
  }
}
"""

AUTHORED_PULL_REQUESTS = """
query($q: String!, $page: Int!, $cursor: String) {
  rateLimit { cost remaining }
  search(query: $q, type: ISSUE, first: $page, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        createdAt
        mergedAt
        state
        repository { nameWithOwner }
      }
    }
  }
}
"""

#: Reviews carry an author filter that applies within the same query, so review
#: events arrive alongside the pull requests that hold them with no per-request
#: fan-out. Two details keep the cost down. GraphQL charges for the nodes a query
#: could return, so nesting multiplies: requesting a large page of reviews inside
#: a page of pull requests cost more than twenty times what a modest page does,
#: for identical results. And selecting only a count of inline comments, with no
#: page argument, removes a level of nesting entirely.
REVIEWED_PULL_REQUESTS = """
query($q: String!, $login: String!, $page: Int!, $reviewPage: Int!, $cursor: String) {
  rateLimit { cost remaining }
  search(query: $q, type: ISSUE, first: $page, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        author { login }
        repository { nameWithOwner }
        reviews(author: $login, first: $reviewPage) {
          totalCount
          pageInfo { hasNextPage }
          nodes { state submittedAt body author { login } comments { totalCount } }
        }
      }
    }
  }
}
"""

#: Unlike reviews, the comments connection has no author filter, so this returns
#: every comment on every matching pull request and the author filtering happens
#: locally. The waste is considerable and unavoidable.
COMMENTED_PULL_REQUESTS = """
query($q: String!, $page: Int!, $cursor: String) {
  rateLimit { cost remaining }
  search(query: $q, type: ISSUE, first: $page, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        author { login }
        repository { nameWithOwner }
        comments(first: 100) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { author { login } createdAt }
        }
      }
    }
  }
}
"""

#: A pull request with more comments than one page holds needs its own
#: pagination, or its later comments are invisible.
COMMENT_PAGE = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  rateLimit { cost remaining }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { author { login } createdAt }
      }
    }
  }
}
"""

_TOUCHED_FIELDS = {"reviewed": "reviewed-by", "commented": "commenter"}


def authored_query(login: str, since: dt.date, until: dt.date) -> str:
    """Search for pull requests opened by the account.

    Creation dates never change, so a two-sided range is always safe here.
    """
    return f"author:{login} is:pr created:{since}..{until}"


def touched_query(kind: str, login: str, since: dt.date,
                  created_from: dt.date | None, created_to: dt.date | None) -> str:
    """Search for pull requests the account reviewed or commented on.

    The update bound is always open-ended. A two-sided update range is only safe
    when it ends at the present moment: otherwise a pull request whose in-window
    activity was followed by later changes falls outside the upper bound and
    disappears completely. Events are filtered afterwards by their own
    timestamps, which is correct whatever the window.

    Slicing is therefore by creation date, which never changes and so slices
    disjointly. Passing no lower bound produces the leading slice that catches
    pull requests created before the window but touched inside it.
    """
    field = _TOUCHED_FIELDS[kind]
    query = f"{field}:{login} is:pr updated:>={since}"
    if created_from is None and created_to is not None:
        return f"{query} created:<{created_to}"
    if created_from is not None and created_to is not None:
        return f"{query} created:{created_from}..{created_to}"
    if created_from is not None:
        return f"{query} created:>={created_from}"
    return query
