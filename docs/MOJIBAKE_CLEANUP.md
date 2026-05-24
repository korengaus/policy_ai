# Mojibake Sentinel Cleanup — M11.6

## Background

`claude_audit_phase1.md` §1.5 #6 identified two string literals in
`official_crawler.py` that are encoding-corrupted ("mojibake"). The
audit cited lines 815 and 938; the actual current line numbers are
**1010** and **1133** — drift from intervening edits.

Both literals were used as `in (title or "")` membership checks
inside `fetch_best_official_document`, gated by `result["site_key"]
== "fss"`. They were meant to detect an FSS (Financial Supervisory
Service / 금융감독원) error-page response and short-circuit the
fetch. Because the page titles produced by `_extract_html_text` are
properly-decoded UTF-8 Korean strings, the mojibake byte sequences
never appeared in any real title — the checks were silently dead.

M11.6 deletes both dead checks. The cleanup is byte-identical to
today's production behavior: a check that always failed has the same
control-flow effect whether it's present or absent.

## Diagnosis Results

### Sentinel 1 — Line 1010

**Raw bytes:** `?\xeb\xa8\xae\xec\x9c\xad?\xec\x84\x8f\xec\x94\xa0\xef\xa7\x9e\xc2\x80` (16 bytes — note the trailing `\xef\xa7\x9e` is U+F9DE, a CJK compatibility ideograph, and `\xc2\x80` is the control character U+0080, both classic mojibake fingerprints).

**Function context:** Initial-search short-circuit. If the check matched, the function would populate one fake attempt entry, set `usable=False` + `error="FSS search returned error page"`, and `return result` early. Removed because the literal can never match.

**Probable intended Korean string:** likely `에러페이지` or `오류페이지` ("error page"), but the exact original is unrecoverable.

**Confidence in intent:** MEDIUM — error message + site_key gate are clear, but the original glyphs are ambiguous.

### Sentinel 2 — Line 1133

**Raw bytes:** `?\xe7\x99\x92?\xec\x91\x8e??\xeb\xa5\x81\xeb\xb5\xa0\xe7\xad\x8c\xec\x99\x96\xc2\x80` (22 bytes — same `\xc2\x80` trailing marker, plus several CJK-range bytes that don't roundtrip to common Korean).

**Function context:** Per-attempt skip inside the retry loop. If matched, the function would log the attempt with the same `"FSS search returned error page"` error and `continue` to the next attempt. Removed because the literal can never match.

**Probable intended Korean string:** likely `데이터없음` / `결과없음` / similar "no data" marker, but unrecoverable.

**Confidence in intent:** MEDIUM (same reasoning).

## Resolution

| Sentinel | Line | Action | Lines removed |
| --- | --- | --- | --- |
| 1 (`?먮윭?섏씠吏…`) | 1010 | APPLIED (DELETE) | ~17 (full `if` block + trailing blank) |
| 2 (`?癒?쑎??륁뵠筌왖…`) | 1133 | APPLIED (DELETE) | ~5 (full `if` block + trailing blank) |

After cleanup the file shrank from 1523 lines to 1501 lines (22 lines removed). Module compiles, imports cleanly, and `fetch_best_official_document` still returns the documented public shape — confirmed by `tests/test_mojibake_cleanup.py`.

## Default-to-delete rationale

The platform has been operating in production with these dead checks for an unknown duration. Render's verdict outputs reflect the dead-check state — FSS error pages currently flow through the normal link-extraction logic rather than being short-circuited. **Restoring** a check (by guessing the intended Korean string) would change that production behavior: pages that currently pass through would suddenly be marked `usable=False` with a synthesized error. That is a feature change, not a cleanup, and is out of scope for M11.6.

If a future PR wants to add an FSS error-page detector, it should be a deliberate, behavior-changing PR with its own approval cycle — not a side-effect of fixing encoding garbage.

## What's NOT in M11.6

- Exception swallowing fixes (audit §1.5 #8 — future M11.7)
- Magic threshold consolidation (audit §1.5 #7 — future M11.x)
- Korean keyword duplication beyond M11.2 (future M11.5b)
- Verdict producer unification (audit §1.5 #1 — future M11.0d)

## Verification pins

- `tests/test_mojibake_cleanup.py` (M11.6 — 7 cases: byte-sequence absence × 2, dead-error-string absence, generic `?<Hangul>` heuristic, module import smoke, public-shape smoke, fall-through pin for the deleted Sentinel 1 path)
- `tests/test_source_crawler.py` (regression)
- `tests/test_source_registry.py` (regression)
- `tests/test_artifact_extractor.py` (regression)
- `tests/test_artifact_evidence_linker.py` (regression)
- `tests/test_verdict_label_b08_fix.py` (24 — regression)
- `tests/test_verdict_label_diagnostic.py` (42 — regression)
- `tests/test_verdict_producer_comparison.py` (37 — regression)
- `tests/test_dead_code_removal.py` (M11.5 regression)
- `tests/test_verification_card_dedup.py` (M11.4b regression)
- `npm test` (regression unchanged)
