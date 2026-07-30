"""Everything `utils.py` does, in the order that file does it.

Five concerns, each under its own banner: date and window arithmetic, the
classification tables and the language fold, assembling and splitting the payload,
the two checks that gate a write, and the figures the cards derive.

`languages.yml` is about 160 hand-written judgements and the classification section
is the only thing keeping them honest. Every failure mode of that file is silent: a
row that can never fire removes a language from the breakdown while every total
still reconciles.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from constants import (
    CONTRIB_MAX_DAYS,
    MAX_DROP,
    MIN_COMPARABLE,
    PUBLISHED_FIELDS,
    REVIEW_STATES,
    SECTIONS,
    STATS_PATH,
)
from utils import (
    CollectError,
    active_dates,
    build_commit_records,
    build_payload,
    check_day_over_day,
    check_non_zero,
    chunk_window,
    classify,
    created_partitions,
    cycle_hours,
    extension,
    fold_languages,
    headline_metrics,
    in_window,
    language_totals,
    load_policy,
    parse_timestamp,
    policy,
    require_window_fits,
    resolve_window,
    reviews_given,
    set_policy,
    sort_sections,
    strip_attribution,
    total_lines,
    utc_day,
    window_bounds,
    write_atomic,
    SORT_KEYS,
)

D = dt.date


# --------------------------------------------------------------------------- #
# Dates, windows, and the slicing of searches
# --------------------------------------------------------------------------- #

class TestChunkWindow:
    def test_slices_tile_the_window_without_gap_or_overlap(self):
        since, until = D(2025, 7, 30), D(2026, 7, 29)
        slices = chunk_window(since, until)
        assert slices[0][0] == since
        assert slices[-1][1] == until
        for (_, end), (next_start, _) in zip(slices, slices[1:]):
            assert next_start == end + dt.timedelta(days=1)

    def test_every_date_appears_exactly_once(self):
        since, until = D(2025, 7, 30), D(2026, 7, 29)
        seen: list[D] = []
        for start, end in chunk_window(since, until):
            day = start
            while day <= end:
                seen.append(day)
                day += dt.timedelta(days=1)
        assert len(seen) == len(set(seen))
        assert len(seen) == (until - since).days + 1

    def test_a_single_date_is_one_slice(self):
        day = D(2026, 1, 1)
        assert chunk_window(day, day) == [(day, day)]

    def test_a_short_window_is_not_extended_past_its_end(self):
        assert chunk_window(D(2026, 1, 1), D(2026, 1, 10)) == [
            (D(2026, 1, 1), D(2026, 1, 10))]

    @pytest.mark.parametrize("since,until,days", [
        (D(2026, 1, 2), D(2026, 1, 1), 91),
        (D(2026, 1, 1), D(2026, 1, 2), 0),
    ])
    def test_impossible_arguments_raise(self, since, until, days):
        with pytest.raises(ValueError):
            chunk_window(since, until, days)


class TestCreatedPartitions:
    def test_the_first_slice_is_open_at_the_start(self):
        """Pull requests created before the window but touched inside it must
        still be found, and slicing only within the window would drop them."""
        parts = created_partitions(D(2025, 7, 30), D(2026, 7, 29))
        assert parts[0] == (None, D(2025, 7, 30))

    def test_the_bounded_slices_tile_the_window(self):
        since, until = D(2025, 7, 30), D(2026, 7, 29)
        bounded = [p for p in created_partitions(since, until) if p[0]]
        assert bounded[0][0] == since
        assert bounded[-1][1] == until
        for (_, end), (next_start, _) in zip(bounded, bounded[1:]):
            assert next_start == end + dt.timedelta(days=1)


class TestWindowBounds:
    def test_the_end_covers_the_whole_final_date(self):
        start, end = window_bounds(D(2025, 7, 30), D(2026, 1, 31))
        assert start == "2025-07-30T00:00:00Z"
        assert end == "2026-01-31T23:59:59Z"

    def test_the_future_is_never_requested(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        _, end = window_bounds(today - dt.timedelta(days=10), today)
        assert parse_timestamp(end) <= dt.datetime.now(dt.timezone.utc)

    def test_the_same_window_always_produces_the_same_bounds(self):
        """Enumeration and history share these bounds. When they diverged, a
        repository active only in the gap was never enumerated at all."""
        since, until = D(2025, 7, 30), D(2026, 1, 31)
        assert window_bounds(since, until) == window_bounds(since, until)


class TestWindowFits:
    def test_a_window_longer_than_a_year_is_refused(self):
        with pytest.raises(CollectError, match="366 dates"):
            require_window_fits(D(2025, 7, 29), D(2026, 7, 29))

    def test_the_error_names_the_date_that_would_work(self):
        with pytest.raises(CollectError, match="2025-07-30"):
            require_window_fits(D(2025, 7, 29), D(2026, 7, 29))

    def test_a_window_of_exactly_a_year_of_dates_is_accepted(self):
        require_window_fits(D(2025, 7, 30), D(2026, 7, 29))


class TestInvertedWindow:
    def test_it_is_refused_before_any_request(self):
        """It used to pass `require_window_fits`, because a negative span is not
        greater than the maximum, then fail inside the first search — after a full
        history walk and thousands of file reads."""
        with pytest.raises(CollectError, match="ends before it starts"):
            require_window_fits(D(2026, 1, 31), D(2026, 1, 1))

    def test_a_single_date_window_is_still_fine(self):
        assert require_window_fits(D(2026, 1, 1), D(2026, 1, 1)) is None


class TestResolveWindow:
    def test_an_explicit_window_is_used_unchanged(self):
        since, until = D(2025, 7, 30), D(2026, 7, 29)
        assert resolve_window(since, until, 365) == (since, until)

    def test_the_horizon_counts_dates_and_always_fits(self):
        since, until = resolve_window(None, None, CONTRIB_MAX_DAYS)
        assert until == dt.datetime.now(dt.timezone.utc).date()
        assert (until - since).days + 1 == CONTRIB_MAX_DAYS
        require_window_fits(since, until)


class TestTimestamps:
    def test_days_are_bucketed_in_utc(self):
        assert utc_day("2026-01-01T23:30:00Z") == D(2026, 1, 1)
        assert utc_day("2026-01-02T02:30:00+08:00") == D(2026, 1, 1)

    def test_the_window_includes_both_of_its_ends(self):
        since, until = D(2026, 1, 1), D(2026, 1, 31)
        assert in_window("2026-01-01T00:00:00Z", since, until)
        assert in_window("2026-01-31T23:59:59Z", since, until)
        assert not in_window("2025-12-31T23:59:59Z", since, until)
        assert not in_window("2026-02-01T00:00:00Z", since, until)

    def test_a_missing_timestamp_is_outside_every_window(self):
        assert not in_window(None, D(2026, 1, 1), D(2026, 1, 31))


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

CORE_ROWS = {
    "src/app.py": "Python",
    "main.go": "Go",
    "README.md": "Markdown",
    "src/index.ts": "TypeScript",
    "app/page.tsx": "TypeScript",
    "deploy/values.yaml": "YAML",
    "scripts/build.sh": "Shell",
    "BUILD.bazel": "Bazel",
    "infra/main.tf": "Terraform",
    "migrations/001.sql": "SQL",
    "api/service.proto": "Protocol Buffers",
    "notes.txt": "Text",
    "Dockerfile": "Dockerfile",
    "CODEOWNERS": "Text",
    "analysis.ipynb": "Jupyter Notebook",
    "setup.cfg": "INI",
    ".flake8": "INI",
    "Makefile": "Makefile",
}

#: Every language the last real collection produced. One that stops being reachable
#: disappears from the cards with nothing else failing.
COLLECTED = ["Bazel", "Go", "Markdown", "Protocol Buffers", "Python", "SQL",
             "Shell", "Terraform", "Text", "TypeScript", "YAML"]


def names() -> set[str]:
    return set(policy().include_extensions.values()) | set(
        policy().include_filenames.values())


class TestCoreRowsSurvive:
    @pytest.mark.parametrize("path,name", sorted(CORE_ROWS.items()))
    def test_a_known_path_resolves_to_its_language(self, path, name):
        assert classify(path) == name

    @pytest.mark.parametrize("name", COLLECTED)
    def test_every_collected_language_is_still_reachable(self, name):
        assert name in names()

    def test_one_language_has_exactly_one_name(self):
        """Two spellings of the same language split its lines across two segments
        and neither total looks wrong."""
        every = list(policy().include_extensions.values()) + list(
            policy().include_filenames.values())
        folded = {name.lower().replace(" ", "") for name in every}
        assert len(folded) == len(set(every))


class TestEveryRowIsLive:
    """Walks the tables, so a row that cannot fire fails a test.

    Deleting a row merely shortens these loops, which is why `CORE_ROWS` exists
    beside them.
    """

    def test_every_included_extension_resolves_to_its_name(self):
        for suffix, name in policy().include_extensions.items():
            assert classify(f"probe.{suffix}") == name, suffix

    def test_every_included_filename_resolves_to_its_name(self):
        for basename, name in policy().include_filenames.items():
            assert classify(basename) == name, basename
            assert classify(f"nested/dir/{basename}") == name, basename

    def test_every_excluded_extension_is_dropped(self):
        for suffix in policy().exclude_extensions:
            assert classify(f"probe.{suffix}") is None, suffix

    def test_every_excluded_filename_is_dropped(self):
        for basename in policy().exclude_filenames:
            assert classify(basename) is None, basename

    def test_every_excluded_directory_is_dropped(self):
        """Matched against any whole component, so depth must not matter."""
        for directory in policy().exclude_directories:
            assert classify(f"{directory}/app.py") is None, directory
            assert classify(f"src/{directory}/deep/app.py") is None, directory

    def test_every_excluded_glob_is_dropped(self):
        for pattern in policy().exclude_globs:
            probe = pattern.replace("*", "probe")
            assert classify(probe) is None, pattern
            assert classify(f"pkg/{probe}") is None, pattern


class TestRuleOrder:
    """Exclusions run first, and filenames beat extensions. Both orderings are
    observable only on a row that two rules disagree about."""

    def test_an_excluded_glob_beats_an_included_extension(self):
        """The whole point of the codegen globs: `go` counts, `service.pb.go` does
        not. A glob that swallowed every `.go` file would remove Go from the
        breakdown while every total still reconciled."""
        assert classify("api/service.go") == "Go"
        assert classify("api/service.pb.go") is None

    def test_an_excluded_directory_beats_an_included_extension(self):
        assert classify("app.py") == "Python"
        assert classify("vendor/app.py") is None

    def test_an_excluded_extension_beats_an_included_filename(self, make_policy):
        set_policy(make_policy(include_filenames={"pins.json": "JSON"},
                               exclude_extensions=frozenset({"json"})))
        assert classify("pins.json") is None

    def test_an_excluded_directory_beats_an_included_filename(self, make_policy):
        set_policy(make_policy(include_filenames={"Dockerfile": "Dockerfile"},
                               exclude_directories=frozenset({"vendor"})))
        assert classify("vendor/Dockerfile") is None

    def test_an_included_filename_beats_an_included_extension(self, make_policy):
        set_policy(make_policy(include_extensions={"cfg": "INI"},
                               include_filenames={"odd.cfg": "Text"}))
        assert classify("odd.cfg") == "Text"
        assert classify("other.cfg") == "INI"

    def test_a_dotfile_is_reachable_only_by_filename(self):
        """`.flake8` has the extension `flake8`, so nothing but a filename row can
        claim it. This is why filenames are consulted first."""
        assert extension(".flake8") == "flake8"
        assert classify(".flake8") == "INI"


class TestRegressions:
    """Types that once counted as a language and must never do so again."""

    @pytest.mark.parametrize("path", [
        "package-lock.json", "yarn.lock", "poetry.lock", "go.sum",
        ".terraform.lock.hcl", "LICENSE", "LICENSE.md", "COPYING",
        "node_modules/left-pad/index.js", "vendor/github.com/x/y.go",
        "api/service.pb.go", "api/service_pb2.py", "api/service_pb2.pyi",
        "coverage/lcov.info", "dist/bundle.js", "__pycache__/app.pyc",
        "logo.png", "font.woff2", "archive.tar.gz",
    ])
    def test_it_does_not_count(self, path):
        assert classify(path) is None


class TestPathHandling:
    @pytest.mark.parametrize("path", ["x.lock", "x.LOCK", "x.Lock"])
    def test_extensions_are_matched_without_regard_to_case(self, path):
        assert classify(path) is None

    def test_an_uppercase_extension_still_resolves_to_a_language(self):
        assert classify("SCRIPT.PY") == "Python"

    def test_the_last_suffix_wins(self):
        assert extension("archive.tar.gz") == "gz"
        assert classify("bundle.min.js") is None

    def test_a_file_with_no_extension_and_no_row_does_not_count(self):
        assert classify("bin/cloudwatch") is None

    def test_directories_do_not_change_the_answer(self):
        assert classify("a/b/c/d/app.py") == classify("app.py") == "Python"

    def test_globs_are_matched_case_sensitively(self):
        """`fnmatch` folds case on Windows, which would make `license` match
        `LICENSE*` there and nowhere else."""
        assert classify("LICENSE") is None
        assert classify("license.py") == "Python"

    def test_nothing_touches_the_filesystem(self):
        """Paths come from the API and never exist locally."""
        assert classify("/nonexistent/elsewhere/app.py") == "Python"

    @pytest.mark.parametrize("path", [
        ".airflowignore", "service.tpl", "build.ksh", "scripts/run",
    ])
    def test_an_unrecognized_type_does_not_count(self, path):
        assert classify(path) is None


class TestFolding:
    """The fold produces `commit["languages"]`, which is the whole content of the
    published file."""

    RECORDS = [
        {"path": "src/app.py", "additions": 10, "deletions": 2},
        {"path": "src/other.py", "additions": 5, "deletions": 0},
        {"path": "main.go", "additions": 7, "deletions": 3},
        {"path": "vendor/dep.go", "additions": 900, "deletions": 900},
        {"path": "pins.lock", "additions": 40, "deletions": 40},
    ]

    def test_it_sums_per_language(self):
        assert fold_languages(self.RECORDS) == {
            "Go": {"additions": 7, "deletions": 3},
            "Python": {"additions": 15, "deletions": 2},
        }

    def test_excluded_records_reach_no_language(self):
        """The vendored and locked records are 1,880 lines that must not appear."""
        folded = fold_languages(self.RECORDS)
        assert sum(c["additions"] + c["deletions"] for c in folded.values()) == 27

    def test_additions_and_deletions_stay_apart(self):
        """Collapsing them here would make that choice permanent, and whether a
        card shows churn or net change belongs to rendering."""
        assert fold_languages(self.RECORDS)["Python"] == {"additions": 15,
                                                         "deletions": 2}

    def test_the_result_is_ordered_by_name(self):
        assert list(fold_languages(self.RECORDS)) == ["Go", "Python"]

    def test_nothing_in_means_nothing_out(self):
        assert fold_languages([]) == {}


class TestTheCommittedTables:
    def test_they_load(self):
        assert load_policy().include_extensions["py"] == "Python"

    def test_they_resolve_relative_to_this_file_not_the_working_directory(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        set_policy(None)
        assert "json" in policy().exclude_extensions

    def test_they_are_loaded_once(self, monkeypatch):
        """Classification runs per file record, and there are thousands."""
        import utils

        calls = []
        real = load_policy
        monkeypatch.setattr(utils, "load_policy",
                            lambda *a, **k: calls.append(1) or real(*a, **k))
        set_policy(None)
        for _ in range(50):
            classify("main.py")
        assert len(calls) == 1

    def test_a_file_missing_a_table_is_refused(self, tmp_path):
        """A mistyped table name would otherwise disable every rule in it."""
        from utils import CollectError

        path = tmp_path / "languages.yml"
        path.write_text("include_extensions: {py: Python}\n")
        with pytest.raises(CollectError, match="expected a mapping"):
            load_policy(path)

    def test_an_installed_policy_changes_the_answer(self, make_policy):
        set_policy(make_policy(exclude_extensions=frozenset({"py"})))
        assert classify("main.py") is None


# --------------------------------------------------------------------------- #
# Assembling the two payloads
# --------------------------------------------------------------------------- #

FORBIDDEN_KEYS = {"repo", "path", "author", "login", "user", "oid", "sha",
                  "number", "url"}


def walk(node, visit):
    if isinstance(node, dict):
        for key, value in node.items():
            visit(key, value)
            walk(value, visit)
    elif isinstance(node, list):
        for item in node:
            walk(item, visit)


class TestCommitRecords:
    def test_paths_are_classified_and_then_discarded(self):
        commits = [{"oid": "a1", "committedDate": "2026-01-02T10:00:00Z",
                    "repo": "owner/private"}]
        files = {"a1": {"records": [
            {"path": "src/app.py", "additions": 10, "deletions": 2},
            {"path": "data/big.json", "additions": 500, "deletions": 0},
            {"path": "docs/readme.md", "additions": 4, "deletions": 1},
        ]}}
        records = build_commit_records(commits, files)
        assert records[0]["date"] == "2026-01-02"
        assert records[0]["languages"] == {
            "Markdown": {"additions": 4, "deletions": 1},
            "Python": {"additions": 10, "deletions": 2},
        }
        assert "src/app.py" not in json.dumps(records)

    def test_the_date_is_the_utc_day_not_the_reported_prefix(self):
        """Truncating the timestamp instead of converting it would put a commit on
        the wrong day whenever the two disagree."""
        commits = [{"oid": "a1", "committedDate": "2026-01-03T02:30:00+08:00",
                    "repo": "owner/private"}]
        files = {"a1": {"records": [{"path": "a.py", "additions": 1, "deletions": 0}]}}
        assert build_commit_records(commits, files)[0]["date"] == "2026-01-02"

    def test_a_commit_of_only_ignored_files_is_still_a_commit(self):
        """It happened, and dropping it would undercount the commit total."""
        commits = [{"oid": "a1", "committedDate": "2026-01-02T10:00:00Z",
                    "repo": "owner/private"}]
        files = {"a1": {"records": [{"path": "x.json", "additions": 9, "deletions": 0}]}}
        records = build_commit_records(commits, files)
        assert len(records) == 1
        assert records[0]["languages"] == {}


class TestStripping:
    def test_every_repository_name_is_removed(self, payload):
        published = strip_attribution(payload)
        assert "private" not in json.dumps(published)
        for section in SECTIONS:
            for record in published[section]:
                assert "repo" not in record

    def test_everything_else_survives_unchanged(self, payload):
        published = strip_attribution(payload)
        assert len(published["commits"]) == len(payload["commits"])
        assert [c["languages"] for c in published["commits"]] == \
            [c["languages"] for c in payload["commits"]]
        assert published["window"] == payload["window"]

    def test_the_published_payload_carries_no_identifying_key(self, payload):
        published = strip_attribution(payload)
        seen: list[str] = []
        walk(published, lambda key, _: seen.append(key))
        assert FORBIDDEN_KEYS.isdisjoint(seen)

    def test_no_published_string_looks_like_a_path(self, payload):
        published = strip_attribution(payload)
        strings: list[str] = []
        walk(published, lambda _, value: strings.append(value)
             if isinstance(value, str) else None)
        assert all("/" not in value for value in strings)

    def test_the_time_to_merge_is_published_as_a_duration(self):
        """Dates alone cannot express it, because a large share of pull requests
        merge the day they are opened. A duration reveals how long something took
        without revealing when the work happened."""
        payload = {"window": {}, "commits": [], "review_envelopes": [],
                   "comments": [],
                   "prs": [{"created": "2026-01-02", "merged": "2026-01-03",
                            "state": "MERGED", "cycle_hours": 41.25,
                            "repo": "owner/private"}]}
        published = strip_attribution(payload)
        assert published["prs"][0]["cycle_hours"] == 41.25
        assert "T" not in json.dumps(published["prs"])


def grouped_payload() -> dict:
    """Commits from two repositories, arriving grouped and sharing dates.

    Records really do arrive grouped by repository, and several land on the same
    day. A fixture whose records all carry one repository, or all carry distinct
    dates, cannot tell a content-based sort from one that merely happens to leave
    arrival order intact.
    """
    return {
        "window": {"from": "2026-01-01", "to": "2026-01-31", "generated_at": "z"},
        "commits": [
            {"date": "2026-01-10", "repo": "owner/big",
             "languages": {"Python": {"additions": 3, "deletions": 0}}},
            {"date": "2026-01-10", "repo": "owner/big",
             "languages": {"Python": {"additions": 1, "deletions": 0}}},
            {"date": "2026-01-10", "repo": "owner/small",
             "languages": {"Go": {"additions": 2, "deletions": 0}}},
            {"date": "2026-01-10", "repo": "owner/small",
             "languages": {"Go": {"additions": 4, "deletions": 0}}},
        ],
        "prs": [], "review_envelopes": [], "comments": [],
    }


class TestOrdering:
    def test_records_end_up_in_date_order(self, payload):
        """Records arrive grouped by repository, and the points at which the date
        stopped descending marked the repository boundaries, letting a reader infer
        how the commits were distributed."""
        sorted_payload = sort_sections(payload)
        dates = [c["date"] for c in sorted_payload["commits"]]
        assert dates == sorted(dates)

    def test_order_does_not_depend_on_which_repository_a_record_came_from(self, payload):
        """Otherwise the ordering itself becomes metadata."""
        other = json.loads(json.dumps(payload))
        for record in other["commits"]:
            record["repo"] = "owner/somewhere-else"
        assert strip_attribution(sort_sections(payload)) == \
            strip_attribution(sort_sections(other))

    def test_records_sharing_a_date_are_ordered_by_their_content(self):
        """Sorting on the date alone would leave same-date records in arrival
        order, and arrival order is grouped by repository. What is asserted here is
        the weaker but achievable property: order is a function of the published
        content. Where content happens to correlate with repository it will still
        correlate, but no information beyond the records themselves is added."""
        published = strip_attribution(sort_sections(grouped_payload()))
        keys = [json.dumps(c["languages"], sort_keys=True) for c in published["commits"]]
        assert keys == sorted(keys)
        assert len(set(keys)) == len(keys), "the fixture must not rely on ties"

    def test_reordering_the_input_cannot_change_the_output(self):
        """The stronger form of the property: any arrival order, one result."""
        import itertools
        results = set()
        base = grouped_payload()["commits"]
        for permutation in itertools.permutations(range(len(base))):
            candidate = grouped_payload()
            candidate["commits"] = [base[index] for index in permutation]
            results.add(json.dumps(strip_attribution(sort_sections(candidate))))
        assert len(results) == 1, "the sort is not a total order over content"

    def test_order_does_not_depend_on_the_order_records_arrived_in(self, payload):
        forward = strip_attribution(sort_sections(json.loads(json.dumps(payload))))
        reversed_payload = json.loads(json.dumps(payload))
        for section in SECTIONS:
            reversed_payload[section].reverse()
        assert strip_attribution(sort_sections(reversed_payload)) == forward

    def test_every_published_section_is_sorted(self):
        """A section left out would keep whatever grouping collection produced."""
        assert set(SORT_KEYS) == set(SECTIONS)

    def test_sorting_neither_adds_nor_drops_records(self, payload):
        before = {section: len(payload[section]) for section in SECTIONS}
        sort_sections(payload)
        assert {section: len(payload[section]) for section in SECTIONS} == before


class TestPayload:
    def test_it_is_built_sorted_and_complete(self, payload):
        built = build_payload(
            window=payload["window"], commits=payload["commits"],
            pull_requests=payload["prs"], envelopes=payload["review_envelopes"],
            comments=payload["comments"])
        assert set(built) == {"window", *SECTIONS}
        dates = [c["date"] for c in built["commits"]]
        assert dates == sorted(dates)

    def test_the_reviews_section_is_not_named_reviews(self, payload):
        """Its length is the number of review events rather than the number of
        reviews given, and the awkward name exists to stop a reader reaching for
        the obvious and being wrong."""
        published = strip_attribution(payload)
        assert "review_envelopes" in published
        assert "reviews" not in published


# --------------------------------------------------------------------------- #
# The two checks that gate a write
# --------------------------------------------------------------------------- #

class TestCheckNonZero:
    def test_a_real_run_passes(self, bulk_payload):
        assert check_non_zero(bulk_payload(), 500) == []

    def test_no_reported_contributions_fails(self, bulk_payload):
        """The account-wide total comes from a different query than the history
        walk, so the two disagreeing means one of them failed."""
        assert check_non_zero(bulk_payload(), 0)

    def test_no_commits_fails(self, bulk_payload):
        assert check_non_zero(bulk_payload(commits=0), 500)

    def test_no_lines_fails(self, bulk_payload):
        payload = bulk_payload()
        for commit in payload["commits"]:
            commit["languages"] = {}
        assert any("lines" in problem for problem in check_non_zero(payload, 500))

    def test_it_is_a_conjunction_not_a_single_emptiness_check(self, bulk_payload):
        """When authorization lapses the response carries only personal
        repositories, so the failure looks like a small plausible number. Any one
        of these three alone would wave that through."""
        assert check_non_zero(bulk_payload(commits=1, lines=1), 0)


class TestCheckDayOverDay:
    def test_a_first_run_is_skipped_not_failed(self, bulk_payload, tmp_path):
        """Reported as a skip so a first run is possible at all. It also means a
        first run is unguarded against the failure this exists to catch."""
        problems, skipped = check_day_over_day(bulk_payload(),
                                               tmp_path / "absent.json")
        assert problems == []
        assert skipped == "no previous file"

    def test_an_unreadable_previous_file_is_skipped(self, bulk_payload, tmp_path):
        path = tmp_path / "stats.json"
        path.write_text("{not json")
        assert check_day_over_day(bulk_payload(), path)[1]

    def test_a_previous_file_of_another_schema_is_skipped(self, bulk_payload, tmp_path):
        path = tmp_path / "stats.json"
        path.write_text(json.dumps({"window": {}, "commits": []}))
        assert "predates" in check_day_over_day(bulk_payload(), path)[1]

    def test_a_previous_file_with_older_record_shapes_is_skipped(self, bulk_payload,
                                                                tmp_path):
        """Section names alone do not prove the records still carry the same fields.
        This used to raise `KeyError` out of the check and abort the run with a
        traceback, where the documented behaviour is a skip."""
        previous = bulk_payload()
        previous["commits"] = [{"date": "2026-01-02"}]      # no `languages`
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(previous))
        problems, skipped = check_day_over_day(bulk_payload(), path)
        assert problems == []
        assert skipped == "the previous file predates this schema"

    def test_a_changed_window_length_is_skipped(self, bulk_payload, tmp_path):
        """A deliberately shorter window legitimately holds less, so comparing the
        two would fail every time the horizon changed."""
        previous = bulk_payload()
        previous["window"]["from"] = "2026-01-01"
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(previous))
        assert "window length" in check_day_over_day(bulk_payload(), path)[1]

    def test_a_collapse_is_refused(self, bulk_payload, tmp_path):
        """The failure that actually happened: a token with the right scopes but no
        organization authorization returned twelve figures down 99 to 100%."""
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(bulk_payload()))
        problems, skipped = check_day_over_day(bulk_payload(commits=3, lines=90), path)
        assert skipped is None
        assert any("commits fell" in problem for problem in problems)

    def test_ordinary_movement_passes(self, bulk_payload, tmp_path):
        """A rolling window moves every day, so the bound has to be impossible to
        reach honestly or it blocks good runs."""
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(bulk_payload(commits=467)))
        assert check_day_over_day(bulk_payload(commits=430), path)[0] == []

    def test_a_drop_exactly_at_the_bound_passes(self, bulk_payload, tmp_path):
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(bulk_payload(commits=400)))
        held = int(400 * (1 - MAX_DROP))
        assert check_day_over_day(bulk_payload(commits=held), path)[0] == []

    def test_a_metric_too_small_to_compare_is_skipped(self, bulk_payload, tmp_path):
        """A tenth of three is less than one, so any movement at all in a small
        metric reads as a collapse."""
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(bulk_payload(approvals=MIN_COMPARABLE - 1)))
        problems, _ = check_day_over_day(bulk_payload(approvals=0), path)
        assert not any("approved" in problem for problem in problems)


class TestWriteAtomic:
    def test_it_writes_the_payload(self, tmp_path):
        path = tmp_path / "nested" / "stats.json"
        write_atomic(path, {"a": 1})
        assert json.loads(path.read_text()) == {"a": 1}

    def test_it_leaves_no_temporary_behind(self, tmp_path):
        path = tmp_path / "stats.json"
        write_atomic(path, {"a": 1})
        assert list(p.name for p in tmp_path.iterdir()) == ["stats.json"]

    def test_it_replaces_an_existing_file(self, tmp_path):
        path = tmp_path / "stats.json"
        write_atomic(path, {"a": 1})
        write_atomic(path, {"b": 2})
        assert json.loads(path.read_text()) == {"b": 2}

# --------------------------------------------------------------------------- #
# Figures derived from a payload
# --------------------------------------------------------------------------- #

class TestThePublishedFileMatchesTheDeclaredShape:
    """Checked against the committed output, not a fixture, so a field the collector
    started emitting cannot pass by being absent from the fixture."""

    def test_every_record_carries_exactly_its_declared_fields(self):
        payload = json.loads(STATS_PATH.read_text())
        for section, fields in PUBLISHED_FIELDS.items():
            shapes = {tuple(sorted(record)) for record in payload[section]}
            assert shapes == {tuple(sorted(fields))}, section

    def test_the_window_carries_exactly_three_fields(self):
        payload = json.loads(STATS_PATH.read_text())
        assert sorted(payload["window"]) == ["from", "generated_at", "to"]

    def test_no_section_is_missing_or_extra(self):
        payload = json.loads(STATS_PATH.read_text())
        assert sorted(payload) == sorted(("window", *SECTIONS))


class TestHeadlineMetrics:
    """Three mutations used to survive the whole suite here, all of them in figures
    the cards print."""

    def test_envelopes_is_not_the_review_count(self, payload):
        """`review_envelopes` holds self-reviews too, so its length is a third
        larger than reviews given. Returning the wrong one of the two moves no other
        figure, and approvals are what gets reported."""
        m = headline_metrics(payload)
        assert m["review_envelopes"] == 2
        assert m["reviews_given"] == 1
        assert m["review_envelopes"] != m["reviews_given"]

    def test_substantive_counts_a_body_or_an_inline_comment(self, payload):
        """Either, not both. Requiring both would silently undercount every review
        that carried one and not the other."""
        payload["review_envelopes"] = [
            {"date": "2026-01-05", "state": "APPROVED", "inline_count": 0,
             "has_body": True, "on_own_pr": False},
            {"date": "2026-01-05", "state": "APPROVED", "inline_count": 3,
             "has_body": False, "on_own_pr": False},
            {"date": "2026-01-05", "state": "APPROVED", "inline_count": 0,
             "has_body": False, "on_own_pr": False},
        ]
        assert headline_metrics(payload)["reviews_substantive"] == 2

    def test_active_dates_unions_all_four_sections(self, payload):
        """A date on which only comments happened still counts. Dropping any one
        section from the union lowers the figure and nothing else."""
        payload["comments"] = [{"date": "2026-02-09", "kind": "inline",
                                "on_own_pr": False}]
        dates = active_dates(payload)
        assert "2026-02-09" in dates
        assert dates == {"2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05",
                         "2026-01-06", "2026-02-09"}

    def test_each_section_contributes_its_own_date(self, payload):
        """Checked one section at a time, so dropping any single one fails."""
        for section, key, date in [("commits", "date", "2026-03-01"),
                                   ("prs", "created", "2026-03-02"),
                                   ("review_envelopes", "date", "2026-03-03"),
                                   ("comments", "date", "2026-03-04")]:
            fresh = dict(payload)
            fresh[section] = [dict(payload[section][0], **{key: date})]
            assert date in active_dates(fresh), section

    def test_reviews_per_state_sum_to_reviews_given(self, payload):
        m = headline_metrics(payload)
        assert sum(m[f"reviews_{s.lower()}"] for s in REVIEW_STATES) == \
            m["reviews_given"]

    def test_reviews_given_excludes_ones_own_pull_requests(self, payload):
        """Counting them would mean being reviewed more made one look more active."""
        assert len(reviews_given(payload["review_envelopes"])) == 1

    def test_comments_split_by_kind_and_the_parts_sum(self, payload):
        m = headline_metrics(payload)
        assert (m["comments_inline"], m["comments_conversational"]) == (1, 1)

    def test_total_lines_sums_additions_and_deletions(self, payload):
        assert total_lines(payload["commits"]) == 25

    def test_language_totals_merge_across_commits(self, payload):
        payload["commits"].append({"date": "2026-01-04", "repo": "owner/private",
                                   "languages": {"Go": {"additions": 1,
                                                        "deletions": 1}}})
        assert language_totals(payload["commits"])["Go"] == {"additions": 8,
                                                            "deletions": 4}

    def test_cycle_hours_skips_unmerged_pull_requests(self, payload):
        assert cycle_hours(payload["prs"]) == [24.0]
