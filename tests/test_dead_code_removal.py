"""M11.5 — pins for the audit-identified dead-code removals.

Per claude_audit_phase1.md §1.5 #5, four dead paths were identified.
This suite pins the three that diagnosed SAFE:

  Item 1: evidence_comparator._make_summary duplicate
          "excluded_non_policy_page" branch (was lines 301-315).
  Item 2: evidence_extraction_agent.extract_evidence_snippets
          double-built claim_evidence_map (loop accumulation that was
          immediately overwritten by the post-loop rebuild).
  Item 3: frontend renderResultsLegacy + buildReportTextLegacy — both
          replaced by renderResults / buildReportText long ago and never
          called.

The fourth item (source_retrieval_agent.OFFICIAL_DOMAIN_QUERY_HINTS) is
DEFERRED — see docs/DEAD_CODE_REMOVAL.md. It is read by reachable code
even though the audit asserts no Google query is ever issued with the
site: operators it produces.

Each item gets:
  (a) a static "defined exactly once" / "removed entirely" assertion
      sourced from the file directly (resilient to line drift), and
  (b) a behavioral pin that exercises the surviving path so any future
      regression in the kept code lights up here.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Item 1 — evidence_comparator._make_summary "excluded_non_policy_page"
# branch must appear exactly once.
# ---------------------------------------------------------------------------


class DeadCodeRemoval_EvidenceComparatorTests(unittest.TestCase):
    def setUp(self):
        self.source_path = _PROJECT_ROOT / "evidence_comparator.py"
        self.source = self.source_path.read_text(encoding="utf-8")

    def test_excluded_branch_defined_exactly_once(self):
        """The duplicate `if verification_level == "excluded_non_policy_page":`
        block inside `_make_summary` was deleted in M11.5. Counting
        substring occurrences inside the function body must be 1."""
        tree = ast.parse(self.source)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_make_summary":
                target = node
                break
        self.assertIsNotNone(
            target, "_make_summary not found in evidence_comparator.py"
        )
        body_src = ast.get_source_segment(self.source, target)
        self.assertIsNotNone(body_src)
        occurrences = body_src.count(
            'verification_level == "excluded_non_policy_page"'
        )
        self.assertEqual(
            occurrences, 1,
            "Exactly one `excluded_non_policy_page` branch must remain "
            f"inside _make_summary; found {occurrences}.",
        )

    def test_surviving_branch_produces_has_detail_url_message(self):
        """When the excluded results contain a detail-page URL, the
        surviving branch returns the '상세 공식문서는 찾았지만…' message.
        The deleted dead branch never produced this output, so this
        confirms the kept branch is the canonical one."""
        from evidence_comparator import _make_summary

        excluded_with_detail = [
            {
                "should_exclude_from_verification": True,
                "evidence_grade": "D",
                "document_type": "non_policy_page",
                "classification_reasons": ["off-topic detail"],
                "selected_document_url": "https://example.go.kr/detail/1",
                "is_detail_page": True,
            }
        ]
        out = _make_summary(
            status="official_evidence_missing",
            support_score=0,
            semantic_support_score=0,
            matched_keywords=[],
            semantic_matched_concepts=[],
            verification_level="excluded_non_policy_page",
            official_evidence_results=excluded_with_detail,
        )
        self.assertIn("상세 공식문서는 찾았지만", out)
        self.assertIn("non_policy_page: off-topic detail", out)

    def test_surviving_branch_produces_collected_excluded_message(self):
        """When no detail-page URL is present, the surviving branch
        returns the '수집된 공식 페이지가…' message. This is the same
        text the deleted duplicate produced — proving the survivor
        covers both sub-cases."""
        from evidence_comparator import _make_summary

        excluded_no_detail = [
            {
                "should_exclude_from_verification": True,
                "evidence_grade": "E",
                "document_type": "list_page",
                "classification_reasons": ["index page only"],
            }
        ]
        out = _make_summary(
            status="official_evidence_missing",
            support_score=0,
            semantic_support_score=0,
            matched_keywords=[],
            semantic_matched_concepts=[],
            verification_level="excluded_non_policy_page",
            official_evidence_results=excluded_no_detail,
        )
        self.assertIn("수집된 공식 페이지가 검증 대상에서 제외됐습니다", out)
        self.assertIn("list_page: index page only", out)


# ---------------------------------------------------------------------------
# Item 2 — extract_evidence_snippets must build claim_evidence_map
# exactly once (the post-loop rebuild from sorted snippets).
# ---------------------------------------------------------------------------


class DeadCodeRemoval_EvidenceExtractionAgentTests(unittest.TestCase):
    def setUp(self):
        self.source_path = _PROJECT_ROOT / "evidence_extraction_agent.py"
        self.source = self.source_path.read_text(encoding="utf-8")

    def test_claim_evidence_map_assigned_exactly_once_in_extractor(self):
        """The function used to set ``claim_evidence_map = {}`` once
        before the loop and again after the loop. Both pre-loop init
        and the in-loop per-index write were deleted in M11.5. Only
        the post-loop reset + rebuild remain — a single bare
        ``claim_evidence_map = {}`` assignment."""
        tree = ast.parse(self.source)
        target = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "extract_evidence_snippets"
            ):
                target = node
                break
        self.assertIsNotNone(target)
        bare_assigns = 0
        subscript_writes = 0
        for child in ast.walk(target):
            if not isinstance(child, ast.Assign):
                continue
            for tgt in child.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == "claim_evidence_map"
                ):
                    bare_assigns += 1
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "claim_evidence_map"
                ):
                    subscript_writes += 1
        self.assertEqual(
            bare_assigns, 1,
            "claim_evidence_map must be reassigned exactly once inside "
            f"extract_evidence_snippets; got {bare_assigns}.",
        )
        self.assertEqual(
            subscript_writes, 0,
            "The in-loop ``claim_evidence_map[str(index)] = ...`` write "
            "was removed in M11.5 because the post-loop rebuild "
            "overwrites it. Re-introducing it makes the loop assignment "
            "dead again.",
        )

    def test_extractor_returns_consistent_claim_evidence_map(self):
        """End-to-end behavioral pin. The claim_evidence_map returned by
        the function must agree with evidence_snippets: every snippet's
        evidence_id is grouped under its str(claim_index), and the set
        of (claim_index -> evidence_ids) reconstructs identically."""
        from evidence_extraction_agent import extract_evidence_snippets

        article_body = (
            "정부는 청년 버팀목 전세대출 한도를 1억 5천만 원으로 확대한다고 발표했다. "
            "국토부는 시행 시기를 다음 분기 첫째 주로 잡았다."
        )
        normalized_claims = [
            {
                "claim_text": "청년 버팀목 전세대출 한도를 1억 5천만 원으로 확대한다",
                "actor": "국토부",
                "action": "확대",
                "target": "청년 버팀목",
                "object": "전세대출 한도",
                "quantity": "1억 5천만 원",
                "date_or_time": "다음 분기 첫째 주",
                "location": "",
            },
        ]
        source_candidates = [
            {
                "source_id": "official_sample_1",
                "claim_index": 0,
                "title": "버팀목 전세대출 한도 확대 공식 보도자료",
                "url": "https://molit.go.kr/press/1",
                "publisher": "국토교통부",
                "source_type": "official_government",
                "raw_text_available": True,
                "official_body_text": (
                    "국토교통부는 청년 버팀목 전세대출 한도를 1억 5천만 원으로 "
                    "확대한다고 밝혔다. 다음 분기부터 시행된다."
                ),
                "official_evidence_score": 80,
                "official_body_match": True,
            },
        ]
        out = extract_evidence_snippets(
            normalized_claims=normalized_claims,
            source_candidates=source_candidates,
            article_body=article_body,
        )
        snippets = out["evidence_snippets"]
        emap = out["claim_evidence_map"]

        # Map must be keyed by str(claim_index), with each entry equal
        # to the list of evidence_ids whose claim_index matches.
        rebuilt = {}
        for snip in snippets:
            rebuilt.setdefault(str(snip["claim_index"]), []).append(
                snip["evidence_id"]
            )
        self.assertEqual(
            emap, rebuilt,
            "claim_evidence_map must be derivable purely from the sorted "
            "evidence_snippets list — that is the M11.5 invariant.",
        )
        # And the loop is non-empty for our fixture.
        self.assertIn("0", emap)
        self.assertGreater(len(emap["0"]), 0)


# ---------------------------------------------------------------------------
# Item 3 — frontend legacy functions must be absent from both the
# source file (main.js) and the built artifact (web/index.html), while
# the live replacements must still be present.
# ---------------------------------------------------------------------------


class DeadCodeRemoval_FrontendLegacyTests(unittest.TestCase):
    LEGACY_NAMES = ("buildReportTextLegacy", "renderResultsLegacy")
    LIVE_NAMES = ("buildReportText", "renderResults")

    def setUp(self):
        self.main_js = (_PROJECT_ROOT / "frontend" / "scripts" / "main.js").read_bytes()
        self.served_html = (_PROJECT_ROOT / "web" / "index.html").read_bytes()

    def test_legacy_names_absent_from_main_js(self):
        for name in self.LEGACY_NAMES:
            needle = f"function {name}".encode("utf-8")
            self.assertNotIn(
                needle, self.main_js,
                f"M11.5 removed {name} from frontend/scripts/main.js but "
                "the function definition is still present.",
            )

    def test_legacy_names_absent_from_built_html(self):
        for name in self.LEGACY_NAMES:
            needle = f"function {name}".encode("utf-8")
            self.assertNotIn(
                needle, self.served_html,
                f"web/index.html still contains {name} — rebuild "
                "via `python frontend/build_index.py`.",
            )

    def test_live_replacements_still_defined_in_main_js(self):
        for name in self.LIVE_NAMES:
            needle = f"function {name}".encode("utf-8")
            self.assertIn(
                needle, self.main_js,
                f"Live replacement {name} missing from main.js — the "
                "M11.5 deletion overshot.",
            )

    def test_live_replacements_still_defined_in_built_html(self):
        for name in self.LIVE_NAMES:
            needle = f"function {name}".encode("utf-8")
            self.assertIn(
                needle, self.served_html,
                f"Live replacement {name} missing from web/index.html.",
            )

    def test_live_replacements_still_called(self):
        """Pin that the live functions are referenced (not just defined).
        Each must have at least one call site outside its own definition.
        """
        for name in self.LIVE_NAMES:
            def_needle = f"function {name}".encode("utf-8")
            call_count = self.main_js.count(f"{name}(".encode("utf-8"))
            # Subtract the single definition occurrence.
            def_count = self.main_js.count(def_needle)
            callers = call_count - def_count
            self.assertGreater(
                callers, 0,
                f"{name} has {call_count} occurrences of `{name}(` and "
                f"{def_count} `function {name}` — no remaining callers. "
                "The live function may itself now be dead.",
            )


# ---------------------------------------------------------------------------
# Item 4 — DEFERRED. OFFICIAL_DOMAIN_QUERY_HINTS was flagged by the
# audit as dead data, but grep shows it is read by _official_site_query
# and the produced strings are returned to API consumers / persisted in
# the verification card. M11.5 leaves it in place pending a deeper
# investigation. This pin documents the deferral so a future cleanup
# can find context.
# ---------------------------------------------------------------------------


class DeadCodeRemoval_SourceRetrievalAgentTests(unittest.TestCase):
    def setUp(self):
        from source_retrieval_agent import OFFICIAL_DOMAIN_QUERY_HINTS  # noqa: F401
        self.module_source = (
            _PROJECT_ROOT / "source_retrieval_agent.py"
        ).read_text(encoding="utf-8")

    def test_constant_still_present_and_read_by_helper(self):
        """The constant remains and is read by _official_site_query.
        See docs/DEAD_CODE_REMOVAL.md for why this item was deferred —
        the audit's claim is about Google-issuance, not reachability,
        and the strings are user-visible in source_queries output."""
        self.assertIn("OFFICIAL_DOMAIN_QUERY_HINTS = {", self.module_source)
        self.assertIn(
            "for keyword, site in OFFICIAL_DOMAIN_QUERY_HINTS.items():",
            self.module_source,
            "If the helper read site changes, re-open the M11.5 "
            "deferral analysis — the constant may now be removable.",
        )

    def test_helper_emits_site_prefixed_query_for_known_institution(self):
        """Behavioral pin on _official_site_query: when a known
        institution token appears in the context_text, the produced
        query begins with the matching site: operator. This is what
        the audit flagged as 'output never reaches Google' — but it
        IS produced and serialized."""
        from source_retrieval_agent import _official_site_query

        out = _official_site_query("전세대출 한도", "국토부 보도자료 전세대출 확대")
        self.assertTrue(
            out.startswith("site:molit.go.kr "),
            f"Expected molit.go.kr site: prefix; got {out!r}.",
        )


if __name__ == "__main__":
    unittest.main()
