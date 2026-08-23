"""ARTICLE-CHROME (71-APPLY) — tests for claim_extractor's chrome stripping.

Pins each shipped pattern with a REAL specimen from the 1,300-row measurement
(ids referenced per case), and pins the false-positive cases: legitimate
look-alike claims that must pass through UNCHANGED. The rules only STRIP
matched chrome — no whole-sentence rejection — so a glued wire run keeps its
real claim (the id=15721 case) and pure chrome shrinks below the 18-char
floor. Offline: pure functions, no DB, no network.
"""

import inspect
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import claim_extractor  # noqa: E402
from claim_extractor import _strip_article_chrome, extract_verifiable_claims  # noqa: E402


class ChromeStripSpecimenTests(unittest.TestCase):
    """Each measured shape strips to the claim text alone."""

    def test_operator_specimen_edit_dateline_run(self):
        # id=15285 — the task's specimen: 편집-prefixed dateline with an inline
        # byline in one unbroken run. All five display-side patterns miss it.
        text = ("편집 2026.08.10 [23:13] 대전 동구, 16개 동 찾아가는 기초생활보장 "
                "순회교육…복지 현장 대응력 강화 기사입력 2026/08/10 [21:14] "
                "입력 : 2026/08/10 [21:14] (대전=뉴스충청인) 김수환 기자 = "
                "대전 동구가 복지서비스 최일선에서 주민을 만나는 담당 공무원의 "
                "전문성을 높인다.")
        self.assertEqual(
            _strip_article_chrome(text),
            "대전 동구, 16개 동 찾아가는 기초생활보장 순회교육…복지 현장 대응력 강화 "
            "대전 동구가 복지서비스 최일선에서 주민을 만나는 담당 공무원의 전문성을 높인다.")

    def test_paren_dateline_with_byline(self):
        # id=15711
        text = "(서울=국제뉴스) 손병욱 기자 = 금호석유화학이 18년째 지원을 이어가고 있다."
        self.assertEqual(_strip_article_chrome(text),
                         "금호석유화학이 18년째 지원을 이어가고 있다.")

    def test_square_bracket_dateline_with_byline(self):
        # id=14339
        text = "[세종=뉴스핌] 오종원 기자 = 정부가 사업화 자금을 지원한다."
        self.assertEqual(_strip_article_chrome(text),
                         "정부가 사업화 자금을 지원한다.")

    def test_square_bracket_outlet_reporter(self):
        # id=15718 — outlet=reporter inside one bracket, no separate "=".
        text = "[더퍼블릭=안은혜 기자]내년 최저임금이 시간당 1만700원으로 결정됐다."
        self.assertEqual(_strip_article_chrome(text),
                         "내년 최저임금이 시간당 1만700원으로 결정됐다.")

    # 102-APPLY — outlet ㅣ dateline separator inside the bracket. One
    # specimen per separator character (Hangul ㅣ U+3163, ASCII |, full-width
    # ｜). Re-measured after the edit over all 16,267 stored claim texts:
    # exactly 7 rows match, every one a byline, zero legitimate claims.
    def test_bracket_dateline_hangul_i_separator(self):
        # id=5405
        text = "[스포츠서울ㅣ원주=김기원기자]국가데이터처가 주관하는 인구주택총조사가 실시된다."
        self.assertEqual(_strip_article_chrome(text),
                         "국가데이터처가 주관하는 인구주택총조사가 실시된다.")

    def test_bracket_dateline_spaced_hangul_i_separator(self):
        # id=7548
        text = "[스포츠서울 ㅣ 순창=고봉석 기자] 전북 순창군이 귀농·귀촌 우수지자체상을 수상했다."
        self.assertEqual(_strip_article_chrome(text),
                         "전북 순창군이 귀농·귀촌 우수지자체상을 수상했다.")

    def test_bracket_dateline_ascii_pipe_separator(self):
        # id=15695
        text = "[더팩트 | 정리=손원태 기자] 정부가 수도권 23만호 공급책을 내놓았다."
        self.assertEqual(_strip_article_chrome(text),
                         "정부가 수도권 23만호 공급책을 내놓았다.")

    def test_bracket_dateline_fullwidth_pipe_separator(self):
        text = "[더팩트｜서천=노경완 기자] 충남 서천군이 농림어업총조사를 마무리했다."
        self.assertEqual(_strip_article_chrome(text),
                         "충남 서천군이 농림어업총조사를 마무리했다.")

    def test_bare_byline_with_role(self):
        # id=14749
        text = "박재찬 보험전문기자 = 삼성생명이 판매 전략을 전환했다."
        self.assertEqual(_strip_article_chrome(text),
                         "삼성생명이 판매 전략을 전환했다.")

    def test_correspondent_byline(self):
        # id=15123 — "[베이징=뉴스핌] 최헌규 베이징 특파원= …"
        text = "[베이징=뉴스핌] 최헌규 베이징 특파원= 베이징이 부양책에 돌입했다."
        self.assertEqual(_strip_article_chrome(text),
                         "베이징이 부양책에 돌입했다.")

    def test_pipe_terminated_outlet_byline(self):
        # id=15713
        text = "매일일보 = 이지완 기자 | 농업 현장의 공공데이터 활용이 확대되고 있다."
        self.assertEqual(_strip_article_chrome(text),
                         "농업 현장의 공공데이터 활용이 확대되고 있다.")

    def test_trailing_byline_after_sentence(self):
        # id=14895
        text = "노동자 생계 안정과 기업의 고용 유지를 지원하고 있다. 정근산 기자"
        self.assertEqual(_strip_article_chrome(text),
                         "노동자 생계 안정과 기업의 고용 유지를 지원하고 있다.")

    def test_byline_glued_to_mangled_email(self):
        # id=15009
        text = "이정민기자 ljm7damdilbo.com 조직개편 실마리 풀릴까"
        self.assertEqual(_strip_article_chrome(text),
                         "조직개편 실마리 풀릴까")

    def test_leading_input_stamp(self):
        # id=15734
        text = "입력 2026.08.15 18:22 20일 교육부 추진계획서 제출 시한 임박"
        self.assertEqual(_strip_article_chrome(text),
                         "20일 교육부 추진계획서 제출 시한 임박")

    def test_colon_input_and_edit_stamps_mid_text(self):
        # id=15399 — 입력/수정 with colonless… uses "입력 2026-08-11 20:19:30
        # 수정 2026-08-11 20:19:30" mid-text; colon form from id=15544.
        text = ("부산 중구, 상담원 교육 실시 | 입력 : 2026-08-12 17:19:44 "
                "부산 중구는 맞춤형 복지교육을 실시했다.")
        self.assertEqual(_strip_article_chrome(text),
                         "부산 중구, 상담원 교육 실시 부산 중구는 맞춤형 복지교육을 실시했다.")

    def test_register_stamp_with_clock(self):
        # id=14560
        text = "산업활동동향 브리핑 등록 2026.07.31 09:13:55 브리핑을 하고 있다."
        self.assertEqual(_strip_article_chrome(text),
                         "산업활동동향 브리핑 브리핑을 하고 있다.")

    def test_yonhap_footer_strips_to_nothing(self):
        # id=15754 — a pure-chrome claim must strip to "" (then the 18-char
        # floor drops it at extraction).
        text = ("제보는 카카오톡 okjebo <저작권자(c) 연합뉴스, 무단 전재-재배포, "
                "AI 학습 및 활용 금지> 2026년08월17일 06시04분 송고")
        self.assertEqual(_strip_article_chrome(text), "")

    def test_glued_wire_run_keeps_real_claim(self):
        # id=15721 — the reason the rules STRIP instead of rejecting: the same
        # unbroken run carries a real claim after the footer.
        text = ("[표] 종사상 지위별 취업자 증감 (단위 : 천명) 제보는 카카오톡 "
                "okjebo<저작권자(c) 연합뉴스,무단 전재-재배포, AI 학습 및 활용 "
                "금지>2026/08/16 05:49 송고2026년08월16일 05시49분 송고 "
                "(서울=연합뉴스) 강민지 기자 = 국가데이터처 조사에서 청년층 "
                "비율이 가장 높은 수준으로 나타났다.")
        self.assertEqual(
            _strip_article_chrome(text),
            "[표] 종사상 지위별 취업자 증감 (단위 : 천명) 국가데이터처 조사에서 "
            "청년층 비율이 가장 높은 수준으로 나타났다.")

    def test_yonhap_tv_footer(self):
        # id=14806
        text = ("연합뉴스TV 기사문의 및 제보 : 카톡/라인 jebo23 이재경"
                "(jack0yna.co.kr) 연합뉴스TV, 무단 전재-재배포, AI 학습 및 활용 금지")
        stripped = _strip_article_chrome(text)
        for marker in ("기사문의", "카톡", "무단", "재배포", "AI 학습", "금지"):
            self.assertNotIn(marker, stripped)

    def test_leading_photo_caption(self):
        # id=15733 — post-split sentence starting with the glued caption.
        text = "사진=삼성생명 삼성생명은 대표 건강보험을 개정했다."
        self.assertEqual(_strip_article_chrome(text),
                         "삼성생명은 대표 건강보험을 개정했다.")

    def test_paren_photo_caption(self):
        # id=15493
        text = "(사진=김양균 기자) 복지부는 지원사업을 시행 중입니다."
        self.assertEqual(_strip_article_chrome(text),
                         "복지부는 지원사업을 시행 중입니다.")

    def test_multiword_caption_with_vocabulary_tail(self):
        # id=15149 / id=15649
        self.assertEqual(
            _strip_article_chrome("자료사진=본 기사와 무관 2027년 최저임금이 확정됐다."),
            "2027년 최저임금이 확정됐다.")
        self.assertEqual(
            _strip_article_chrome("/사진=온라인 커뮤니티 캡처 사연이 다시 거론됐다."),
            "사연이 다시 거론됐다.")


