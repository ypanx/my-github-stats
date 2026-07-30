"""Classification of file paths into languages.

Every case here came from a real misclassification or from a documented quirk of
the tagging library. The classifier's failure mode is silence — the totals still
reconcile when a language is wrong — so these cases are the only thing standing
between a mistake and a published figure.
"""

from __future__ import annotations

import pytest
from identify import identify

from collector.classify import classify, extension, unknown_type
from collector.policy import policy


def status(path: str) -> str:
    return classify(path)[1]


def name(path: str) -> str | None:
    return classify(path)[0]


class TestRegressions:
    """Types that once counted as languages and must never do so again."""

    def test_an_svg_is_ignored_rather_than_counted_as_an_image(self):
        """The tags include an image tag that sorts first alphabetically, so
        choosing one tag arbitrarily counted vector graphics as a language."""
        assert classify("logo.svg") == (None, "ignored")

    def test_repository_metadata_is_ignored(self):
        assert classify(".gitattributes") == (None, "ignored")
        assert classify(".gitignore") == (None, "ignored")

    @pytest.mark.parametrize("path", [
        "logo.svg", ".gitattributes", "data.json", "table.csv", "go.sum",
        "poetry.lock", "image.png", "settings.cfg",
    ])
    def test_nothing_but_a_counted_file_gets_a_name(self, path):
        """Returning a tag for an ignored file is how metadata became a language."""
        display, state = classify(path)
        assert state != "counted"
        assert display is None


class TestLockfiles:
    """Lockfiles are ignored, and the ordering of the rules is what does it."""

    @pytest.mark.parametrize("path", [
        "poetry.lock", "Cargo.lock", "Pipfile.lock", "yarn.lock",
        "Gemfile.lock", "unknown.lock",
    ])
    def test_every_lockfile_is_ignored(self, path):
        assert classify(path) == (None, "ignored")

    def test_the_extension_rule_is_what_catches_them(self):
        """Some lockfiles are tagged by their content format and would count as
        configuration; others carry no tags at all. Only the extension covers
        both, which is why it is checked before tags and before the residual."""
        rules = policy()
        tags = frozenset(identify.tags_from_filename("poetry.lock"))
        assert not tags & rules.ignore, "no ignored tag applies to this file"
        assert extension("poetry.lock") in rules.ignore_extensions

    def test_an_untagged_lockfile_would_otherwise_be_unclassifiable(self):
        assert not identify.tags_from_filename("yarn.lock")
        assert classify("yarn.lock") == (None, "ignored")


class TestCountedNames:
    @pytest.mark.parametrize("path,expected", [
        ("main.py", "Python"),
        ("main.go", "Go"),
        ("notes.md", "Markdown"),
        ("app.tsx", "TypeScript"),
        ("app.ts", "TypeScript"),
        ("app.jsx", "JavaScript"),
        ("lib.rs", "Rust"),
        ("run.sh", "Shell"),
        ("schema.sql", "SQL"),
        ("api.proto", "Protocol Buffers"),
        ("main.tf", "Terraform"),
        ("config.yaml", "YAML"),
        ("stubs.pyi", "Python"),
        ("notes.txt", "Text"),
        ("settings.toml", "Toml"),
        ("setup.cfg.ini", "Ini"),
        ("Dockerfile", "Dockerfile"),
        ("BUILD.bazel", "Bazel"),
        ("Makefile", "Makefile"),
    ])
    def test_display_names(self, path, expected):
        assert classify(path) == (expected, "counted")

    def test_every_name_is_capitalized_or_explicitly_mapped(self):
        """A raw lowercase tag reaching the table would look like a bug to a
        reader and is never intended."""
        mapped = set(policy().display.values())
        for path in ("main.py", "notes.md", "app.tsx", "settings.toml",
                     "Dockerfile", "README.md", "api.proto"):
            display = classify(path)[0]
            assert display is not None
            assert display in mapped or display[0].isupper()


