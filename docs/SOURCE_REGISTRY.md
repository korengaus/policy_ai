# Official Source Registry (Phase 2 M10.0)

A small, deliberately conservative, **operator-curated** registry of
official-source *candidates* the policy_ai pipeline can later consume
when future ingestion layers (HTTP fetchers, browser automation,
n8n / OpenClaw / browser-use orchestration) come online.

The registry is **infrastructure-only**: it does **not** fetch, scrape,
verify truth, or change verdict logic. Every future ingestion layer
must consult the registry *before* touching any external URL.

## A. Why this exists before scraping / browser automation

policy_ai's safety contracts (M8.0–M9.5) have hardened the review
workflow and exposure surface. The natural next foundation is *source
traceability*: which domains is the pipeline *allowed* to consider as
candidates for official evidence?

Adding scraping or browser automation without an explicit, validated
registry would invite three failure modes:

1. **Lookalike domains.** A typo or a confusable hostname would not be
   caught at ingestion time.
2. **Truth conflation.** Code in another module might assume "this URL
   came from a known source, therefore its content is true." The
   registry explicitly refuses to express that assumption.
3. **Implicit browser automation.** Code might silently invoke
   Playwright or browser-use because a source was tagged "official."
   The registry instead declares `browser_automation` as a separate,
   explicit field that future automation must honor.

The registry pins these invariants *before* any automation lands so
the automation can be reviewed against a stable contract.

## B. Files

| path | purpose |
| --- | --- |
| `source_registry.py` | Pure-stdlib loader / validator / lookup module |
| `data/source_registry.json` | Operator-curated seed registry (schema version 1) |
| `scripts/validate_source_registry.py` | Offline validator CLI |
| `tests/test_source_registry.py` | Unit + integration tests (55 tests as of M10.0) |
| `docs/SOURCE_REGISTRY.md` | This document |

## C. Schema (version 1)

Top-level JSON object:

```jsonc
{
  "schema_version": 1,
  "registry_name": "policy_ai_source_registry",
  "registry_notes": "Conservative M10.0 seed …",
  "sources": [
    /* zero or more source records (see below) */
  ]
}
```

Per-source record:

| field | type | required | meaning |
| --- | --- | --- | --- |
| `source_id` | string (snake_case ASCII) | yes | Unique. Pattern `^[a-z][a-z0-9_]{2,79}$`. |
| `display_name` | string | no | Human-readable label. |
| `source_type` | enum | yes | One of `government_policy`, `government_press`, `law_or_regulation`, `parliament`, `local_government`, `public_agency`, `news`, `fact_check`, `demo`. |
| `jurisdiction` | string | no | Free-text jurisdiction tag (e.g. `KR`). |
| `base_url` | string | yes | Must be `https://…`. `http://localhost` / `127.0.0.1` / `::1` allowed only when `source_type=demo`. Never with credentials. Never with query string or fragment. |
| `allowed_domains` | list[string] | yes | Bare hostnames. No scheme / path / port / userinfo. No wildcards. No duplicates. |
| `allow_subdomains` | bool | no | Defaults to `false`. When `true`, strict subdomain match (`host.endswith('.' + allowed)`) is accepted. |
| `default_enabled` | bool | no | Defaults to `false`. Future ingestion layers must check this. |
| `capture_method` | enum | yes | One of `manual_or_http`, `rss`, `api`, `html`, `pdf`, `browser_required`, `unknown`. |
| `browser_automation` | enum | yes | One of `not_required`, `maybe_required`, `required`, `unknown`. `capture_method=browser_required` + `browser_automation=not_required` is a validation error. |
| `operator_review_required` | bool | no | Defaults to `true`. Setting to `false` requires a non-empty `operator_review_required_justification` string. |
| `official_source_candidate` | bool | no | Defaults to `false`. **Candidate ≠ truth.** See §D. |
| `truth_claim` | bool | no | Must be `false`. The registry never asserts truth; setting `true` is a hard validation error. |
| `semantic_debug_only` | bool | no | Defaults to `false`. Semantic signals tied to this source stay debug metadata; future ingestion must not surface them as user-facing truth. |
| `notes` | string | no | Free-text operator notes. Scanned for token-shaped literals. |
| `tags` | list[string] | no | Free-text tags. Scanned for token-shaped literals. |
| `operator_review_required_justification` | string | conditional | Required when `operator_review_required=false`. |