class ChromeFalsePositiveTests(unittest.TestCase):
    """Legitimate claims that LOOK like chrome must pass through unchanged.
    Each was the reason a looser pattern was NOT shipped."""

    LEGITIMATE = [
        # colonless 수정 + date in prose (why bare 입력/수정 needs a colon
        # unless claim-leading):
        "정부는 시행령 개정안 수정 2026년 3월 12일 공포를 목표로 하고 있다.",
        # 승인 + date (why 승인 stamps are not shipped at all):
        "국무회의 승인 2026년 1월 5일 이후 예산이 집행된다.",
        # 등록 + date WITHOUT a clock (why 등록 requires date AND clock):
        "법인 등록 2026년 3월 이후 신청 기업은 감면 대상에서 제외된다.",
        # parentheticals without "=" (why the dateline needs the = shape):
        "삼성 New플러스원 건강보험(무배당, 저해약환급금형)이 개정 출시됐다.",
        "종사상 지위별 취업자 증감(단위 : 천명) 통계가 발표됐다.",
        # 기자 without the byline "=" (why the byline requires it):
        "정부는 기자 간담회를 열어 부동산 대책을 발표했다.",
        # ordinary dates/numbers:
        "최저임금위원회는 2026년 8월 5일 시간당 1만700원 인상안을 의결했다.",
        # 사진 mentioned as a word, not a caption:
        "위성 사진 분석 결과 개발제한구역 내 불법 건축물이 확인됐다.",
        # 102-APPLY look-alikes: a separator inside a bracket tag is NOT a
        # dateline without the "=" (why the separator only widens the
        # left-of-= class, and the = shape is still required):
        "[기획ㅣ청년정책] 정부가 청년 월세 지원 대상을 확대한다.",
        "[단독 | 분석] 국토부가 수도권 공급 대책을 다음 달 발표한다.",
        "[표｜분기별 취업자 증감] 통계청이 고용 동향을 발표했다.",
        # separator + = but no bracket at all (the bracket is required):
        "정부는 서울ㅣ경기=수도권 광역 교통망 확충에 3조원을 투입한다.",
    ]

    def test_legitimate_claims_unchanged(self):
        for claim in self.LEGITIMATE:
            self.assertEqual(_strip_article_chrome(claim), claim)

    def test_zero_false_positives_pinned(self):
        # Measured 2026-08-17 over 1,300 real rows / 3,927 stored claim texts:
        # 166 texts (95 rows) stripped, every removed fragment reviewed as
        # chrome, ZERO legitimate claims altered. This test pins the per-
        # pattern guardrails above so that stays true.
        for claim in self.LEGITIMATE:
            for pattern in claim_extractor._CHROME_PATTERNS:
                self.assertIsNone(pattern.search(claim),
                                  f"{pattern.pattern!r} matched {claim!r}")


