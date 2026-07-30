# my-github-stats

Profile statistics built around **reviews and pull request activity** across every
repository the account touches, rather than around stars and forks. Derived from a
rolling window of the trailing year and refreshed daily by a scheduled workflow.

Two outputs: a pair of SVG cards for a profile README, and an interactive dashboard
on GitHub Pages.

## Files

```
collect.py            collection, orchestration, command line
client.py             GitHub API access, with retry and backoff
cards.py              the two SVG templates and their rendering
queries.py            GraphQL documents and search strings
constants.py          measured API limits and the published shape
utils.py              dates, classification, assembly, derived figures, the checks
languages.yml         which files count as which language
language-colors.json  segment colours, read by cards.py and the dashboard
cards/                the two rendered cards
dashboard/            the dashboard
data/                 the two output files
tests/                one test file per implementation file
```

Three dependencies: `requests`, `PyYAML`, `pytest`. The dashboard has none.

## What gets published

| Path | Contents |
|---|---|
| `data/stats.json` | Aggregated records. No repository names, file paths, commit hashes, pull request numbers, logins, or activity timestamps. |
| `data/stats.local.json` | The same records with repository attribution. **Gitignored**, and never leaves the machine that produced it. |
| `cards/*.svg` | The two rendered cards, committed and served over `raw.githubusercontent.com`. |
| `dashboard/index.html` | The dashboard, which reads `../data/stats.json`. |

## Running it

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

export GITHUB_TOKEN=<classic token, scopes read:user and repo, SSO-authorized>

.venv/bin/python collect.py --horizon 365      # what the schedule runs
.venv/bin/python collect.py --since 2025-07-31 --until 2026-07-30
.venv/bin/python cards.py                      # re-render the two cards
.venv/bin/python -m pytest -q
```

Both window ends are inclusive and `--horizon` counts dates, so 365 covers 365
dates, which is the most the contributions API answers in one query.

Two checks run before anything is written, and a failure leaves the previous pair of
files untouched: that the run saw data at all, and that no headline figure fell by
more than half against the previous run.

`--verbose` adds repository names, file paths and commit hashes to the output. Local
use only — workflow logs on a public repository are permanent.

## The cards

```markdown
![Activity](https://raw.githubusercontent.com/ypanx/my-github-stats/main/cards/activity.svg)
![Languages](https://raw.githubusercontent.com/ypanx/my-github-stats/main/cards/languages.svg)
```

One image each. Each file carries both themes in a media query, defaulting to dark;
do not add a second image or a `#gh-dark-mode-only` fragment.

## The dashboard

Served from Pages at `https://ypanx.github.io/my-github-stats/dashboard/`. Locally:

```bash
.venv/bin/python -m http.server 8000     # then open /dashboard/
```

It must be served from the project root rather than opened as a file, because it
fetches `../data/stats.json`. Window presets are 30, 60, 90 and 365 days, the current
quarter, year to date, and a custom range, all filtered in the browser.

## The schedule

`.github/workflows/main.yml` runs daily: collect, render, test, commit.

Two things it needs that are not in this repository:

1. `secrets.ACCESS_TOKEN` — a classic token with `read:user` and `repo`,
   SSO-authorized for every organization that should be counted.
2. Pages pointed at the **root** of the default branch, which is a repository
   setting. Pages accepts only `/` or `/docs`, and the dashboard lives below the
   root.

Collect once by hand first. The day-over-day check reports a skip rather than a
failure when there is no previous file, so a first scheduled run is unguarded against
the failure it exists to catch.
