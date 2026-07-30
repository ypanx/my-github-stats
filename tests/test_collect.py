"""The collection paths, driven by a fake client.

This module holds most of the checks that catch a silently wrong result. Each
test below corresponds to a single-line change that would leave every other test
passing while producing figures that are plainly wrong — a commit truncated at
one page, another person's comments counted as one's own, unsubmitted drafts
counted as reviews. Those are exactly the mistakes no total would reveal.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from client import scrub
from collect import Collector, is_merge_commit
from constants import (
    MAX_REPOSITORIES,
    REST_FILE_CEILING,
    REST_FILE_PAGE,
    REVIEW_PAGE,
    SEARCH_CAP,
)
from utils import CollectError

from conftest import FIXTURES
from fake_client import (
    FakeClient,
    comment,
    commented_node,
    commit_files_response,
    commit_node,
    contributions_response,
    history_response,
    pull_request_node,
    review,
    reviewed_node,
    search_response,
    viewer_response,
)

SINCE, UNTIL = dt.date(2026, 1, 1), dt.date(2026, 1, 31)
ME = "someone"


def make_collector(client: FakeClient, **kwargs) -> Collector:
    collector = Collector(client, SINCE, UNTIL, log=lambda *_: None, **kwargs)
    collector.viewer = {"login": ME, "id": "MDQ6VXNlcjE="}
    return collector


class TestViewer:
    def test_the_account_is_read_from_the_api(self):
        """Nothing committed names the account, so it has to be resolved."""
        client = FakeClient()
        client.queue_graphql("viewer", viewer_response("resolved"))
        collector = Collector(client, SINCE, UNTIL, log=lambda *_: None)
        assert collector.resolve_viewer()["login"] == "resolved"
        assert collector.login == "resolved"

    def test_the_login_is_not_logged_unless_asked_for(self):
        lines: list[str] = []
        client = FakeClient()
        client.queue_graphql("viewer", viewer_response("private-login"))
        Collector(client, SINCE, UNTIL, log=lines.append).resolve_viewer()
        assert not any("private-login" in line for line in lines)


class TestRepositories:
    def test_repositories_and_the_contribution_total_are_returned(self):
        client = FakeClient()
        client.queue_graphql("contributions", contributions_response(
            [("owner/big", 400), ("owner/small", 5)]))
        result = make_collector(client).enumerate_repositories()
        assert [r["name_with_owner"] for r in result["repositories"]] == \
            ["owner/big", "owner/small"]
        assert result["total_commit_contributions"] == 405

    def test_nothing_beyond_repositories_and_the_total_is_returned(self):
        """The published heatmap is derived from the records this project
        collects, so no separate per-date series is carried out of here to be
        mistaken for one."""
        client = FakeClient()
        client.queue_graphql("contributions", contributions_response(
            [("owner/repo", 1)]))
        result = make_collector(client).enumerate_repositories()
        assert set(result) == {"repositories", "total_commit_contributions"}

    def test_repositories_come_back_busiest_first(self):
        client = FakeClient()
        client.queue_graphql("contributions", contributions_response(
            [("owner/small", 5), ("owner/big", 400)]))
        result = make_collector(client).enumerate_repositories()
        assert result["repositories"][0]["contributions"] == 400

    def test_the_maximum_number_of_repositories_is_refused(self):
        """There is no pagination behind the limit, so receiving exactly that many
        cannot be told apart from the list having been truncated."""
        client = FakeClient()
        client.queue_graphql("contributions", contributions_response(
            [(f"owner/repo{i}", 1) for i in range(MAX_REPOSITORIES)]))
        with pytest.raises(CollectError, match="truncated"):
            make_collector(client).enumerate_repositories()


class TestCommitHistory:
    def test_commits_are_collected_with_their_repository(self):
        client = FakeClient()
        client.queue_graphql("commit history",
                             history_response([commit_node("aaa"), commit_node("bbb")]))
        commits = make_collector(client).walk_commits(
            [{"name_with_owner": "owner/repo", "contributions": 2}])
        assert [c["oid"] for c in commits] == ["aaa", "bbb"]
        assert all(c["repo"] == "owner/repo" for c in commits)

    def test_merge_commits_are_left_out(self):
        """A merge's diff is the whole merged change, so counting it duplicates
        the branch commits history already returned."""
        client = FakeClient()
        client.queue_graphql("commit history", history_response([
            commit_node("aaa"), commit_node("mmm", parents=2), commit_node("bbb")]))
        collector = make_collector(client)
        commits = collector.walk_commits(
            [{"name_with_owner": "owner/repo", "contributions": 3}])
        assert [c["oid"] for c in commits] == ["aaa", "bbb"]
        assert any("merge commits skipped: 1" in note for note in collector.notes)

    def test_history_is_paged_to_the_end(self):
        client = FakeClient()
        client.queue_graphql(
            "commit history",
            history_response([commit_node("aaa")], has_next=True, cursor="c1"),
            history_response([commit_node("bbb")]))
        commits = make_collector(client).walk_commits(
            [{"name_with_owner": "owner/repo", "contributions": 2}])
        assert len(commits) == 2
        assert client.graphql_calls[1][1]["cursor"] == "c1"

    def test_a_commit_reachable_from_two_repositories_is_counted_once(self):
        """Otherwise it is counted twice in the line totals while the file fetch,
        which is keyed by hash, counts it once."""
        client = FakeClient()
        client.queue_graphql("commit history",
                             history_response([commit_node("shared")]),
                             history_response([commit_node("shared")]))
        collector = make_collector(client)
        commits = collector.walk_commits([
            {"name_with_owner": "owner/fork", "contributions": 1},
            {"name_with_owner": "owner/upstream", "contributions": 1}])
        assert len(commits) == 1
        assert any("more than one repository" in note for note in collector.notes)

    def test_a_repository_without_a_default_branch_is_skipped(self):
        client = FakeClient()
        client.queue_graphql("commit history", {"repository": {"defaultBranchRef": None}})
        assert make_collector(client).walk_commits(
            [{"name_with_owner": "owner/empty", "contributions": 0}]) == []

    def test_history_is_requested_over_the_window(self):
        client = FakeClient()
        client.queue_graphql("commit history", history_response([]))
        make_collector(client).walk_commits(
            [{"name_with_owner": "owner/repo", "contributions": 0}])
        variables = client.graphql_calls[0][1]
        assert variables["since"].startswith("2026-01-01")
        assert variables["until"].startswith("2026-01-31")


class TestCommitFiles:
    def test_a_single_short_page_is_complete(self):
        client = FakeClient()
        client.queue_rest(commit_files_response(["a.py", "b.py"]))
        result = make_collector(client).commit_files("owner/repo", "aaa")
        assert len(result["records"]) == 2
        assert result["complete"] is True

    def test_a_full_page_is_followed_by_another(self):
        """The page size is not a limit on how many files a commit may have, and
        treating it as one truncates every large commit."""
        client = FakeClient()
        client.queue_rest(
            commit_files_response([f"f{i}.py" for i in range(REST_FILE_PAGE)]),
            commit_files_response(["last.py"]))
        result = make_collector(client).commit_files("owner/repo", "aaa")
        assert len(result["records"]) == REST_FILE_PAGE + 1
        assert result["complete"] is True
        assert client.rest_calls_made[1][1]["page"] == 2

    def test_an_empty_final_page_still_proves_completeness(self):
        client = FakeClient()
        client.queue_rest(
            commit_files_response([f"f{i}.py" for i in range(REST_FILE_PAGE)]),
            commit_files_response([]))
        result = make_collector(client).commit_files("owner/repo", "aaa")
        assert len(result["records"]) == REST_FILE_PAGE
        assert result["complete"] is True

    def test_a_commit_at_the_ceiling_is_read_to_the_end(self):
        """It fills its last page exactly, so it needs one more empty page to be
        proven complete rather than being abandoned as too large."""
        pages = REST_FILE_CEILING // REST_FILE_PAGE
        client = FakeClient()
        for _ in range(pages):
            client.queue_rest(commit_files_response(
                [f"f{i}.py" for i in range(REST_FILE_PAGE)]))
        client.queue_rest(commit_files_response([]))
        result = make_collector(client).commit_files("owner/repo", "aaa")
        assert len(result["records"]) == REST_FILE_CEILING
        assert result["complete"] is True

    def test_a_file_whose_diff_was_declined_is_counted(self):
        """No line counts and no patch, on a status that must have changed
        something, means the diff was declined rather than that the file is empty.
        Those lines are lost and have to be reported."""
        client = FakeClient()
        client.queue_rest({"stats": {"total": 500}, "files": [
            {"filename": "big.json", "additions": 0, "deletions": 0,
             "status": "modified"},
            {"filename": "a.py", "additions": 1, "deletions": 0,
             "status": "modified", "patch": "@@"}]})
        result = make_collector(client).commit_files("owner/repo", "aaa")
        assert result["undiffable"] == 1

    def test_a_pure_rename_is_not_mistaken_for_a_declined_diff(self):
        client = FakeClient()
        client.queue_rest({"stats": {"total": 0}, "files": [
            {"filename": "new.py", "additions": 0, "deletions": 0,
             "status": "renamed"}]})
        assert make_collector(client).commit_files("owner/repo", "aaa")["undiffable"] == 0


class TestFetchAllFiles:
    def test_every_commit_is_fetched(self):
        client = FakeClient()
        client.queue_rest(commit_files_response(["a.py"]),
                          commit_files_response(["b.py"]))
        commits = [{"oid": "aaa", "repo": "owner/repo"},
                   {"oid": "bbb", "repo": "owner/repo"}]
        results = make_collector(client).fetch_all_files(commits)
        assert set(results) == {"aaa", "bbb"}

    def test_a_commit_that_cannot_be_read_fails_the_run(self):
        """Skipping it would undercount the line totals with nothing to show it."""
        client = FakeClient()
        commits = [{"oid": "aaa", "repo": "owner/repo"}]
        with pytest.raises(CollectError, match="undercount"):
            make_collector(client).fetch_all_files(commits)

    def test_a_failure_message_carries_no_repository_or_full_hash(self):
        """These messages reach logs that may be public."""
        client = FakeClient()
        commits = [{"oid": "a5275c6d4c8f1e2b3a4d5e6f7a8b9c0d1e2f3a4b",
                    "repo": "acme-corp/secret-service"}]
        with pytest.raises(CollectError) as caught:
            make_collector(client).fetch_all_files(commits)
        message = str(caught.value)
        assert "secret-service" not in message
        assert "a5275c6d4c8f1e2b3a4d5e6f7a8b9c0d1e2f3a4b" not in message

    def test_a_bare_hash_in_an_error_body_is_trimmed_too(self):
        """The realistic shape, and the one the previous test does not reach: a 422
        body naming the commit. A bare hash has no slash, so the repository rules
        do not touch it and only the hash rule can."""
        message = scrub('{"message":"No commit found for SHA: '
                        'a5275c6d4c8f1e2b3a4d5e6f7a8b9c0d1e2f3a4b"}')
        assert "a5275c6d4c8f1e2b3a4d5e6f7a8b9c0d1e2f3a4b" not in message
        assert "a5275c6d" in message

    def test_a_commit_above_the_ceiling_warns_instead_of_failing(self):
        """No further pages exist to fetch, so failing would block every run until
        the commit left the window a year later."""
        pages = REST_FILE_CEILING // REST_FILE_PAGE + 1
        client = FakeClient()
        for _ in range(pages):
            client.queue_rest(commit_files_response(
                [f"f{i}.py" for i in range(REST_FILE_PAGE)]))
        collector = make_collector(client)
        collector.fetch_all_files([{"oid": "huge", "repo": "owner/repo"}])
        assert any("undercounted" in note for note in collector.notes)


class TestSearch:
    def test_pages_are_followed_to_the_end(self):
        client = FakeClient()
        client.queue_graphql(
            "search",
            search_response([{"number": 1}], matches=2, has_next=True, cursor="c1"),
            search_response([{"number": 2}], matches=2))
        nodes = make_collector(client).search("query", "q", "search")
        assert len(nodes) == 2

    def test_a_result_set_exactly_at_the_cap_is_read(self):
        """`PAGE_SIZE` divides `SEARCH_CAP` exactly, so the thousandth result is the
        last of a full page and is reachable. Refusing at the boundary would block a
        run whose results were entirely readable."""
        client = FakeClient()
        nodes = [{"number": n} for n in range(SEARCH_CAP)]
        client.queue_graphql("search", search_response(nodes, matches=SEARCH_CAP))
        assert len(make_collector(client).search("query", "q", "search")) == SEARCH_CAP

    def test_more_matches_than_can_be_read_is_refused(self):
        """The match count is not subject to the cap even though the results are,
        which is what makes it a trustworthy detector."""
        client = FakeClient()
        client.queue_graphql("search", search_response([], matches=SEARCH_CAP + 1))
        with pytest.raises(CollectError, match="truncated"):
            make_collector(client).search("query", "q", "search")

    def test_reading_fewer_results_than_matched_is_refused(self):
        client = FakeClient()
        client.queue_graphql("search", search_response([{"number": 1}], matches=5))
        with pytest.raises(CollectError, match="stopped early"):
            make_collector(client).search("query", "q", "search")

    def test_empty_nodes_are_discarded(self):
        client = FakeClient()
        client.queue_graphql("search", search_response([{"number": 1}, None], matches=1))
        assert len(make_collector(client).search("query", "q", "search")) == 1


class TestPullRequests:
    def test_they_are_recorded_with_a_duration_rather_than_timestamps(self):
        client = FakeClient()
        client.queue_graphql("authored", search_response([pull_request_node(
            1, created="2026-01-10T00:00:00Z", merged="2026-01-11T12:00:00Z")]))
        prs = make_collector(client).collect_pull_requests()
        assert prs[0]["created"] == "2026-01-10"
        assert prs[0]["merged"] == "2026-01-11"
        assert prs[0]["cycle_hours"] == 36.0

    def test_an_unmerged_pull_request_has_no_duration(self):
        client = FakeClient()
        client.queue_graphql("authored", search_response([pull_request_node(
            1, merged=None, state="OPEN")]))
        prs = make_collector(client).collect_pull_requests()
        assert prs[0]["merged"] is None
        assert prs[0]["cycle_hours"] is None

    def test_one_created_outside_the_window_is_dropped(self):
        """The search bounds the window, but the result is filtered on the event's
        own timestamp so that a boundary cannot admit anything extra."""
        client = FakeClient()
        client.queue_graphql("authored", search_response([
            pull_request_node(1, created="2026-01-10T00:00:00Z"),
            pull_request_node(2, created="2025-12-31T23:59:59Z")]))
        assert len(make_collector(client).collect_pull_requests()) == 1

    def test_the_same_pull_request_from_two_slices_is_counted_once(self):
        client = FakeClient()
        client.queue_graphql("authored", search_response([pull_request_node(7)]))
        prs = make_collector(client).collect_pull_requests()
        assert len(prs) == 1


class TestReviewEnvelopes:
    def queue(self, client: FakeClient, nodes: list[dict]) -> None:
        """Answer the first creation-date slice, then the rest with nothing."""
        client.queue_graphql("reviewed", search_response(nodes))
        for _ in range(5):
            client.queue_graphql("reviewed", search_response([]))

    def test_a_review_is_recorded_with_its_state_and_inline_count(self):
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [review("APPROVED", inline=3,
                                                    body="looks good")])])
        envelopes = make_collector(client).collect_review_envelopes()
        assert envelopes[0]["state"] == "APPROVED"
        assert envelopes[0]["inline_count"] == 3
        assert envelopes[0]["has_body"] is True
        assert envelopes[0]["on_own_pr"] is False

    def test_an_unsubmitted_draft_is_not_a_review(self):
        """A pending review is visible only to its author and carries no
        submission time."""
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [
            {"state": "PENDING", "submittedAt": None, "body": "",
             "author": {"login": ME}, "comments": {"totalCount": 0}},
            review("APPROVED")])])
        envelopes = make_collector(client).collect_review_envelopes()
        assert [e["state"] for e in envelopes] == ["APPROVED"]

    def test_an_unfamiliar_state_stops_the_run(self):
        """Schema drift would otherwise pass unnoticed into the output."""
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [review("ESCALATED")])])
        with pytest.raises(CollectError, match="unexpected review state"):
            make_collector(client).collect_review_envelopes()

    def test_an_unfamiliar_state_is_caught_outside_the_window_too(self):
        """The check is a statement about the schema, so it must not depend on
        which reviews happen to fall inside the window."""
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [
            review("ESCALATED", submitted="2020-01-01T00:00:00Z")])])
        with pytest.raises(CollectError, match="unexpected review state"):
            make_collector(client).collect_review_envelopes()

    def test_a_review_submitted_outside_the_window_is_dropped(self):
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [
            review("APPROVED", submitted="2026-01-10T00:00:00Z"),
            review("APPROVED", submitted="2025-06-01T00:00:00Z")])])
        assert len(make_collector(client).collect_review_envelopes()) == 1

    def test_a_review_on_ones_own_pull_request_is_flagged_not_dropped(self):
        """It is not a review given, but its inline comments are real work, so the
        distinction is recorded and the decision left to the renderer."""
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [review("COMMENTED", inline=2)],
                                         author=ME)])
        envelopes = make_collector(client).collect_review_envelopes()
        assert envelopes[0]["on_own_pr"] is True
        assert envelopes[0]["inline_count"] == 2

    def test_a_whitespace_only_body_counts_as_no_body(self):
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [review("COMMENTED", body="   \n\t ")])])
        assert make_collector(client).collect_review_envelopes()[0]["has_body"] is False

    def test_another_persons_review_stops_the_run(self):
        """The query filters by author, and if that filter cannot be trusted then
        every pull request would have to be fetched separately."""
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [review("APPROVED", author="someone-else")])])
        with pytest.raises(CollectError, match="other people"):
            make_collector(client).collect_review_envelopes()

    def test_truncated_reviews_stop_the_run(self):
        """Otherwise a busy pull request silently contributes only its first page."""
        client = FakeClient()
        self.queue(client, [reviewed_node(1, [review()], has_next=True)])
        with pytest.raises(CollectError, match="truncated"):
            make_collector(client).collect_review_envelopes()

    def test_thin_headroom_is_noted(self):
        client = FakeClient()
        reviews = [review(submitted="2026-01-1%dT00:00:00Z" % (i % 10))
                   for i in range(REVIEW_PAGE - 1)]
        self.queue(client, [reviewed_node(1, reviews)])
        collector = make_collector(client)
        collector.collect_review_envelopes()
        assert any("headroom" in note for note in collector.notes)


class TestComments:
    def queue(self, client: FakeClient, nodes: list[dict]) -> None:
        client.queue_graphql("commented", search_response(nodes))
        for _ in range(5):
            client.queue_graphql("commented", search_response([]))

    def test_only_ones_own_comments_are_counted(self):
        """The comments connection has no author filter, so everything on every
        matching pull request comes back and has to be filtered locally."""
        client = FakeClient()
        self.queue(client, [commented_node(1, [
            comment(author=ME), comment(author="colleague"), comment(author=ME)])])
        records = make_collector(client).collect_comments([])
        assert len(records) == 2
        assert all(r["kind"] == "conversational" for r in records)

    def test_a_comment_outside_the_window_is_dropped(self):
        client = FakeClient()
        self.queue(client, [commented_node(1, [
            comment(created="2026-01-10T00:00:00Z"),
            comment(created="2025-01-10T00:00:00Z")])])
        assert len(make_collector(client).collect_comments([])) == 1

    def test_a_busy_pull_request_has_its_later_comments_read(self):
        """Without this its comments beyond the first page are invisible."""
        client = FakeClient()
        self.queue(client, [commented_node(1, [comment()], has_next=True, cursor="c1")])
        client.queue_graphql("comment page", {"repository": {"pullRequest": {
            "comments": {"totalCount": 2,
                         "pageInfo": {"hasNextPage": False, "endCursor": None},
                         "nodes": [comment()]}}}})
        assert len(make_collector(client).collect_comments([])) == 2

    def test_inline_comments_are_emitted_one_per_counted_comment(self):
        """They are available only as a count against each review, so one record
        is emitted per comment and dated to the review that carried it."""
        client = FakeClient()
        self.queue(client, [])
        envelopes = [{"date": "2026-01-05", "inline_count": 3, "on_own_pr": False,
                      "repo": "owner/repo"}]
        records = make_collector(client).collect_comments(envelopes)
        inline = [r for r in records if r["kind"] == "inline"]
        assert len(inline) == 3
        assert all(r["date"] == "2026-01-05" for r in inline)

    def test_inline_comments_on_ones_own_pull_requests_still_count(self):
        client = FakeClient()
        self.queue(client, [])
        envelopes = [{"date": "2026-01-05", "inline_count": 2, "on_own_pr": True,
                      "repo": "owner/repo"}]
        records = make_collector(client).collect_comments(envelopes)
        assert len(records) == 2
        assert all(r["on_own_pr"] for r in records)

    def test_the_two_kinds_are_distinguishable(self):
        client = FakeClient()
        self.queue(client, [commented_node(1, [comment(author=ME)])])
        envelopes = [{"date": "2026-01-05", "inline_count": 1, "on_own_pr": False,
                      "repo": "owner/repo"}]
        records = make_collector(client).collect_comments(envelopes)
        kinds = sorted(r["kind"] for r in records)
        assert kinds == ["conversational", "inline"]


class TestVerbosity:
    def test_repository_names_are_withheld_by_default(self):
        lines: list[str] = []
        client = FakeClient()
        client.queue_graphql("commit history", history_response([commit_node("aaa")]))
        collector = Collector(client, SINCE, UNTIL, verbose=False, log=lines.append)
        collector.viewer = {"login": ME, "id": "x"}
        collector.walk_commits([{"name_with_owner": "acme-corp/secret", "contributions": 1}])
        assert not any("secret" in line for line in lines)

    def test_they_are_shown_when_asked_for(self):
        lines: list[str] = []
        client = FakeClient()
        client.queue_graphql("commit history", history_response([commit_node("aaa")]))
        collector = Collector(client, SINCE, UNTIL, verbose=True, log=lines.append)
        collector.viewer = {"login": ME, "id": "x"}
        collector.walk_commits([{"name_with_owner": "acme-corp/secret", "contributions": 1}])
        assert any("secret" in line for line in lines)


def fixture_commits() -> list[dict]:
    return json.loads((FIXTURES / "merge_commits.json").read_text())["commits"]


class TestMergeFilter:
    def test_the_fixture_contains_both_kinds(self):
        commits = fixture_commits()
        assert any(c["expect_merge"] for c in commits)
        assert any(not c["expect_merge"] for c in commits)

    def test_every_recorded_commit_is_identified_correctly(self):
        for commit in fixture_commits():
            assert is_merge_commit(commit) is commit["expect_merge"], commit["oid"][:10]

    def test_a_commit_without_parent_information_is_an_error(self):
        """Treating it as a non-merge would silently include it."""
        with pytest.raises(CollectError, match="parent"):
            is_merge_commit({"oid": "abc123"})
        with pytest.raises(CollectError):
            is_merge_commit({"oid": "abc123", "parents": {}})

    @pytest.mark.parametrize("count,expected", [(1, False), (2, True), (3, True)])
    def test_more_than_one_parent_makes_a_merge(self, count, expected):
        assert is_merge_commit({"oid": "x", "parents": {"totalCount": count}}) is expected
