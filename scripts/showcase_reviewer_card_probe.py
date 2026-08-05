# SHOWCASE-REVIEWER Phase 2 — CARD-DETAIL reviewer probe (pin-OUT, SELECT-only).
#
# The title layer passed (recall 4/4, drift 0, ~$0.013/wk). This probe measures
# the layer that matters: the card DETAIL, where every defect only a human ever
# caught actually lived. ★Row 13977 (a 2021년 11월 statistics release judged in
# a 2026 window, its official documents five years apart from the claim) is the
# real test — it passed every machine check and was found by eye. Row 13700
# carried 30+ irrelevant official documents. If the reviewer cannot see what
# only a person saw, this layer is not worth building.
#
# ★WHAT IS FED (the design decision): NOT the raw /history record — that shows
# scores, matched words and internal diagnostics no reader sees, and feeding it
# truncated a previous session's attempt. Instead the sample rows are rendered
# through the REAL frontend/scripts/main.js chain by REUSING the committed
# scripts/card_render_audit.js (its renderRow + pinned helpers, never
# reimplemented): an embedded Node driver vm-executes the audit with a trapped
# process.exit, then calls renderRow per row and emits the reader-visible text
# of every public card section (tags stripped, per-section 3,500-char cap with
# an explicit […N자 생략] marker; the 공식 문서 후보 count survives any cut).
# Node is required — the same requirement the C8 render-scan gate already
# imposes on this environment.
#
# AS-JUDGED CONDITION: BOTH ground-truth rows are reconstructed to the
# 2026-07-29 human read (see AS-JUDGED-RECONSTRUCTION below): 13977 drops the
# #13 caveat-chip lines; 13700 undoes the #14 exclusion labels and the #15
# heading suffix (#18 is a verified no-op for it). Every string a strip
# depends on is pinned against main.js — a renamed product string exits
# loudly instead of silently feeding a modern card. Disclosed in the output
# with per-row line counts.
#
# SAMPLE: this week's weekly top-10 representative rows (13700 is one of them)
# + the first 6 distinct representatives from older archived weeks + row 13977
# forced in — every one reachable by a reader from the exposed surface.
# Reviewer: claude-sonnet-5, thinking disabled (extraction task; Phase 1
# measured thinking exhausting the cap), 3 cards per request, TWO passes over
# identical batches so determinism is measurable, smoke-check after call 1,
# parse failure prints response structure and exits 2 (never a clean-looking
# empty run).
#
# REMOVAL DRIFT: the pins catch a RENAME. They cannot catch a REMOVAL — see
# AS_JUDGED_KNOWN_GAPS. What a removal leaves behind (a strip matching zero
# lines) is now announced on the CAUGHT/MISSED line itself, and the two known
# gaps are printed before any verdict. The reconstructions are SUFFICIENT, not
# FAITHFUL, and the output says so.
#
# Joe runs once in the Render Worker Shell:
#     PYTHONPATH=. python scripts/showcase_reviewer_card_probe.py
# Offline, no DB/API/cost — proves the drift machinery is not vacuous:
#     PYTHONPATH=. python scripts/showcase_reviewer_card_probe.py --selftest
#
# SAFETY: SELECT-only (weekly_reports.payload_json, analysis_results render
# columns). Temp-file dump for the Node driver only (the C8 precedent) — no DB
# write, no stored field touched: truth_claim stays False,
# operator_review_required stays True, verdict_label untouched. Credentials
# from the environment ONLY, never printed. pin-OUT scripts/* — zero log.*
# call sites, the 331/16 pins do not move. Output ≤ ~40 lines.

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

MODEL = "claude-sonnet-5"
PRICE_STD = (3.0, 15.0)    # $/MTok in, out
PRICE_INTRO = (2.0, 10.0)  # through 2026-08-31
BATCH_SIZE = 3             # detail cards are ~3K tokens each
MAX_TOKENS_PER_BATCH = 2000
THINKING = {"type": "disabled"}  # extraction task; budget_tokens 400s on Sonnet 5
MUST_INCLUDE = (13977, 13700)
STAMP = "공식 본문 불일치 가능성"  # post-13977 caveat chip (#13/#19 MATCHER-GUARD)

# ---------------------------------------------------------------------------
# AS-JUDGED-RECONSTRUCTION — both ground-truth rows, not just 13977.
#
# The expectations at HEADLINE_EXPECTATIONS encode what a HUMAN caught by eye
# on 2026-07-29. Product repairs since then changed what the cards SAY, so the
# probe reconstructs each card as it stood when the human judged it — otherwise
# a framing fix silently flips an answer and the recall check indicts the
# reviewer for a card that got honest (which is exactly what happened to 13700:
# ledger #14/#15 landed the same evening and its CAUGHT expectation became a
# phantom MISSED).
#
# 13977 — strip the #13/#19 caveat chip (STAMP) lines. Unchanged behaviour.
# 13700 — undo the two framing fixes that postdate the read; ledger #20 (the
#   88-document roster itself) is WONTFIX, so the DOCUMENTS are already
#   as-judged — only the framing moved:
#   * #15 CANDIDATE-SECTION-FRAMING (b6dd708): the heading gained a suffix.
#     Pre-fix heading was byte-identical minus the suffix (stated in the
#     commit), so the rewrite keeps group 1 verbatim.
#   * #14 CANDIDATE-EXCLUSION-WIRING (1f2b2f6): fetched-and-unmatched
#     candidates gained (i) the 제외/불일치 trace badge, (ii) the 공개 표시
#     판단 explanation 주제 불일치로 제외, (iii) the standalone exclusion
#     label line. Pre-fix, those SAME candidates fell through to the weak
#     trace tier — 공식 약한 후보 with its shipped explanation — which is
#     DETERMINISTIC here: the tier (OFFICIAL-EVIDENCE-DISPLAY-HONESTY STEP 2)
#     shipped 07-22, a week before the read, and 13700 measures 86/88
#     candidates labelled ONLY via the #14 branch, 0 via any pre-#14 branch.
#     So: badge lines rewrite to the weak badge, explanation lines rewrite to
#     the weak explanation, standalone label lines drop. Every substitution
#     string is shipped vocabulary quoted from main.js, never invented.
#   * #18 GENUINE-GATE-REMOVAL (19de54c): verified NO-OP for 13700 — the row
#     stores has_genuine_official_support=False, and the pre-#18 gate
#     (=== false) fired identically. Nothing to strip.
#
# STALENESS MADE VISIBLE: every string a strip depends on is pinned below and
# verified against main.js at render time. If a fix's string is renamed or
# removed, the probe EXITS LOUDLY instead of quietly feeding a modern card —
# a strip that silently no-ops is the same silent-flip defect one layer up.
WEAK_BADGE = "공식 약한 후보"
WEAK_EXPLANATION = "공식 후보이지만 상세 본문 또는 직접 일치가 제한적입니다."
EXCL_BADGE = "제외/불일치"
EXCL_LABEL = "주제 불일치로 제외"
DISPLAY_ROW = "공개 표시 판단"
HEADING_SUFFIX_RE = re.compile(
    r"^(공식 출처 후보 \d+개)(?: 중 \d+개| — 모두) 직접 근거에서 제외됨$")