## D. Conservative meaning of `official_source_candidate`

`official_source_candidate: true` means **only** that the operator
believes a source *could* serve as an official-evidence reference in a
future milestone. It does **not** mean:

- the source is verified
- content from this source is true
- the pipeline may publish content from this source
- automated ingestion is approved
- semantic-similarity hits against this source can be trusted as
  user-facing truth

Operator review remains required (`operator_review_required: true` by
default) on every candidate.

## E. What the registry does NOT do

- **Does not fetch.** Zero HTTP calls. `source_registry.py` imports
  no `urllib.request`, no `requests`, no `httpx`. Pinned by
  `tests/test_source_registry.py::StaticSafetyTests`.
- **Does not scrape.** No HTML parsing, no XPath, no DOM walking.
  No Playwright, no browser-use, no OpenClaw, no n8n.
- **Does not verify truth.** No content arrives at the registry —
  only operator-curated metadata. `truth_claim` must remain `false`.
- **Does not change `final_decision` / `policy_confidence` /
  `verification_card`.** The registry is a *catalog* of candidates;
  verdict modules stay separate.
- **Does not publish.** No publication path is added. The reviewer
  workflow contracts from M8.0–M9.5 are unchanged.
- **Does not bypass human review.** Every seed source has
  `operator_review_required: true`. The registry's `_validate_*`
  helpers refuse to silently flip that to `false`.
- **Does not call OpenAI.** Pinned by import-line tests.
- **Does not modify Render env.** Loader is read-only.

## F. URL / domain safety rules

`is_url_allowed_for_source(source, url)` returns `true` only when **all**
of the following hold:

1. The URL parses cleanly.
2. The scheme is `https` (or `http` with a localhost host, *only* when
   `source_type == "demo"`).
3. The URL carries no credentials (`user:pass@`).
4. The hostname is ASCII-only (matches `^[a-z0-9](?:[a-z0-9.-]{1,251}[a-z0-9])$`).
   This intentionally rejects IDN / punycode / mixed-Unicode lookalikes.
5. The hostname **exactly matches** one of the source's
   `allowed_domains`, **or** `allow_subdomains: true` is set and the
   hostname ends with `.<allowed-domain>` (strict subdomain match).

Lookalike patterns the rule rejects:

| pattern | example | rejected because |
| --- | --- | --- |
| Suffix lookalike | `example.go.kr.evil.com` | host doesn't equal any `allowed_domains` entry, doesn't strict-suffix any of them |
| Prefix lookalike | `evil-example.go.kr` | not an exact match, not a strict subdomain |
| Capital-letter lookalike | `exampIe.go.kr` (Latin I) | fails ASCII-only hostname regex |
| Credential leak | `https://user:pass@example.go.kr` | rejected by step 3 |
| Wrong scheme | `http://example.go.kr` | rejected unless source is `demo` AND host is localhost |

## G. Capture plans

`build_source_capture_plan(source, url=None)` returns a stable dict
*without performing any I/O*. Keys:

```jsonc
{
  "source_id": "…",
  "capture_method": "…",
  "browser_automation": "…",
  "operator_review_required": true,
  "official_source_candidate": false,
  "default_enabled": false,
  "url": "https://…",
  "url_allowed": true,
  "network_fetch_performed": false,
  "notes": "…",
  "next_step": "manual_review"
}
```

`next_step` is one of:

| value | meaning |
| --- | --- |
| `manual_review` | operator must look at this source manually before any ingestion (default for disabled sources / unknown methods) |
| `http_fetch_candidate` | a future HTTP-style ingestion layer may consider this source |
| `browser_candidate` | requires browser automation; an HTTP-only ingester must skip |
| `unsupported` | the registry shape is too incomplete to produce a plan |

`network_fetch_performed` is always `false` — pinned by every capture-plan
test.

## H. How future ingestion layers should consume the registry

