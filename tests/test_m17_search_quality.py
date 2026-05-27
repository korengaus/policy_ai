"""M17-search-quality — _candidate_score query-overlap scoring tests.

Phase 1 diagnosis identified that ``_candidate_score`` (news_collector.py)
unconditionally added +25 to titles containing the hard-coded keyword set
{대출, 금리, 부동산, 정책, 규제, 지원, 전세, 주택}, biasing the forced-fallback
selection toward 전세대출 articles regardless of the user's actual query.
Phase 2 replaced that with query-token overlap: the bonus only fires
when the article title shares at least one token (length >= 2) with
the lowercased user query.

These tests pin the new contract:

* The hard-coded housing bonus is gone (no bonus when query is None).
* The overlap bonus is +25 for one match, +35 for two-plus matches.
* Short tokens (Korean particles like '의', '를', '이') are filtered.
* ``_force_select_best`` returns the query-relevant candidate over the
  housing-biased candidate when a query is supplied.

No network calls; no mocking — the helpers are pure functions on dicts.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import news_collector  # noqa: E402


def _candidate(title: str, url: str = "https://example.com/article/1") -> dict:
    """Minimal news-item shape consumed by ``_candidate_score`` /
    ``_force_select_best``. Only the fields the scorer reads are
    populated."""
    return {
        "title": title,
        "original_url": url,
        "google_link": url,
        "link": url,
    }


class QueryTokensForScoringTests(unittest.TestCase):
    """``_query_tokens_for_scoring`` normalization + length filter."""

    def test_query_tokens_filters_short_tokens(self):
        # "내" is length 1 → filtered. "정책" is length 2 → kept.
        tokens = news_collector._query_tokens_for_scoring("내 정책")
        self.assertEqual(tokens, {"정책"})

    def test_query_tokens_empty_for_blank_query(self):
        self.assertEqual(news_collector._query_tokens_for_scoring(""), set())
        self.assertEqual(news_collector._query_tokens_for_scoring(None), set())  # type: ignore[arg-type]

    def test_query_tokens_lowercases_and_collapses_whitespace(self):
        tokens = news_collector._query_tokens_for_scoring("  Climate    Policy  ")
        self.assertEqual(tokens, {"climate", "policy"})


class CandidateScoreOverlapTests(unittest.TestCase):
    """``_candidate_score`` query-overlap bonus contract."""

    def test_candidate_score_no_query_no_housing_bias(self):
        """Without a query the old +25 housing keyword bonus must NOT
        fire — that bonus is the H2 bug being fixed. A 전세대출 title
        and a generic climate title with the same length should score
        identically (modulo the URL/sentence-shape bonuses)."""
        housing_score = news_collector._candidate_score(
            _candidate("청년 버팀목 전세대출 2년새 반토막")
        )
        neutral_score = news_collector._candidate_score(
            _candidate("기후변화 대응 정책 발표 환경부 보도자료")
        )
        # Without the housing bonus the two titles score within the
        # length-bonus band (both >= 15 chars, both Korean, both
        # sentence-shaped). The exact equality is too brittle, but the
        # housing title must NOT outscore the climate title.
        self.assertLessEqual(housing_score, neutral_score + 5)

    def test_candidate_score_query_overlap_bonus(self):
        """Same title, different queries: the query whose tokens
        overlap with the title gets +25; the query whose tokens
        don't overlap gets nothing. Keeping the title fixed isolates
        the overlap bonus from length / Korean-shape bonuses."""
        title = "기후변화 대응 정책 환경부 보도자료 발표 오늘"
        with_overlap = news_collector._candidate_score(
            _candidate(title), query="기후변화",
        )
        without_overlap = news_collector._candidate_score(
            _candidate(title), query="최저임금",
        )
        self.assertEqual(with_overlap - without_overlap, 25)

    def test_candidate_score_korean_query_overlap_bonus(self):
        """Same contract with Korean tokens: '기후변화' in title gets
        +25; a 전세대출 title without the token gets nothing."""
        climate = news_collector._candidate_score(
            _candidate("기후변화 대응 정책 환경부 보도자료 발표"),
            query="기후변화 정책",
        )
        housing = news_collector._candidate_score(
            _candidate("청년 버팀목 전세대출 2년새 반토막 - 아시아투데이"),
            query="기후변화 정책",
        )
        self.assertGreater(climate, housing)

    def test_candidate_score_two_token_overlap_extra_bonus(self):
        """Same title, two queries with different overlap counts:
        one-token-match adds +25; two-token-match adds +35 (an extra
        +10 over the single-match case). Holding the title fixed
        isolates the bonus from other scoring factors."""
        title = "기후변화 정책 발표 환경부 보도자료 오늘"
        one_match = news_collector._candidate_score(
            _candidate(title), query="기후변화",
        )
        two_match = news_collector._candidate_score(
            _candidate(title), query="기후변화 정책",
        )
        self.assertEqual(two_match - one_match, 10)


class ForceSelectBestQueryRelevanceTests(unittest.TestCase):
    """``_force_select_best`` integration — the user-visible contract."""

    def test_force_select_best_prefers_query_relevant_article(self):
        """Mixed candidates, one matching the user's climate query, one
        matching the historical housing bias. With query='기후변화', the
        climate article wins."""
        candidates = [
            _candidate(
                "청년 버팀목 전세대출 2년새 반토막 보도 - 아시아투데이",
                url="https://news.example.com/housing/1",
            ),
            _candidate(
                "기후변화 대응 정책 발표 환경부 보도자료 오늘",
                url="https://news.example.com/climate/1",
            ),
        ]
        selected = news_collector._force_select_best(
            candidates, source="google_rss", query="기후변화 정책",
        )
        self.assertEqual(len(selected), 1)
        self.assertIn("기후변화", selected[0]["title"])

    def test_force_select_best_returns_no_housing_bias_when_query_unrelated(self):
        """Query '최저임금' against mixed candidates including a 전세대출
        title — the 전세대출 title must NOT be selected purely because
        of its housing keywords."""
        candidates = [
            _candidate(
                "금융당국 규제지역 내 1주택자 전세대출 파악 나섰다 - 한국경제",
                url="https://news.example.com/housing/2",
            ),
            _candidate(
                "최저임금위원회 내년 인상안 논의 본격화 - 연합뉴스",
                url="https://news.example.com/wage/1",
            ),
        ]
        selected = news_collector._force_select_best(
            candidates, source="google_rss", query="최저임금 인상",
        )
        self.assertEqual(len(selected), 1)
        self.assertNotIn("전세대출", selected[0]["title"])


if __name__ == "__main__":
    unittest.main()
