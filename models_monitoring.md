# ClinePass Models Monitoring: Implementation Plan and Methodology

## 1. Purpose and Current Design

This project produces a weekly, static comparison of the models available on ClinePass (the `cline-pass/*` catalog) including the free-of-charge models Cline publishes alongside it. It evaluates value in two workload scenarios, planning and execution, using one independent quality measure and the per-million-token rates that Cline billing actually applies to this account's usage. Each successful run creates a timestamped report, a dated historical report, and a JSON snapshot; the GitHub Actions workflow publishes the generated site to GitHub Pages and commits report history to the repository.

Design inputs are deliberately narrow:

- **Quality:** the Artificial Analysis (AA) Intelligence Index is the only quality input.
- **Price:** ClinePass per-class rates measured from the Cline billing usage API, with Cline's published ClinePass reference-rate table as a dated fallback only.
- **OpenRouter:** optional model metadata (display name, context window, capabilities) only. Its prices are never substituted for Cline rates and never affect quality, cost, efficiency, or recommendations.

The workflow's narrative model is `cline-pass/deepseek-v4.1-flash`, selected from the ClinePass catalog. It summarizes already-computed weekly changes; it does not generate benchmark scores or rates. If that model is absent from the catalog, use `cline-pass/glm-5.3-flash`, then `cline-pass/kimi-k3`; the fallback chain lives in `src/run.py`, and the report and snapshot record which model produced the summary. If every candidate fails, a deterministic change summary is used.

## 2. End-to-End Data Flow

1. **Discover the ClinePass catalog.** Request `https://api.cline.bot/api/v1/ai/cline/recommended-models` with `Authorization: Bearer <CLINE_API_KEY>` and parse the `clinePass` array (`cline-pass/<slug>` ids with optional display name and description) and the `free` array (promotional free models). Free-model ids are not uniformly prefixed (`cline-free/<slug>` and provider-style ids such as `stealth/space-bunny-alpha` both occur), so membership in the `free` array is the discriminator, not the id prefix. The payload carries no prices, context lengths, or capabilities. If the endpoint is unreachable, returns a non-OK status, or yields no `clinePass` entries, fall back to the pinned list in `data/clinepass_catalog_fallback.json` and mark the run as catalog-fallback in the report and snapshot.
2. **Enrich model metadata (optional).** Request `https://openrouter.ai/api/v1/models` and join display name, context window, and capabilities by slug (strip the `cline-pass/` prefix; use `data/model_mapping.json` aliases where the slug differs). If this public endpoint is unavailable, continue with Cline-provided names and unknown metadata. No price is taken from OpenRouter.
3. **Read Cline billing usage.** `GET /api/v1/users/me` to resolve the account id (which must equal `CLINE_USER_ID` when that variable is set), then `GET /api/v1/users/{id}/usages?limit=1000` following `nextToken` until exhausted, and `GET /api/v1/users/{id}/usages/daily?startDate=&endDate=` for server-aggregated totals in windows of at most 31 days. Every response is wrapped as `{ data, error, success }`; `success: false` or `data: null` is a failure, not an empty result. `GET /api/v1/users/{id}/balance`, `GET /api/v1/users/me/plan`, and `GET /api/v1/users/me/plan/usage-limits` are optional quota context.
4. **Derive rates from billing usage.** Fit per-class rates for every ClinePass model with enough usage rows in the rate window, assign dated reference rates where the fit is not estimable, and assign declared zero rates to catalog-free models. See §3.2.
5. **Fetch current AA evidence.** Request the configured AA LLM models endpoint with `ARTIFICIAL_ANALYSIS_API_KEY`, then match ClinePass models to AA models using the explicit mapping in `data/model_mapping.json`, supported by the provider name and `metadata.raw_model` recorded on the usage rows. Live publication requires at least one matched Intelligence Index value. Missing credentials, a failed AA request, or no matched Intelligence Index makes the live run fail closed; old benchmark values are not reused as current evidence.
6. **Score models.** Normalize the AA Intelligence Index onto 0–100, calculate planning and execution task costs in US dollars and their separate efficiency values, and determine profile-specific Pareto membership. The AA Coding Index is retained for display only.
7. **Compare with the preceding snapshot.** Select the latest snapshot earlier than the report date and detect catalog additions/removals, AA quality changes, measured rate changes per class, and observed blended-rate drift. The narrative model receives this factual delta and the current model evidence to produce a short summary, with a deterministic fallback if narrative inference fails.
8. **Render, deploy, and push.** Write the current dashboard, benchmark/methodology page, archive index, dated report, and JSON snapshot. Upload the `site/` tree as the Pages artifact and deploy it, and commit `data/history/` plus the generated `site/` tree to the GitHub `main` branch.

