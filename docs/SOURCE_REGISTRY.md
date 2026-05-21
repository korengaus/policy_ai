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
