# HONESTY-GUARD B3 Phase 2a — pure structural validator for outgoing payloads.
#
# Mechanizes the moat: tickedin NEVER emits a truth/falsity verdict. This
# module is a CHECKER only — it raises no verdict, mutates nothing, changes
# no behavior anywhere. Nothing imports it yet (middleware wiring is a later
# slice); it is import-safe and side-effect-free (constants + pure functions).
#
# Design: _honesty_guard_b3_design.md (Phase 1). Invariants:
#   I1  wherever the key `truth_claim` appears (any depth)          -> exactly False
#   I2  wherever `operator_review_required` appears                 -> exactly True
#   I3  wherever `verdict_label` appears  -> member of the LEGAL closed set;
#       wherever `policy_alert_level` appears -> member of the legal alert set
#   I4  NO truth-probability-shaped key anywhere (denylist predicate),
#       EXCEPT the exact whitelisted evidence-confidence keys
#   I5  forbidden truth-vocab ONLY on designated GENERATED-label fields
#       (never titles/claims/our fixed honest copy), plus the fixed framing
#       strings whitelisted BYTE-EXACT — any drift from them IS a violation
#       (the generate_weekly_report.honesty_guard_ok precedent).
#
# VERDICT ISOLATION: deliberately imports NO verdict module (verification_card
# / policy_decision / llm_judge / api_server). The closed sets below are
# duplicated constants; tests/test_honesty_guard.py SYNC-PINS each one against
# its authoritative source, so divergence fails CI instead of drifting silently.
# pin-OUT: new file, no log sites in pinned files — 331/16 unaffected.

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# I3 — legal closed sets.
# ---------------------------------------------------------------------------
# AUTHORITATIVE SOURCE: verification_card.py:486-564 (_verdict_label return
# literals) + the AnalyzeResult default "" (api_server.py:208). Sync-pinned by
# tests/test_honesty_guard.py::SyncWithAuthoritativeSourcesTests.
LEGAL_VERDICT_LABELS = frozenset({
    "",  # unset default (api_server.AnalyzeResult.verdict_label)
    "draft_disputed",
    "draft_high_risk_review",
    "draft_needs_review",
    "draft_needs_official_confirmation",
    "draft_needs_context",
    "draft_verified",
    "draft_likely_true",
    "draft_unverified",
})

# AUTHORITATIVE SOURCE: policy_decision.py:90-124 (_policy_alert_level return
# literals). None/"" tolerated as "unset" (old rows); sync-pinned like above.
LEGAL_ALERT_LEVELS = frozenset({"HIGH", "MEDIUM", "WATCH", "LOW"})

# ---------------------------------------------------------------------------
# I4 — truth-probability-shaped key denylist (normalized-key predicate).
# A key "reads as P(true)" when its normalized form is one of the exact names
# or starts with one of the prefixes. Normalization: lowercase + any run of
# non-alphanumerics collapsed to "_", so "P(true)", "p-true", "P True" all
# normalize to "p_true".
# ---------------------------------------------------------------------------
_TRUTH_PROB_EXACT = frozenset({
    "truth_probability", "truth_likelihood", "truth_score",
    "p_true", "prob_true", "probability_true", "likelihood_true",
    "likely_true",  # as a bare KEY; the verdict_label VALUE draft_likely_true is legal (I3)
    "is_true", "is_false",
    "fact_score", "accuracy_score",
})
_TRUTH_PROB_PREFIXES = ("veracity", "factuality", "truthfulness")

# Exact-key whitelist — legal EVIDENCE-confidence fields a broader scan might
# flag. These measure pipeline/evidence strength, never P(true).
CONFIDENCE_KEY_WHITELIST = frozenset({
    "verdict_confidence", "policy_confidence_score",
})

# ---------------------------------------------------------------------------
# I5 — forbidden vocab, applied ONLY to fields this system GENERATES as
# labels. Titles / claims / snippets are journalist passthrough; the honest
# fixed copy (missing_context, evidence_summary, framing) legitimately uses
# 검증 and is exempt BY FIELD SCOPE, not by content matching.
# ---------------------------------------------------------------------------
# AUTHORITATIVE SOURCE: scripts/build_brainmap_graph.FORBIDDEN_LABEL_VOCAB
# (sync-pinned) + the endpoint-test extras (test_trending_endpoint.py:223).
FORBIDDEN_LABEL_VOCAB = ("검증", "confirmed", "verified", "truth", "probability")

# The generated-label fields the vocab rule scans.
GENERATED_LABEL_FIELDS = frozenset({"size_label", "kind"})

