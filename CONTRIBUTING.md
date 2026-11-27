# Contributing advisories

The registry only stays useful if people who get bitten by a shutdown add it. A good
advisory PR takes about ten minutes and is cheap to review, because CI checks the
schema, the dates, the regexes and the source link for you.

## What belongs here

Anything that **stops working on a date** (or when you upgrade something) and that
leaves a recognisable identifier in a repository:

- AI model ids and API endpoints that a vendor shuts down
- third-party API versions that expire (Graph API versions, dated API versions, SDK major lines)
- endpoints, fields, headers or parameters removed across all versions
- container images and Helm charts that are deleted, moved or paywalled
- infrastructure projects that are archived or stop getting security fixes
- packages that pin you to an old major version of something else (upgrade blockers)
- CI runner images, action versions and runtimes that get retired

Not here: security vulnerabilities (use OSV/GHSA), plain package end-of-life that
endoflife.date and xeol already track, or Kubernetes apiVersion removals (use pluto).

**Do not hand-curate model retirements that endoflife.date or LiteLLM already carry.**
`sunset-scan registry sync` imports those into `registry/imported/`. Curate a model
only when upstream is missing it or gets it wrong (a curated identifier always wins,
and the sync job prints a drift report when the dates disagree).

## Add one

1. Pick the file in `registry/curated/` that fits, or create `registry/curated/<topic>.yaml`.
2. Append an entry:

```yaml
  - id: acme/widgets-api-v1            # vendor/slug, lowercase, unique
    title: Acme Widgets API v1 shutdown # tense-neutral: it is shown before and after the date
    vendor: Acme
    kind: shutdown                      # shutdown | unmaintained | removed | upgrade-blocker | behavior-change
    effective: 2027-03-31               # the day it stops working
    announced: 2026-09-30               # optional
    replacement: Widgets API v2 (/v2/widgets)
    summary: >-
      Optional context in a sentence or two.
    source: https://docs.acme.test/changelog#widgets-v1   # official, https, says the date
    last_verified: 2026-11-27           # when a human last checked the source
    affected:
      - type: literal
        ids: [api.acme.test/v1/widgets, acme.widgets.v1]
```

3. Check it locally:

```bash
pip install -e .
sunset-scan registry validate registry       # schema, dates, regexes, duplicates
sunset-scan scan path/to/a/repo --registry registry   # try it on real code
sunset-scan registry build                   # regenerate src/sunset_scan/data/registry.json
```

Pick `kind` by what happens to working code after the date, because it sets the label
users see once the date has passed:

| kind | after the date the report says | use it when |
|---|---|---|
| `shutdown`, `removed` | `BROKEN` | calls fail, images can't be pulled, the field is rejected |
| `behavior-change` | `CHANGED` | it still answers but differently (e.g. Meta upgrades calls to an expired API version) |
| `unmaintained` | `EOL` | it keeps running but gets no more releases or security fixes |
| `upgrade-blocker` | `BLOCKER` (or `BROKEN` if the blocked version is already in the repo) | it stops you upgrading something else |

Every kind except `upgrade-blocker` fails CI once its date has passed, so choosing a softer
kind changes the wording, never whether the problem is reported.

4. Open a PR with the source link in the description. Commit the rebuilt JSON too
   (CI fails if it is stale).

## Matcher types (`affected[].type`)

| type | matches | notes |
|---|---|---|
| `model` | exact model ids with identifier boundaries | `gpt-4o` does not match `gpt-4o-mini` or `gpt-4.1`; `ft:gpt-4o-...:org::id` and `openai/gpt-4o` do match. Set `quoted: true` for short ids like `o1` so only `"o1"` matches; a quoted match in a file with no AI-related words (openai, model, llm, gpt, ...) is reported as low confidence. Case-sensitive. |
| `image` | container image names | `bitnami/postgresql` matches `docker.io/bitnami/postgresql:16` but not `bitnami/postgresql-ha` or `bitnamilegacy/postgresql`. |
| `literal` | exact strings | word boundaries are applied at the ends that are letters or digits. Good for endpoint paths, annotations, env vars. |
| `pattern` | a Python regular expression | add `flags: i` / `m` if needed. Quote it in single quotes in YAML. |
| `npm`, `pypi` | packages in manifests and lockfiles | optional `versions: ">=24.0.0, <25.0.0"`. Reads package.json, package-lock, yarn.lock, pnpm-lock, requirements*.txt, pyproject, poetry.lock, uv.lock, Pipfile(.lock). |

Every text matcher also accepts:

- `context: [facebook, instagram]`: the file must contain one of these strings (case-insensitive). Use it to keep generic tokens such as `"v21.0"` from matching unrelated files.
- `files: [".github/workflows/*"]`: only scan matching paths (gitignore-style globs).
- `replacement:`: overrides the advisory-level replacement for this identifier.

Advisory-level extras:

- `kind: upgrade-blocker` needs `blocks: {ecosystem: npm, package: eslint, versions: ">=10.0.0"}`. It is reported as an upgrade blocker, and as BROKEN only if the blocking version is already in the repo.
- `fallback: true` marks a catch-all; it is dropped on lines where a specific advisory matched.

## Keeping false positives down

Reviewers will ask:

- Would this identifier appear in code that is *not* affected? Prefer full ids, add `context`, `files` or `quoted`.
- Is the date the day things **break**, not the announcement date?
- Is the source the vendor's own page, not a blog post about it?

Matches in comments and docs, model ids passed to tokenizer lookups, and package matches
whose installed version can't be determined are reported as low confidence (`CHECK`) and
do not fail CI unless `--strict` is set, so do not contort a pattern to avoid comments.
