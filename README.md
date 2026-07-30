# my-github-stats

Profile statistics built around **reviews and pull request activity** across every
repository the account touches, rather than around stars and forks.

One dataset, two outputs:

1. **Two SVG cards**, rendered and committed here, served over `raw.githubusercontent.com`.
2. **An interactive dashboard** on GitHub Pages with a configurable time window.

Everything is derived from a rolling window of the trailing year and refreshed
daily by a scheduled workflow.

## What gets published

| Path | Contents |
|---|---|
| `data/stats.json` | Aggregated records. No repository names, file paths, commit hashes, pull request numbers, or logins. |
| `data/stats.local.json` | The same records with repository attribution. **Gitignored**, and never leaves the machine that produced it. |
| `generated/*.svg` | The two rendered cards. **Not built yet.** |
| `docs/index.html` | The dashboard. **Not built yet.** |

The collector already withholds everything identifying: `data/stats.json` contains
no repository name, file path, commit hash, login, pull request number, or activity
timestamp, and the ordering of its records is a function of their content so that
it cannot imply how work was distributed across repositories.

A standalone privacy check that rejects the file structurally, and the scheduled
workflow that would run it before each commit, are **still to be written**. Until
then nothing is published automatically.

## Layout

```
client/          GitHub API access, with retry and cost accounting
collector/       collection, classification, checks, and reporting
collect.py       entry point
policy.yml       which files count as which language
tests/           offline test suite
```

The collector is layered so each concern can be read on its own:

| Module | Responsibility |
|---|---|
| `constants` | measured API limits, thresholds, and the published shape |
| `windows` | dates, windows, and the slicing of searches |
| `queries` | GraphQL documents and the search strings that fill them |
| `collector` | the collection paths that fetch every record |
| `policy` | loading and validating the classification policy |
| `classify` | deciding what language a file is, from its path alone |
| `aggregate` | accumulating classified records into language totals |
| `assemble` | building the two payloads from collected records |
| `metrics` | figures derived from a payload |
| `guards` | the checks that must pass before anything is written |
| `report` | human-readable summaries |
| `collect` | orchestration and the command line |

## Running it

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

export GITHUB_TOKEN=<classic token, scopes read:user and repo, SSO-authorized>

.venv/bin/python collect.py --horizon 365        # what the schedule runs
.venv/bin/python collect.py --since 2025-07-30 --until 2026-07-29
.venv/bin/python collect.py --summary data/stats.json
.venv/bin/python -m pytest tests/
```

Both window ends are inclusive, and `--horizon` counts dates, so a horizon of 365
covers 365 dates. A window longer than that is refused before any request is made,
because the contributions API cannot answer it in one query.

`--verbose` adds repository names, file paths and commit hashes to the output. It
is a local convenience only: workflow logs on a public repository are public, so
the schedule must never pass it.

`--classification` prints a table of every file tag with its line totals and what
became of it. That table is the most useful diagnostic here, because a data or
metadata type quietly counting as a language does not disturb any total.

## Notes

Versions in `requirements.txt` are pinned deliberately. The language breakdown
depends on the tag tables of the `identify` library, so an unpinned upgrade would
move the line counts with nothing else having changed.

Nothing is written unless every check passes, so a failed run leaves the previous
pair of files exactly as it found them.