class ChromeEndToEndTests(unittest.TestCase):
    def test_extracted_claim_is_chrome_free(self):
        body = ("(대전=뉴스충청인) 김수환 기자 = 정부가 청년 전세대출 금리를 "
                "0.5%p 인하하는 방안을 추진한다. 관계 부처는 다음 달 시행을 "
                "목표로 세부 지원 기준을 검토하고 있다. "
                "제보는 카카오톡 okjebo <저작권자(c) 연합뉴스, 무단 전재-재배포, "
                "AI 학습 및 활용 금지> 2026년08월17일 06시04분 송고")
        claims = extract_verifiable_claims(body)
        self.assertTrue(claims)
        for claim in claims:
            self.assertNotIn("기자", claim)
            self.assertNotIn("카카오톡", claim)
            self.assertNotIn("송고", claim)
            self.assertNotIn("저작권자", claim)
        self.assertIn("전세대출", claims[0])

    def test_pure_footer_never_becomes_a_claim(self):
        # The footer contains 금지 (a POLICY_KEYWORD) + numbers, which is how
        # it passed _is_verifiable before; post-strip it is empty and drops.
        body = ("제보는 카카오톡 okjebo <저작권자(c) 연합뉴스, 무단 전재-재배포, "
                "AI 학습 및 활용 금지> 2026년08월17일 06시04분 송고 " * 3)
        self.assertEqual(extract_verifiable_claims(body), [])

    def test_fallback_summary_chrome_stripped(self):
        # The summary/title fallback path never passes _collect_sentences;
        # _clean_claim strips it there.
        claims = extract_verifiable_claims(
            "", title="", summary="[세종=뉴시스] 오종원 기자 = 정부가 최대 "
                                  "1억5000만원의 사업화 자금을 지원한다.")
        self.assertEqual(claims, ["정부가 최대 1억5000만원의 사업화 자금을 지원한다."])