Primary ownership is `src/monitor.py` for data parsing and scoring, `src/run.py` for orchestration and external calls, `src/site.py` for report rendering and history, and `.github/workflows/weekly-monitor.yml` for scheduling, runner setup, deployment, and push.

## 3. Scoring Methodology

### 3.1 Quality

For each mapped model, let $Q$ be its AA Intelligence Index on the published 0–100 scale. If the API reports a different declared scale, normalize it using its bounds:

$$Q = \mathrm{clamp}\left(100 \times \frac{s-s_{min}}{s_{max}-s_{min}}, 0, 100\right)$$

where $s$ is the returned score. The active feed is expected to provide the AA Intelligence Index on 0–100, so the normalized value is effectively the published score. A model without a current matched Intelligence Index has no quality or efficiency score and cannot be recommended. The API retrieval date is recorded as retrieval metadata; it is not represented as the date that AA evaluated the model.

The AA Coding Index is displayed as contextual information only. It is not added to $Q$, task-specific quality, efficiency, or best-value selection. No minimum-quality gate is applied.

### 3.2 Cline Billing Rates and Task Cost

Every rate below is US dollars per one million tokens, taken from Cline billing for this account. Three rate classes are priced per model: fresh input, output, and cache read; cache write is carried as a fourth field when a source defines it.

#### Measured rates (primary source)

For model $m$, collect this account's ClinePass usage rows in the rate window $W$ (`rate_window_days`, default 30, in `scoring_config.json`). ClinePass rows are selected by `aiModelTypeName == "cline-pass"`; rows for the same model billed through the usage-billing provider are excluded from the fit. For a row $r$:

- $t^{cr}_r$ = `cachedTokens`
- $t^{in}_r$ = `promptTokens` − `cachedTokens` (cached tokens are a subset of prompt tokens)
- $t^{out}_r$ = `completionTokens`
- $y_r = \texttt{costUsd}_r / 10^8$ (US dollars; the raw integer is retained in the snapshot)

Then fit

$$y_r \approx \frac{t^{in}_r P_{in} + t^{out}_r P_{out} + t^{cr}_r P_{cr}}{1{,}000{,}000}$$

by non-negative ordinary least squares over $(P_{in}, P_{out}, P_{cr})$. ClinePass bills a single reference cost per request, so the per-class rates are recovered jointly rather than read from a price field. The fit is accepted only when all of these hold:

