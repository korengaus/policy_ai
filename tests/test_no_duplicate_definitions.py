"""audit §1.5 #2 re-audit (2026-05-26): no-duplicate-definitions pins.

This file generalises M11.4b's single-function uniqueness pin (which
only guarded ``verification_card._missing_context_specific``) into a
codebase-wide AST-walk pin AND adds a cross-file name-collision
allowlist so any new collision requires explicit operator review.

The Phase 1 re-audit confirmed:

  - CASE A (verification_card._missing_context_specific): resolved
    by M11.4b. Single definition; behaviour pinned by 11 tests in
    tests/test_verification_card_dedup.py.
  - CASE B (_official_adjusted_* cross-file): audit
    misclassification — the two functions have different names and
    different signatures (dict-with-flag vs bool-direct). M11.4
    closed this as "no action needed".

This file adds 6 pins as a forward-looking defense-in-depth:

  1. NoIntraFileDuplicateDefsPin.test_no_module_level_duplicate_defs
     — AST-walk every repo-root *.py; no module has two same-named
     module-level defs.

  2. NoIntraFileDuplicateDefsPin.test_no_class_level_duplicate_defs
     — Within each class body, no two methods share a name.

  3. Case1ResolvedPin.test_missing_context_specific_still_unique
     — Defense-in-depth restating M11.4b's pin; guards against
     accidental deletion of the existing pin file.

  4. Case2NotADuplicatePin.test_official_adjusted_functions_have_different_signatures
     — Asserts the two functions are not the same callable AND have
     different parameter shapes. Pins M11.4's classification.

  5. KnownGoodCrossFileDuplicatesAllowlist.test_cross_file_name_collisions_match_allowlist
     — Pins the current set of cross-file name collisions. Growth
     requires updating the allowlist (and ideally a separate
     consolidation milestone).

  6. Case2BodyEquivalencePin.test_official_adjusted_functions_produce_equivalent_output
     — Runtime pin: given the same effective ``official_mismatch``
     truth value, both functions return byte-identical dicts.

Scope: walks repo-root *.py files. The ``tests/`` and ``scripts/``
directories are excluded — test files commonly contain duplicate
``setUp`` / ``tearDown`` methods across classes (legal per-class),
and ``scripts/`` are operator utilities outside the production
pipeline.
"""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _iter_repo_root_python_files() -> list[Path]:
    """Return every *.py file at repo root, excluding tests/ and
    scripts/ (which have their own conventions). Sorted for
    deterministic iteration."""
    return sorted(
        path
        for path in _PROJECT_ROOT.glob("*.py")
        if path.is_file()
    )