# Fixed honest framing copy, whitelisted BYTE-EXACT. The weekly framing
# deliberately contains 검증 inside a negation; the faded framing carries 진위.
# Any drift from these exact bytes is a violation (I5_FRAMING_DRIFT).
# AUTHORITATIVE SOURCES (sync-pinned): scripts/generate_weekly_report.FRAMING_TEXT,
# api_server._FADED_FRAMING, and scripts/build_brainmap_graph.SYNDICATION_FRAMING
# (B5d 2b — the spread-structure syndication line exposed via /api/spread).
FRAMING_WHITELIST = frozenset({
    "확산 규모 기준 · 사실 검증 아님",
    "이 목록은 후속 보도가 끊긴 사실만 보여줍니다. 주장의 진위나 정책의 "
    "추진·성패에 대한 판단이 아니며, 후속 보도가 저희 수집망 밖에 "
    "있었을 수도 있습니다.",
    "첫 보도와 제목·주장 문구가 거의 동일",
})
_FRAMING_FIELD = "framing"


def _normalize_key(key: str) -> str:
    """Lowercase; collapse every run of non-alphanumerics to one "_"."""
    out = []
    prev_sep = False
    for ch in key.lower():
        if ch.isalnum():
            out.append(ch)
            prev_sep = False
        elif not prev_sep:
            out.append("_")
            prev_sep = True
    return "".join(out).strip("_")


def _is_truth_probability_key(key: str) -> bool:
    if key in CONFIDENCE_KEY_WHITELIST:
        return False
    normalized = _normalize_key(key)
    if normalized in _TRUTH_PROB_EXACT:
        return True
    return normalized.startswith(_TRUTH_PROB_PREFIXES)


def _violation(path: str, rule: str, detail: str) -> dict:
    return {"path": path, "rule": rule, "detail": detail}


def _check_pair(key: str, value: Any, path: str, violations: list) -> None:
    """All per-key rules for one dict entry (the value's subtree is walked
    separately by _walk)."""
    # I1
    if key == "truth_claim" and value is not False:
        violations.append(_violation(
            path, "I1_TRUTH_CLAIM_NOT_FALSE", "truth_claim=%r" % (value,)))
    # I2
    if key == "operator_review_required" and value is not True:
        violations.append(_violation(
            path, "I2_REVIEW_NOT_REQUIRED",
            "operator_review_required=%r" % (value,)))
    # I3
    if key == "verdict_label":
        if not (isinstance(value, str) and value in LEGAL_VERDICT_LABELS):
            violations.append(_violation(
                path, "I3_ILLEGAL_VERDICT_LABEL", "verdict_label=%r" % (value,)))
    if key == "policy_alert_level":
        # None/"" tolerated as unset (old rows); anything else must be legal.
        if value not in (None, "") and not (
                isinstance(value, str) and value in LEGAL_ALERT_LEVELS):
            violations.append(_violation(
                path, "I3_ILLEGAL_ALERT_LEVEL",
                "policy_alert_level=%r" % (value,)))
    # I4
    if _is_truth_probability_key(key):
        violations.append(_violation(
            path, "I4_TRUTH_PROBABILITY_KEY",
            "key %r reads as a truth-probability field" % key))
    # I5 — generated-label fields only.
    if key in GENERATED_LABEL_FIELDS and isinstance(value, str):
        lowered = value.lower()
        for word in FORBIDDEN_LABEL_VOCAB:
            if word in lowered:
                violations.append(_violation(
                    path, "I5_FORBIDDEN_VOCAB",
                    "generated field %r carries %r" % (key, word)))
    # I5 — the framing field must be one of the fixed strings, byte-exact.
    if key == _FRAMING_FIELD and value not in FRAMING_WHITELIST:
        violations.append(_violation(
            path, "I5_FRAMING_DRIFT",
            "framing drifted from the fixed honest copy"))


def _walk(node: Any, path: str, violations: list, seen: set) -> None:
    # Containers can't cycle in JSON payloads, but the validator must never
    # hang or raise on odd in-process objects — guard by identity.
    if isinstance(node, dict):
        if id(node) in seen:
            return
        seen.add(id(node))
        for key, value in node.items():
            child_path = "%s.%s" % (path, key) if path else str(key)
            if isinstance(key, str):
                _check_pair(key, value, child_path, violations)
            _walk(value, child_path, violations, seen)
    elif isinstance(node, (list, tuple)):
        if id(node) in seen:
            return
        seen.add(id(node))
        for index, item in enumerate(node):
            _walk(item, "%s[%d]" % (path, index), violations, seen)
    # Scalars / unknown types: nothing to check at this level (fail-open on
    # SHAPE — value rules fire only where the keys exist; fail-closed on VALUE).


def validate_payload(payload: Any) -> tuple[bool, list[dict]]:
    """Validate one outgoing (JSON-serializable) payload against I1-I5.

    Returns (ok, violations); each violation is {"path", "rule", "detail"}.
    Pure and deterministic: never mutates the payload, never raises a verdict,
    performs no I/O, and same input always yields the same output.
    """
    violations: list[dict] = []
    _walk(payload, "", violations, set())
    return (not violations), violations