Order of operations for any future fetcher / browser agent:

1. Load and validate the registry on startup. Refuse to start if
   `validate_source_registry(...)` returns any error.
2. For every candidate URL (from search results, RSS feeds, news
   collectors, etc.), call `classify_url_against_registry(reg, url)`.
   - If `allowed=false` → skip the URL entirely (never fetch).
   - If `allowed=true` → continue.
3. Call `build_source_capture_plan(source, url)`.
   - If `next_step == "manual_review"` → queue for operator review;
     do NOT fetch.
   - If `next_step == "http_fetch_candidate"` → an HTTP fetcher may
     proceed *only if* the operator has separately enabled the source
     in their environment.
   - If `next_step == "browser_candidate"` → only a browser-equipped
     ingester may proceed.
   - If `next_step == "unsupported"` → skip and surface the source for
     registry curation.
4. Honor `operator_review_required` end-to-end. The registry surface
   never authorizes content trust on its own.
5. Never re-label semantic signals tied to a registry source as
   user-facing truth. `semantic_debug_only=true` should be treated
   as a one-way blocker against semantic-as-truth surfacing.

## I. Validation

Offline tests + CLI:

```
python tests/test_source_registry.py
python scripts/validate_source_registry.py
python scripts/validate_source_registry.py --json
python scripts/validate.py
```

`scripts/validate.py` invokes `tests/test_source_registry.py` as part
of the standard offline suite (added in M10.0). The validator CLI is
also safe to run on its own and exits:

- `0` — registry is valid
- `1` — validation errors detected
- `2` — CLI usage error (bad path, bad flag)

JSON output keys (stable, pinned):

```
passed, schema_version, registry_name, source_path, sources_count,
enabled_count, disabled_count, source_types, browser_required_count,
issues, warnings
```

## J. Future work (post-M10.0)

- Wire `classify_url_against_registry` into `official_metadata` /
  `official_relevance` so the existing pipeline's official-domain
  detection consults the registry alongside its current `OFFICIAL_AUTHORITY_DOMAINS`
  set. M10.0 leaves those constants untouched.
- Add CLI commands for adding / removing sources (still operator-
  curated; not autonomous discovery).
- Allow per-source rate limits + capture quotas, once an ingestion
  layer exists to honor them.
- Add a separate, equally-conservative registry of "explicitly NOT
  trusted" domains (denylist) once we have content samples to back
  the call.

## URL Classifier CLI

Use `scripts/classify_source_url.py` (Phase 2 M10.1) to classify URLs
against the registry **offline**. The CLI never fetches, scrapes,
contacts any external service, or modifies the registry file. It is
the read-only consumer of the M10.0 helpers
(`classify_url_against_registry` + `build_source_capture_plan`)
documented in §G.

### Usage

```
python scripts/classify_source_url.py https://www.law.go.kr/page
python scripts/classify_source_url.py --json https://www.law.go.kr/page
python scripts/classify_source_url.py --url https://www.law.go.kr/a --url https://other.com/b
python scripts/classify_source_url.py --registry-path data/source_registry.json https://www.law.go.kr/page
```

### Per-URL status values

| status | meaning |
| --- | --- |
| `MATCHED` | URL matched a registry entry and is allowed (`reason="matched"` + `allowed=true` + non-null `matched_source_id`) |
| `NO_MATCH` | No registry entry claimed the URL (`reason="no_match"`) |
| `REJECTED` | URL safety rejection by the helper (`reason` in `credentials_in_url`, `missing_scheme_or_host`, `invalid_host`, `empty_url`) |
| `ERROR` | Unexpected exception during classification, or registry-side inconsistency (`reason` in `registry_not_object`, `registry_sources_not_list`) |

### Important notes

- This CLI makes no network requests. `urllib.parse` is the only
  `urllib` submodule it touches; `urllib.request` is intentionally
  not imported.
- `MATCHED` only means the URL matches a registry candidate entry —
  it does **not** guarantee the truthfulness of any content at that
  URL. The CLI prints this safety note on every URL, in both human
  and `--json` output.