# {row: {ledger ref: [strings that must still exist in main.js]}} — both the
# fix's own strings AND the pre-fix vocabulary a rewrite reinstates.
AS_JUDGED_PINS = {
    13977: {"#13/#19 MATCHER-GUARD chip": [STAMP]},
    13700: {
        "#15 CANDIDATE-SECTION-FRAMING heading": [
            " — 모두 직접 근거에서 제외됨", "개 직접 근거에서 제외됨"],
        "#14 CANDIDATE-EXCLUSION-WIRING badge": [EXCL_BADGE, WEAK_BADGE],
        "#14 CANDIDATE-EXCLUSION-WIRING label": [EXCL_LABEL, WEAK_EXPLANATION,
                                                 DISPLAY_ROW],
    },
}

# What each headline expectation HOLDS CONSTANT — printed beside CAUGHT/MISSED
# so the output itself names the repair each answer depends on.
HEADLINE_EXPECTATIONS = (
    (13977, "consistency", "as-judged holds #13 stamp stripped"),
    (13700, "consistency",
     "as-judged holds #14 labels + #15 heading stripped; #18 verified no-op"),
)

# ---------------------------------------------------------------------------
# REMOVAL-DRIFT — the gap the pins CANNOT see, written down instead of closed.
#
# AS_JUDGED_PINS verify that every string a strip DEPENDS ON still exists in
# main.js, so a RENAME exits loudly. They are blind to a REMOVAL of something no
# strip ever named: a product change that DELETES reader text leaves nothing to
# pin and nothing to no-op, and the reconstruction silently drifts one element
# closer to the modern card. That has now happened twice (below).
#
# ★NOT FIXED BY DESIGN. Restoring deleted product text would mean re-authoring
# card output from a ledger entry; a card that is APPROXIMATELY the old one
# tests the reviewer against a card that never existed, which is worse than a
# known gap. The goal here is that the gap is VISIBLE and BOUNDED, not zero. So
# these are recorded, printed beside every CAUGHT/MISSED, and deliberately left
# open. Adding a strip for any of them is the re-authoring this forbids.
#
# Each entry: (direction, what the fed card gets wrong, why it is not repaired).
#   MISSING = the human saw it, the modern card no longer emits it, the
#             reconstruction cannot put it back.
#   EXTRA   = the modern card emits it, the human never saw it, no strip removes
#             it (the reconstruction is incomplete in the other direction).
AS_JUDGED_KNOWN_GAPS = {
    13977: (
        ("EXTRA", "#14 CANDIDATE-EXCLUSION-WIRING framing on the candidate "
                  "roster — 제외/불일치 badges, 공개 표시 판단 rows and 주제 "
                  "불일치로 제외 labels. The 13700 reconstruction rewrites these; "
                  "13977 has no such strip, so its fed roster carries framing "
                  "that landed 07-29 and the human may never have seen.",
         "a strip here would be a second reconstruction written from "
         "inference, not from a commit that states the pre-fix text"),
        ("EXTRA", "the 대조 검토 section's whole shape — the count-reconcile "
                  "lines (후보/매칭, 판정 근거) and the rewired rebuttal path. "
                  "REBUTTAL-PATH-WIRING (c9cdbb8) and REBUTTAL-COUNT-RECONCILE "
                  "(7f556d7) both landed 2026-07-30, the day AFTER the human "
                  "read, so neither row was judged against this text; both rows "
                  "are fed it. Worst on 13977, whose expectation is the "
                  "CONSISTENCY axis and 대조 검토 is the section that speaks to "
                  "it — the expectation still passes and its signal (a "
                  "five-year date gap) is untouched, but the section carrying "
                  "the reviewer's strongest cue for that axis is not as-judged. "
                  "card_render_audit.js already carries mirror comments for "
                  "both commits, so the drift was documented in the scanner and "
                  "not here.",
         "the pre-fix section text is not stated by either commit, so a strip "
         "would be reconstruction by inference — re-authoring, not restoring"),
    ),
    13700: (
        ("MISSING", "출처 신뢰도 N and the 맥락 참고 role line on EXCLUDED "
                    "candidates. EXCLUDED-CANDIDATE-DROP-SCORE-AND-ROLE stopped "
                    "emitting both for rows the card had already set aside; the "
                    "human read them on 88 candidates and nothing restores them.",
         "the deleted values are per-candidate product output; reconstructing "
         "them means re-authoring numbers from a ledger entry"),
    ),
}

# ---------------------------------------------------------------------------
# ★EXIT CONDITION — when to ABANDON the reconstruction instead of annotating it.
#
# Recording a gap is a STAY, not a cure. Every product change moves the fed card
# one step further from the 2026-07-29 read, and a gap list with no stopping
# rule becomes an indefinite one by default — each new entry individually
# reasonable, the whole quietly no longer describing the card a human judged.
#
#   When the recorded gap count reaches FOUR, or when either expectation flips
#   to MISSED, the reconstruction has drifted far enough that annotating it is
#   no longer honest. At that point the correct response is to establish a NEW
#   GROUND TRUTH — a fresh human read of current cards — NOT to add a fifth gap.
#
# A MISSED trips it on its own because the expectations are the only evidence
# that the reconstruction still carries its signal; once one stops holding,
# nothing distinguishes "the reviewer regressed" from "the card we feed is no
# longer the card that was judged", and no further annotation can separate them.
# The count is printed against this ceiling wherever the gaps are shown, so the
# distance is read rather than counted.
AS_JUDGED_GAP_CEILING = 4
AS_JUDGED_EXIT_CONDITION = (
    "EXIT CONDITION: at %d recorded gaps, or the first MISSED, STOP annotating "
    "— the reconstruction has drifted too far to describe the judged card. "
    "Establish a new ground truth (a fresh human read of current cards) instead "
    "of recording another gap." % AS_JUDGED_GAP_CEILING)


def as_judged_gap_count(gaps=None):
    """Distinct recorded gaps, not per-row mentions — one product change that
    hits both rows is ONE gap, and must not inflate the count toward the exit
    ceiling twice."""
    src = AS_JUDGED_KNOWN_GAPS if gaps is None else gaps
    return sum(len(entries) for entries in src.values())


