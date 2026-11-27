# Sunset Scan

**Advisories for things that stop, not things that are vulnerable.** Sunset Scan finds
retiring AI models, expiring API versions, archived infrastructure and removed container
images in your repo. For each one it tells you the file and line, how many days you have
left, what to switch to, and links the vendor's own announcement.

```text
$ sunset-scan demo --today 2026-11-27
 ...
 CHANGED 52 days ago    Meta Marketing API v24.0 expiration (facebook-business SDK 24.x)
                        2026-10-06 · Meta · behavior-change · meta/marketing-api-v24.0
                        reports/meta_insights.py:20  api_version="v24.0"
                          same text also matches meta/graph-api-v24.0 (2028-02-18); which applies depends on what this code calls
                        requirements.txt:3           facebook-business 24.0.1
                        fix: Marketing API v25.0 or newer (facebook-business / facebook-nodejs-business-sdk 25.x+)
                        src: https://developers.facebook.com/docs/graph-api/changelog/versions
 BROKEN  31 days ago    Meta Commerce Order Management API removed from all Graph API versions
                        2026-10-27 · Meta · shutdown · meta/commerce-order-management-api
                        reports/meta_insights.py:15  commerce_orders
                        fix: None: Checkout on Facebook and Instagram Shops was sunset and Meta offers no replacement API
                        src: https://developers.facebook.com/docs/graph-api/changelog/version26.0
 URGENT  14 days left   OpenAI API shutdown: gpt-5-mini, gpt-5-mini-2025-08-07
                        2026-12-11 · OpenAI · shutdown · openai/gpt-5-mini
                        src/summarize.py:7  gpt-5-mini
                        fix: gpt-6.1-terra
                        src: https://developers.openai.com/api/docs/deprecations
 SOON    48 days left   OpenAI API shutdown: gpt-4o-mini, gpt-4o-mini-2024-07-18
                        2027-01-14 · OpenAI · shutdown · openai/gpt-4o-mini
                        src/classify.ts:9  gpt-4o-mini
                        fix: gpt-6-luna
                        src: https://developers.openai.com/api/docs/deprecations
 ...
 23 findings: 7 broken · 4 past the date but may still run · 2 within 30 days · 3 within 90 days · 2 within a year · 1 later · 3 upgrade blockers · 1 to check (comments, docs or unclear matches)
 exit 0 (the demo always exits 0; add --exit-code for the real code): 13 findings are past the date or due within 30 days; a normal run would exit 1.
```

(Excerpt from `sunset-scan demo`, which scans a bundled sample repo. On your own repo,
`sunset-scan .` would exit 1 here.)

**Reading the labels.** `BROKEN` means a shutdown or removal date has passed, so calls
fail. `CHANGED` means a behaviour change or an expired API version: Meta, for example,
upgrades calls to an expired version, so the code may still run but behaves differently.
`EOL` means unmaintained: it still runs but gets no more fixes. `URGENT`, `SOON` and
`LATER` count down to a future date. `BLOCKER` means a package that stops you upgrading
something else. `CHECK` means every match was uncertain (only in a comment or docs, a
tokenizer call, a very short id in a file with no sign of AI code, or a package whose
version could not be determined), so look before acting.

## Who it is for

A two-person agency or an indie developer with an OpenAI-powered feature, a few
Meta Graph API reports and a Helm chart, and no platform team reading changelogs.
Dependabot and Renovate watch package versions. Nothing watches the model names,
API version strings, endpoint paths and image names that are hard-coded all over
a repo, and those break on a date the vendor picked:

