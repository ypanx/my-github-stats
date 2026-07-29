# my-github-stats

Profile stats built around **reviews and PR activity** across every repository the
account touches — not stars and forks.

One dataset, two outputs:

1. **Two SVG cards**, rendered and committed here, served via `raw.githubusercontent.com`.
2. **An interactive dashboard** on GitHub Pages with a configurable time window.

Everything is derived from a rolling trailing 365 days and refreshed daily by a
GitHub Actions cron.

## Status

Under construction. Phase 0 of 7 — repo skeleton and auth smoke test.

## What gets published

| File | Contents |
|---|---|
| `stats.json` | Aggregated event records. No repo names, file paths, commit SHAs, PR numbers, or logins. |
| `generated/*.svg` | The two rendered cards. |
| `docs/index.html` | The dashboard. |

A `privacy_check.py` gate runs before every commit in CI and rejects `stats.json`
if it carries anything identifying. Repo-attributed output stays in a gitignored
`stats.local.json` that never leaves the machine that produced it.

## Local setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

export GITHUB_TOKEN=<classic PAT, scopes read:user + repo, SSO-authorized>
.venv/bin/python verify_auth.py
```

`verify_auth.py` exists because the failure mode is silent: an unauthorized token
returns HTTP 200 with zeros rather than an error. Run it before trusting any
number this project produces.