- The capture plan is a **future plan only**. No scraping or crawling
  is performed. The JSON `capture_plan.network_fetch_performed` field
  is always `false`.
- All registry entries remain `default_enabled=false` and
  `operator_review_required=true` until the operator explicitly
  changes them. The CLI does not modify the registry.

### Exit codes (strict)

- `0` — every URL matched a known, allowed registry entry
- `1` — any URL was `NO_MATCH`, `REJECTED`, `ERROR`, or the registry
  file failed to load
- `2` — CLI usage error (no URLs provided, unrecognized arguments)

A single `NO_MATCH` in a batch forces exit `1` even when every other
URL matches — pinned by `tests/test_source_url_classifier.py::
MultipleUrlsTests::test_one_no_match_in_batch_forces_exit_1`.

### Sample human output

```
=== URL Classification Results ===

URL: https://www.law.go.kr/sample
Status: MATCHED
source_id: kr_law_open_data_candidate
source_type: law_or_regulation
allowed: True
official_source_candidate: True

[Important]
- MATCHED only means this URL matches a registry candidate.
- official_source_candidate does not imply truth.
- The capture plan is a future plan only. No scraping or crawling is performed by this CLI.

Capture Plan:
  capture_method: manual_or_http
  browser_automation: maybe_required
  plan_status: manual_review
  operator_review_required: True
  default_enabled: False
  network_fetch_performed: False

Summary: 1 processed | matched=1 | no_match=0 | rejected=0 | errors=0

[Safety] official_source_candidate does not imply truth.
[Safety] The capture plan is a future plan only. No scraping or crawling is performed by this CLI.
[Safety] All registry entries remain operator_review_required=true and default_enabled=false until explicitly enabled by an operator.
```

### JSON output shape

Stable top-level keys: `cli_version`, `registry_path`, `processed_at`,
`results`, `summary`, `safety_notes`. Each `results` entry carries:

```jsonc
{
  "url": "…",
  "status": "MATCHED" | "NO_MATCH" | "REJECTED" | "ERROR",
  "classification": {
    "matched_source_id": "…",
    "allowed": true,
    "reason": "matched",
    "host": "…",
    "source_type": "…",                 // present only on MATCHED
    "official_source_candidate": true   // present only on MATCHED
  },
  "capture_plan": {                     // present only on MATCHED
    "capture_method": "…",
    "browser_automation": "…",
    "operator_review_required": true,
    "default_enabled": false,
    "url_allowed": true,
    "network_fetch_performed": false,
    "plan_status": "manual_review"
  },
  "safety_note": "official_source_candidate does not imply truth"
}
```

`summary.all_matched_safely` is `true` only when every URL in the
batch reached `MATCHED`. The CLI exit code is the boolean
`summary.all_matched_safely` (true → 0, false → 1).

### Operational profile

`scripts/run_operational_checks.py --profile source-registry`
(M10.1) chains the four offline checks:

1. `scripts/validate_source_registry.py --json` — schema check.
2. `scripts/classify_source_url.py --help` — CLI smoke.
3. `scripts/classify_source_url.py https://www.law.go.kr/sample` —
   expected `MATCHED` against `kr_law_open_data_candidate`.
4. `scripts/classify_source_url.py https://unknown-source-example.invalid/page`
   — expected `NO_MATCH` (the runner's custom parser treats exit-code-1
   as a PASS here because the conservative exit policy is the
   contract under test).

The profile never hits Render, never calls OpenAI, never starts a
server, and never modifies the registry.

### Valid enum values (from current registry schema)

`source_type`:
`government_policy`, `government_press`, `law_or_regulation`,
`parliament`, `local_government`, `public_agency`, `news`,
`fact_check`, `demo`.

`capture_method`:
`manual_or_http`, `rss`, `api`, `html`, `pdf`, `browser_required`,
`unknown`.

`browser_automation`:
`not_required`, `maybe_required`, `required`, `unknown`.

`plan_status` (from `build_source_capture_plan`):
`manual_review`, `http_fetch_candidate`, `browser_candidate`,
`unsupported`.

## Static Crawler (`source_crawler.py`)