# ★THE FIDELITY CLAIM — printed wherever a CAUGHT/MISSED is reported, because a
# CAUGHT read without it means more than it should.
AS_JUDGED_FIDELITY = (
    "FIDELITY: the reconstruction is SUFFICIENT, not FAITHFUL — it preserves "
    "the SIGNAL each expectation tests (13977: a five-year date gap between "
    "claim and official documents; 13700: documents unrelated to the claim), "
    "NOT the whole card the human read. See KNOWN GAPS above; a CAUGHT means "
    "the reviewer saw that signal, not that it read the 2026-07-29 card.")

# ★ZERO-HIT POLICY: WARNING, not a hard failure — and bound to the verdict line
# so it cannot be skimmed.
#   Against a hard failure: a strip legitimately matches zero lines when the
#   ROW's stored data changed (a candidate leaving the roster, a re-analysis
#   dropping a chip). Exiting there blocks a $0.81 run — against roughly $11
#   remaining — for a benign reason, and the probe's whole value is that it can
#   be run.
#   Against a bare warning: the per-op count line already existed and is exactly
#   the "number a reader might skim past" — it prints ~30 lines above the
#   verdict that actually gets read.
#   So: keep the run, and make the warning ride ON the CAUGHT/MISSED line via
#   RECONSTRUCTION DEGRADED, plus a consolidated banner. The operator cannot
#   read the answer without reading that its reconstruction was incomplete.
ZERO_HIT_NOTE = (" — ZERO lines matched: the fed text no longer carries this "
                 "fix's strings; the reconstruction may be moot or the "
                 "rendering moved. Read before trusting the expectation.")


def verify_as_judged_pins(src=None):
    """Every string the reconstructions depend on must still exist in
    main.js. A missing one means a framing fix moved again — exit loudly
    BEFORE any render or API spend, mirroring LABEL PIN LOST. ``src`` is for
    the loud-failure demonstration only; production always reads main.js."""
    if src is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "frontend", "scripts", "main.js"),
                  encoding="utf-8") as fh:
            src = fh.read()
    lost = [(rid, ref, pin)
            for rid, refs in AS_JUDGED_PINS.items()
            for ref, pins in refs.items()
            for pin in pins if pin not in src]
    if lost:
        for rid, ref, pin in lost:
            print("AS-JUDGED PIN LOST: row %s holds %s constant via %r — "
                  "string gone from main.js. The reconstruction would silently "
                  "feed a modern card; review the product change, then update "
                  "the strip." % (rid, ref, pin))
        raise SystemExit(2)


def report_as_judged(rid, hits):
    """Print one line per as-judged op and return the refs that matched NOTHING.
    Shared by render_cards and --selftest so the demonstration exercises the
    production path rather than a copy of it."""
    zero = [ref for ref, count in hits.items() if not count]
    for ref, count in hits.items():
        print("AS-JUDGED %s: %s -> %d line(s)%s"
              % (rid, ref, count, "" if count else ZERO_HIT_NOTE))
    if zero:
        print("AS-JUDGED RECONSTRUCTION DEGRADED [%s]: %d of %d operation(s) "
              "matched nothing (%s). A removal the pins cannot see: the strip's "
              "target text is gone from the RENDER, not just from main.js, so "
              "this row is fed one element closer to the modern card than the "
              "human read. The expectation below is reported WITH this caveat, "
              "not silently."
              % (rid, len(zero), len(hits), "; ".join(zero)))
    return zero


def print_known_gaps(gaps=None):
    """The gaps are printed BEFORE any verdict, so nobody reads a CAUGHT
    without having read what its reconstruction does not restore — and the
    count is printed AGAINST the exit ceiling, so the distance to abandoning
    the reconstruction is read rather than counted. ``gaps`` is for the
    at-ceiling demonstration only; production always reads the module table."""
    src = AS_JUDGED_KNOWN_GAPS if gaps is None else gaps
    n = as_judged_gap_count(src)
    print("AS-JUDGED KNOWN GAPS: %d of %d before the exit condition — recorded, "
          "deliberately NOT repaired (restoring deleted product text would "
          "re-author a card that never existed):"
          % (n, AS_JUDGED_GAP_CEILING))
    for rid, entries in src.items():
        for direction, what, why in entries:
            print("  %s [%s] %s" % (rid, direction, what))
            print("      not repaired because: %s" % why)
    print(AS_JUDGED_EXIT_CONDITION)
    if n >= AS_JUDGED_GAP_CEILING:
        print("★EXIT CONDITION REACHED: %d recorded gaps >= %d. Do NOT record a "
              "%dth. The reconstruction no longer describes the card the human "
              "judged closely enough for a CAUGHT to mean what it says — "
              "establish a new ground truth (a fresh human read of current "
              "cards) and re-baseline the expectations against it."
              % (n, AS_JUDGED_GAP_CEILING, n + 1))
    else:
        print("  (%d more recorded gap(s) would reach it)"
              % (AS_JUDGED_GAP_CEILING - n))


def print_missed_exit_condition(missed):
    """A MISSED is an exit-condition trigger on its own — print it as one."""
    if not missed:
        return
    print("★EXIT CONDITION REACHED — MISSED on %s. Neither an added gap nor "
          "another annotation can settle this: the expectations were the only "
          "evidence that the reconstruction still carries its signal, so once "
          "one stops holding, 'the reviewer regressed' and 'the card we feed is "
          "no longer the card that was judged' become indistinguishable. "
          "Establish a new ground truth — a fresh human read of current cards — "
          "and re-baseline the expectations against it. Do not add a gap for "
          "this." % ", ".join(str(r) for r in missed))


def apply_as_judged(rid, text):
    """Reconstruct a MUST_INCLUDE row's fed text as it stood at the human
    read. Returns (text, {ledger ref: lines affected}); zero-hit ops are the
    caller's to report — row data can legitimately change."""
    if rid == 13977:
        lines = text.splitlines()
        kept = [ln for ln in lines if ln.strip() != STAMP]
        return "\n".join(kept), {
            "#13/#19 MATCHER-GUARD chip": len(lines) - len(kept)}
    if rid == 13700:
        hits = {"#15 CANDIDATE-SECTION-FRAMING heading": 0,
                "#14 CANDIDATE-EXCLUSION-WIRING badge": 0,
                "#14 CANDIDATE-EXCLUSION-WIRING label": 0}
        out = []
        prev_stripped = ""
        for ln in text.splitlines():
            stripped = ln.strip()
            heading = HEADING_SUFFIX_RE.match(stripped)
            if heading:
                out.append(ln.replace(stripped, heading.group(1)))
                hits["#15 CANDIDATE-SECTION-FRAMING heading"] += 1
            elif stripped == EXCL_BADGE:
                out.append(ln.replace(EXCL_BADGE, WEAK_BADGE))
                hits["#14 CANDIDATE-EXCLUSION-WIRING badge"] += 1
            elif stripped == EXCL_LABEL and prev_stripped == DISPLAY_ROW:
                out.append(ln.replace(EXCL_LABEL, WEAK_EXPLANATION))
                hits["#14 CANDIDATE-EXCLUSION-WIRING label"] += 1
            elif stripped == EXCL_LABEL:
                hits["#14 CANDIDATE-EXCLUSION-WIRING label"] += 1
                # pre-fix: no exclusion div — line dropped, nothing appended
            else:
                out.append(ln)
            prev_stripped = stripped
        return "\n".join(out), hits
    return text, {}

