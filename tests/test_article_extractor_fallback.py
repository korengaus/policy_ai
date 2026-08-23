# SHORT-BODY FALLBACK (102-APPLY) — `_extract_candidate_text` used to let
# the BeautifulSoup container replace a clean trafilatura body under the
# 300-char floor purely because the container was longer, carrying the
# related-headline list into the stored text (id 15912). The rule now
# judges what the fallback ADDS. These tests pin both directions: the
# headline list must lose, a genuinely missing lead sentence must still win,
# and the empty/broken-trafilatura branch is untouched.
import sys
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import article_extractor  # noqa: E402
from article_extractor import _extract_candidate_text, _fallback_adds_body_text  # noqa: E402

_BODY_1 = ("강원 평창 대관령원예농협이 10일 본점에서 폭염 피해 예방을 위한 "
           "농업용 선풍기 전달식을 열고 조합원들에게 선풍기 500대를 지원했다.")
_BODY_2 = ("이번 지원은 고온 현상으로 인한 작물 생육 피해를 줄이고 농민들의 "
           "작업 환경을 개선하기 위해 이뤄졌다.")
_RELATED = [
    "도시 전체가 건축박물관, 세종에서 현대건축 진수 만나볼까",
    "[농촌의 벌이] “AI 대체 불가능에 초봉 월 300만원”…들녘 구원투수 ‘농기계정비사’",
    "농장서 실무 쌓고 공공기관 도전…“공무원 연봉·안정적 일터 만족”",
    "3개 태풍 동시 발생…주말 한반도 영향은",
]

# The reproducing shape (id 15912, nongmin.com): a short clean body and a
# related-headline <ul> inside the same article container.
_REPRO_HTML = (
    "<html><head><title>t</title></head><body><div class=\"article_view\">"
    "<h1>평창 대관령원예농협, 채소농가 선풍기 500대 전달</h1>"
    "<div class=\"date\">입력 : 2026-08-19 00:00</div>"
    f"<p>{_BODY_1}</p><p>{_BODY_2}</p>"
    "<ul class=\"related\">"
    + "".join(f"<li><a href=\"/{i}\">{h}</a></li>" for i, h in enumerate(_RELATED))
    + "</ul></div></body></html>"
)


class ShortBodyFallbackTests(unittest.TestCase):
    def test_reproducing_case_keeps_clean_trafilatura_body(self):
        # Real HTML through the real extractors: trafilatura yields the two
        # body sentences (<300 chars); the BS4 container is longer only
        # because of the headline list.
        trafilatura_text = article_extractor._extract_with_trafilatura_html(_REPRO_HTML)
        self.assertLess(len(trafilatura_text), 300)
        self.assertIn(_BODY_1, trafilatura_text)
        bs4_text = article_extractor._extract_with_beautifulsoup_html(_REPRO_HTML)
        self.assertGreater(len(bs4_text), len(trafilatura_text))
        self.assertIn(_RELATED[1], bs4_text)

        result = _extract_candidate_text(_REPRO_HTML, "utf-8")
        self.assertIn(_BODY_1, result)
        self.assertIn(_BODY_2, result)
        for headline in _RELATED:
            self.assertNotIn(headline, result)

    def test_fallback_that_adds_lead_sentence_still_wins(self):
        # id 14434 (ksilbo.co.kr): trafilatura dropped the lead paragraph;
        # the BS4 container restores it (plus an e-mail line). The fallback
        # is genuinely better and must keep winning.
        short = ("이번 후원은 시각장애인의 ESG 탄소중립 실천 및 정서안정을 위한 "
                 "헬시플레저 사업의 일환이다. 시각장애인들은 친환경 체험과 걷기 "
                 "활동 등을 통해 탄소중립의 중요성을 배울 예정이다. 정혜윤기자")
        lead = ("한전KPS(주)울산사업소는 30일 울산시시각장애인복지연합회를 방문해 "
                "시각장애인의 문화체험 사업 지원금 500만원을 전달했다.")
        fallback = lead + "\n" + short + "\nhy040430@ksilbo.co.kr"
        self.assertTrue(_fallback_adds_body_text(short, fallback))
        with mock.patch.object(article_extractor, "_extract_with_trafilatura_html",
                               return_value=short), \
             mock.patch.object(article_extractor, "_extract_with_beautifulsoup_html",
                               return_value=fallback):
            result = _extract_candidate_text("<html></html>", "utf-8")
        self.assertIn(lead, result)

    def test_fallback_that_adds_headline_and_ui_line_loses(self):
        # id 14839 (nnewss.com): the container adds the page headline, a
        # font-size notice and an e-mail — more fragment than sentence.
        short = ("박찬대 인천시장은 지난 7월 30일 인천형 공공간호장학생 장학증서 "
                 "수여식에서 장학증서를 전달하며 지원 의지를 밝혔다.")
        fallback = ("박찬대 인천시장 인천형 공공간호장학생 장학증서 수여… 지역 공공의료 인재 육성\n"
                    "기사의 본문 내용은 이 글자크기로 변경됩니다.\n"
                    + short + "\nnewnews1080@gmail.com")
        self.assertFalse(_fallback_adds_body_text(short, fallback))
        with mock.patch.object(article_extractor, "_extract_with_trafilatura_html",
                               return_value=short), \
             mock.patch.object(article_extractor, "_extract_with_beautifulsoup_html",
                               return_value=fallback):
            result = _extract_candidate_text("<html></html>", "utf-8")
        self.assertEqual(result, short)

    def test_empty_trafilatura_branch_unchanged(self):
        # newsmaker.or.kr shape: trafilatura returns nothing; the BS4 body
        # wins as before, headline-shaped lines or not.
        fallback = "\n".join(_RELATED)
        with mock.patch.object(article_extractor, "_extract_with_trafilatura_html",
                               return_value=""), \
             mock.patch.object(article_extractor, "_extract_with_beautifulsoup_html",
                               return_value=fallback):
            result = _extract_candidate_text("<html></html>", "utf-8")
        self.assertEqual(result, article_extractor.clean_extracted_text(fallback))

    def test_identical_texts_accepted(self):
        self.assertTrue(_fallback_adds_body_text(_BODY_1, _BODY_1))


if __name__ == "__main__":
    unittest.main()