`source_crawler.py` (Phase 2 M10.2) provides a bounded static HTTP
fetcher for registry-candidate URLs. It uses `requests` +
`BeautifulSoup` only — no Playwright, no browser automation, no
JavaScript execution. Results are stored as raw fetch artifacts in
the `source_fetch_artifacts` SQLite table; they do **not** affect
`final_decision`, `policy_confidence`, `verification_card`, or
semantic matching in any way.

### Important safety constraints

- Fetches are refused unless explicitly triggered by an operator via
  `scripts/fetch_registry_source.py --save`.
- The pipeline (`main.py` / `analyze_pipeline` / `api_server.py`)
  never imports `source_crawler`. Pinned by
  `tests/test_source_crawler.py::StaticSafetyTests::test_crawler_not_imported_by_pipeline_entry_points`.
- All registry entries are `default_enabled=false`. No automated
  fetch occurs against the current seed.
- Fetch results are stored as raw artifacts only. They do **not**
  affect verdict logic, `policy_confidence`, `verification_card`,
  or semantic matching.
- `truth_claim` is always `False` in every fetch result. The
  database layer forces this to 0 on `save_fetch_artifact` even
  if a caller passes `truth_claim=True`.
- Fetch results require separate human review before any use in
  verification.

### Operator fetch CLI

```
python scripts/fetch_registry_source.py --source-id <id> --url <url> --dry-run
python scripts/fetch_registry_source.py --source-id <id> --url <url> --save
python scripts/fetch_registry_source.py --source-id <id> --url <url> --json
```

`--dry-run` is the default. The dry-run path runs all safety checks
**without** any network request — `_run_safety_checks` is invoked
directly, never `requests.get`. Use `--save` only when intentionally
fetching and persisting.

The CLI prints these safety notes in every output mode:

- `truth_claim: False — fetch results do not imply truth of any content`
- `official_source_candidate does not guarantee content accuracy`
- `Fetch results are raw artifacts and require separate human review before any use in verification.`

### Safety checks enforced before any fetch (first match wins)

1. `default_enabled` must be `True` → otherwise refuse with
   `"source not enabled for automated fetch"`.
2. `operator_review_required` must be `False` → otherwise refuse with
   `"operator review required before fetch"`.
3. URL scheme must be `https` → otherwise refuse with
   `"only https urls are permitted"`.
4. URL host must be in `allowed_domains` (exact match by default;
   strict subdomain match when `allow_subdomains=true`) → otherwise
   refuse with `"url host not in allowed_domains"`.
5. `browser_automation` must not be `"required"` → otherwise refuse
   with `"source requires browser automation, static fetch not appropriate"`.

A refusal returns a `FetchResult` with `success=False`,
`network_fetch_performed=False`, `truth_claim=False`, and a
descriptive `error` string. No exception is raised.

### Bounded behavior on actual fetches

- Single attempt — no retries.
- At most `MAX_REDIRECTS` (3) redirects.
- `Content-Length` header above `MAX_CONTENT_BYTES` (2 MB) aborts
  *before* reading the body.
- Body bytes capped at `MAX_CONTENT_BYTES` regardless of headers.
- Extracted text capped at `MAX_TEXT_CHARS` (50 000 chars).
- Text extraction strips `<script>`, `<style>`, `<nav>`,
  `<footer>`, `<header>` before extracting text.
- Text extraction is skipped for non-`text/html` content types
  (PDF / JSON / XML); the `error` field carries a note explaining
  the skip.
- Default timeout is `DEFAULT_TIMEOUT_SECONDS` (15 s); override via
  `config["timeout"]` or `--timeout-seconds`.
- No cookies are sent; sessions are never reused across fetches.
- The default User-Agent is a neutral descriptive string
  (`policy_ai-source-crawler/M10.2 …`), not a bot identifier and
  not a browser impersonation.

### `FetchResult` fields (stable wire shape)

```
url, source_id, status_code, content_type, raw_html, text_content,
fetch_timestamp, fetch_duration_ms, success, error,
network_fetch_performed, truth_claim, official_source_candidate
```

`truth_claim` is **always** `False`.
`network_fetch_performed` is `True` only when `requests.get` was
actually invoked; safety-check refusals leave it `False`.

