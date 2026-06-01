"""M19-source-reliability-2 Option A — relevance gate on official candidate
generation.

These tests pin the behaviour of the gate added in
``official_source_search.generate_official_source_candidates``: every catalog
institution must now produce at least one ``matched_reason`` (keyword / topic
/ topic-family hit) to be generated, EXCEPT the single always-include
cross-government fallback (Korea Policy Briefing / korea.kr).

Contract verified here:
  * Irrelevant central authorities (FSC / FSS / Police / Assembly) are NOT
    injected for an off-topic query (e.g. 고유가 지원금).
  * Genuinely relevant authorities still appear (MOLIT + HUG for 전세사기,
    FSC / FSS for 전세대출 규제).
  * A zero-match topic still yields the Korea Policy Briefing fallback, so
    candidate generation never returns an empty official set.
  * Output stays capped at max_candidates (5).

All tests are deterministic and offline — they call the real generator with
plain-text args (mirroring main.py:615-619) and never touch the network,
OpenAI, or any crawl.
"""

from __future__ import annotations

import unittest

from official_source_search import (
    ALWAYS_INCLUDE_SOURCE_NAMES,
    generate_official_source_candidates,
)


def _names(candidates: list[dict]) -> set[str]:
    return {candidate.get("source_name") or "" for candidate in candidates}


class OfficialCandidateRelevanceGateTests(unittest.TestCase):
    def test_irrelevant_financial_regulator_not_generated_for_fuel_query(self):
        # 고유가(high fuel price) regional subsidy has no overlap with the
        # FSC/FSS/Police/Assembly keyword lists, so the gate must drop them.
        candidates = generate_official_source_candidates(
            news_title="고유가 대응 지역 지원금 확대",
            core_policy_issue="정부가 고유가에 대응해 지역 주민에게 지원금을 확대 지급한다.",
            topic="energy_subsidy",
        )
        names = _names(candidates)
        for irrelevant in (
            "Financial Services Commission",
            "Financial Supervisory Service",
            "Korean National Police Agency",
            "National Assembly",
        ):
            self.assertNotIn(
                irrelevant,
                names,
                f"{irrelevant!r} should not be generated for an off-topic "
                f"고유가 query; got {sorted(names)}",
            )

    def test_relevant_molit_hug_generated_for_jeonse_fraud(self):
        candidates = generate_official_source_candidates(
            news_title="전세사기 피해자 지원 대책 발표",
            core_policy_issue="국토교통부와 주택도시보증공사가 전세사기 피해자 지원 대책을 발표했다.",
            topic="jeonse_fraud",
        )
        names = _names(candidates)
        self.assertIn(
            "Ministry of Land, Infrastructure and Transport",
            names,
            f"MOLIT should be generated for a 전세사기 query; got {sorted(names)}",
        )
        self.assertIn(
            "Korea Housing & Urban Guarantee Corporation",
            names,
            f"HUG should be generated for a 전세사기 query; got {sorted(names)}",
        )

    def test_relevant_fsc_generated_for_jeonse_loan(self):
        candidates = generate_official_source_candidates(
            news_title="전세대출 규제 강화",
            core_policy_issue="금융위원회가 전세대출 규제를 강화한다고 밝혔다.",
            topic="rental_loan_regulation",
        )
        names = _names(candidates)
        self.assertTrue(
            {"Financial Services Commission", "Financial Supervisory Service"}
            & names,
            f"FSC/FSS should be generated for a 전세대출 규제 query; got "
            f"{sorted(names)}",
        )

    def test_general_fallback_always_present(self):
        # A topic with no overlap against ANY catalog keyword must still
        # return the single always-include fallback rather than an empty set.
        candidates = generate_official_source_candidates(
            news_title="zzz qqq unrelated foreign topic xyz",
            core_policy_issue="completely unrelated content with no policy terms",
            topic="unrelated_topic",
        )
        names = _names(candidates)
        self.assertTrue(candidates, "candidate generation should never be empty")
        self.assertTrue(
            ALWAYS_INCLUDE_SOURCE_NAMES.issubset(names),
            f"always-include fallback {ALWAYS_INCLUDE_SOURCE_NAMES} must be "
            f"present on a zero-match topic; got {sorted(names)}",
        )

    def test_max_candidates_still_capped_at_5(self):
        # A query that matches many institutions must still be truncated.
        candidates = generate_official_source_candidates(
            news_title="전세대출 주택담보대출 금리 부동산 세제 양도세 전세사기 보증 정책",
            core_policy_issue=(
                "금융위원회 금융감독원 국토교통부 기획재정부 국세청 한국은행이 "
                "전세대출 주택담보대출 금리 부동산 양도세 전세사기 정책을 발표했다."
            ),
            topic="omnibus_policy",
        )
        self.assertLessEqual(len(candidates), 5)


if __name__ == "__main__":
    unittest.main()