def _module_level_function_names(tree: ast.Module) -> dict[str, list[int]]:
    """Map module-level def name → list of line numbers where it is
    defined. A name with len(...) > 1 is a duplicate."""
    out: dict[str, list[int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node.lineno)
    return out


def _class_level_function_names(
    tree: ast.Module,
) -> dict[str, dict[str, list[int]]]:
    """For each top-level ClassDef in ``tree``, return its method
    names → line numbers. A name with len(...) > 1 inside the same
    class is a duplicate."""
    out: dict[str, dict[str, list[int]]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        method_names: dict[str, list[int]] = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_names.setdefault(item.name, []).append(item.lineno)
        out[node.name] = method_names
    return out


# ---------------------------------------------------------------------------
# 1+2: No intra-file duplicate defs (module-level OR class-level)
# ---------------------------------------------------------------------------


class NoIntraFileDuplicateDefsPin(unittest.TestCase):
    """Generalises M11.4b's single-function pin to the whole
    codebase. Catches any future PR that accidentally re-introduces
    a same-named def within a single module — the audit §1.5 #2
    failure mode that produced silent function shadowing."""

    def test_no_module_level_duplicate_defs(self):
        offenders: list[str] = []
        for path in _iter_repo_root_python_files():
            try:
                # ``utf-8-sig`` strips a leading BOM if present so the
                # AST parser doesn't choke on it (one pre-existing
                # repo-root file ships with a BOM).
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError as exc:
                self.fail(f"{path.name} did not parse: {exc}")
            for name, linenos in _module_level_function_names(tree).items():
                if len(linenos) > 1:
                    offenders.append(
                        f"{path.name}::{name} defined at lines {linenos!r} "
                        "(later definitions silently shadow the earlier "
                        "ones — see audit §1.5 #2 / M11.4b for the canonical "
                        "failure mode this pin guards against)"
                    )
        if offenders:
            self.fail(
                "Intra-file duplicate module-level def(s) detected:\n  "
                + "\n  ".join(offenders)
            )

    def test_no_class_level_duplicate_defs(self):
        offenders: list[str] = []
        for path in _iter_repo_root_python_files():
            try:
                # ``utf-8-sig`` strips a leading BOM if present so the
                # AST parser doesn't choke on it (one pre-existing
                # repo-root file ships with a BOM).
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError as exc:
                self.fail(f"{path.name} did not parse: {exc}")
            for class_name, methods in _class_level_function_names(tree).items():
                for method_name, linenos in methods.items():
                    if len(linenos) > 1:
                        offenders.append(
                            f"{path.name}::{class_name}.{method_name} "
                            f"defined at lines {linenos!r}"
                        )
        if offenders:
            self.fail(
                "Intra-class duplicate method def(s) detected:\n  "
                + "\n  ".join(offenders)
            )


# ---------------------------------------------------------------------------
# 3: CASE A — _missing_context_specific defense-in-depth
# ---------------------------------------------------------------------------


class Case1ResolvedPin(unittest.TestCase):
    """Defense-in-depth: restate the M11.4b pin from
    ``tests/test_verification_card_dedup.py`` so a future PR that
    accidentally deletes the existing pin file does not silently
    drop the protection."""

    def test_missing_context_specific_still_unique(self):
        path = _PROJECT_ROOT / "verification_card.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _module_level_function_names(tree)
        linenos = names.get("_missing_context_specific", [])
        self.assertEqual(
            len(linenos), 1,
            f"verification_card._missing_context_specific must be "
            f"defined exactly once (M11.4b resolution). Found "
            f"{len(linenos)} definition(s) at lines {linenos!r}. Did "
            f"the audit §1.5 #2 fix regress?",
        )


# ---------------------------------------------------------------------------
# 4: CASE B — different signatures pin
# ---------------------------------------------------------------------------


class Case2NotADuplicatePin(unittest.TestCase):
    """The audit §1.5 #2 text claimed
    ``_official_adjusted_evidence_quality`` is defined in both
    ``verification_card.py`` and ``pipeline_debug.py``. M11.4 found
    this was an audit misclassification — the two functions have
    different names AND different signatures. This pin codifies that
    finding: a future "consolidation" PR that re-introduces literal
    name + signature duplication will fail this test."""

    def test_official_adjusted_functions_have_different_signatures(self):
        import verification_card
        import pipeline_debug

        vc_fn = verification_card._official_adjusted_evidence_quality
        pd_fn = pipeline_debug._official_adjusted_quality_summary

        # They must be distinct callables.
        self.assertIsNot(
            vc_fn, pd_fn,
            "verification_card._official_adjusted_evidence_quality and "
            "pipeline_debug._official_adjusted_quality_summary are the "
            "same object — audit §1.5 #2 / M11.4 resolution was reverted.",
        )

        # Their parameter shapes must differ.
        vc_params = list(inspect.signature(vc_fn).parameters.keys())
        pd_params = list(inspect.signature(pd_fn).parameters.keys())
        self.assertEqual(
            vc_params, ["quality_summary", "source_reliability_summary"],
            f"verification_card._official_adjusted_evidence_quality "
            f"signature changed; expected (quality_summary, "
            f"source_reliability_summary), got {vc_params!r}",
        )
        self.assertEqual(
            pd_params, ["quality_summary", "official_mismatch"],
            f"pipeline_debug._official_adjusted_quality_summary "
            f"signature changed; expected (quality_summary, "
            f"official_mismatch), got {pd_params!r}",
        )


# ---------------------------------------------------------------------------
# 5: Cross-file name-collision allowlist
# ---------------------------------------------------------------------------


# Pinned allowlist captured 2026-05-26 (audit §1.5 #2 re-audit).
# Each name appears as a module-level `def` in TWO OR MORE repo-root
# Python files. Most are independent per-module helpers (timestamps,
# normalization, level mapping) that are NOT semantically equivalent
# across files — consolidating any of them is a separate behavior-
# affecting milestone.
#
# Growth of this set requires either:
#   (a) updating the allowlist if the new collision is intentional,
#       OR
#   (b) consolidating the new collision to a single source.
_KNOWN_CROSS_FILE_NAME_COLLISIONS: frozenset[str] = frozenset({
    "_claim_text",
    "_coerce_analysis_id",
    "_coerce_int",
    "_domain",
    "_empty_result",
    "_extract_title",
    "_hangul_count",
    "_has_any",
    "_level",
    "_normalize",
    "_normalize_text",
    "_now_iso",
    "_numbers",
    "_policy_claims_text",
    "_reconstruct_claim_count",
    "_reconstruct_evidence_comparison",
    "_response_from_cache_entry",
    "_row_to_dict",
    "_safe_json_load",
    "_same_domain",
    "_split_sentences",
    "_tokens",
    "_utc_now_iso",
    "get_job_status",
    "health_check",
    "main",
    "normalize_domain",
})


class KnownGoodCrossFileDuplicatesAllowlist(unittest.TestCase):
    """Pin the set of cross-file name collisions to today's value.
    A future PR that introduces a new cross-file collision (or
    consolidates an existing one) must update this allowlist
    accordingly.

    This is a *drift detector*, not a quality bar — the current
    allowlist includes legitimate per-module helpers like
    ``_now_iso`` / ``_utc_now_iso`` that are intentionally
    duplicated across modules. The pin's job is to surface NEW
    collisions for operator review."""

    def test_cross_file_name_collisions_match_allowlist(self):
        name_to_files: dict[str, list[str]] = {}
        for path in _iter_repo_root_python_files():
            try:
                # ``utf-8-sig`` strips a leading BOM if present so the
                # AST parser doesn't choke on it (one pre-existing
                # repo-root file ships with a BOM).
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError as exc:
                self.fail(f"{path.name} did not parse: {exc}")
            for name in _module_level_function_names(tree).keys():
                name_to_files.setdefault(name, []).append(path.name)
        current_collisions = frozenset(
            name for name, files in name_to_files.items()
            if len(files) > 1
        )

        new_collisions = current_collisions - _KNOWN_CROSS_FILE_NAME_COLLISIONS
        gone_collisions = _KNOWN_CROSS_FILE_NAME_COLLISIONS - current_collisions

        messages: list[str] = []
        if new_collisions:
            details = []
            for name in sorted(new_collisions):
                files = sorted(name_to_files[name])
                details.append(f"{name} (files: {', '.join(files)})")
            messages.append(
                "NEW cross-file name collision(s) introduced — add to "
                "the allowlist if intentional, OR consolidate to a "
                "single source:\n  " + "\n  ".join(details)
            )
        if gone_collisions:
            messages.append(
                "PREVIOUSLY-collided name(s) no longer collide — these "
                "appear to have been consolidated. Remove from the "
                "allowlist:\n  " + "\n  ".join(sorted(gone_collisions))
            )
        if messages:
            self.fail("\n\n".join(messages))


# ---------------------------------------------------------------------------
# 6: CASE B body-equivalence runtime pin
# ---------------------------------------------------------------------------


class Case2BodyEquivalencePin(unittest.TestCase):
    """Although CASE B's two functions are NOT a literal duplicate
    (different names + different signatures — see Case2NotADuplicatePin),
    their function bodies after the early-return are byte-identical
    per the audit doc. This pin asserts the runtime behavior matches:
    given the same effective ``official_mismatch`` truth value, both
    functions return byte-identical dicts.

    Guards against future drift where someone edits the body of one
    function without updating the other — exactly the
    drift-via-duplication failure mode the audit was worried about."""

    def _quality_summary_with_mismatch(self) -> dict:
        return {
            "strong": 2,
            "medium": 3,
            "weak": 4,
            "average_evidence_quality_score": 72,
            "evidence_quality_overall_label": "medium",
        }

    def test_official_adjusted_functions_produce_equivalent_output(self):
        import verification_card
        import pipeline_debug

        # --- Case 1: official_mismatch=True → both should apply the
        # downgrade.
        qs_input_a = self._quality_summary_with_mismatch()
        qs_input_b = self._quality_summary_with_mismatch()
        vc_out = verification_card._official_adjusted_evidence_quality(
            qs_input_a, {"official_mismatch": True},
        )
        pd_out = pipeline_debug._official_adjusted_quality_summary(
            qs_input_b, True,
        )
        self.assertEqual(
            vc_out, pd_out,
            "Body drift detected — when official_mismatch=True the "
            "two functions must produce byte-identical output. "
            "verification_card → " + repr(vc_out) + "; "
            "pipeline_debug → " + repr(pd_out),
        )

        # --- Case 2: official_mismatch=False → both should pass the
        # input through unchanged (just a dict copy).
        qs_input_c = self._quality_summary_with_mismatch()
        qs_input_d = self._quality_summary_with_mismatch()
        vc_pass = verification_card._official_adjusted_evidence_quality(
            qs_input_c, {"official_mismatch": False},
        )
        pd_pass = pipeline_debug._official_adjusted_quality_summary(
            qs_input_d, False,
        )
        self.assertEqual(
            vc_pass, pd_pass,
            "Body drift detected — when official_mismatch=False the "
            "two functions must produce byte-identical output. "
            "verification_card → " + repr(vc_pass) + "; "
            "pipeline_debug → " + repr(pd_pass),
        )
        # And the passthrough should equal the input (dict-copy
        # contract).
        self.assertEqual(vc_pass, self._quality_summary_with_mismatch())
        self.assertEqual(pd_pass, self._quality_summary_with_mismatch())


if __name__ == "__main__":
    unittest.main()
