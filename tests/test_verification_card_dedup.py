"""Phase 2 M11.4b — dead-duplicate removal pin tests.

The first definition of ``verification_card._missing_context_specific``
(L491 before M11.4b) was shadowed by a second definition (L530 before
M11.4b) and never executed. M11.4b deleted the dead L491 copy. These
tests pin:

    1. Uniqueness: ``def _missing_context_specific`` appears exactly
       once in the source.
    2. Signature is preserved (the L530 version's signature).
    3. URL-acceptance behaviour is the L530 strict variant
       (only ``selected_document_url`` counts as a valid detail URL).
    4. The longer, comma-rich Korean user-facing strings are preserved.
    5. Smoke import: every caller of the function still imports cleanly.

Production behaviour is byte-identical to today — Python was already
running the L530 version. These tests only catch future regressions
that would re-introduce the divergence (e.g., somebody copy-pasting
the function back in or relaxing the URL check).
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import verification_card  # noqa: E402


_SOURCE_PATH = _PROJECT_ROOT / "verification_card.py"
_DEF_PATTERN = re.compile(r"^def _missing_context_specific\(", re.MULTILINE)


# ---------------------------------------------------------------------------
# 1. Uniqueness pin
# ---------------------------------------------------------------------------


class UniquenessTests(unittest.TestCase):
    def test_missing_context_specific_defined_exactly_once(self):
        source = _SOURCE_PATH.read_text(encoding="utf-8")
        matches = _DEF_PATTERN.findall(source)
        self.assertEqual(
            len(matches), 1,
            "verification_card._missing_context_specific must be defined "
            f"exactly once; found {len(matches)} definitions. Did the "
            "M11.4b dedup get reverted?",
        )


# ---------------------------------------------------------------------------
# 2. Signature pin
# ---------------------------------------------------------------------------


class SignatureTests(unittest.TestCase):
    def test_signature_matches_l530_pin(self):
        sig = inspect.signature(verification_card._missing_context_specific)
        param_names = list(sig.parameters.keys())
        self.assertEqual(
            param_names,
            ["official_sources", "evidence_comparison", "official_evidence_results"],
        )

    def test_returns_list(self):
        result = verification_card._missing_context_specific(
            official_sources=[],
            evidence_comparison={},
            official_evidence_results=[],
        )
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)


# ---------------------------------------------------------------------------
# 3. URL-acceptance behaviour — L530 strict variant
# ---------------------------------------------------------------------------


class StrictUrlAcceptanceTests(unittest.TestCase):
    """L530 accepts ONLY ``selected_document_url`` as evidence of a
    usable detail URL. ``official_search_url`` / ``search_url`` do
    NOT satisfy the check (they did in the dead L491 version)."""

    def test_no_selected_document_url_falls_back_to_no_url_message(self):
        result = verification_card._missing_context_specific(
            official_sources=[],
            evidence_comparison={},
            official_evidence_results=[
                {
                    "selected_document_url": "",
                    "official_search_url": "https://example.go.kr/search",
                    "search_url": "https://example.go.kr/search?q=a",
                    "document_text_length": 0,
                    "document_text_snippet": "",
                    "error": None,
                }
            ],
        )
        # L530 must report "no detail URL available" — search URLs do
        # NOT count. The Korean message variant is the L530 phrasing.
        joined = " ".join(result)
        self.assertIn(
            "확인 가능한 상세 문서 URL이 부족합니다", joined,
            f"Expected L530 'no detail URL' message; got {result!r}",
        )

    def test_with_selected_document_url_no_url_message_absent(self):
        result = verification_card._missing_context_specific(
            official_sources=[],
            evidence_comparison={},
            official_evidence_results=[
                {
                    "selected_document_url": "https://example.go.kr/detail/1",
                    "document_text_length": 0,
                    "document_text_snippet": "",
                    "error": None,
                }
            ],
        )
        joined = " ".join(result)
        # When a detail URL exists, the "URL부족" message must NOT appear.
        self.assertNotIn("확인 가능한 상세 문서 URL이 부족합니다", joined)

    def test_url_present_but_body_short_returns_not_collected_message(self):
        result = verification_card._missing_context_specific(
            official_sources=[],
            evidence_comparison={},
            official_evidence_results=[
                {
                    "selected_document_url": "https://example.go.kr/detail/2",
                    "document_text_length": 50,    # < 300
                    "document_text_snippet": "짧은 본문",
                    "error": None,
                }
            ],
        )
        joined = " ".join(result)
        self.assertIn(
            "실제 본문 또는 상세 문서 본문은 아직 수집되지 않았습니다",
            joined,
        )


# ---------------------------------------------------------------------------
# 4. Korean message pin — the longer, comma-rich L530 phrasings
# ---------------------------------------------------------------------------


class KoreanMessagePinTests(unittest.TestCase):
    """Pin the L530 user-facing strings so a future edit that
    accidentally reverts to L491's terser phrasing is caught."""

    def test_weak_official_match_message_pin(self):
        result = verification_card._missing_context_specific(
            official_sources=[{"url": "https://example.go.kr/a"}],
            evidence_comparison={"verification_level": "weak_official_match"},
            official_evidence_results=[],
        )
        joined = " ".join(result)
        self.assertIn(
            "공식 출처가 기사 내용과 직접 일치하지 않아 추가 확인이 필요합니다",
            joined,
        )

    def test_excluded_non_policy_page_message_pin(self):
        result = verification_card._missing_context_specific(
            official_sources=[{"url": "https://example.go.kr/a"}],
            evidence_comparison={"verification_level": "excluded_non_policy_page"},
            official_evidence_results=[],
        )
        joined = " ".join(result)
        self.assertIn(
            "수집된 공식 문서가 목록, 안내, 민원 문서로 분류되어 검증 근거에서 제외했습니다",
            joined,
        )

    def test_default_fallback_message_pin(self):
        # A path where none of the conditional checks trigger — the
        # function falls back to its default "review before publish"
        # message (L530's longer variant).
        result = verification_card._missing_context_specific(
            official_sources=[{"url": "https://example.go.kr/a"}],
            evidence_comparison={"verification_level": "supported"},
            official_evidence_results=[],
        )
        joined = " ".join(result)
        self.assertIn(
            "최종 공개 전에는 원문과 공식 발표를 다시 확인하는 것이 좋습니다",
            joined,
        )


# ---------------------------------------------------------------------------
# 5. Smoke — every caller still imports cleanly
# ---------------------------------------------------------------------------


class CallerSmokeTests(unittest.TestCase):
    """Re-import verification_card (and any module that calls
    ``_missing_context_specific``) to confirm no ImportError /
    NameError / AttributeError surfaced after the dedup."""

    def test_verification_card_imports_clean(self):
        # Re-import via importlib.reload so the module load actually
        # re-executes the file (not just hands back the cached object).
        importlib.reload(verification_card)
        self.assertTrue(
            callable(verification_card._missing_context_specific),
            "_missing_context_specific is missing after reload",
        )

    def test_internal_caller_at_module_level_still_resolves(self):
        # The lone caller inside verification_card.py is the verification
        # card builder. Smoke-call the function the same way it does to
        # make sure no NameError surfaces.
        out = verification_card._missing_context_specific(
            official_sources=[],
            evidence_comparison={},
            official_evidence_results=[],
        )
        self.assertIsInstance(out, list)


if __name__ == "__main__":
    unittest.main()
