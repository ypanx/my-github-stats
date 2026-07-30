"""Rendering the two SVG cards from the published payload.

Both templates are here as strings rather than as separate files, so one file
holds the whole card: size, styles, icon artwork, the light-preference query, and
the layout arithmetic.

Only counted lines reach a card, which is what lets the languages legend total the
lines figure exactly. The renderer asserts that and refuses to write a card that
does not add up.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from constants import CARDS_DIR, COLORS_PATH, STATS_PATH
from utils import (
    cycle_hours,
    headline_metrics,
    language_totals,
    nearest_rank,
    total_lines,
)

#: Languages named individually in the legend. Everything below the eighth is
#: sub-pixel on a 440px bar, so naming more would imply a segment that is not
#: visibly there.
NAMED_LANGUAGES = 8

#: Row geometry, shared by both columns of the activity card.
_FIRST_ROW = 86
_ROW_STEP = 23
_INDENT = 14
_COLUMNS = ((20, 222), (260, 460))


class CardError(Exception):
    """A card could not be rendered, or would not have reconciled."""


@dataclass(frozen=True)
class Row:
    """One line of the activity card."""

    icon: str
    label: str
    value: str
    indented: bool = False


@dataclass(frozen=True)
class Segment:
    """One language, or the fold, as it appears on the languages card."""

    name: str
    lines: int
    color_dark: str
    color_light: str

    def share(self, total: int) -> float:
        return self.lines / total if total else 0.0


ACTIVITY_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="200"
     viewBox="0 0 480 200" role="img" aria-label="{{alt}}">
  <title>{{alt}}</title>

  <!-- Base styles are dark and a light-preference query overrides them, so a
       viewer expressing no preference, or a renderer ignoring the query, gets
       dark. That is the intended look rather than a fallback. -->
  <style>
    .card    { fill: #0d1117; stroke: #30363d; }
    .title   { fill: #e6edf3; font-weight: 600; font-size: 14px; }
    .window  { fill: #8b949e; font-size: 11px; }
    .column  { fill: #8b949e; font-size: 10px; font-weight: 600; letter-spacing: 0.08em; }
    .label   { fill: #c9d1d9; font-size: 12px; }
    .value   { fill: #e6edf3; font-size: 12px; font-weight: 600; }
    .divider { stroke: #30363d; }
    .icon      { fill: none; stroke: #8b949e; stroke-width: 1.4;
                 stroke-linecap: round; stroke-linejoin: round; }
    .icon-fill { fill: #8b949e; stroke: none; }

    @media (prefers-color-scheme: light) {
      .card    { fill: #ffffff; stroke: #d0d7de; }
      .title   { fill: #1f2328; }
      .window  { fill: #656d76; }
      .column  { fill: #656d76; }
      .label   { fill: #424a53; }
      .value   { fill: #1f2328; }
      .divider { stroke: #d0d7de; }
      .icon      { stroke: #656d76; }
      .icon-fill { fill: #656d76; }
    }

    text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica,
                        Arial, sans-serif; }
  </style>

  <!-- Drawn rather than set as Unicode glyphs. An SVG served through GitHub's
       image proxy renders in whatever fonts the viewer has, and the obvious
       glyphs for these are not in common system fonts. -->
  <defs>
    <g id="icon-opened" class="icon"><path d="M6 1.5 10.5 6 6 10.5 1.5 6Z"/></g>
    <g id="icon-merged" class="icon"><path d="M2.5 6.5 5 9l4.5-5"/></g>
    <g id="icon-commits" class="icon">
      <circle cx="6" cy="6" r="2.5"/><path d="M0.5 6h3M8.5 6h3"/>
    </g>
    <g id="icon-lines" class="icon"><path d="M6 1.5v5M3.5 4h5M2.5 10h7"/></g>
    <g id="icon-cycle" class="icon">
      <circle cx="6" cy="6" r="4.5"/><path d="M6 3.5V6l2 1.5"/>
    </g>
    <g id="icon-reviews" class="icon">
      <circle cx="6" cy="6" r="4.5"/><circle cx="6" cy="6" r="1.8" class="icon-fill"/>
    </g>
    <g id="icon-comments" class="icon">
      <path d="M10.5 3.5v4A1.5 1.5 0 0 1 9 9H6l-2.5 2V9H3A1.5 1.5 0 0 1 1.5 7.5v-4A1.5 1.5 0 0 1 3 2h6a1.5 1.5 0 0 1 1.5 1.5Z"/>
    </g>
  </defs>

  <rect class="card" x="0.5" y="0.5" width="479" height="199" rx="6"/>

  <text class="title" x="20" y="30">{{title}}</text>
  <text class="window" x="460" y="30" text-anchor="end">{{window}}</text>

  <line class="divider" x1="240" y1="48" x2="240" y2="184"/>
  <text class="column" x="20" y="62">AUTHORING</text>
  <text class="column" x="260" y="62">REVIEWING</text>

{{rows}}
</svg>
"""