### `source_fetch_artifacts` table (SQLite)

```
id                        INTEGER PRIMARY KEY AUTOINCREMENT
source_id                 TEXT NOT NULL
url                       TEXT NOT NULL
fetch_timestamp           TEXT NOT NULL
status_code               INTEGER
content_type              TEXT
success                   INTEGER NOT NULL DEFAULT 0
error                     TEXT
text_content              TEXT
raw_html                  TEXT
fetch_duration_ms         INTEGER
truth_claim               INTEGER NOT NULL DEFAULT 0
official_source_candidate INTEGER NOT NULL DEFAULT 0
created_at                TEXT NOT NULL
```

Created idempotently via `_ensure_source_fetch_artifacts_table` from
`init_db()` (or the standalone `init_source_fetch_artifacts_table()`).
The `idx_source_fetch_artifacts_source` index covers the
`(source_id, fetch_timestamp)` lookup used by `get_fetch_artifacts`.

### Operational profile

`scripts/run_operational_checks.py --profile source-crawler` chains
four **offline** checks:

1. `scripts/validate_source_registry.py --json` — schema check.
2. `scripts/fetch_registry_source.py --help` — CLI smoke.
3. `scripts/fetch_registry_source.py --source-id kr_law_open_data_candidate --url https://www.law.go.kr/sample --dry-run`
   — expected safety refusal (the seed entry is
   `default_enabled=false`); the runner's custom parser treats the
   refusal as PASS.
4. `scripts/classify_source_url.py https://www.law.go.kr/sample`
   — same URL through the M10.1 classifier as a consistency check.

The profile never hits the network, never calls OpenAI, never starts
a server, and never enables any registry entry.

### Sample dry-run output (offline, no network)

```
=== fetch_registry_source: DRY RUN — no network request made ===
source_id: kr_law_open_data_candidate
url: https://www.law.go.kr/sample
registry_path: <repo>/data/source_registry.json
source_found: True
safety_refusal: source not enabled for automated fetch
result: would refuse fetch — safety check failed
network_fetch_performed: False
truth_claim: False

[Safety] truth_claim: False — fetch results do not imply truth of any content
[Safety] official_source_candidate does not guarantee content accuracy
[Safety] Fetch results are raw artifacts and require separate human review before any use in verification.
```

The exit code in this example is `1` because the safety check
refused. Both `--dry-run` and `--save` reserve exit `0` for "fetch
would actually proceed" / "fetch succeeded".

## Operator Enable Workflow

Use `scripts/enable_registry_source.py` (Phase 2 M10.3) to enable a
registry entry so that `scripts/fetch_registry_source.py --save` will
accept it. The CLI is fully offline — it never fetches, scrapes,
contacts any external service, or touches the database. Every enable
requires an explicit operator justification (>= 20 characters) and a
typed `YES` confirmation; the file write is atomic
(`tmp + os.replace`) so a crashed run can never leave a half-written
registry.

### Usage

```
python scripts/enable_registry_source.py --list
python scripts/enable_registry_source.py --list --json
python scripts/enable_registry_source.py --source-id <id> --justification "<reason>" --dry-run
python scripts/enable_registry_source.py --source-id <id> --justification "<reason>"
python scripts/enable_registry_source.py --source-id <id> --justification "<reason>" --yes
python scripts/enable_registry_source.py --source-id <id> --justification "<reason>" --allow-browser
```

`--list` prints every registry entry with its current `default_enabled`
+ `operator_review_required` flags so the operator can confirm the
current safe state before making any change.

### Pre-checks before any enable

All pre-checks must pass before the CLI proposes a state change. The
order below matches the implementation:

1. `--source-id` must be a non-empty string and the entry must exist
   in the registry.
2. The registry record's `source_id` must match the CLI argument
   (registry-consistency sanity check).
3. The registry record's `truth_claim` must be `false` — the CLI
   refuses any entry with `truth_claim=true` and never writes one.
4. `capture_method='browser_required'` is refused unless the operator
   passes `--allow-browser` to acknowledge that the M10.2 static
   crawler cannot service this entry.
