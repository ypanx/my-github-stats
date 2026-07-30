"""The two SVG cards.

A broken card is committed and served without anything failing, so the invariants
here are mostly about the file still being a document and the legend still adding
up. Both are failures a reader would see and no other check would.
"""

from __future__ import annotations

import json
from xml.etree import ElementTree

import pytest

from cards import (
    CARD_HEIGHT,
    NAMED_LANGUAGES,
    CardError,
    activity_rows,
    load_colors,
    render_activity,
    render_all,
    render_languages,
    segments,
)
from constants import CARDS_DIR, STATS_PATH
from utils import headline_metrics, nearest_rank, total_lines

SVG = "{http://www.w3.org/2000/svg}"
NAME = "Test Person"


@pytest.fixture
def real():
    return json.loads(STATS_PATH.read_text())


@pytest.fixture
def colors():
    return load_colors()


@pytest.fixture
def many(payload):
    """A payload with more languages than the card names, so a fold exists."""
    churn = {"Python": 5000, "Go": 4000, "Markdown": 3000, "TypeScript": 900,
             "YAML": 800, "Shell": 700, "Bazel": 600, "Terraform": 500,
             "SQL": 40, "Protocol Buffers": 30, "Text": 20}
    payload["commits"] = [
        {"date": "2026-01-02",
         "languages": {name: {"additions": lines, "deletions": 0}}}
        for name, lines in churn.items()
    ]
    return payload


def elements(svg: str, tag: str) -> list:
    return list(ElementTree.fromstring(svg).iter(f"{SVG}{tag}"))


def texts(svg: str) -> list[str]:
    return [element.text or "" for element in elements(svg, "text")]


class TestBothCardsAreDocuments:
    @pytest.mark.parametrize("filename", ["activity.svg", "languages.svg"])
    def test_the_output_parses(self, real, filename):
        """The failure this catches shipped once: an unescaped `<` in the alt text
        made the file invalid XML, and a browser drew a parser error instead of a
        card."""
        assert ElementTree.fromstring(render_all(real, NAME)[filename]) is not None

    @pytest.mark.parametrize("filename", ["activity.svg", "languages.svg"])
    def test_the_committed_card_matches_a_fresh_render(self, real, filename):
        """The cards are committed, so a stale one is published until someone
        notices. This fails whenever they are out of date."""
        committed = (CARDS_DIR / filename).read_text()
        assert committed == render_all(real, "John Pan")[filename]

    def test_a_value_needing_escaping_is_escaped(self, real):
        """Substituted text is escaped where it goes in, so this stays a document."""
        svg = render_activity(real, "A < B & C")
        assert ElementTree.fromstring(svg).get("aria-label", "").startswith("A < B & C")

    def test_a_value_that_would_break_the_document_is_refused(self):
        """The guard behind that escaping. Generated fragments are markup and are
        deliberately not escaped, so the check has to be on the finished file."""
        from cards import _fill

        with pytest.raises(CardError, match="invalid XML"):
            _fill('<svg xmlns="http://www.w3.org/2000/svg" a="{{v}}"/>', "card.svg",
                  {"v": "unescaped < here"})

    def test_a_card_carrying_a_placeholder_is_refused(self, real, monkeypatch):
        import cards

        monkeypatch.setattr(cards, "ACTIVITY_TEMPLATE",
                            '<svg xmlns="http://www.w3.org/2000/svg">'
                            "{{title}}{{unfilled}}</svg>")
        with pytest.raises(CardError, match=r"\{\{unfilled\}\}"):
            render_activity(real, NAME)

    @pytest.mark.parametrize("filename", ["activity.svg", "languages.svg"])
    def test_each_card_carries_a_text_alternative(self, real, filename):
        root = ElementTree.fromstring(render_all(real, NAME)[filename])
        assert root.get("aria-label")
        assert root.find(f"{SVG}title") is not None