- at least 12 rows for that model inside $W$;
- the token-mix matrix has rank 3 with a scaled condition number ≤ $10^6$ (i.e. at least three independent token mixes, not one repeated prompt shape);
- no fitted rate is negative, and both $P_{in}$ and $P_{out}$ are strictly positive;
- reconciliation of the total: $|\sum \hat{y} - \sum y| / \sum y \le 1\%$;
- per-row RMS residual ≤ max(2% of the model's mean row cost, $2\times10^{-5}$ USD) to absorb integer rounding of `costUsd`.

Rows with missing or inconsistent token counts (`promptTokens < cachedTokens`) are dropped, and the dropped count is stored in the snapshot.

#### Reference rates (fallback source)

`data/clinepass_reference_rates.json` holds a dated copy of Cline's published ClinePass reference table (input, output, cached read, cached write per 1M USD) together with its source URL and retrieval date. It is used for a model whose fit is rejected, for a model with no ClinePass usage inside $W$, and for cache write, which usage rows do not report separately. Cache write is never fitted; when the reference table has no value, cache write uses $P_{in}$.

#### Free models

Models listed in the catalog's `free` array carry explicit zero input, output, and cache rates with provenance `catalog-free`. They are never fitted — Cline documents free-model usage as unavailable through the API, so no billing rows are expected — and inference never turns a zero rate into a nonzero one.

Every rate field in the snapshot records its value, provenance (`measured`, `reference`, `catalog-free`, or `input-fallback`), window start/end, row count, and fit residual where applicable. A report must never describe a `reference` or fallback value as a measured current rate.

#### Task cost

For rate $P_j$ and task token volume $T_j$ across cache-read, fresh input, cache-write and output classes, estimated task cost is:

$$C = \frac{T_{cr}P_{cr} + T_{in}P_{in} + T_{cw}P_{cw} + T_{out}P_{out}}{1{,}000{,}000} \times V$$

Here $V$ is the model-specific verbosity multiplier from `scoring_config.json`, default 1.0. If the cache-read or cache-write rate is absent, that token class uses the input rate. An explicitly reported zero cache rate means free cache tokens. Missing or zero base input or output rates make the task cost unavailable rather than creating a misleading zero-cost model; the single exception is a catalog-free model, where zero input and output rates are a declared promotion rather than missing data.

The checked-in task profiles and penalties are:

| Profile   | Cache read | Fresh input | Cache write | Output | Cost penalty $\lambda$ |
| --------- | ---------: | ----------: | ----------: | -----: | ---------------------: |
| Planning  | 200,000    | 30,000      | 10,000      | 8,000  | 5                      |
| Execution | 400,000    | 40,000      | 20,000      | 15,000 | 8                      |

All prices and task costs are **US dollars**. Input, output, cache-read, and cache-write rates are stored separately. The table and chart label rates per 1M tokens and estimated profile cost per task; the estimate is a standardized comparison scenario, not a promise of actual production usage.

When both base input and output rates are known, the implementation also stores an informational blended rate:

$$P_{blend} = 0.8P_{input} + 0.2P_{output}$$

This field is used for weekly price-change detection, not task efficiency. Alongside it, billing usage yields an observed blended rate per model,

$$\bar{P}_m = \frac{\sum y_r}{\sum t^{total}_r / 10^6}$$

where $t^{total}_r$ is `totalTokens` (falling back to prompt + completion). The observed blended rate describes what this account actually paid per token: it is stored per model on every run, and a move beyond 10% between runs is reported as a billing-rate change event. Neither blended value replaces the explicit token-profile cost calculation.

### 3.3 Planning and Execution Efficiency

Planning and execution share the same quality input $Q$ but use their own task cost, catalog median, and penalty. For each profile, the reference median is calculated from all discovered models with a valid positive task cost for that profile; the quality score is not required to contribute to the median. For a model with a current quality score and a valid positive cost:

$$E_{plan} = Q - 5 \times \log_2\left(\frac{C_{plan}}{median(C_{plan})}\right)$$

$$E_{exec} = Q - 8 \times \log_2\left(\frac{C_{exec}}{median(C_{exec})}\right)$$

These are **efficiency points**, not dollars, credits, or a percentage. A model at its profile median cost has $E=Q$. A model costing twice the median loses 5 planning points or 8 execution points; one costing half the median gains the corresponding number of points. Higher is better. The two values remain separate in the model table and profile-specific best-value cards.

No efficiency is produced if the current AA quality, positive task cost, or a valid median reference is unavailable. A missing cost/score is rendered as unavailable, not treated as zero. Catalog-free models have a zero task cost, so $\log_2$ has no value for them: they are excluded from both medians and from efficiency, and they are shown with an explicit `free` marker instead.

### 3.4 Pareto and Combined Chart

For each profile, a model is Pareto-optimal if it has a quality score and that profile's task cost and no other model has both equal-or-higher quality and equal-or-lower cost with at least one strict improvement. The profile Pareto booleans are calculated separately for planning and execution. Best-value cards select the highest efficiency among that profile's Pareto models; there is no quality threshold. Because catalog-free models have no efficiency value, a best-value card always names the best paid model for that profile; when free models are present, the lowest-cost figure shows $0 and the free models are flagged so the paid recommendation is not mistaken for the cheapest option available.

The single cost-efficiency chart plots each eligible model once:

- Vertical axis: AA Intelligence Index $Q$.
- Horizontal axis: arithmetic mean of available planning and execution task costs in USD, on a logarithmic scale. If only one profile cost is available, that available cost is plotted.
- Marks: one point per model with a quality score and at least one positive profile cost.
- Free models: a zero cost has no place on a logarithmic axis, so catalog-free models are drawn as labeled `$0 (free)` markers in a band along the left edge of the plot, with a note. They remain members of the non-dominated set, because no model can be cheaper.
- Single frontier: the non-dominated set computed using the plotted average cost and $Q$; frontier models are labeled. The curve is not two separate profile frontiers.
- Legend: lower-right within the chart plot.

This combined chart frontier is distinct from the profile-specific Pareto flags used for planning/execution best-value recommendations. The comparison table remains the detailed place to compare each profile independently.

## 4. Report, Interactions, and History

The generated static dashboard includes discovered/priced/benchmarked counts, highest quality, planning and execution best value, weekly change summary, the combined cost-quality chart, the sortable/filterable model table, methodology, and historical report links. The table includes model/family, AA quality, informational Coding Index, Cline billing input/output/cache-read/cache-write rates in USD per million with their source, planning/execution task cost in USD, planning/execution efficiency points, and AA evidence metadata. Header sorting, per-column filters, model search, family selection, and quick filters including **Benchmarked & Priced** and **Measured rates** are implemented in the generated page.

Top planning/execution comparison fields are not benchmark-derived dimensions in the current scoring design; the earlier capability matrix and component-benchmark planner/executor rankings were removed from the active report. This preserves the single AA quality measure while retaining separate planning/execution cost and efficiency columns.

Each run stores a report timestamp (date, time, and timezone where available) in the dashboard and snapshot. The date is in `Europe/Warsaw` for report selection. The archive stores a dated HTML report and `archive.json`; the benchmark/methodology guide is also generated. Historical links point from the current dashboard to each retained report.

Every rate and cost figure carries its provenance into the page: a measured rate is shown with its rate window and row count, a fallback rate is labelled as Cline's published reference price, and a catalog-free model is labelled `free`. The page never presents a fallback catalog or a fallback rate as a live value.

The current delta detector compares model additions/removals, material changes in AA quality, measured rate changes per class, and drift in the observed blended rate. It does **not** currently compare every rate field, efficiency rank, or Pareto/recommendation shift. The LLM summary is constrained to the supplied changes and available model evidence, at 100 words maximum; it must not invent missing values. This scope should be preserved unless change detection is deliberately expanded and tested.

## 5. Weekly Workflow and Deployment

`.github/workflows/weekly-monitor.yml` runs on a GitHub-hosted `ubuntu-latest` runner. Since GitHub schedules use UTC and Warsaw alternates between UTC+1 and UTC+2, the workflow schedules both Monday UTC candidates:

- `04:00 UTC` on Monday, for `06:00 Europe/Warsaw` during summer time (CEST).
- `05:00 UTC` on Monday, for `06:00 Europe/Warsaw` during standard time (CET).

The application decides whether to proceed by computing the current time with `zoneinfo` (`Europe/Warsaw`) — GitHub runners keep a UTC clock, so the host's local time must not be used — and runs only when all of the following hold:

- the Warsaw date is a Monday;
- the Warsaw hour is 06 or 07, which absorbs the delay GitHub scheduled runs can experience under load;
- no snapshot exists yet for that Warsaw date, which makes the two cron entries mutually exclusive and prevents a delayed or repeated invocation from publishing a second report for the same day.

Manual `workflow_dispatch` bypasses both the hour check and the same-day snapshot check.

The build job checks out `main`, installs Python 3.11 or newer together with `requirements.txt`, runs `python -m src.run`, commits changed `data/history/` and `site/` files as `github-actions[bot]` and pushes them to `main`, then uploads `site/` as the `github-pages` artifact. A second, dependent deploy job uses the `github-pages` environment and deploys that artifact. A push made with the workflow's own token does not trigger another workflow run, so the history commit cannot loop.

Workflow configuration:

- `permissions:` `contents: write` (history commit), `pages: write`, `id-token: write` (Pages deployment).
- `concurrency: group: weekly-monitor, cancel-in-progress: false`, so a scheduled run never cancels an in-flight one.
- Repository setting: Settings → Pages → Source = **GitHub Actions**.
- `TZ: Europe/Warsaw` is exported for the job so log timestamps and any library default agree with the report time zone.

Required credentials/configuration:

- `CLINE_API_KEY` (repository secret) — account API key created in app.cline.bot under Settings → API Keys, belonging to the account whose usage prices the report.
- `ARTIFICIAL_ANALYSIS_API_KEY` (repository secret; mandatory for live scored publication).
- `CLINE_USER_ID` (optional repository variable) — when set it must equal `id` from `/api/v1/users/me`; a mismatch fails the run.
- Optional `OPENROUTER_API_KEY` if the metadata request needs an authenticated quota.
- Runner network access to `api.cline.bot`, `artificialanalysis.ai`, `openrouter.ai`, and GitHub.

The narrative model is explicitly set to `cline-pass/deepseek-v4.1-flash` in the workflow and defaults to the same id in `src/monitor.py`. The live catalog must contain that model and permit chat completions; when it does not, the documented fallback chain applies and the report records which model was used. Offline runs use fixtures, do not access these systems, and must never be represented or published as a live evaluation.

Repository setup (creating the GitHub repository, enabling Pages, and adding the secrets) requires an authorized credential; running the offline report and tests alone does not establish a repository, a Pages site, or a deployed artifact.

## 6. Configuration and Data Ownership

- `data/scoring_config.json`: AA endpoint and evaluation keys, profile token assumptions, penalties, verbosity multipliers, `rate_window_days`, and the rate-fit acceptance thresholds from §3.2.
- `data/model_mapping.json`: ClinePass slug to AA identifier, OpenRouter slug, and version aliases. Uncertain matches must be reviewed instead of silently mapped.
- `data/clinepass_reference_rates.json`: dated published ClinePass reference-rate table (USD per 1M per class) with its source URL and retrieval date; the fallback rate source and the cache-write source.
- `data/clinepass_catalog_fallback.json`: pinned ClinePass model list used only when the recommended-models endpoint is unavailable; using it marks the run as catalog-fallback.
- `data/history/YYYY-MM-DD.json`: dated snapshots including all model fields, per-field rate provenance with window and row count, fit statistics, observed blended rates, changes, summary, and report generation time.
- `tests/fixtures/`: offline recommended-models payload, paged usage responses generated from known ground-truth rates, AA payload, plan/usage-limit responses, and malformed-envelope examples.
- `site/`: generated current report, benchmark guide, archive index, and dated archive reports. Treat these as build outputs generated by the report pipeline and deployed to Pages.

The checked-in `data/benchmarks.json` and legacy model fields are not inputs to current scores. Do not re-enable their values in scoring without a separately specified methodology, provenance/freshness rules, source-specific normalization, mapping validation, and tests that demonstrate why combining them with the AA composite does not double-count evidence.

## 7. Implementation and Verification Sequence

The implementation is already present in the workspace. For maintenance or a rebuild, use this order so each boundary is independently verifiable:

1. **Catalog discovery:** validate `clinePass` and `free` parsing, provider-style free ids, missing or empty arrays, non-OK responses, and the pinned-fallback path. Confirm a catalog-fallback run is marked in the report and snapshot and is never described as live discovery.
2. **Billing usage ingestion:** unwrap the `{ data, error, success }` envelope, paginate on `nextToken` with a repeated-cursor guard, split daily queries into windows of at most 31 days, derive fresh input as prompt − cached and reject negative results, and verify the money scales (`costUsd` stored raw and divided by 10^8; `creditsUsed` and `balance` divided by 10^6) against a known request before any value is used.
3. **Rate fitting:** generate synthetic usage rows from known rates and require the fit to recover them within tolerance; test rank-deficient token mixes, negative-rate rejection, the reconciliation and residual thresholds, the free-model path, and the `reference` and `input-fallback` provenance labels. Confirm classes that cannot be identified stay unavailable instead of being guessed.
4. **Mapping and AA ingestion:** test ID normalization/aliases, Intelligence Index normalization, Coding Index display-only behavior, and fail-closed live publication for a missing key, feed, or match. Confirm a missing current score clears any old derived score fields.
5. **Cost and efficiency:** use known rates and token volumes to hand-check both task cost equations in USD, missing-cache fallback, explicit zero cache pricing, catalog-free zero rates, per-profile medians, logarithmic penalties, and absence of any quality gate.
6. **Pareto/chart:** prove each model appears once, the combined average-cost frontier is non-dominated, free models are in the frontier but outside the log axis, every frontier model has an annotation, and the legend is inside the lower-right chart area. Test missing/partial profile costs and no eligible data.
7. **Report interactions and history:** run the offline pipeline and assert report timestamp, USD rate and cost labels with provenance, formulas, sorting/filtering controls, AA link and methodology guide, archive links, and complete snapshot metadata. Compare consecutive snapshots to validate that only the supported change classes, including measured rate changes, are summarized.
8. **End-to-end live workflow:** with repository secrets and connectivity, run a manual dispatch, verify narrative model selection and fallback behavior and the recorded model id, the Pages artifact and deployed URL including archive links, the history commit on `main`, and the skipped second invocation. Verify the scheduled run across both CET and CEST UTC slots and ensure only one invocation analyses on a given Warsaw Monday.

Local repeatable checks:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m src.run --offline
```

Offline tests establish parser, rate-fitting, scoring, rendering, archive, and fixture pipeline behavior only. Live Cline authentication and usage schemas, AA availability, Pages configuration, and GitHub push permissions must be verified in the target environment.

## 8. Operational Caveats to Track

- GitHub Pages sites are publicly readable on the internet even when the repository is private, and Pages on a private repository requires a paid plan (GitHub Pro, Team, or Enterprise). Before the first live run, decide whether the report is allowed to be public; if it must stay internal, keep an internal publication target alongside Pages or choose a private hosting option instead.
- Artifact-based Pages deployment needs a GitHub.com repository; on GitHub Enterprise Server only `actions/deploy-pages@v3` and later GHES releases are usable, and earlier versions cannot deploy this way at all.
- GitHub does not guarantee scheduled-run timing: runs can be delayed under load, and scheduled workflows are disabled after a prolonged period without repository activity in public repositories. The hour tolerance and the same-day snapshot check exist for this reason, and a missed Monday must be reported as a missed run rather than silently skipped.
- Measured rates are this account's and this window's rates. DeepSeek peak/off-peak billing appears as one blended measured rate, and only models with enough independent token mixes can be fitted at all; never present a measured rate as a canonical price list, and always keep the window and the row count next to it.
- Cline's published reference table drifts from what billing actually charges, which is exactly why it is only a fallback. Mixing measured and reference classes for one model is not allowed, and the provenance field must stay visible in the report.
- The integer money scales (`costUsd` at 1e-8 USD; `creditsUsed` and `balance` at 1e-6 USD) come from an audited third-party client rather than from public API documentation. Keep the raw integers in snapshots and re-verify the scale if Cline changes the API.
- Free-model availability is a rotating promotion, and free usage is documented as unavailable through the Cline API, so a free model's zero price cannot be confirmed from billing. It is catalog-declared, and it can disappear or become paid without notice.
- The generated report displays the workflow narrative-engine label, matching the configured primary narrative model. If the workflow model changes, keep `src/monitor.py`, workflow environment, report label, and README aligned.
- Older planning documents describe additional benchmarks and other dimensions. They are superseded where they conflict with the implementation documented here; keep this methodology and executable tests as the source of truth.

## 9. Assumptions and Open Decisions in This Revision

- **Price interpretation.** "Prices from Cline billing usage" is implemented as measured per-class rates fitted from this account's ClinePass usage records, with Cline's published reference table as a dated fallback for models without an estimable fit and for cache write. The simpler alternative is to price every model from the published reference table and use billing usage only as a drift monitor; that removes the fit and reduces §3.2 to the reference table plus the observed blended rate, at the cost of reporting prices that demonstrably differ from what billing charges.
- **Whose account is priced.** Measured rates describe one account and one usage window. The usage endpoints are user-scoped (`/api/v1/users/{id}/usages`), so the account behind `CLINE_API_KEY` must be chosen deliberately. An organization key would instead need `/api/v1/organizations/{orgId}/usages`, whose per-model and per-token-class fields are not publicly documented and must be verified before it can replace the user-scoped path.
- **Deployment shape.** This plan deploys `site/` through a Pages artifact and commits history to `main`. The alternative is publishing the generated tree from a `gh-pages` branch with Pages set to "Deploy from a branch", which needs no artifact permissions and keeps every published page in git, at the cost of a second branch and a longer repository history.
- **Site visibility.** Pages implies a publicly readable report. Confirm that is acceptable for this comparison data, otherwise pick a different hosting target.
- **Quality coverage.** The AA Intelligence Index remains the only quality input, so how many ClinePass entries can be scored depends on AA coverage of open and contributor models. An unmatched model is listed with its rates and without a score rather than with an estimated one.
- **Repository host.** Confirm whether the repository lives on github.com or GitHub Enterprise Server, because Pages availability and the usable artifact-deployment versions differ.