class TestNaming:
    """Choosing a name when several tags survive."""

    @pytest.mark.parametrize("path,expected", [
        ("pyproject.toml", "Toml"),
        ("Cargo.toml", "Toml"),
        (".flake8", "Ini"),
        (".coveragerc", "Ini"),
        (".gitconfig", "Ini"),
        ("PKGBUILD", "Bash"),
        ("README.rst", "Rst"),
        ("CHANGELOG.rst", "Rst"),
        ("NOTICE.py", "Python"),
        ("AUTHORS.yaml", "YAML"),
    ])
    def test_a_tool_marker_loses_to_the_real_language(self, path, expected):
        """The tagging library unions the tags of every dot-separated part of a
        name, so a marker or a generic text tag can sort ahead of the language."""
        assert classify(path) == (expected, "counted")

    def test_a_demoted_tag_is_still_used_when_it_is_all_there_is(self):
        assert classify("AUTHORS") == ("Text", "counted")
        assert classify("notes.txt") == ("Text", "counted")

    def test_readme_is_markdown_by_rule_rather_than_by_luck(self):
        """This name was previously correct only because one tag happened to sort
        before another."""
        assert "plain-text" in identify.tags_from_filename("README.md")
        assert "plain-text" in policy().demote
        assert classify("README.md") == ("Markdown", "counted")

    def test_a_generic_tag_never_names_a_file(self):
        """A text tag sorts ahead of several real languages."""
        assert "text" in identify.tags_from_filename("settings.toml")
        assert classify("settings.toml") == ("Toml", "counted")


class TestRescues:
    def test_a_notebook_counts_as_its_own_language(self):
        """Notebooks are stored as data, so the rule that ignores data would
        otherwise discard them along with genuine data files."""
        assert classify("analysis.ipynb") == ("Jupyter Notebook", "counted")

    @pytest.mark.parametrize("path", ["data.json", "big.json", ".eslintrc.json",
                                      ".babelrc"])
    def test_rescuing_notebooks_does_not_rescue_data(self, path):
        assert classify(path) == (None, "ignored")

    def test_an_ignored_tag_never_supplies_the_name(self):
        """A rescued notebook carries a data tag too, and letting that name the
        file would add a row for the storage format instead of the language."""
        assert {"json", "jupyter"} <= set(identify.tags_from_filename("a.ipynb"))
        assert name("a.ipynb") == "Jupyter Notebook"


class TestGeneratedAndVendored:
    @pytest.mark.parametrize("path", [
        "api.pb.go", "service_pb2.py", "service_pb2_grpc.py",
        "bundle.min.js", "site.min.css",
        "vendor/lib.go", "a/b/vendor/lib.go",
        "third_party/dep/mod.py", "node_modules/pkg/index.js",
    ])
    def test_generated_and_vendored_paths_are_ignored(self, path):
        """Tags cannot express this. A generated Go file really is Go, and what
        disqualifies it is where it came from."""
        assert classify(path) == (None, "ignored")

    @pytest.mark.parametrize("path", [
        "src/app.py", "mocks/mock_client.py", "db/migrations/001_init.sql",
        "vendoring/notes.md", "a/my_vendor_lib/tool.go", "src/api.go",
    ])
    def test_the_rules_do_not_reach_further_than_intended(self, path):
        """Mocks and migrations are frequently written by hand, and a directory
        name is matched as a whole component rather than as a substring."""
        assert status(path) == "counted"


class TestLicences:
    """A licence is copied in whole rather than written, so it must not count.

    One `LICENSE` file supplied 674 of the 677 lines the report attributed to
    "Text", which made a language row out of a file nobody here wrote a word of.
    """

    @pytest.mark.parametrize("path", [
        "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE-APACHE",
        "LICENCE", "LICENCE.md", "COPYING", "COPYING.LESSER",
    ])
    def test_every_spelling_of_a_licence_is_ignored(self, path):
        assert classify(path) == (None, "ignored")

    @pytest.mark.parametrize("path", [
        "LICENSE", "docs/LICENSE", "a/b/c/LICENSE.md", "docs/legal/COPYING",
    ])
    def test_a_licence_is_ignored_wherever_it_sits(self, path):
        """The name is what disqualifies the file rather than the location, which
        is why this is a basename glob and not a directory rule. Filing a licence
        under a subdirectory does not make it authored work."""
        assert classify(path) == (None, "ignored")

    def test_a_lowercase_licence_escapes_the_glob_but_still_does_not_count(self):
        """Globs are matched case-sensitively, so `LICENSE*` does not catch a
        lower-case `license`. Listing every case variant is unnecessary rather
        than merely tedious: the tagging library does not recognise the
        lower-case spelling either, so the file carries no tags and falls into
        the unclassifiable residual instead of counting as a language."""
        assert not identify.tags_from_filename("license")
        assert classify("license") == (None, "unknown")

    @pytest.mark.parametrize("path", [
        "requirements.txt", "requirements-dev.txt", "constraints.txt",
        "AUTHORS", "NOTICE", "notes.txt",
    ])
    def test_the_rule_does_not_reach_further_than_intended(self, path):
        """Dependency pins are edited by hand and choosing a version is a real
        change, and both `AUTHORS` and `NOTICE` routinely carry attribution that
        somebody sat down and wrote."""
        assert classify(path) == ("Text", "counted")