# ---------------------------------------------------------------- prompt ----
# THE PROMPT IS THE DESIGN — printed verbatim. The consistency-vs-truth
# distinction is stated with the operator's own example pair.
SYSTEM_PROMPT = """당신은 정책 뉴스 검증 카드의 화면 표시 심사관이다. 각 항목은 카드 상세 화면에서 독자에게 실제로 보이는 문자열 전체다(섹션 제목은 [ ]로 표시, […N자 생략]은 길이 제한 표시일 뿐 결함이 아니다).
허용된 질문은 정확히 셋뿐이다:
(a) genre — 이 카드의 주장이 정책 관련 보도가 아닌 장르인가? (형사사건, 사건사고, 시세, 부고, 광고)
행사·포럼·설명회 개최 보도와 사업·교육 참가자 모집 공고는 정책 집행의 일부이므로 심사 대상에 포함된다. 이런 항목은 (a) genre에서만 제외된다 — 장르를 이유로는 어떤 표현으로도 flag하지 않는다. (b) surface와 (c) consistency는 이런 항목에도 그대로 전부 적용된다.
(b) surface — 독자에게 기계 부스러기가 보이는가? (영어 코드 조각, enum 값, 원시 타임스탬프, 리터럴 이스케이프, HTML이 글자로 노출, 깨진 인코딩, 단어 중간에서 잘린 문장)
(c) consistency — 화면에 보이는 것끼리 서로 모순되는가? (주장을 뒷받침한다고 제시된 공식 문서의 연도·시점이 주장과 동떨어짐, 라벨이 자기 숫자와 어긋남, 기간이 말이 안 됨, 제시된 문서들이 주장 내용과 무관함)
구분 예시 — "이 공식 문서는 주장과 연도가 다르다"는 consistency 관찰이므로 허용. "이 주장은 거짓 같다", "이 출처는 신뢰할 수 없다"는 진위·신뢰 판정이므로 절대 금지. 주장의 사실 여부와 출처의 신뢰성은 어떤 표현으로도 평가하지 않는다.
카드에 표시된 필드 이름(예: 최고 신뢰 출처, 출처 신뢰도)을 문서를 지칭하기 위해 그대로 인용하는 것은 허용된다. 다만 출처의 신뢰성이나 주장의 사실 여부에 대한 당신 자신의 판단은 여전히 금지다.
확신이 없으면 flag하지 않는다. 지시된 JSON으로만 답한다."""
# ^ DRIFT-DETECTOR-AND-PERSISTENCE added the label-quotation line. PROMPT
#   CHANGED => determinism is RE-BASELINED; the next run's disagreement count
#   is not comparable to earlier runs.
# ^ REVIEWER-GENRE-SCOPE dropped 단순 행사 안내 from the genre list and added
#   the in-scope line. OPERATOR DECISION: this product measures how policy
#   claims CIRCULATE and does not select which deserve attention; a local
#   government's programme-recruitment notice is policy execution, and no
#   mechanical signal separates an event notice from a programme announcement,
#   so drawing that line would be us judging. Left in, it flagged the weekly
#   top one and two every single week — a gate that fires every run trains the
#   operator to skim. PROMPT CHANGED AGAIN => determinism re-baselined.
# ^ REVIEWER-GENRE-SCOPE-NARROW: the first wording ("다른 어떤 질문으로도
#   flag하지 않는다") was too broad — 13700 (a recruitment notice carrying 88
#   unrelated official documents) went CAUGHT -> MISSED with an empty note,
#   i.e. the clause exempted the whole card rather than just its genre. The
#   clause now exempts (a) genre ONLY and states that (b) surface and (c)
#   consistency still apply in full. Determinism improving in that same run
#   (4/17 -> 1/17) is NOT read as a gain: a reviewer looking at less also
#   disagrees with itself less. PROMPT CHANGED AGAIN => re-baselined again.