5. `--justification` must be at least 20 characters (whitespace
   stripped).

If any pre-check fails the CLI exits 1, no file is written, and the
refusal reason is surfaced in both human and JSON output.

If the targeted entry is already enabled
(`default_enabled=true` + `operator_review_required=false`) the CLI
exits 0 idempotently without writing.

### What the enable writes

The enable mutates only the targeted source entry, leaves every other
entry and every top-level field exactly as-is, and writes the file
atomically. The fields it changes:

- `default_enabled: true`
- `operator_review_required: false`
- `operator_review_required_justification: "<the supplied --justification>"`
- `operator_enable_record: { justification, enabled_at, cli_version }`
- `truth_claim: false` (re-asserted; never flipped to `true`)

`operator_enable_record.enabled_at` is an ISO 8601 UTC timestamp;
`cli_version` matches `scripts/enable_registry_source.CLI_VERSION`
(currently `"1.0"`).

### Confirmation prompt

When `--yes` is not passed, the CLI prints a state-transition preview
and then waits for the operator to type exactly `YES`. Any other input
(including blank lines, `Y`, `yes`, or `NO`) aborts with exit 1 and
no file write. The prompt is bypassed entirely under `--dry-run`.

### Important safety notes

These notes appear in every output mode (human + `--json`):

- Enabling a source does **not** imply truth or guarantee accuracy
  of any content fetched from it.
- Fetch results remain raw artifacts requiring separate human review
  before any verification use.
- Enabling only authorizes operator-triggered fetches via
  `scripts/fetch_registry_source.py --save`. The analysis pipeline
  does not auto-fetch enabled sources.

The CLI is also pinned by tests to:

- Use atomic write (`tmp + os.replace`) so partial writes are
  impossible.
- Refuse to set `truth_claim=true`.
- Stay out of the analysis pipeline — `main.py`, `api_server.py`,
  and `scheduler.py` do not import the CLI.
- Avoid any network / browser / OpenAI imports
  (`requests`, `httpx`, `urllib.request`, `socket`, `playwright`,
  `browser_use`, `openclaw`, `selenium`, `openai`, `anthropic`).

### Exit codes (strict)

- `0` — success (enabled, already-enabled idempotent, dry-run, or
  `--list` exited cleanly)
- `1` — pre-check failure, source not found, confirmation refused,
  or file write error
- `2` — CLI usage error (missing required flags, unrecognized args)

### JSON output shape

Stable top-level keys for the `--list` payload:
`cli_version`, `mode` (`"list"`), `registry_path`, `processed_at`,
`sources`, `summary`, `safety_notes`.

Stable top-level keys for the enable / dry-run payload:
`cli_version`, `mode` (`"enable"` | `"dry_run"`), `processed_at`,
`registry_path`, `source_id`, `source_found`, `already_enabled`,
`justification`, `refusal_reason`, `current_state`, `proposed_state`,
`written`, `truth_claim` (always `false`), `safety_notes`.

### Operational profile

`scripts/run_operational_checks.py --profile source-enable` (M10.3)
chains four **offline** checks:

1. `scripts/validate_source_registry.py --json` — schema check.
2. `scripts/enable_registry_source.py --list` — status smoke.
3. `scripts/enable_registry_source.py --source-id kr_law_open_data_candidate --justification "operator dry run test justification text" --dry-run`
   — expected dry-run summary (exit 0; nothing is written).
4. `tests/test_enable_registry_source.py` — full offline test suite.

The profile never hits Render, never calls OpenAI, never starts a
server, and never modifies `data/source_registry.json`. The dry-run
step's tests use temp registry copies so the real seed is never
written.

## Text Extraction Pipeline

Use `scripts/extract_artifact_text.py` (Phase 2 M10.4) to extract
structured text from rows in `source_fetch_artifacts`. Reads from the
local SQLite DB and writes (only with `--save`) to the new
`artifact_text_extractions` table. Fully offline — no HTTP, no
browser, no OpenAI. The source `source_fetch_artifacts` table is
never mutated.

### Usage