- OpenAI lists a run of shutdowns this winter: `/v1/prompts` and Evals on Nov 30, image
  models on Dec 1, GPT-5 and o3 on Dec 11, TTS on Jan 6, GPT-4o and GPT-4.1 on Jan 14,
  realtime/audio models on Jan 20 and Whisper on Feb 26
  ([OpenAI deprecations](https://developers.openai.com/api/docs/deprecations)).
- Meta removed the Commerce Order Management API from *every* Graph API version on
  Oct 27, 2026, with no replacement
  ([Graph API v26.0 changelog](https://developers.facebook.com/docs/graph-api/changelog/version26.0)).
- Ingress NGINX was retired in March 2026, with no more releases or security fixes
  ([Kubernetes blog](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/)).
  The MinIO community edition repo was archived on Apr 25, 2026.
- Bitnami deleted its free `docker.io/bitnami` catalog on Sep 29, 2025, so pulls of
  versioned `bitnami/*` images fail
  ([bitnami/containers#83267](https://github.com/bitnami/containers/issues/83267)).

## Quickstart (60 seconds)

You need Python 3.11+ and nothing else: the scanner has zero dependencies.

```bash
# See a report on the bundled sample repo (no install, needs uv)
uvx --from git+https://github.com/larcx-idea/sunset-scan sunset-scan demo

# Scan your own repo
uvx --from git+https://github.com/larcx-idea/sunset-scan sunset-scan .

# Or install it
pipx install git+https://github.com/larcx-idea/sunset-scan
sunset-scan .

# Or from a clone
git clone https://github.com/larcx-idea/sunset-scan && cd sunset-scan
pip install . && sunset-scan demo
```

It is not on PyPI yet, so install from git for now.

## What it checks

The bundled registry has 228 advisories. 147 are curated by hand from vendor pages and
were verified on 2026-11-27. The other 81 are imported automatically, because people
already maintain them elsewhere: 21 from endoflife.date (Anthropic) and 60 from LiteLLM's
`deprecation_date` field (29 Google, 9 Mistral, 8 Moonshot, 7 xAI, 3 Cerebras, 2 DeepSeek,
and 2 undated Anthropic aliases, `claude-sonnet-4-5` and `claude-opus-4-5`).

| Area | Examples | Advisories |
|---|---|---|
| OpenAI models and endpoints | `gpt-4o-mini`, `o3`, `whisper-1`, `dall-e-3`, Assistants API, `/v1/prompts` + `pmpt_` ids, `/v1/audio/translations`, the `realtime=v1` beta header | 73 curated |
| Meta Graph and Marketing API | `graph.facebook.com/v20.0`, `"v21.0"` in Facebook/Instagram/WhatsApp code, `facebook-business==24.x` and `facebook-nodejs-business-sdk` SDK lines, `commerce_orders`, removed `delivery_estimate` fields, poll ads | 21 curated |
| Archived infrastructure | ingress-nginx images, chart repo and `nginx.ingress.kubernetes.io/` annotations; `minio/minio` | 2 curated |
| Removed images | 28 named `bitnami/*` images with upstream replacements, Bitnami Helm charts, and a catch-all for other `bitnami/*` | 30 curated |
| CI | `ubuntu-20.04`, `windows-2019`, `macos-13` runners; `upload-artifact@v3`; `actions/cache@v2`; Node 20 removal | 8 curated |
| Frontend upgrade blockers | `enzyme` and its adapters vs React 17/18/19; `eslint-plugin-react`, `-jsx-a11y`, `-import` vs ESLint 10; React 19 removals (`ReactDOM.render`, `findDOMNode`, `react-dom/test-utils`, ...) | 12 curated |
| Web analytics | Universal Analytics `UA-` ids | 1 curated |
| Anthropic models | `claude-3-5-haiku-20241022`, `claude-sonnet-4-5-20250929`, ... | 21 via endoflife.date, 2 aliases via LiteLLM |
| Other LLM providers | Google Gemini/Vertex, Mistral, Moonshot, xAI, Cerebras, DeepSeek | 58 via LiteLLM |

It reads source code in any language, Dockerfiles, Compose files, Helm charts and values,
`.env` templates, CI workflows, and npm/PyPI manifests and lockfiles
(`package.json`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `requirements*.txt`,
`pyproject.toml`, `poetry.lock`, `uv.lock`, `Pipfile(.lock)`).

Browse it as a calendar with `sunset-scan list --within 90`, or look one up with
`sunset-scan show openai/gpt-4o-mini`.

## Use it in CI

The exit code is `0` when nothing is wrong, `1` when something is already past its date
(`BROKEN`, `CHANGED` or `EOL`) or breaks within `--fail-within` days (default 30), and `2`
on errors. Upgrade blockers only fail the run with `--fail-on-blockers`. Low-confidence
(`CHECK`) matches never fail the run unless you pass `--strict`. With `--no-fail` the run
always exits 0, and the report says what would have failed.

```bash
sunset-scan . --fail-within 45
sunset-scan . --format sarif -o sunset.sarif   # GitHub code scanning
sunset-scan . --format json                    # for scripts
sunset-scan . --report markdown=sunset.md --report sarif=sunset.sarif   # several at once
```

**GitHub Action.** Copy [`examples/workflows/sunset-scan.yml`](examples/workflows/sunset-scan.yml)
into your repo. It runs every Monday. It keeps **one tracking issue** up to date: it opens
the issue when something is found, edits it on later runs, and closes it when everything
is fixed. It also uploads SARIF, so each finding appears inline in code scanning, and fails
the job when something is due within 30 days. It installs into its own virtualenv, so
it does not change the `python` your later steps use. The issue body is cut to fit
GitHub's 65,536-character limit (the job summary and `--format json` keep everything),
and the example skips the SARIF upload on pull requests from forks, which cannot write
to code scanning.

```yaml
- uses: larcx-idea/sunset-scan@main   # pin a tag or SHA once released
  with:
    fail-within: "30"        # or "never" to only report
    sarif-file: sunset.sarif # upload with github/codeql-action/upload-sarif
    issue: "true"            # needs permissions: issues: write
```

## Ignore, snooze, tune

- Add `sunset-scan: ignore` to a line, or `sunset-scan: ignore-next-line` on the line before it.
- Use a `.sunsetignore` file. It takes gitignore-style path globs and advisory ids, and an
  id can carry an expiry so the snooze can't rot:

  ```text
  tests/fixtures/
  legacy/**/*.py
  id: openai/gpt-4o-mini until 2027-01-07   # migration ticket ENG-142
  id: bitnami/other-images
  ```

  An expired snooze is reported again, with a note. A date that is not a real day
  (`until 2027-02-30`) or a line it can't read snoozes nothing and is reported as a note,
  as is an id that matches no advisory. Paths are relative to the file's own directory.
  The file is read from the scanned directory, or else from the nearest parent up to the
  git repository root, so scanning a subdirectory still uses the root `.sunsetignore`.
- Flags: `--exclude GLOB`, `--ignore-id ID`, `--within DAYS`, `--no-docs`, `--strict`,
  `--today YYYY-MM-DD`, `--registry PATH` (use your own or a local registry checkout).

## How matching works

- **Model ids** match exactly, with identifier boundaries. `gpt-4o` does not match
  `gpt-4o-mini` or `gpt-4.1`. Fine-tuned ids (`ft:gpt-4o-2024-08-06:org::id`) and
  provider prefixes (`openai/gpt-4o`) do match. Very short ids like `o1` and `o3` only
  match inside quotes, and they are `CHECK` (low confidence) unless the file also
  mentions OpenAI, a model, an LLM, GPT, completions, prompts or reasoning. A model id
  passed to a tokenizer lookup (`tiktoken.encoding_for_model("...")`) is also `CHECK`,
  because tokenizers run locally and keep working.
- **Generic tokens need context.** A quoted `"v21.0"` only counts in files that also
  mention Facebook, Instagram, WhatsApp or the Graph API. Runner labels only count in
  `.github/workflows/`.
- **Matches in comments and docs** are reported as low confidence. They are dimmed in
  the text report, collapsed in the issue, and never fail CI unless you pass `--strict`.
- **Packages** come from manifests and lockfiles, with version ranges. For example,
  `facebook-business 24.0.1` maps to Marketing API v24.0. A version from a lockfile beats
  the range declared in `package.json` or `pyproject.toml`. When the version can't be
  determined (`file:`, `workspace:`, git URLs, `*`, `latest`, or no version at all), a
  version-ranged advisory is reported as `CHECK`, not as broken.
- **One string, two verdicts.** When the same text matches advisories with different
  statuses (a quoted `"v24.0"` is an expired Marketing API version but a Graph API version
  with years left), each one says so, because only you know which API the code calls.
- **Upgrade blockers** such as `enzyme` and `eslint-plugin-react` are listed as BLOCKER.
  They are listed as BROKEN only if the version they block (React 19, ESLint 10) is
  already in the repo.
- **Replacement chains** are flagged. If the suggested replacement is itself retiring
  (`gpt-4-32k` → `gpt-4o`, which retires 2027-01-14), the report says so.
- `node_modules`, `vendor`, `dist`, `build`, virtualenvs and binaries are skipped.
  On this machine the Python 3.12 stdlib (2,225 text files) scans in about 2.5 seconds
  with zero findings.
- **Nothing is skipped silently.** Files that can't be read, look binary, or are over
  2 MB, and manifests that can't be parsed, are listed as notes at the end of the
  report. UTF-16 files are decoded and scanned.

## The registry

Advisories are YAML in [`registry/`](registry/), in an OSV-like shape. Each has an id,
affected identifiers, an effective date, a replacement and a source URL. They are
compiled into the JSON bundle that ships with the CLI.

```yaml
- id: openai/gpt-4o-mini
  title: 'OpenAI API shutdown: gpt-4o-mini, gpt-4o-mini-2024-07-18'
  vendor: OpenAI
  kind: shutdown        # shutdown | unmaintained | removed | upgrade-blocker | behavior-change
  effective: 2027-01-14
  announced: 2026-10-14
  replacement: gpt-6-luna
  source: https://developers.openai.com/api/docs/deprecations
  last_verified: 2026-11-27
  affected:
    - type: model       # model | image | literal | pattern | npm | pypi
      ids: [gpt-4o-mini, gpt-4o-mini-2024-07-18]
```

- **Curate only the gaps.** `registry/curated/` holds what nobody else tracks: API
  versions, endpoint shutdowns, archived infrastructure, image catalogs and upgrade
  blockers. `sunset-scan registry sync` imports model retirements from
  [endoflife.date](https://endoflife.date) and LiteLLM's
  [`deprecation_date`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
  field into `registry/imported/`. A scheduled workflow opens a PR when they change.
  Curated identifiers always win.
- **Drift detection.** The sync compares curated dates against endoflife.date. Today it
  flags 8 OpenAI model ids where endoflife.date groups aliases under one date and
  OpenAI's page gives a different one. That is how a moved vendor date gets noticed.
- **Cheap review.** The registry CI checks the schema, dates, unique ids, regexes,
  duplicate identifiers, that the bundle was rebuilt, and that every source link
  resolves (weekly and on PRs). Server errors and timeouts are retried; a link that
  still returns a 5xx is a warning, and a 404 is an error. The YAML is a strict subset that parses the same way
  in PyYAML, so contributors can use any editor.

Adding an advisory takes about ten minutes; see [CONTRIBUTING.md](CONTRIBUTING.md).

## How it relates to existing tools

| Tool | What it covers | Relationship |
|---|---|---|
| [xeol](https://github.com/xeol-io/xeol) | EOL packages in images and SBOMs | Complementary. Sunset Scan reads source text for model ids, API versions and endpoints. |
| [pluto](https://github.com/FairwindsOps/pluto) / kubent | Kubernetes apiVersion removals | Use them for Kubernetes APIs. This tool covers everything else in the repo. |
| [endoflife.date](https://endoflife.date) | EOL data, including OpenAI and Claude models | An upstream data source, imported nightly. |
| LiteLLM model map | `deprecation_date` on many LLM models | An upstream data source, imported nightly. |
| Dependabot / Renovate | Package version bumps | They cannot see model names, API version strings or endpoint paths. |

## Limitations

- **Python CLI only.** There is no npm/npx package yet, and it is not on PyPI yet.
- **Heuristic text matching.** It can't see model names built at runtime or set outside
  the repo (deployment env vars, database config). It also can't tell providers apart:
  Azure OpenAI, Bedrock and Vertex have their own retirement schedules, but
  `azure/gpt-4o` still matches the OpenAI advisory.
- **Imported data is unverified by us.** LiteLLM dates are community-maintained. When
  Vertex AI and the Gemini API list different dates, the earlier one is used.
- **Past the date is not always broken.** ingress-nginx and MinIO keep working after
  their dates (reported as `EOL`); you just stop getting security patches. Meta version
  expiries change behaviour rather than always erroring (reported as `CHANGED`). Both
  still fail CI, because both need action.
- **Suggested fixes are partly inferred.** When the vendor's suggested replacement is
  itself retiring, the report follows the chain and shows that replacement's own
  successor first, with a note naming the vendor's original suggestion. Check the
  source link before migrating.
- **Version ranges are approximate.** Without a lockfile, `package.json` ranges are
  approximated by their lower bound (`^7.37.5` is read as 7.37.5).
- **No `.gitignore` parsing.** It uses built-in excludes plus `.sunsetignore`.
- **The registry is a snapshot.** The CLI warns when the bundled registry is more than
  60 days old. Always confirm a date on the source link before acting on it: vendors
  move dates (OpenAI pulled fine-tuning job creation forward from Jan 6 to Dec 15).
- **The CI integrations have not run on GitHub yet.** The Action's scan step and the
  issue script were exercised locally (with a stubbed `gh`), and the workflow YAML is
  validated. The Action, SARIF upload and sync PRs have not run in GitHub's runners.

## Feedback wanted

This is a v0. If you try it, please
[open an issue](https://github.com/larcx-idea/sunset-scan/issues/new) describing your
use case: your stack, what broke on you last time, and what the scan missed or flagged
wrongly. Advisory PRs for shutdowns that bit you are the most useful contribution of all.

## Development

```bash
pip install -e . pytest
pytest -q                                   # 80 tests
sunset-scan registry validate registry      # schema, dates, regexes
sunset-scan registry build                  # rebuild src/sunset_scan/data/registry.json
sunset-scan registry sync                   # import endoflife.date + LiteLLM (network)
sunset-scan . --strict                      # dogfood: this repo scans clean
```

---

Built with AI agents (Claude Code) as part of the larcx-idea open-source factory. Reviewed by an independent agent before release; please report anything wrong.

## License

MIT, copyright larcx-idea contributors. Imported data comes from endoflife.date (MIT)
and LiteLLM (MIT).
