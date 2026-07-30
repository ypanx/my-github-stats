"""Human-readable summaries of a payload.

File paths appear only under verbose output. Repository names never appear here
at all, because everything this module reads has already had them removed or is
about to be published.
"""

from __future__ import annotations

from typing import Any

from collector.aggregate import Aggregate, check_unknown_share, identify_version, tag_disposition
from collector.constants import REVIEW_STATES
from collector.metrics import (
    cycle_hours,
    headline_metrics,
    language_totals,
    nearest_rank,
    reviews_given,
    total_lines,
)


def print_summary(payload: dict[str, Any], *, title: str = "summary") -> None:
    """Everything a card or dashboard would derive, computed from the payload."""
    metrics = headline_metrics(payload)
    languages = language_totals(payload["commits"])
    lines = total_lines(payload["commits"])

    print(f"\n{title}")
    print("=" * len(title))
    print(f"  window                {payload['window']['from']} to {payload['window']['to']}")
    print(f"  generated             {payload['window']['generated_at']}")
    print(f"  commits               {metrics['commits']:>8,}")
    print(f"  lines changed         {lines:>8,}")

    print("\n  languages")
    for name, counts in sorted(languages.items(),
                               key=lambda item: -(item[1]["additions"] + item[1]["deletions"])):
        churn = counts["additions"] + counts["deletions"]
        share = churn / lines if lines else 0.0
        print(f"    {name:<18} {churn:>8,}  {share:>7.2%}   "
              f"+{counts['additions']:,} / -{counts['deletions']:,}")

    print(f"\n  pull requests opened  {metrics['prs_opened']:>8,}")
    print(f"  merged                {metrics['prs_merged']:>8,}")
    print(f"  still open or closed  {metrics['prs_opened'] - metrics['prs_merged']:>8,}")
    durations = cycle_hours(payload["prs"])
    if durations:
        print(f"  time to merge, median {nearest_rank(durations, 0.5):>8.1f}h")
        print(f"  time to merge, p90    {nearest_rank(durations, 0.9):>8.1f}h")

    given = reviews_given(payload["review_envelopes"])
    print(f"\n  reviews given         {len(given):>8,}")
    for state in REVIEW_STATES:
        print(f"    {state.lower().replace('_', ' '):<18} "
              f"{sum(1 for r in given if r['state'] == state):>8,}")
    print(f"  substantive           {metrics['reviews_substantive']:>8,}")
    print(f"  envelopes in total    {metrics['review_envelopes']:>8,}")
    print(f"  on own pull requests  "
          f"{metrics['review_envelopes'] - len(given):>8,}")

    print(f"\n  comments              {len(payload['comments']):>8,}")
    print(f"    inline              {metrics['comments_inline']:>8,}")
    print(f"    conversational      {metrics['comments_conversational']:>8,}")

    print(f"\n  active dates          {metrics['active_days']:>8,}")


def print_classification(totals: Aggregate, *, verbose: bool = False) -> None:
    """The tag-level view of how file records were classified.

    This table is the most useful diagnostic the collector produces. Reading it is
    what reveals a data or metadata type quietly counting as a language, which no
    total can show, because the totals still reconcile either way.
    """
    print("\nclassification by tag")
    print("=====================")
    print(f"  tagging library       identify {identify_version()}")
    print("\n  The outcome column reports what became of files carrying each tag,")
    print("  not what the tag would mean alone: an image tag resolves to ignored")
    print("  when the file also carries a data tag.\n")

    rows = []
    for tag in sorted(totals.tag_lines, key=lambda t: (-totals.tag_lines[t], t)):
        observed = totals.tag_outcomes[tag]
        if len(observed) == 1:
            outcome = next(iter(observed))
        else:
            shown = list(observed.items())[:3]
            outcome = "; ".join(f"{name} x{count:,}" for name, count in shown)
            if len(observed) > len(shown):
                outcome += f"; and {len(observed) - len(shown)} more"
        note = tag_disposition(tag)
        rows.append((tag, f"{totals.tag_records[tag]:,}", f"{totals.tag_lines[tag]:,}",
                     outcome + (f" [{note}]" if note else "")))

    widths = [max(len(row[i]) for row in rows) for i in range(4)] if rows else [0, 0, 0, 0]
    header = ("tag", "records", "lines", "outcome")
    print("  " + "  ".join(h.ljust(widths[i]) if i in (0, 3) else h.rjust(widths[i])
                           for i, h in enumerate(header)))
    print("  " + "  ".join("-" * max(widths[i], len(header[i])) for i in range(4)))
    for row in rows:
        print("  " + "  ".join(row[i].ljust(widths[i]) if i in (0, 3)
                               else row[i].rjust(widths[i]) for i in range(4)))

    share = check_unknown_share(totals)
    print("\n  unclassifiable by type")
    for name, counts in sorted(totals.unknown_types.items(),
                               key=lambda item: (-item[1]["lines"], item[0])):
        print(f"    {name:<18} {counts['records']:>5} records  {counts['lines']:>6} lines")
    print(f"\n  share                 {share.unknown_lines:,} of {share.denominator:,}"
          f" = {share.share:.4%}")
    print(f"  measured against      {share.basis}")
    print(f"  threshold             {share.threshold:.0%}")

    if verbose:
        print("\n  example paths per unclassifiable type")
        for name in sorted(totals.unknown_examples):
            for path in totals.unknown_examples[name]:
                print(f"    {name:<18} {path}")
    else:
        print("\n  Example paths are shown only with verbose output.")