```
python scripts/extract_artifact_text.py --list-artifacts
python scripts/extract_artifact_text.py --list-artifacts --source-id <id>
python scripts/extract_artifact_text.py --source-id <id> --dry-run
python scripts/extract_artifact_text.py --source-id <id> --save
python scripts/extract_artifact_text.py --artifact-id <int> --dry-run --json
```

`--dry-run` and `--save` are mutually exclusive. With neither flag the
extractor still runs in a read-only mode (results are printed; nothing
is written). `--db-path <path>` retargets both the read and the write
to a different SQLite file (used by tests and by an operator working
against an isolated DB).

### What extraction produces

Each `ExtractionResult` carries the following fields (stable wire
shape, mirrored on the `artifact_text_extractions` row):

- `artifact_id` — id of the source `source_fetch_artifacts` row
- `source_id` — copied from the source row
- `url` — copied from the source row
- `extraction_timestamp` — ISO 8601 UTC
- `extraction_duration_ms` — wall-clock ms for the BeautifulSoup pass
- `success` — bool; `False` when the source row had no `raw_html` or
  the fetch itself was unsuccessful
- `error` — short reason string when `success=False`
- `title` — text inside the page `<title>` tag, stripped
- `main_text` — cleaned body text after `script` / `style` / `nav` /
  `footer` / `header` are stripped, truncated to 50 000 chars
- `sections` — JSON-encoded list of `{heading, text}` pairs derived
  from the document's `h1` / `h2` / `h3` structure
- `word_count` — whitespace-split word count of `main_text`
- `language_hint` — `"ko"` (Hangul mass > 20% of non-whitespace
  characters), `"en"` (ASCII alpha > 60%), or `"unknown"`
- `truth_claim` — **always** `False`
- `official_source_candidate` — mirrored from the source row

### Important safety notes

These notes appear in every output mode (human + `--json`):

- `truth_claim=False` — extraction results do not imply truth of
  any content.
- Extraction results are raw text artifacts requiring separate human
  review.
- This extractor never feeds the analysis pipeline or verdict logic;
  extractions are stored as raw artifacts only.

The module is pinned by tests to:

- Force `truth_claim=False` even when the source row carries
  `truth_claim=True`.
- Never modify `source_fetch_artifacts`.
- Stay out of the analysis pipeline — `main.py`, `api_server.py`,
  and `scheduler.py` do not import the extractor or its CLI.
- Avoid every network / browser / OpenAI import
  (`requests`, `httpx`, `urllib.request`, `socket`, `playwright`,
  `browser_use`, `openclaw`, `selenium`, `openai`, `anthropic`).

### Database table

```
CREATE TABLE IF NOT EXISTS artifact_text_extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    extraction_timestamp TEXT NOT NULL,
    extraction_duration_ms INTEGER,
    success INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    title TEXT,
    main_text TEXT,
    sections TEXT,
    word_count INTEGER,
    language_hint TEXT,
    truth_claim INTEGER NOT NULL DEFAULT 0,
    official_source_candidate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_text_extractions_artifact
    ON artifact_text_extractions(artifact_id);
```

Created idempotently via `_ensure_artifact_text_extractions_table`
from `init_db()` (or the standalone
`init_artifact_text_extractions_table()`). The `truth_claim` column
is forced to `0` on every `save_extraction_result` call regardless of
the caller's input — defense-in-depth against future regressions.

### Exit codes

- `0` — extractions attempted successfully (some individual rows may
  have `success=False`; that's reported, not fatal)
- `1` — DB error, no artifacts found, or every extraction failed
- `2` — CLI usage error (missing required flags, conflicting flags,
  unrecognized args)

### Operational profile

`scripts/run_operational_checks.py --profile source-extractor`
(M10.4) chains three **offline** checks:

1. `scripts/validate_source_registry.py --json` — schema check.
2. `scripts/extract_artifact_text.py --help` — CLI smoke.
3. `tests/test_artifact_extractor.py` — full offline test suite
   (uses temp SQLite files; the real `policy_ai.db` is never
   touched).

The profile never hits Render, never calls OpenAI, never starts a
server, and never modifies the source `source_fetch_artifacts` table.