USER_TEMPLATE = """다음 {n}개 카드를 심사하라.

{rows}

각 카드마다 {{"id","genre","surface","consistency","note"}}를 내라. note는 flag한 경우에만 한두 문장, 아니면 빈 문자열."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "genre": {"type": "boolean"},
                    "surface": {"type": "boolean"},
                    "consistency": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["id", "genre", "surface", "consistency", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

# DRIFT-DETECTOR-AND-PERSISTENCE: the bare-noun detector below fired on our
# own field label (최고 신뢰 출처, main.js:1424) — the instrument reported
# itself, the &#039; class of error. It is KEPT only for old-vs-new
# comparison. The real detector masks UI label phrases EXTRACTED FROM main.js
# (never hand-written; sentinel-pinned so a rename fails loudly like the 106
# render pins) and then matches PREDICATE forms derived from the prompt's ban
# (사실 여부·진위·신뢰도 판단 금지 + its two example sentences).
OLD_DRIFT_RE = re.compile(r"사실|허위|진위|거짓|신뢰|믿을|믿기")
DRIFT_PREDICATES = ("신뢰할 수 없", "신뢰하기 어렵", "신뢰할 만", "신뢰할 수 있",
                    "믿기 어렵", "믿을 수 없", "믿을 만", "믿기 힘들",
                    "사실이 아니", "사실과 다르", "사실로 보", "사실일 가능성",
                    "사실 여부", "진위", "허위", "거짓")
# Sentinel labels that MUST come out of the extraction — a missing one breaks
# the sentinel and the probe exits loudly instead of silently under-masking
# (the pinned-dependency posture).
#
# LABEL-SENTINEL-DERIVATION: the list was hand-typed and went stale the way
# every hand-typed constant in this repo eventually has — 사실 가능성 높음 was
# deliberately REMOVED from the product (it asserted a truth probability) and
# the sentinel kept demanding it. Sentinels are now DERIVED at runtime from
# the vocabulary's owner: every value of the closed VERDICT_LABELS map (the
# literal map plus its `VERDICT_LABELS.x = "…"` extensions, sliced from
# main.js by structure, not by the extraction regex being verified) that
# contains 신뢰/사실/믿 — so a renamed status label moves the sentinel with
# it, and a removed one stops being demanded. MEASURED TODAY that derived set
# is EMPTY, and that is by design: the truth/trust-word purge stripped the
# whole closed map of the extraction's character class. The remaining canaries
# are therefore the two UI FIELD labels below — bare literals with no owning
# map, so they cannot be derived, only held; the probe STATES the snapshot it
# holds in its output so a divergence is visible before it fires.
FIELD_LABEL_SENTINELS = ("최고 신뢰 출처", "출처 신뢰도")


def derive_label_sentinels(src):
    """Sentinels the extraction must capture: closed-map values carrying the
    extraction's character class (derived from the VERDICT_LABELS literal and
    its property-assignment extensions), plus the held field-label snapshot."""
    derived = set()
    start = src.find("const VERDICT_LABELS = {")
    if start >= 0:
        block = src[start:src.index("};", start)]
        derived.update(re.findall(r':\s*"([^"]+)"', block))
    derived.update(re.findall(r'VERDICT_LABELS\.[A-Za-z_]+\s*=\s*"([^"]+)"', src))
    from_map = sorted(v for v in derived if re.search(r"신뢰|사실|믿", v))
    return tuple(from_map) + FIELD_LABEL_SENTINELS
# The real flagged note from the first Worker run — the label-quote control:
# OLD must flag it (that was the artifact), NEW must not.
LABEL_QUOTE_CONTROL = ("카드 핵심 주장은 2021년 11월 고용동향인데, 근거 문서와 "
                       "최고 신뢰 출처로 제시된 자료는 2026년 6월 고용동향으로 "
                       "시점이 전혀 다르다.")
DRIFT_CONTROLS = ("이 출처는 신뢰할 수 없다", "이 주장은 거짓 같다")


def load_ui_labels():
    """Korean phrases containing 신뢰/사실/믿 extracted from main.js string
    space (identifiers are ASCII, so Korean only occurs in literals/comments).
    Sorted longest-first for greedy masking."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "frontend", "scripts", "main.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    labels = {m.strip() for m in
              re.findall(r"[가-힣0-9 /·]*(?:신뢰|사실|믿)[가-힣0-9 /·]*", src)
              if m.strip()}
    sentinels = derive_label_sentinels(src)
    # STATE the vocabulary held: how many sentinels are owner-derived (moves
    # with a rename by itself) vs the typed field-label snapshot (cannot be
    # derived — no owning map — so a divergence must be visible here, in the
    # output, before the moment it fires).
    print("LABEL SENTINELS: %d (closed-map-derived %d + field snapshot %s)"
          % (len(sentinels), len(sentinels) - len(FIELD_LABEL_SENTINELS),
             list(FIELD_LABEL_SENTINELS)))
    missing = [s for s in sentinels if s not in labels]
    if missing:
        print("LABEL PIN LOST: %s — renamed or removed in main.js; drift "
              "masking would silently under-cover. Review the rename, then "
              "update FIELD_LABEL_SENTINELS (map-derived sentinels update "
              "themselves)." % missing)
        raise SystemExit(2)
    return sorted(labels, key=len, reverse=True)


def drift_flags(note, labels):
    """(old_hit, new_hit): old = bare-noun regex on the raw note; new = mask
    every extracted UI-label phrase, then match predicate forms only."""
    masked = note
    for label in labels:
        masked = masked.replace(label, "▢")
    return (bool(OLD_DRIFT_RE.search(note)),
            any(p in masked for p in DRIFT_PREDICATES))

# COLUMN-OWNER: MIRRORS scripts/card_render_audit.js ROW_COLUMNS
# (card_render_audit.js:91) — this probe renders through that committed audit
# chain, so the audit owns the column list. Kept equal to it column-for-column
# and in its order; original_url (TITLE-TAIL-STRIP) was missing here too.
RENDER_COLS = ("title", "claim_text", "content_nature", "claims",
               "normalized_claims", "evidence_snippets", "evidence_sources",
               "source_candidates", "source_reliability_summary",
               "source_reliability_reason", "evidence_summary",
               "debug_summary", "evidence_extraction_summary",
               "contradiction_summary", "contradiction_checks",
               "missing_context", "verdict_label", "policy_alert_level",
               "original_url")

# ------------------------------------------------------------ Node driver ---
# Reuses the COMMITTED scripts/card_render_audit.js: vm-executes it with a
# trapped process.exit (the scan's own verdict is irrelevant here), then calls
# its renderRow per row. visibleText mirrors the audit's `visible` const.
NODE_DRIVER = r'''
const fs = require("fs"), path = require("path"), vm = require("vm");
const [ROOT, rowsPath, outPath] = process.argv.slice(2);
const AUDIT = path.join(ROOT, "scripts", "card_render_audit.js");
const src = fs.readFileSync(AUDIT, "utf8");
const fakeProcess = {
  argv: ["node", "card_render_audit.js", rowsPath], env: {},
  exit: (code) => { const e = new Error("audit exit " + code); e.__exit = code; throw e; },
  stdout: { write() {} }, stderr: { write() {} },
};
const ctx = vm.createContext({
  require, console: { log() {}, error() {} }, process: fakeProcess,
  __dirname: path.join(ROOT, "scripts"), __filename: AUDIT,
  JSON, Math, Date, String, Number, Array, Object, RegExp, Error, Buffer,
  setTimeout, clearTimeout, TextEncoder, TextDecoder, URL,
});
let execError = null;
try { vm.runInContext(src, ctx, { filename: AUDIT }); }
catch (e) { execError = e; }
if (typeof ctx.renderRow !== "function") {
  console.error("DRIVER FAIL: renderRow not defined after executing " + AUDIT
    + " - " + (execError && execError.message));
  process.exit(1);
}
// SCANNER-DECODE-FIX — KEEP IN SYNC with `decode` in
// scripts/card_render_audit.js (same map, same &amp;-last order, so the two
// instruments read identical reader text; that file is the authoritative
// copy and carries the full rationale).
const cp = (n) => (n >= 0 && n <= 0x10ffff ? String.fromCodePoint(n) : "�");
const decode = (s) => s
  .replace(/&#(\d{1,7});/g, (_, d) => cp(Number(d)))
  .replace(/&#x([0-9a-fA-F]{1,6});/g, (_, h) => cp(parseInt(h, 16)))
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
  .replace(/&amp;/g, "&");
const visibleText = (html) => decode(String(html)
  .replace(/<[^>]*>/g, "\n")).replace(/[ \t]+/g, " ");
const SECTION_NAMES = { hero: "핵심 문장", label: "상태 라벨", claims: "주장 목록",
  snippets: "근거 문장", sources: "근거 출처", cands: "공식 문서 후보",
  srs: "출처 요약", extract: "근거 추출 요약", contra: "대조 검토" };
const CAP = 3500;
const rows = JSON.parse(fs.readFileSync(rowsPath, "utf8")).rows;
const out = {};
for (const id of Object.keys(rows)) {
  let rendered;
  try { rendered = ctx.renderRow(id, rows[id]); }
  catch (e) { out[id] = { error: String(e && e.message) }; continue; }
  const parts = [];
  const face = visibleText(rendered.face || "").trim();
  if (face) parts.push("[카드 요약] " + face);
  parts.push("[공식 문서 후보 수] " + rendered.nCands + "건");
  for (const key of Object.keys(rendered.sections)) {
    let text = visibleText(rendered.sections[key] || "")
      .split("\n").map(s => s.trim()).filter(Boolean).join("\n");
    if (!text) continue;
    if (text.length > CAP) text = text.slice(0, CAP) + " …[이하 " + (text.length - CAP) + "자 생략]";
    parts.push("[" + (SECTION_NAMES[key] || key) + "]\n" + text);
  }
  out[id] = { text: parts.join("\n"), nCands: rendered.nCands };
}
fs.writeFileSync(outPath, JSON.stringify(out), "utf8");
'''