class PersonnelGateTests(unittest.TestCase):
    """PERSONNEL-GATE (92-APPLY) — a title-marker gate routes personnel
    articles to the summary/title fallback so a promotions roster is never
    ranked as the claim. Titles below are REAL corpus titles (ids noted),
    measured over all 16,026 rows: 20 marker hits, all personnel, 0 policy
    articles caught."""

    # A real roster body (id=15949's stored claim): department names carry
    # policy keywords as substrings, so unGATED it ranks as a claim.
    ROSTER_BODY = ("국가데이터 4급 승진 국가데이터처 서주희 감사담당관실 "
                   "유달순 통계정책과 임지우 통계정책과 조성현 통계서비스기획과 "
                   "최경아 물가동향과 이정화 고용통계과 권순필 교육기획과 "
                   "이주희 조사관리국 운영지원과 국회사무처 전보 발령 명단이다.")

    def test_bracket_insa_marker(self):
        # id=15949
        self.assertTrue(claim_extractor._is_personnel_notice_title(
            "[인사] 국가데이터처"))

    def test_bracket_insa_marker_glued(self):
        # id=8030 — no space after the tag
        self.assertTrue(claim_extractor._is_personnel_notice_title(
            "[인사]전남광주특별시교육청 광주청사"))

    def test_bracket_insa_jonghap_marker(self):
        # id=15948 — dated digest tag
        self.assertTrue(claim_extractor._is_personnel_notice_title(
            "[8월18일 인사종합] 국가데이터처 외"))

    def test_insa_jonghap_suffix_marker(self):
        # id=8026 — wire roundup suffix
        self.assertTrue(claim_extractor._is_personnel_notice_title(
            "전남광주통합특별시교육청 출범 후 첫 5급 이상 인사(종합)"))

    def test_lookalike_insight_tag_passes(self):
        # id=25 — [부동산 인사이트] is a column tag, not a roster
        self.assertFalse(claim_extractor._is_personnel_notice_title(
            "[부동산 인사이트] 전세, 월세가 사라지고 있다 - 비즈한국"))

    def test_lookalike_policy_insight_tag_passes(self):
        # id=14128
        self.assertFalse(claim_extractor._is_personnel_notice_title(
            "[정책 인사이트] '한국형 주치의' 윤곽…치매·장애인 넘어 고혈압·당뇨"))

    def test_lookalike_insawi_passes(self):
        # id=9530 — 인사위 회부 is a disciplinary-referral story
        self.assertFalse(claim_extractor._is_personnel_notice_title(
            '[단독] "아동학대 부실 대응"...양주시 공무원 인사위 회부'))

    def test_lookalike_insa_prose_passes(self):
        # id=8037 — marker-less personnel PROSE headline: stays ungated (the
        # gate keys on the wire label, never on topic)
        self.assertFalse(claim_extractor._is_personnel_notice_title(
            "전남광주통합특별시교육청, 5급 이상 일반직 공무원 인사 단행"))

    def test_gated_article_falls_back_to_title(self):
        # Roster body + marker title → the summary/title fallback is the
        # claim; the roster is never selected. claims stays NON-EMPTY, so the
        # row is still recorded and counted.
        claims = extract_verifiable_claims(
            self.ROSTER_BODY, title="[인사] 국가데이터처", summary="")
        self.assertEqual(claims, ["[인사] 국가데이터처"])

    def test_ungated_title_still_ranks_body(self):
        # Same body under a non-marker title: selection is untouched — the
        # gate recognises the document type, it does not judge sentences.
        claims = extract_verifiable_claims(
            self.ROSTER_BODY, title="국가데이터처 조직 개편", summary="")
        self.assertTrue(claims)
        self.assertNotEqual(claims, ["국가데이터처 조직 개편"])