class TestResiduals:
    @pytest.mark.parametrize("path", [
        "settings.cfg", "CODEOWNERS", "deploy.template", "config.in",
        "alerts.cloudwatch", ".airflowignore",
    ])
    def test_unrecognized_types_are_left_unclassified(self, path):
        assert classify(path) == (None, "unknown")

    def test_a_binary_file_is_excluded_rather_than_unclassified(self):
        """GitHub reports no lines for a binary file regardless, so this is a
        fact about the data rather than a policy choice."""
        assert classify("logo.png") == (None, "binary")
        assert classify("archive.whl") == (None, "binary")

    def test_a_binary_file_stays_binary_even_under_a_vendored_path(self):
        """Being undiffable is a fact about the data, so it is settled before any
        policy rule gets a say. Reversing the two would report a vendored image as
        ignored, which is a policy decision the data never left open."""
        assert classify("vendor/logo.png") == (None, "binary")
        assert classify("third_party/dep/icon.png") == (None, "binary")

    def test_residuals_are_grouped_by_extension_then_by_name(self):
        assert unknown_type("deploy.template") == "template"
        assert unknown_type("path/to/CODEOWNERS") == "CODEOWNERS"
        assert unknown_type(".airflowignore") == "airflowignore"


class TestPathHandling:
    def test_directories_do_not_change_the_answer(self):
        assert classify("a/b/c/main.py") == ("Python", "counted")
        assert classify("deep/nested/logo.svg") == (None, "ignored")

    @pytest.mark.parametrize("path", ["x.lock", "x.LOCK", "x.Lock", "yarn.LOCK"])
    def test_extensions_are_matched_without_regard_to_case(self, path):
        """The tagging library lower-cases extensions, so a case-sensitive rule
        here made this the only case-sensitive rule in the classifier."""
        assert classify(path) == (None, "ignored")

    def test_uppercase_extensions_still_resolve_to_a_language(self):
        assert classify("MAIN.PY") == ("Python", "counted")
        assert classify("README.MD") == ("Markdown", "counted")

    def test_the_last_suffix_wins(self):
        assert classify("a.b.c.py") == ("Python", "counted")
        assert classify("notes.txt.md") == ("Markdown", "counted")
        assert classify("notes.md.txt") == ("Text", "counted")

    def test_classification_does_not_change_between_calls(self):
        for _ in range(3):
            assert classify("main.py") == ("Python", "counted")
            assert classify("logo.svg") == (None, "ignored")


class TestTaggingLibrary:
    """Characterization tests on the library the policy depends on.

    A release that changes these tables moves the language breakdown without
    anything in this repository having been touched. These turn that into a
    failing test rather than a quietly different report.
    """

    def test_the_tag_sets_the_policy_relies_on(self):
        tags = lambda path: frozenset(identify.tags_from_filename(path))  # noqa: E731
        assert tags("poetry.lock") == {"text", "toml"}
        assert tags("Cargo.lock") == {"cargo-lock", "text", "toml"}
        assert tags("Pipfile.lock") == {"json", "text"}
        assert not tags("yarn.lock")
        assert {"xml"} <= tags("logo.svg")
        assert "binary" in tags("logo.png")
        assert tags("settings.cfg") == {"text"}
        assert not tags("CODEOWNERS")
        assert tags(".gitattributes") == {"gitattributes", "text"}

    def test_readme_carries_both_markdown_and_plain_text(self):
        assert frozenset(identify.tags_from_filename("README.md")) == {
            "markdown", "plain-text", "text"}

    def test_notebooks_carry_both_a_data_tag_and_their_own(self):
        assert frozenset(identify.tags_from_filename("a.ipynb")) == {
            "json", "jupyter", "text"}