def pick_sample_ids(conn):
    """This week's top-10 reps + 6 older-week reps + MUST_INCLUDE, in a
    deterministic order both passes share."""
    with conn.cursor() as cur:
        cur.execute("SELECT week_start, payload_json FROM weekly_reports "
                    "ORDER BY id DESC")
        weeks = []
        seen_weeks = set()
        for week_start, payload_json in cur.fetchall():
            if week_start in seen_weeks:
                continue
            seen_weeks.add(week_start)
            weeks.append(json.loads(payload_json))
    ids = []
    for item in (weeks[0].get("top") if weeks else []) or []:
        rid = item.get("representative_analysis_id")
        if rid and rid not in ids:
            ids.append(rid)
    older = []
    for week in weeks[1:]:
        for item in week.get("top") or []:
            rid = item.get("representative_analysis_id")
            if rid and rid not in ids and rid not in older:
                older.append(rid)
    ids += older[:6]
    for rid in MUST_INCLUDE:
        if rid not in ids:
            ids.append(rid)
    return ids


def render_cards(conn, ids):
    """SELECT render columns, dump to a temp file, render via the embedded
    driver through the committed audit chain. Returns {id: reader_text}."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, %s FROM analysis_results WHERE id = ANY(%%s)"
                    % ", ".join(RENDER_COLS), (list(ids),))
        fetched = cur.fetchall()
    rows = {str(r[0]): {c: (None if v is None else str(v))
                        for c, v in zip(RENDER_COLS, r[1:])} for r in fetched}
    missing = [i for i in ids if str(i) not in rows]
    if missing:
        print("MISSING ROWS (not in analysis_results): %s" % missing)
    tmpdir = tempfile.mkdtemp(prefix="showcase_probe_")
    rows_path = os.path.join(tmpdir, "rows.json")
    driver_path = os.path.join(tmpdir, "driver.js")
    out_path = os.path.join(tmpdir, "rendered.json")
    with open(rows_path, "w", encoding="utf-8") as fh:
        json.dump({"_meta": {"max_id": max(ids)}, "rows": rows}, fh,
                  ensure_ascii=False)
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(NODE_DRIVER)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(["node", driver_path, root, rows_path, out_path],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    if proc.returncode != 0 or not os.path.exists(out_path):
        print("RENDER DRIVER FAILED (node required, as for the C8 gate): %s"
              % (proc.stderr or proc.stdout or "").strip()[-300:])
        raise SystemExit(2)
    with open(out_path, encoding="utf-8") as fh:
        rendered = json.load(fh)
    # AS-JUDGED-RECONSTRUCTION: pins verified BEFORE any strip is applied, so
    # a renamed product string exits loudly here rather than feeding a modern
    # card to the recall check (this runs before any API spend in main()).
    verify_as_judged_pins()
    cards = {}
    degraded = {}
    for rid in ids:
        entry = rendered.get(str(rid)) or {}
        if "error" in entry:
            print("RENDER ERROR on %s: %s" % (rid, entry["error"][:120]))
            continue
        text = entry.get("text") or ""
        text, hits = apply_as_judged(rid, text)
        zero = report_as_judged(rid, hits)
        if zero:
            degraded[rid] = zero
        cards[rid] = text
    return cards, degraded


def _extract_json(text):
    trimmed = text.strip()
    if trimmed.startswith("```"):
        trimmed = re.sub(r"^```[a-zA-Z]*\s*", "", trimmed)
        trimmed = re.sub(r"\s*```$", "", trimmed)
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start != -1 and end > start:
        return json.loads(trimmed[start:end + 1])
    return json.loads(trimmed)


def _dump_response(resp, text, tag, exc):
    kinds = ",".join(b.type for b in resp.content) or "NO CONTENT BLOCKS"
    print("PARSE FAILURE on %s: %s" % (tag, exc))
    print("  stop_reason=%s | %d content blocks [%s]"
          % (resp.stop_reason, len(resp.content), kinds))
    if resp.stop_reason == "max_tokens":
        print("  -> TRUNCATED at max_tokens=%d — raise MAX_TOKENS_PER_BATCH"
              % MAX_TOKENS_PER_BATCH)
    print("  raw text head: %r" % text[:400])
    raise SystemExit(2)


def run_reviewer(client, ordered_ids, cards, smoke=False):
    verdicts, tokens_in, tokens_out = {}, 0, 0
    batches = [ordered_ids[i:i + BATCH_SIZE]
               for i in range(0, len(ordered_ids), BATCH_SIZE)]
    fmt = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    for index, batch in enumerate(batches, start=1):
        rows = "\n\n".join("=== 카드 id=%s ===\n%s" % (rid, cards[rid])
                           for rid in batch)
        kwargs = dict(
            model=MODEL, max_tokens=MAX_TOKENS_PER_BATCH, thinking=THINKING,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": USER_TEMPLATE.format(n=len(batch), rows=rows)}])
        try:
            resp = client.messages.create(output_config=fmt, **kwargs)
        except TypeError:
            resp = client.messages.create(extra_body={"output_config": fmt},
                                          **kwargs)
        tag = "batch %d/%d" % (index, len(batches))
        if resp.stop_reason == "refusal":
            print("REVIEWER REQUEST REFUSED on %s (stop_reason=refusal)" % tag)
            raise SystemExit(2)
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            batch_verdicts = _extract_json(text)["verdicts"]
        except (ValueError, KeyError, TypeError) as exc:
            _dump_response(resp, text, tag, exc)
        if smoke and index == 1:
            print("SMOKE: %s stop_reason=%s, %d/%d verdicts — continuing"
                  % (tag, resp.stop_reason, len(batch_verdicts), len(batch)))
        for verdict in batch_verdicts:
            # normalize "카드 id=13977"-style echoes to the bare numeric id
            match = re.search(r"\d+", str(verdict["id"]))
            verdicts[match.group(0) if match else str(verdict["id"])] = verdict
        tokens_in += resp.usage.input_tokens
        tokens_out += resp.usage.output_tokens
    return verdicts, (tokens_in, tokens_out)


def flagged(v):
    return v and (v["genre"] or v["surface"] or v["consistency"])


def persist_run(tag, verdicts, usage):
    """Title-probe pattern: every verdict object from the pass, re-readable,
    so drift claims can always be re-checked after the fact."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "probe_card_run_%d.json" % tag)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"verdicts": verdicts,
                   "usage": {"in": usage[0], "out": usage[1]}},
                  fh, ensure_ascii=False, indent=1)
    return path


