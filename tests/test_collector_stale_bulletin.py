# STALE-BULLETIN (104-APPLY) — collection-time rule: a periodic-family
# bulletin title whose head names a year+month that ended more than
# STALE_BULLETIN_DAYS (90) before collection is not stored as a claim.
# Reuses the matcher's OFFICIAL_PERIODIC_FAMILY_RE + parse_korean_period_tokens
# (one implementation). Specimens are REAL stored titles; `today` is the row's
# real collection date where one is cited.
import datetime
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import news_collector  # noqa: E402
import official_evidence_resolution  # noqa: E402
from news_collector import (  # noqa: E402
    STALE_BULLETIN_DAYS,
    _reject_title_reason,
    _stale_periodic_bulletin_period,
)

TODAY = datetime.date(2026, 8, 22)


class SharedImplementationTests(unittest.TestCase):
    def test_reuses_matcher_primitives(self):
        self.assertIs(news_collector.parse_korean_period_tokens,
                      official_evidence_resolution.parse_korean_period_tokens)
        self.assertIs(news_collector.OFFICIAL_PERIODIC_FAMILY_RE,
                      official_evidence_resolution.OFFICIAL_PERIODIC_FAMILY_RE)
        self.assertEqual(STALE_BULLETIN_DAYS, 90)


class PeriodicFamilyTests(unittest.TestCase):
    def test_donghyang_archive_edition(self):
        # id 16196, collected 2026-08-21 — an eleven-year-old bulletin
        self.assertEqual(_stale_periodic_bulletin_period(
            "2015년 11월 고용동향 - 서울Pn", datetime.date(2026, 8, 21)), (2015, 11))

    def test_population_donghyang_archive_edition(self):
        # id 16266, collected 2026-08-22
        self.assertEqual(_stale_periodic_bulletin_period(
            "2015년 10월 인구동향(출생, 사망, 혼인, 이혼) - 서울Pn", TODAY), (2015, 10))

    def test_jisu_family(self):
        self.assertEqual(_stale_periodic_bulletin_period(
            "2024년 3월 소비자물가지수 동향 - 서울Pn", TODAY), (2024, 3))

    def test_byeondongryul_family(self):
        self.assertEqual(_stale_periodic_bulletin_period(
            "2023년 7월 주택가격변동률 발표", TODAY), (2023, 7))

    def test_leading_tag_is_skipped(self):
        # id 14102, collected 2026-07-26 — (참고) tag then the period
        self.assertEqual(_stale_periodic_bulletin_period(
            "(참고) 2026년 3월 고용동향 및 평가 - 서울Pn", datetime.date(2026, 7, 26)), (2026, 3))

    def test_non_periodic_title_with_old_year_passes(self):
        self.assertIsNone(_stale_periodic_bulletin_period(
            "2021년 11월 국무회의 의결 사항 안내", TODAY))


class BoundaryTests(unittest.TestCase):
    # Period 2026년 5월 ends at 2026-06-01 (exclusive bound).
    TITLE = "2026년 5월 인구동향(출생, 사망, 혼인, 이혼) - 서울Pn"

    def test_exactly_ninety_days_passes(self):
        self.assertIsNone(_stale_periodic_bulletin_period(
            self.TITLE, datetime.date(2026, 6, 1) + datetime.timedelta(days=90)))

    def test_ninety_one_days_is_stale(self):
        self.assertEqual(_stale_periodic_bulletin_period(
            self.TITLE, datetime.date(2026, 6, 1) + datetime.timedelta(days=91)), (2026, 5))

    def test_december_edition_rolls_year(self):
        # 2025년 12월 ends 2026-01-01; 90 days later is 2026-04-01.
        self.assertIsNone(_stale_periodic_bulletin_period(
            "2025년 12월 고용동향", datetime.date(2026, 4, 1)))
        self.assertEqual(_stale_periodic_bulletin_period(
            "2025년 12월 고용동향", datetime.date(2026, 4, 2)), (2025, 12))


class MustSurviveTests(unittest.TestCase):
    def test_retrospective_analysis_head_shape(self):
        # The shape from the ruling: old period at the head, followed by 이후.
        title = "2021년 6월 이후 고용동향 분석…취업자 증가폭 둔화"
        self.assertIsNone(_stale_periodic_bulletin_period(title, TODAY))
        self.assertNotEqual(_reject_title_reason(title), "stale_periodic_bulletin")

    def test_retrospective_mid_title_real_rows(self):
        # ids 14558 / 3807 — old periods named mid-title
        for title in (
            "국가데이터처, 6월 산업활동동향 발표...2020년 6월 이후 최대폭 증 - 증권일보",
            "‘실업률+물가상승률’ 경제고통지수, 2011년 이후 최고 수준",
        ):
            self.assertIsNone(_stale_periodic_bulletin_period(title, TODAY), title)

    def test_current_release_same_outlet_passes(self):
        # id 15115 — 2026년 5월 인구동향 collected 2026-08-07, ~16 days after
        # KOSTAT released it (인구동향 lags its month by ~55 days). This is the
        # row that a 60-day window rejected.
        title = "2026년 5월 인구동향(출생, 사망, 혼인, 이혼) - 서울Pn"
        self.assertIsNone(_stale_periodic_bulletin_period(title, datetime.date(2026, 8, 7)))

    def test_current_release_other_outlets_pass(self):
        # ids 15515 / 13094 — fresh monthly releases
        self.assertIsNone(_stale_periodic_bulletin_period(
            "2026년 7월 고용동향 발표…취업자 10만 8천 명 증가·실업률 2.6% - BBS불교방송",
            datetime.date(2026, 8, 12)))
        self.assertIsNone(_stale_periodic_bulletin_period(
            "2026년 6월 고용동향 - 대한민국 정책브리핑", datetime.date(2026, 7, 18)))

    def test_quarter_and_half_year_shapes_exempt(self):
        # ids 13097 / 5387 — year-only periods; publication lag runs months
        self.assertIsNone(_stale_periodic_bulletin_period(
            "2025년 4/4분기 가계동향조사 결과(2025년 연간지출 포함) - 대한민국 정책브리핑",
            datetime.date(2026, 7, 18)))
        self.assertIsNone(_stale_periodic_bulletin_period(
            "속초시, 2025년 하반기 경제동향 발표…수산·관광 회복세 뚜렷", datetime.date(2026, 7, 8)))


class WiringTests(unittest.TestCase):
    def test_reject_reason_is_wired(self):
        self.assertEqual(_reject_title_reason("2015년 11월 고용동향 - 서울Pn"),
                         "stale_periodic_bulletin")

    def test_primary_pool_drops_the_reason(self):
        # Both google_rss primary-pool filters name the reason explicitly.
        src = Path(news_collector.__file__).read_text(encoding="utf-8")
        needle = ('not in ("opinion_or_column", "political_subject", '
                  '"stale_periodic_bulletin")')
        self.assertEqual(src.count(needle), 2)

    def test_earlier_tiers_keep_precedence(self):
        # An opinion column about an old bulletin is still opinion_or_column.
        self.assertEqual(_reject_title_reason("[칼럼] 2015년 11월 고용동향이 남긴 것"),
                         "opinion_or_column")


if __name__ == "__main__":
    unittest.main()