class TitleAttributionTests(unittest.TestCase):
    """TITLE-ATTRIB (93-APPLY) — fallback claims drop the feed's trailing
    " - 매체명" attribution. Every title below is a REAL corpus title (ids
    noted). Measured over all 16,026 titles: 659 strip, all 294 distinct
    removed suffixes are outlet/domain/author attribution, 0 content."""

    def test_strips_single_trailing_outlet(self):
        # id=13086
        claims = extract_verifiable_claims(
            "", title="폴란드 통계청, 안양 스마트도시 견학 - 신아일보")
        self.assertEqual(claims, ["폴란드 통계청, 안양 스마트도시 견학"])

    def test_strips_outlet_after_parenthesised_list(self):
        # id=13946
        claims = extract_verifiable_claims(
            "", title="2025년 6월 인구동향(출생, 사망, 혼인, 이혼) - 서울Pn")
        self.assertEqual(claims, ["2025년 6월 인구동향(출생, 사망, 혼인, 이혼)"])

    def test_strips_outlet_but_keeps_glued_hyphens(self):
        # id=13336 — the trailing outlet goes; the glued name-role hyphens
        # are content and stay whole.
        claims = extract_verifiable_claims(
            "", title="대변인-권병기, 지필공실장-손영래, 건강보험국장-유주현 - 데일리팜")
        self.assertEqual(claims,
                         ["대변인-권병기, 지필공실장-손영래, 건강보험국장-유주현"])

    def test_floor_leaves_short_personnel_fallback_alone(self):
        # id=16026 — stripping would leave 11 chars, under the extractor's
        # 18-char floor, so the fallback ships exactly as before (91-APPLY
        # backfill precedent: never store a below-floor fragment).
        claims = extract_verifiable_claims(
            "", title="[인사] 국가데이터처 - 연합뉴스")
        self.assertEqual(claims, ["[인사] 국가데이터처 - 연합뉴스"])

    def test_multiword_subtitle_survives(self):
        # id=9055 — a spaced-dash MULTI-WORD tail is a subtitle, not an
        # outlet; it must survive untouched.
        title = "'추적 60분' 초고령사회의 민낯 - 돌봄 지옥, 사라지는 요양보호사"
        self.assertEqual(
            claim_extractor._strip_trailing_attribution(title), title)

    def test_multiword_subtitle_survives_with_leading_tag(self):
        # id=15601
        title = "[메디컬 窓] 필수의료 살리기 - 낙인이 아닌 신뢰의 제도로"
        self.assertEqual(
            claim_extractor._strip_trailing_attribution(title), title)

    def test_outlet_lookalike_content_without_dash_survives(self):
        # id=12472 — 정책브리핑 is an outlet name but here it is the SUBJECT;
        # with no spaced-dash delimiter nothing is touched.
        title = "정책브리핑 RSS 서비스 제공 중단 안내"
        self.assertEqual(
            claim_extractor._strip_trailing_attribution(title), title)

    def test_leading_bracket_tag_is_not_stripped(self):
        # The section tag names the document type and stays; only the
        # trailing attribution goes. (id=13581)
        claims = extract_verifiable_claims(
            "", title="[보도참고] 최근 청년층 고용동향과 정책지원 방향 - 서울Pn")
        self.assertEqual(claims, ["[보도참고] 최근 청년층 고용동향과 정책지원 방향"])


class ChromeHonestyTests(unittest.TestCase):
    def test_no_verdict_field_in_extractor_source(self):
        # The extractor feeds the verdict layers but must not reach into them.
        source = inspect.getsource(claim_extractor)
        for column in ("verdict_label", "policy_confidence", "truth_claim",
                       "operator_review_required",
                       "has_genuine_official_support", "risk_level",
                       "policy_alert_level", "review_status"):
            self.assertNotIn(column, source)


if __name__ == "__main__":
    unittest.main()
