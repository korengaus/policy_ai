# Duplicate Function Definitions — Audit and Resolution (M11.4)

## Background

`claude_audit_phase1.md` §1.5 #2 flagged two suspected duplicate function
definitions in the verification pipeline:

1. `verification_card._missing_context_specific` defined twice in the same
   module (audit said L504 and L543).
2. `_official_adjusted_evidence_quality` allegedly present in both
   `verification_card.py:377` and `pipeline_debug.py:70`.

M11.4 was tasked with diagnosing each duplicate and removing only those
that are byte-identical, stopping when divergence is found rather than
silently picking a winner.

## Diagnosis Results

### Duplicate 1: `verification_card._missing_context_specific`

**Locations (line numbers drifted from the audit's 504/543):**

- First definition: `verification_card.py:491`
- Second definition: `verification_card.py:530`

Python silently shadows the first definition with the second. The
active version that runs at request time is **L530**.

**Body comparison — DIFFERENT (Case 2 per the M11.4 brief).**

The two functions share the same signature
`(official_sources, evidence_comparison, official_evidence_results) -> list[str]`
but their bodies diverge on at least four observable axes:

| Axis | L491 (shadowed) | L530 (active) |
|---|---|---|
| URL-availability check | `selected_document_url` OR `official_search_url` OR `search_url` — permissive | `selected_document_url` only — strict |
| Error-handling order | check `has_url` then inline-check `any(error)` | precompute `has_error`, then check |
| "Detail URL missing" Korean string | "공식기관 후보는 있으나 확인 가능한 상세 URL이 부족합니다." | "공식기관 후보는 찾았지만, 확인 가능한 상세 문서 URL이 부족합니다." |
| "URL OK but body fetch failed" Korean string | "공식기관 URL은 확인됐지만 본문 수집에 실패했습니다." | "공식기관 URL은 확인됐지만, 실제 상세 본문 확인은 실패했습니다." |
| "Body collected but no match" Korean string | "공식기관 본문은 수집됐지만 핵심 주장과의 직접 일치가 부족합니다." | "공식기관 본문은 수집됐지만, 기사 핵심 주장과 직접 일치하지 않아 신뢰도를 낮게 반영했습니다." |
| "Body not yet collected" Korean string | "공식기관 후보는 확인되었지만 실제 본문 또는 상세 문서 본문은 아직 수집되지 않았습니다." | "공식기관 후보는 확인됐지만, 실제 본문 또는 상세 문서 본문은 아직 수집되지 않았습니다." |
| `weak_official_match` / `low_confidence_match` message | "공식문서와 뉴스 주장 사이의 정책명, 대상, 시행 여부 직접 확인이 필요합니다." | "공식 출처가 기사 내용과 직접 일치하지 않아 추가 확인이 필요합니다." |
| `excluded_non_policy_page` message | "수집된 공식문서가 목록, 안내, 민원 문서로 제외되었습니다." | "수집된 공식 문서가 목록, 안내, 민원 문서로 분류되어 검증 근거에서 제외했습니다." |
| Default fallback when none of the conditions match | "최종 공개 전 사람 검토와 원문 재확인이 필요합니다." | "최종 공개 전에는 원문과 공식 발표를 다시 확인하는 것이 좋습니다." |

The L530 URL-availability check is materially **stricter** than L491
(only accepts `selected_document_url`, not `official_search_url` /
`search_url`). The user-facing Korean strings differ across nearly
every branch. These are not formatting differences — they are real
behavior + UX divergences that an operator must adjudicate.

**Callers within verification_card.py:** `verification_card.py:692`
inside the `missing_context` field of the verification-card builder.
One caller. Python resolves it to L530.

**Callers outside verification_card.py:** none.

**Active version (which one Python actually runs):** L530.

**Safety verdict:** **UNSAFE (STOP).** Per the M11.4 brief's Case 2:

> Different definitions in same module: STOP and report. Do NOT silently
> pick one. Ask the operator which behavior is intended. This case is
> dangerous — the first definition was supposed to do something that
> the second doesn't, but it was being silently overwritten. We need a
> human decision before discarding either version's behavior.

### Duplicate 2: `_official_adjusted_evidence_quality` cross-module

**Locations:**

- `verification_card.py:354` — `_official_adjusted_evidence_quality(quality_summary, source_reliability_summary)`
- `pipeline_debug.py:70` — `_official_adjusted_quality_summary(quality_summary, official_mismatch)`

**Audit accuracy:** the audit said both were named
`_official_adjusted_evidence_quality`. **They are not.** A repo-wide
grep confirms `_official_adjusted_evidence_quality` is defined in
exactly **one** place (verification_card.py:354). The pipeline_debug.py
function with similar internal logic is named
`_official_adjusted_quality_summary` and has a different signature.

**Body comparison — DIFFERENT signatures, same algorithm body.**

| Axis | verification_card:354 | pipeline_debug:70 |
|---|---|---|
| Name | `_official_adjusted_evidence_quality` | `_official_adjusted_quality_summary` |
| Param 2 | `source_reliability_summary: dict` | `official_mismatch: bool` |
| Mismatch check | `source_reliability_summary.get("official_mismatch")` | `official_mismatch` (direct bool) |
| Body after the mismatch check | identical | identical |

The 12 lines after the mismatch check are byte-identical. The
parameter shape differs: verification_card's version pulls the
flag from a dict; pipeline_debug's takes the bool directly.

**Callers of `verification_card._official_adjusted_evidence_quality`:**
verification_card.py:641 (one caller, internal).

**Callers of `pipeline_debug._official_adjusted_quality_summary`:**
pipeline_debug.py:187 (one caller, internal).

**Safety verdict:** **UNSAFE (STOP).** This is not a duplicate in the
literal sense the audit suggested — the two functions have different
names, different signatures, and different consumers. Centralizing
them would require:

1. Picking a canonical signature (dict-with-flag vs. bool-direct).
2. Either rewriting one caller to match the other's expected param
   shape, OR introducing a thin adapter that pulls the bool from the
   dict on one side. Either is a behavior-affecting change.
3. Renaming one of the two functions, which the brief explicitly
   forbids ("Do NOT change function signatures or return shapes").

Per the brief's "If they differ, treat as Case 2 — STOP and report"
rule.

## Resolution

**No source files were modified.** Both diagnoses produced UNSAFE
verdicts.

Per the M11.4 safety invariant
("If diagnosis reveals ANY behavioral difference between duplicate
definitions, STOP. Report findings only. Do not pick one."), this PR
contains only this audit document. The operator must decide:

### Decision points for Duplicate 1

The shadowed L491 version is dead code today — Python doesn't run it —
so deleting it preserves current behavior. The question is whether
L491's *intended* behavior should replace L530's (or be merged into
it). Specifically:

1. **URL acceptance**: should `_missing_context_specific` treat
   `official_search_url` / `search_url` as a fallback "we have a URL"
   signal (L491 behavior), or insist on `selected_document_url` only
   (current L530 behavior)? The L491 logic was originally landed
   first; the L530 version may be a tightening that was supposed to
   replace it but was added below instead of above. If L530 is the
   intended behavior, the dead L491 can be removed without behavior
   change.
2. **Korean message wording**: the L530 messages are uniformly more
   detailed (with explicit commas and qualifying phrases) than L491.
   The L530 messages appear to be a later editorial pass. If
   confirmed, L491 can be dropped.

If the operator confirms "L530 is correct, L491 is the old version
that was supposed to be replaced", a follow-up M11.4b can delete
L491 with a single targeted commit + the dedup uniqueness pin the
brief described.

### Decision points for Duplicate 2

This is a **misclassification in the audit**, not a true duplicate.
Two paths forward, both deferrable:

1. **Leave as-is.** The two functions live in different modules with
   different signatures; they read like deliberately parallel
   implementations for different call contexts (one for the
   verification-card builder which has the full
   `source_reliability_summary` dict in hand, one for the
   pipeline-debug summary which has the bool already extracted).
   No real duplication harm.
2. **Extract a shared helper.** A future M11.5 could pull the
   12-line body into a `_apply_official_mismatch_penalty(quality_summary)`
   utility, and have both call sites adapt the flag extraction
   externally. This would require touching two non-trivial call
   chains and is out of scope for M11.4.

### What this PR ships

- `docs/DUPLICATE_FUNCTION_AUDIT.md` (this file) with the diagnosis
  and the unresolved decision points.
- No source-file changes.
- No new test files.

## Verification pins (regression — must remain green even though no
code changed)

All three verdict suites re-run as part of M11.4's validation pass to
confirm the diagnosis-only outcome did not perturb anything:

- `tests/test_verdict_label_b08_fix.py` — 24 cases, regression
- `tests/test_verdict_label_diagnostic.py` — 42 cases, regression
- `tests/test_verdict_producer_comparison.py` — 37 cases, regression
- `npm test` (regression byte-identical)

## What's NOT in M11.4

- Verdict producer unification (audit §1.5 #1 — separate work, future M11.0d)
- Korean keyword centralization beyond M11.2's 10 constants (future M11.x)
- Removing dead code branches in `evidence_comparator` (future M11.5)
- Source-file modifications to either duplicate — deferred pending
  operator adjudication of Duplicate 1's behavioral divergence.

## Resolution (M11.4b)

**Duplicate 1 (`_missing_context_specific` in verification_card.py):**

- L491 (first, dead — shadowed by L530) was deleted. ~37 lines of
  function body + the 2 blank-line separator that followed it were
  removed; the two blank lines preceding the deleted function now
  serve as the PEP 8 separator before the surviving definition.
- L530 (second, active — what Python was already executing) is now
  the sole definition. It moved up to L491 after the deletion but is
  otherwise byte-for-byte unchanged.
- Production behavior is unchanged: L530 was the only version ever
  called at runtime. Render and the Render baseline smoke should
  return byte-identical results before and after this PR.
- Uniqueness pinned by
  `tests/test_verification_card_dedup.py::UniquenessTests::test_missing_context_specific_defined_exactly_once`.
- Behavioral contract pinned by 3 further classes in the same file:
  - `SignatureTests` — pins the L530 signature.
  - `StrictUrlAcceptanceTests` — pins that ONLY `selected_document_url`
    counts as a usable detail URL (the L530 strict rule; the dead L491
    also accepted `official_search_url` / `search_url`).
  - `KoreanMessagePinTests` — pins three L530 user-facing strings:
    the `weak_official_match` message, the `excluded_non_policy_page`
    message, and the default fallback "최종 공개 전에는…" phrasing.

**Duplicate 2 (`_official_adjusted_evidence_quality` cross-module):**

- Audit misclassification confirmed in M11.4. The functions in
  `verification_card.py` and `pipeline_debug.py` have different names
  (`_official_adjusted_evidence_quality` vs
  `_official_adjusted_quality_summary`) and different signatures
  (dict-with-flag vs. bool-direct). They are not duplicates in any
  literal sense.
- No action needed. Closed.

### M11.4b verification

- All 11 cases in `tests/test_verification_card_dedup.py` pass.
- The 3 verdict regression suites stay green:
  `test_verdict_label_b08_fix` (24), `test_verdict_label_diagnostic` (42),
  `test_verdict_producer_comparison` (37).
- `scripts/validate.py` adds the new dedup test to its run set.
- `npm test` remains byte-identical (frontend / build / regression
  unchanged).
