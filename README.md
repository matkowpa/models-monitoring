# ClinePass models monitoring

Weekly, static comparison of the models available on **ClinePass** (plus Cline's free
promotions), scored on one independent quality measure and the per-million-token rates that
Cline billing actually applies to this account's usage.

**Published report:** <https://matkowpa.github.io/models-monitoring/>

- Current dashboard: `index.html`
- Methodology and provenance guide: `methodology.html`
- Dated reports and snapshots: `archive/index.html`

The full methodology, provenance rules, and verification sequence live in
[`models_monitoring.md`](models_monitoring.md); this README is the operator's view.

## What each run produces

| Output | Purpose |
| --- | --- |
| `site/index.html` | Current dashboard: counts, best values, weekly change summary, chart, model table |
| `site/methodology.html` | How every number is produced, with the formulas and thresholds |
| `site/archive/<date>.html` | The dated report for that run |
| `site/archive/index.html` | Index of every retained report |
| `site/archive.json` | Machine-readable archive index |
| `data/history/<date>.json` | Snapshot: models, rates with provenance, fit statistics, changes, summary |

Rates are **US dollars per 1M tokens**. Quality is the **Artificial Analysis Intelligence
Index** only; the Coding Index is display-only. Cost, efficiency, Pareto membership, and
best-value selection use separate planning and execution workload profiles (section 3 of the
plan).

## How it works

1. Discover the ClinePass catalog and the free promotions from Cline's recommended-models
   endpoint, falling back to the pinned list when it is unavailable; a fallback run is labelled
   as such in the report and snapshot.
2. Read this account's billing usage (`/usages` with cursor paging, `/usages/daily` in windows
   of at most 31 days) and **fit** per-class input, output, and cache-read rates by
   non-negative least squares over the last `rate_window_days` (default 30).
3. Fall back to Cline's dated published reference table for models whose fit cannot be
   estimated and for cache write, which usage rows do not report separately. Measured and
   reference classes are never mixed inside one model, and every rate field carries its
   provenance into the report.
4. Fetch the current AA Intelligence Index. A live run **fails closed** when the key is
   missing, the feed fails, or no catalog model matches a current score.
5. Score, compare against the previous snapshot, summarize (with a deterministic fallback),
   then write the site, the snapshot, and the archive.

The pipeline uses only the Python 3.11+ standard library: no third-party dependencies, no CDN
assets, and no JavaScript a reader's browser must fetch from elsewhere.

## Local checks

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m src.run --offline
```

`--offline` uses `tests/fixtures/` only. It never contacts Cline, Artificial Analysis, or
OpenRouter, it writes to `site-offline/` and `data/history/<date>.offline.json`, and every page
it produces is labelled **OFFLINE - not a live evaluation**. Offline snapshots deliberately use
a different filename so a fixture run can never block the live weekly run.

Useful flags: `--force` (bypass the schedule guard), `--site-dir`, `--history-dir`,
`--fixtures-dir`, `--config`.

## Live run requirements

| Name | Kind | Notes |
| --- | --- | --- |
| `CLINE_API_KEY` | secret | app.cline.bot -> Settings -> API Keys, for the account whose usage prices the report |
| `ARTIFICIAL_ANALYSIS_API_KEY` | secret | required for any scored publication |
| `CLINE_USER_ID` | variable (optional) | must equal `id` from `/api/v1/users/me`; a mismatch fails the run |
| `OPENROUTER_API_KEY` | secret (optional) | only if the metadata request needs an authenticated quota |

The runner needs network access to `api.cline.bot`, `artificialanalysis.ai`, `openrouter.ai`,
and GitHub.

## Deployment

`.github/workflows/weekly-monitor.yml` runs on a GitHub-hosted runner:

- **Schedule:** `0 4 * * 1` and `0 5 * * 1` UTC, which are 06:00 Europe/Warsaw in CEST and CET
  respectively. The application decides with `zoneinfo`: it proceeds only when Warsaw local
  time is Monday 06:00-07:59 **and** no snapshot exists yet for that date, so exactly one of
  the two invocations analyses each week and a delayed run cannot publish twice.
- **Manual dispatch** bypasses both checks.
- **Tests run before** the live pipeline, then the site is committed to `main` and deployed as
  a Pages artifact (`actions/upload-pages-artifact@v3` + `actions/deploy-pages@v4`). A skipped
  schedule skips both the commit and the deployment.
- Pushes made with the workflow's own token cannot re-trigger the workflow.

One-time repository setup:

1. Add the secrets/variable from the table above.
2. Settings -> Pages -> **Source: GitHub Actions**.
3. Pages sites are publicly readable even when the repository is private, and Pages on a
   private repository needs a paid plan. If this comparison must stay internal, choose a
   different publication target (section 8 of the plan).

## Configuration and data

| Path | Owns |
| --- | --- |
| `data/scoring_config.json` | Endpoints and key names, profile token volumes and penalties, verbosity multipliers, `rate_window_days`, rate-fit thresholds, change thresholds, archive limit |
| `data/model_mapping.json` | ClinePass slug -> AA identifier and OpenRouter slug (uncertain matches stay unmatched) |
| `data/clinepass_reference_rates.json` | Dated published reference-rate table with its source URL |
| `data/clinepass_catalog_fallback.json` | Pinned model list used only when discovery is unavailable |
| `tests/fixtures/` | Offline payloads, including usage pages generated from known ground-truth rates |

Code layout: `src/monitor.py` parses, fits, and scores (no I/O), `src/run.py` makes every
external call and orchestrates the run, `src/site.py` renders the pages and owns the snapshot
history.

## Limits worth knowing

- Measured rates describe this account and this window. Peak/off-peak billing (for example
  DeepSeek V4 Pro) appears as one blended measured rate, and only models with enough
  independent token mixes can be fitted at all - the rest are labelled `reference`.
- Free-model availability is a rotating promotion, and free usage is documented as unavailable
  through the Cline API, so a free model's `$0` is catalog-declared and cannot be confirmed
  from billing.
- The integer money scales (`costUsd` at 1e-8 USD; `creditsUsed` and `balance` at 1e-6 USD) are
  inferred rather than documented. Every raw integer is kept in the snapshot, and the pipeline
  raises a run warning when measured rates and the published reference table disagree by more
  than a factor of five, which is what a wrong scale looks like.
- Efficiency points are not dollars or credits, and there is no minimum-quality gate: a cheap
  model can win best value on a profile. Free models have no efficiency value (a zero cost has
  no place in a logarithmic formula); they are reported as free and stay in the Pareto set.
- GitHub does not guarantee scheduled-run timing. The hour tolerance exists for that reason,
  and a missed Monday is reported as a missed run rather than silently skipped.