class TestBothCardsAreTheSameSize:
    """Markdown aligns side-by-side images on the text baseline, so a difference in
    height offsets one card against the other by exactly that difference. Embedding
    them next to each other in a profile README is the whole point."""

    def test_they_are_the_same_height_and_width(self, real):
        cards = render_all(real, NAME)
        sizes = {name: (ElementTree.fromstring(svg).get("width"),
                        ElementTree.fromstring(svg).get("height"))
                 for name, svg in cards.items()}
        assert len(set(sizes.values())) == 1, sizes

    @pytest.mark.parametrize("filename", ["activity.svg", "languages.svg"])
    def test_the_declared_height_matches_the_constant(self, real, filename):
        root = ElementTree.fromstring(render_all(real, NAME)[filename])
        assert root.get("height") == str(CARD_HEIGHT)
        assert root.get("viewBox") == f"0 0 480 {CARD_HEIGHT}"

    @pytest.mark.parametrize("filename", ["activity.svg", "languages.svg"])
    def test_nothing_is_drawn_outside_the_card(self, real, filename):
        """A legend row below the border reads as a clipped card."""
        svg = render_all(real, NAME)[filename]
        for element in ElementTree.fromstring(svg).iter():
            for attribute in ("y", "cy", "y1", "y2"):
                if (value := element.get(attribute)) is not None:
                    assert float(value) <= CARD_HEIGHT, (filename, attribute, value)


class TestTheming:
    """One file per card with a media query inside it. The older
    `#gh-dark-mode-only` fragment is deprecated and now renders both images."""

    @pytest.mark.parametrize("filename", ["activity.svg", "languages.svg"])
    def test_dark_is_the_base_and_light_is_the_query(self, real, filename):
        """This way round matters: a viewer expressing no preference, and a
        renderer ignoring the query, both get dark, which is the intended look."""
        svg = render_all(real, NAME)[filename]
        assert "@media (prefers-color-scheme: light)" in svg
        assert "prefers-color-scheme: dark" not in svg
        assert svg.index("#0d1117") < svg.index("@media")

    @pytest.mark.parametrize("filename", ["activity.svg", "languages.svg"])
    def test_no_deprecated_fragment_mechanism(self, real, filename):
        assert "gh-dark-mode-only" not in render_all(real, NAME)[filename]

    def test_every_segment_has_a_light_colour_too(self, many, colors):
        """Light is an overrides-only table, so a language absent from it has to
        fall back to its dark colour rather than to nothing."""
        svg = render_languages(many, NAME, colors)
        light = svg.split("@media (prefers-color-scheme: light)")[-1]
        for index in range(len(segments(many, colors))):
            assert f".s{index} {{" in light