def load_cleared():
    """Hashes of notes a HUMAN read and cleared (the flag-and-hold ledger).
    The script only reads this file; the operator appends hashes to it."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "probe_card_drift_cleared.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return set(json.load(fh).get("cleared") or [])
    except (OSError, ValueError):
        return set()


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — run in the Render Worker Shell.")
        return 0

    import anthropic
    import psycopg

    # Detector vacuity controls — fail loudly BEFORE spending any API call.
    labels = load_ui_labels()
    old_q, new_q = drift_flags(LABEL_QUOTE_CONTROL, labels)
    ctrl_new = [drift_flags(c, labels)[1] for c in DRIFT_CONTROLS]
    if not (old_q and not new_q and all(ctrl_new)):
        print("VACUOUS DRIFT DETECTOR: label-quote control (old=%s,new=%s) or "
              "judgement controls %s no longer behave — fix the detector "
              "before trusting any drift count." % (old_q, new_q, ctrl_new))
        raise SystemExit(2)

    url = (os.environ["DATABASE_URL"]
           .replace("postgresql+psycopg://", "postgresql://")
           .replace("postgresql+psycopg2://", "postgresql://"))
    with psycopg.connect(url) as conn:
        ids = pick_sample_ids(conn)
        cards, degraded = render_cards(conn, ids)
    ordered_ids = [i for i in ids if i in cards]

    total_chars = sum(len(t) for t in cards.values())
    print("SHOWCASE-REVIEWER CARD PROBE — SELECT-only, %d cards rendered via "
          "the committed card_render_audit.js chain, %d chars total, model %s, "
          "2 passes × %d batches of ≤%d, thinking off"
          % (len(ordered_ids), total_chars, MODEL,
             (len(ordered_ids) + BATCH_SIZE - 1) // BATCH_SIZE, BATCH_SIZE))
    print("AS-JUDGED: 13977 (#13 stamp) + 13700 (#14 labels, #15 heading; "
          "#18 verified no-op) reconstructed to the 2026-07-29 human read — "
          "per-row line counts above; pins verified against main.js")
    print_known_gaps()
    print(AS_JUDGED_FIDELITY)
    print("REVIEWER PROMPT (verbatim):")
    print(SYSTEM_PROMPT)

    client = anthropic.Anthropic()
    run1, usage1 = run_reviewer(client, ordered_ids, cards, smoke=True)
    run2, usage2 = run_reviewer(client, ordered_ids, cards)
    paths = (persist_run(1, run1, usage1), persist_run(2, run2, usage2))
    print("VERDICTS PERSISTED: %s + %s (re-readable; missed-drift sweep reads "
          "these) | PROMPT CHANGED this revision (REVIEWER-GENRE-SCOPE-NARROW: "
          "event/forum/recruitment exempt from GENRE ONLY; surface and "
          "consistency still apply in full) — determinism RE-BASELINED, not "
          "comparable to prior runs"
          % (os.path.basename(paths[0]), os.path.basename(paths[1])))

    print("HEADLINE VERDICTS (run 1, verbatim):")
    missed = []
    for rid, expect, held in HEADLINE_EXPECTATIONS:
        v = run1.get(str(rid))
        if not v:
            print("  %s: NO VERDICT RETURNED" % rid)
            continue
        ok = bool(v.get(expect))
        if not ok:
            missed.append(rid)
        print("  %s [%s expected — %s]: %s | g=%s s=%s c=%s | note: %s"
              % (rid, expect, held, "CAUGHT" if ok else "MISSED",
                 v["genre"], v["surface"], v["consistency"], v["note"][:150]))
        # The gap rides ON the answer, never only above it.
        for direction, what, _why in AS_JUDGED_KNOWN_GAPS.get(rid, ()):
            print("      KNOWN GAP [%s]: %s" % (direction, what.split(".")[0]))
        if rid in degraded:
            print("      ★RECONSTRUCTION DEGRADED this run: %s matched nothing "
                  "— this answer is about a card further from the human read "
                  "than the line above claims." % "; ".join(degraded[rid]))
    print("  " + AS_JUDGED_FIDELITY)
    # ★MISSED was NOT loud on its own — it was one word inside a verdict line,
    # indistinguishable at a glance from the CAUGHT beside it, and it does not
    # move the exit code (3 is reserved for held drift notes, and the audit's R1
    # row reads that contract). A MISSED is one of the two exit-condition
    # triggers, so it says so here, at the moment it happens.
    print_missed_exit_condition(missed)
    known = {str(i) for i in MUST_INCLUDE}
    false_pos = [v for iid, v in run1.items()
                 if iid not in known and flagged(v)]
    print("FLAGS ON BELIEVED-CLEAN CARDS (run 1): %d of %d — classify before "
          "assuming noise (title layer's 8th defect started as an apparent FP)"
          % (len(false_pos), len(ordered_ids) - len(known)))
    for v in false_pos[:3]:
        print("  %s g=%s s=%s c=%s: %s"
              % (v["id"], v["genre"], v["surface"], v["consistency"],
                 v["note"][:110]))
    changed = [iid for iid in run1 if not run2.get(iid) or
               (run1[iid]["genre"], run1[iid]["surface"], run1[iid]["consistency"])
               != (run2[iid]["genre"], run2[iid]["surface"], run2[iid]["consistency"])]
    print("DETERMINISM: %d/%d cards changed verdict between runs%s"
          % (len(changed), len(ordered_ids),
             " (" + ", ".join(changed[:6]) + ")" if changed else ""))
    # TRUTH-DRIFT gate — FLAG-AND-HOLD: never auto-pass, never auto-fail.
    # Old detector counted for comparison only; NEW flags hold for a human
    # read unless the note's hash is in probe_card_drift_cleared.json.
    notes = [(pass_no, iid, v["note"]) for pass_no, run in ((1, run1), (2, run2))
             for iid, v in run.items() if v["note"]]
    old_n = sum(1 for _, _, n in notes if drift_flags(n, labels)[0])
    new_hits = [(p, iid, n) for p, iid, n in notes if drift_flags(n, labels)[1]]
    cleared = load_cleared()
    held = []
    print("TRUTH-DRIFT: old detector %d / new detector %d of %d non-empty notes"
          % (old_n, len(new_hits), len(notes)))
    for pass_no, iid, note in new_hits:
        digest = hashlib.sha256(note.encode("utf-8")).hexdigest()[:12]
        state = "CLEARED (read earlier)" if digest in cleared else "HOLD"
        if digest not in cleared:
            held.append(digest)
        print("  [%s %s] pass%d %s: \"%s\"" % (state, digest, pass_no, iid, note[:110]))
    if held:
        print("HOLD FOR HUMAN READ: %d note(s) above need a person's judgement. "
              "After reading, append each hash to scripts/"
              "probe_card_drift_cleared.json {\"cleared\": [...]} so it is not "
              "re-read next week. Exit 3 = held, NOT a layer verdict." % len(held))
    tokens_in = usage1[0] + usage2[0]
    tokens_out = usage1[1] + usage2[1]
    std = tokens_in / 1e6 * PRICE_STD[0] + tokens_out / 1e6 * PRICE_STD[1]
    intro = tokens_in / 1e6 * PRICE_INTRO[0] + tokens_out / 1e6 * PRICE_INTRO[1]
    per_item = tokens_in / 2.0 / max(1, len(ordered_ids))
    weekly = (std / 2.0) * (10.0 / max(1, len(ordered_ids)))
    print("COST: %d in + %d out tokens for BOTH passes = $%.3f std ($%.3f "
          "intro) | ~%d in-tokens/card" % (tokens_in, tokens_out, std, intro,
                                           per_item))
    print("  projected weekly (≈10 new detail cards, 1 pass) ≈ $%.3f std — "
          "vs the title layer's ~$0.013" % weekly)
    return 3 if held else 0


def selftest() -> int:
    """Offline proof that the drift machinery is not vacuous. NO DB, NO API,
    NO cost. Exercises the SAME functions the run uses:
      1. a strip whose target text is gone from the RENDER -> loud, and the
         degradation is carried, not just counted;
      2. the recorded known gaps print for both rows;
      3. the rename pins still exit 2 when a pinned string leaves main.js.
    Run: PYTHONPATH=. python scripts/showcase_reviewer_card_probe.py --selftest
    """
    print("=== SELFTEST 1 — a strip that matches nothing (REMOVAL) ===")
    # A modern-shaped card with every strip target ALREADY absent, i.e. what a
    # future product removal leaves behind.
    stripped_render = ("[공식 문서 후보]\n공식 출처 후보 88개\n"
                       "어떤 배지도 라벨도 남지 않은 렌더\n[대조 검토]\n검사한 주장\n2")
    failures = []
    for rid in (13977, 13700):
        text, hits = apply_as_judged(rid, stripped_render)
        zero = report_as_judged(rid, hits)
        if len(zero) != len(hits):
            failures.append("row %s: zero-hit detection missed an op" % rid)
        if text != stripped_render:
            failures.append("row %s: a no-op strip still altered the text" % rid)

    print("\n=== SELFTEST 2 — the recorded known gaps ===")
    print_known_gaps()
    print(AS_JUDGED_FIDELITY)
    for rid in (13977, 13700):
        if not AS_JUDGED_KNOWN_GAPS.get(rid):
            failures.append("row %s: no known gap recorded" % rid)

    if as_judged_gap_count() != 3:
        failures.append("expected 3 recorded gaps, found %d — update the "
                        "selftest deliberately, not incidentally"
                        % as_judged_gap_count())
    if as_judged_gap_count() >= AS_JUDGED_GAP_CEILING:
        failures.append("gap count already at the exit ceiling — the "
                        "reconstruction should be abandoned, not annotated")

    print("\n=== SELFTEST 2b — what happens AT the ceiling (simulated 4th gap) ===")
    simulated = {rid: tuple(entries)
                 for rid, entries in AS_JUDGED_KNOWN_GAPS.items()}
    simulated[13700] = simulated[13700] + (
        ("MISSING", "SIMULATED FOURTH GAP — selftest only, not a real finding.",
         "simulation"),)
    print_known_gaps(simulated)
    if as_judged_gap_count(simulated) != AS_JUDGED_GAP_CEILING:
        failures.append("simulated ceiling did not reach %d"
                        % AS_JUDGED_GAP_CEILING)

    print("\n=== SELFTEST 2c — what a MISSED prints ===")
    print_missed_exit_condition([13977])
    print("(no MISSED case:)")
    print_missed_exit_condition([])

    print("\n=== SELFTEST 3 — rename detection still fires ===")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "frontend", "scripts", "main.js"),
              encoding="utf-8") as fh:
        real_src = fh.read()
    try:
        verify_as_judged_pins(real_src)
        print("pins over the REAL main.js: all present (no exit) — correct")
    except SystemExit as exc:
        failures.append("pins fired against unmodified main.js (code %s)" % exc.code)
    renamed = real_src.replace(EXCL_LABEL, "주제가 달라 제외")
    if renamed == real_src:
        failures.append("could not simulate a rename: %r absent" % EXCL_LABEL)
    try:
        verify_as_judged_pins(renamed)
        failures.append("RENAME NOT DETECTED — the pin is vacuous")
    except SystemExit as exc:
        print("simulated rename of %r -> exit %s (loud, as designed)"
              % (EXCL_LABEL, exc.code))

    print("\n=== SELFTEST RESULT ===")
    if failures:
        for f in failures:
            print("FAIL: %s" % f)
        return 1
    print("PASS: zero-hit strips are loud and carried onto the verdict line, "
          "%d known gaps recorded for both rows and printed against the exit "
          "ceiling of %d, a MISSED prints the exit condition, rename pins still "
          "exit 2." % (as_judged_gap_count(), AS_JUDGED_GAP_CEILING))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