LANGUAGES_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="152"
     viewBox="0 0 480 152" role="img" aria-label="{{alt}}">
  <title>{{alt}}</title>

  <style>
    .card   { fill: #0d1117; stroke: #30363d; }
    .title  { fill: #e6edf3; font-weight: 600; font-size: 14px; }
    .window { fill: #8b949e; font-size: 11px; }
    .name   { fill: #c9d1d9; font-size: 11px; }
    .share  { fill: #8b949e; font-size: 11px; }
    .track  { fill: #21262d; }

    @media (prefers-color-scheme: light) {
      .card   { fill: #ffffff; stroke: #d0d7de; }
      .title  { fill: #1f2328; }
      .window { fill: #656d76; }
      .name   { fill: #424a53; }
      .share  { fill: #656d76; }
      .track  { fill: #eaeef2; }
    }

{{colors}}

    text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica,
                        Arial, sans-serif; }
  </style>

  <defs>
    <clipPath id="bar"><rect x="20" y="44" width="440" height="10" rx="5"/></clipPath>
  </defs>

  <rect class="card" x="0.5" y="0.5" width="479" height="151" rx="6"/>

  <text class="title" x="20" y="30">{{title}}</text>
  <text class="window" x="460" y="30" text-anchor="end">{{window}}</text>

  <!-- The track shows through wherever a segment rounds away to nothing, so the
       bar reads as a full bar rather than as one that stops early. -->
  <rect class="track" x="20" y="44" width="440" height="10" rx="5"/>
  <g clip-path="url(#bar)">
{{segments}}
  </g>

{{legend}}
</svg>
"""


def load_colors(path: Path | None = None) -> dict[str, Any]:
    path = path or COLORS_PATH
    colors = json.loads(path.read_text())
    if missing := {"dark", "light", "fold"} - set(colors):
        raise CardError(f"{path.name} is missing {sorted(missing)}")
    return colors


def activity_rows(payload: dict[str, Any]) -> tuple[list[Row], list[Row]]:
    """The two columns, in order.

    Indentation means strict subset. `comments` is not indented under reviews
    given because it counts a different unit, and `commits` is not indented under
    `opened` because it comes from commit history rather than from pull requests.
    There is no `substantive` row: it and `approved` are both subsets of reviews
    given but they overlap, so two indented rows would not add up.
    """
    metrics = headline_metrics(payload)
    authoring = [
        Row("opened", "opened", f"{metrics['prs_opened']:,}"),
        Row("merged", "merged", f"{metrics['prs_merged']:,}", indented=True),
        Row("commits", "commits", f"{metrics['commits']:,}"),
        Row("lines", "lines", f"{metrics['lines']:,}"),
    ]
    if durations := cycle_hours(payload["prs"]):
        authoring.append(
            Row("cycle", "to merge", f"{nearest_rank(durations, 0.5):.1f}h"))

    reviewing = [
        Row("reviews", "reviews given", f"{metrics['reviews_given']:,}"),
        Row("merged", "approved", f"{metrics['reviews_approved']:,}", indented=True),
        Row("comments", "comments",
            f"{metrics['comments_inline'] + metrics['comments_conversational']:,}"),
        Row("comments", "inline", f"{metrics['comments_inline']:,}", indented=True),
    ]
    return authoring, reviewing


def segments(payload: dict[str, Any], colors: dict[str, Any],
             named: int = NAMED_LANGUAGES) -> list[Segment]:
    """Languages by churn descending, with everything past `named` folded.

    The fold is never called "Other", which reads like a language and invites
    conflating however many things happen to be in it.
    """
    totals = language_totals(payload["commits"])
    ordered = sorted(totals.items(),
                     key=lambda item: (-(item[1]["additions"] + item[1]["deletions"]),
                                       item[0]))
    dark, light = colors["dark"], colors["light"]

    found = []
    for name, counts in ordered[:named]:
        if name not in dark:
            raise CardError(f"{name} has no colour in {COLORS_PATH.name}")
        found.append(Segment(name, counts["additions"] + counts["deletions"],
                             dark[name], light.get(name, dark[name])))

    if folded := ordered[named:]:
        lines = sum(counts["additions"] + counts["deletions"] for _, counts in folded)
        found.append(Segment(f"+ {len(folded)} smaller", lines,
                             colors["fold"], colors["fold"]))

    total = total_lines(payload["commits"])
    if (shown := sum(segment.lines for segment in found)) != total:
        raise CardError(f"the legend totals {shown:,} against {total:,} lines")
    return found


def render_activity(payload: dict[str, Any], name: str) -> str:
    authoring, reviewing = activity_rows(payload)
    lines = []
    for rows, (left, right) in zip((authoring, reviewing), _COLUMNS):
        for index, row in enumerate(rows):
            baseline = _FIRST_ROW + index * _ROW_STEP
            label = left + 22 + (_INDENT if row.indented else 0)
            lines.append(
                f'  <use href="#icon-{row.icon}" x="{left}" y="{baseline - 10}"/>\n'
                f'  <text class="label" x="{label}" y="{baseline}">'
                f'{html.escape(row.label)}</text>\n'
                f'  <text class="value" x="{right}" y="{baseline}" '
                f'text-anchor="end">{row.value}</text>')

    return _fill(ACTIVITY_TEMPLATE, "activity.svg", {
        "alt": html.escape(_alt_activity(payload, name)),
        "title": html.escape(f"{name} · Activity"),
        "window": _window(payload),
        "rows": "\n".join(lines),
    })


def render_languages(payload: dict[str, Any], name: str,
                     colors: dict[str, Any]) -> str:
    found = segments(payload, colors)
    total = total_lines(payload["commits"])

    # Segment edges are rounded once, cumulatively, so the pieces tile the bar
    # exactly. Rounding each width on its own leaves visible gaps, and giving a
    # tiny segment a minimum width would make the bar disagree with the legend.
    edges, offset = [20.0], 20.0
    for segment in found:
        offset += 440 * segment.share(total)
        edges.append(offset)

    pieces = []
    for index in range(len(found)):
        start, end = round(edges[index]), round(edges[index + 1])
        if end > start:
            pieces.append(f'    <rect class="s{index}" x="{start}" y="44" '
                          f'width="{end - start}" height="10"/>')

    legend = []
    for index, segment in enumerate(found):
        column, row = index % 3, index // 3
        x = 20 + column * 147
        baseline = 82 + row * 22
        legend.append(
            f'  <circle class="s{index}" cx="{x + 4}" cy="{baseline - 4}" r="4"/>\n'
            f'  <text class="name" x="{x + 15}" y="{baseline}">'
            f'{html.escape(segment.name)}</text>\n'
            f'  <text class="share" x="{x + 133}" y="{baseline}" text-anchor="end">'
            f'{html.escape(_share_text(segment.share(total)))}</text>')

    return _fill(LANGUAGES_TEMPLATE, "languages.svg", {
        "alt": html.escape(_alt_languages(found, total)),
        "title": html.escape(f"{name} · Languages"),
        "window": f"{total:,} lines",
        "colors": _color_rules(found),
        "segments": "\n".join(pieces),
        "legend": "\n".join(legend),
    })


def _color_rules(found: list[Segment]) -> str:
    """One class per segment, with a light override for each.

    Written as rules rather than as inline fills because a media query cannot
    reach a presentation attribute.
    """
    dark = "\n".join(f"    .s{index} {{ fill: {segment.color_dark}; }}"
                     for index, segment in enumerate(found))
    light = "\n".join(f"      .s{index} {{ fill: {segment.color_light}; }}"
                      for index, segment in enumerate(found))
    return f"{dark}\n\n    @media (prefers-color-scheme: light) {{\n{light}\n    }}"


def _window(payload: dict[str, Any]) -> str:
    return f"{payload['window']['from']} to {payload['window']['to']}"


def _share_text(share: float) -> str:
    """One decimal place, except where that would round a real share to zero."""
    return "<0.1%" if 0 < share < 0.001 else f"{share:.1%}"


def _alt_activity(payload: dict[str, Any], name: str) -> str:
    metrics = headline_metrics(payload)
    return (f"{name}: {metrics['prs_opened']:,} pull requests opened, "
            f"{metrics['commits']:,} commits, {metrics['lines']:,} lines changed, "
            f"{metrics['reviews_given']:,} reviews given, between "
            f"{payload['window']['from']} and {payload['window']['to']}.")


def _alt_languages(found: list[Segment], total: int) -> str:
    named = ", ".join(f"{segment.name} {_share_text(segment.share(total))}"
                      for segment in found)
    return f"Languages by lines changed, {total:,} in total: {named}."


def _fill(template: str, label: str, values: dict[str, str]) -> str:
    """Substitute every placeholder, then prove the result is still a document.

    Two failures here are silent and both ship. A mistyped placeholder draws
    `{{window}}` on the card, and an unescaped `<` in a substituted value makes the
    file invalid XML, at which point a browser renders a parser error instead of a
    card and nothing about the file's size or name suggests anything is wrong.
    """
    for key, value in values.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    if "{{" in template:
        remaining = template[template.index("{{"):].split("}}")[0] + "}}"
        raise CardError(f"{label} still holds {remaining}")
    try:
        ElementTree.fromstring(template)
    except ElementTree.ParseError as error:
        raise CardError(f"{label} rendered to invalid XML: {error}") from None
    return template


def render_all(payload: dict[str, Any], name: str,
               colors: dict[str, Any] | None = None) -> dict[str, str]:
    """Both cards, keyed by the filename each belongs in."""
    colors = colors if colors is not None else load_colors()
    return {"activity.svg": render_activity(payload, name),
            "languages.svg": render_languages(payload, name, colors)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cards.py", description="Render the two SVG cards from data/stats.json.")
    parser.add_argument("--stats", default=str(STATS_PATH),
                        help=f"published payload to read (default: {STATS_PATH})")
    parser.add_argument("--out", default=str(CARDS_DIR),
                        help="directory to write the cards into")
    parser.add_argument("--name", default="John Pan",
                        help="name shown on both cards")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.stats).read_text())
    try:
        cards = render_all(payload, args.name)
    except CardError as error:
        print(f"cards not rendered: {error}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for filename, svg in cards.items():
        (out / filename).write_text(svg)
        print(f"wrote {out / filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