class TestLanguagesReconcile:
    def test_the_legend_totals_the_line_count(self, real, colors):
        assert sum(s.lines for s in segments(real, colors)) == total_lines(
            real["commits"])

    def test_a_legend_that_does_not_reconcile_is_refused(self, many, colors,
                                                         monkeypatch):
        """The renderer fails rather than emitting a card that does not add up."""
        import cards

        monkeypatch.setattr(cards, "language_totals",
                            lambda commits: {"Python": {"additions": 1,
                                                        "deletions": 0}})
        with pytest.raises(CardError, match="totals"):
            segments(many, colors)

    def test_it_names_eight_and_folds_the_rest(self, many, colors):
        found = segments(many, colors)
        assert len(found) == NAMED_LANGUAGES + 1
        assert found[-1].name == "+ 3 smaller"

    def test_the_fold_is_never_called_other(self, many, colors):
        """"Other" reads like a language and invites conflating however many
        things happen to be in it."""
        assert "Other" not in render_languages(many, NAME, colors)

    def test_nothing_is_folded_when_everything_fits(self, payload, colors):
        found = segments(payload, colors)
        assert len(found) == 2
        assert not any("smaller" in segment.name for segment in found)

    def test_languages_are_ordered_by_churn(self, many, colors):
        found = segments(many, colors)[:-1]
        assert [s.lines for s in found] == sorted((s.lines for s in found),
                                                 reverse=True)

    def test_every_language_in_the_tables_has_a_colour(self, colors):
        """`segments` only checks the eight it names, so a new include row without a
        colour would break the render on the day that language first reaches the top
        eight rather than on the day it was added."""
        from utils import policy

        rules = policy()
        names = set(rules.include_extensions.values()) | set(
            rules.include_filenames.values())
        assert names <= set(colors["dark"]), sorted(names - set(colors["dark"]))

    def test_every_light_override_names_a_known_language(self, colors):
        """Light is an overrides table; an entry not in dark is a typo that silently
        never applies."""
        assert set(colors["light"]) <= set(colors["dark"])

    def test_a_language_without_a_colour_is_refused(self, payload, colors):
        payload["commits"][0]["languages"] = {"Python": {"additions": 1,
                                                        "deletions": 0}}
        del colors["dark"]["Python"]
        with pytest.raises(CardError, match="no colour"):
            segments(payload, colors)

    def test_a_share_too_small_to_round_is_not_shown_as_zero(self, colors):
        """A rounded-to-zero share reads as "this language did nothing"."""
        payload = {"window": {"from": "2026-01-01", "to": "2026-01-31",
                              "generated_at": "2026-02-01T00:00:00Z"},
                   "prs": [], "review_envelopes": [], "comments": [],
                   "commits": [{"date": "2026-01-02",
                                "languages": {"Python": {"additions": 100_000,
                                                         "deletions": 0},
                                              "Go": {"additions": 20,
                                                     "deletions": 0}}}]}
        assert "&lt;0.1%" in render_languages(payload, NAME, colors)


class TestTheBarTilesExactly:
    """Rounding each segment on its own leaves visible gaps, and a minimum width
    for a tiny segment makes the bar disagree with the legend."""

    def test_the_segments_meet_edge_to_edge(self, real, colors):
        pieces = [r for r in elements(render_languages(real, NAME, colors), "rect")
                  if (r.get("class") or "").startswith("s")]
        edges = [(int(r.get("x")), int(r.get("width"))) for r in pieces]
        assert edges[0][0] == 20
        assert edges[-1][0] + edges[-1][1] == 460
        for (x, width), (next_x, _) in zip(edges, edges[1:]):
            assert x + width == next_x

    def test_a_subpixel_segment_is_dropped_rather_than_widened(self, payload, colors):
        """A segment under half a pixel cannot be drawn. Naming it in the legend
        anyway is the accepted trade, and widening it would make the bar disagree
        with the legend.

        Built rather than read from the real payload: which language happens to be
        sub-pixel changes as the window rolls, and asserting on today's answer would
        fail the scheduled run on an ordinary data shift.
        """
        payload["commits"] = [{"date": "2026-01-02", "languages": {
            "Python": {"additions": 500_000, "deletions": 0},
            "Go": {"additions": 20, "deletions": 0}}}]
        svg = render_languages(payload, NAME, colors)
        drawn = [r for r in elements(svg, "rect")
                 if (r.get("class") or "").startswith("s")]
        assert len(segments(payload, colors)) == 2
        assert len(drawn) == 1
        assert "Go" in svg

    def test_the_track_is_the_full_width(self, real, colors):
        """It shows through wherever a segment rounds away, so the bar reads as
        full rather than as one that stops early."""
        track = [r for r in elements(render_languages(real, NAME, colors), "rect")
                 if r.get("class") == "track"]
        assert [(t.get("x"), t.get("width")) for t in track] == [("20", "440")]


class TestActivityRows:
    def test_every_value_comes_from_the_headline_metrics(self, real):
        metrics = headline_metrics(real)
        authoring, reviewing = activity_rows(real)
        found = {row.label: row.value for row in authoring + reviewing}
        assert found["opened"] == f"{metrics['prs_opened']:,}"
        assert found["merged"] == f"{metrics['prs_merged']:,}"
        assert found["commits"] == f"{metrics['commits']:,}"
        assert found["lines"] == f"{metrics['lines']:,}"
        assert found["reviews given"] == f"{metrics['reviews_given']:,}"
        assert found["approved"] == f"{metrics['reviews_approved']:,}"
        assert found["inline"] == f"{metrics['comments_inline']:,}"

    def test_comments_is_the_sum_of_both_kinds(self, real):
        metrics = headline_metrics(real)
        _, reviewing = activity_rows(real)
        total = metrics["comments_inline"] + metrics["comments_conversational"]
        assert {row.label: row.value for row in reviewing}["comments"] == f"{total:,}"

    @pytest.mark.parametrize("label,indented", [
        ("opened", False), ("merged", True), ("commits", False), ("lines", False),
        ("to merge", False), ("reviews given", False), ("approved", True),
        ("comments", False), ("inline", True),
    ])
    def test_indentation_marks_a_strict_subset(self, real, label, indented):
        """`comments` is not indented under reviews given because it counts a
        different unit, and `commits` is not indented under `opened` because it
        comes from commit history rather than from pull requests."""
        authoring, reviewing = activity_rows(real)
        rows = {row.label: row for row in authoring + reviewing}
        assert rows[label].indented is indented

    def test_an_indented_row_never_exceeds_the_row_above_it(self, real):
        """The contract the indentation asserts, checked against the numbers."""
        for column in activity_rows(real):
            parent = None
            for row in column:
                value = row.value.replace(",", "").rstrip("h")
                if not row.indented:
                    parent = float(value)
                else:
                    assert parent is not None and float(value) <= parent, row.label

    def test_there_is_no_substantive_row(self, real):
        """Approved and substantive are both subsets of reviews given but they
        overlap, so two indented rows would not add up."""
        authoring, reviewing = activity_rows(real)
        assert "substantive" not in {row.label for row in authoring + reviewing}

    def test_the_cycle_row_is_dropped_when_nothing_merged(self, payload):
        for pull_request in payload["prs"]:
            pull_request["cycle_hours"] = None
        authoring, _ = activity_rows(payload)
        assert "to merge" not in {row.label for row in authoring}

    def test_the_rendered_card_shows_every_row(self, real):
        shown = texts(render_activity(real, NAME))
        for column in activity_rows(real):
            for row in column:
                assert row.label in shown
                assert row.value in shown


class TestNoIdentifyingDetail:
    """The cards are committed and served publicly, like the payload they come
    from."""

    @pytest.mark.parametrize("filename", ["activity.svg", "languages.svg"])
    def test_no_card_carries_a_repository_name_or_path(self, real, filename):
        """The payload has none, so this is really a check that rendering does not
        introduce one — a template comment or a hard-coded URL would."""
        for text in texts(render_all(real, NAME)[filename]):
            assert "/" not in text


class TestNearestRank:
    def test_it_does_not_interpolate(self):
        """Pinned to a definition because "median" is ambiguous. Interpolating
        between the two middle values moves the answer in the first decimal place,
        which is the precision the cards report."""
        assert nearest_rank([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0

    def test_the_top_percentile_is_the_largest_value(self):
        assert nearest_rank([1.0, 50.0, 99.0], 1.0) == 99.0

    def test_it_sorts_first(self):
        assert nearest_rank([9.0, 1.0, 5.0], 0.5) == 5.0

    def test_a_percentile_of_nothing_is_an_error(self):
        with pytest.raises(ValueError, match="nothing"):
            nearest_rank([], 0.5)

    @pytest.mark.parametrize("proportion", [0.0, -0.1, 1.5])
    def test_a_proportion_outside_the_range_is_an_error(self, proportion):
        with pytest.raises(ValueError, match="must fall in"):
            nearest_rank([1.0], proportion)
