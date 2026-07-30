# DEFECT LEDGER — every card-detail defect found, and its state
Built 2026-07-31 from: `git log -60` (bodies, not subjects), `scripts/card_render_audit.js`
zero/ceiling classes + `card_render_baselines.json`, `scripts/showcase_reviewer_card_probe.py`,
and the untracked `_*.md` scratch files. UNTRACKED FILE — inventory only, fixes nothing.

State key: **FIXED** · **OPEN** · **WONTFIX** (deliberately not fixed) · **NOT-A-DEFECT** · **UNKNOWN**
`R` = recurred (found, fixed, found again on another surface).

## A. Machine text leaking to readers  — class recurred 4× (the commit says so itself)
| # | Defect | Reader saw | Where | First | State | Gate |
|---|---|---|---|---|---|---|
| 1 | English exclusion sentence | "Official document excluded from verification" on 45% of cards | evidence_summary | 07-26 | FIXED | `z:english-reason-sentence` |
| 2 | Comma-tail launderer gap | English tail after a comma | launderer | 07-26 | FIXED | folded into launderer |
| 3 | Retrieval enums as collection method | `policy_briefing_api` | debug/collection | 07-26 | FIXED | `z:machine-enum` |
| 4 | Evidence strength in English | "품질 strong" on **every** card | strength label | 07-26 | FIXED | `z:english-label` |
| 5 | Claim numbering in English | "claim #2" | claim list | 07-26 | FIXED | `z:english-label` |
| 6 | Conflict reasons as raw enums | `explicit_conflict` | contradiction | 07-26 | FIXED | `z:machine-enum` |
| 7 | Escaped surrogates | `󰊱` | document bullets | 07-26 | FIXED | `z:literal-unicode-escape` |
| 8 | Stored HTML as visible text | `<p style=…>` | body fields | 07-26 | FIXED | `z:html-markup-as-text` |
| 9 | 4 raw enums the sweep missed **R** | snake_case ids on ~4% of cards | several | 07-28 | FIXED | `z:snake-case-identifier` |
| 10 | 3 bare English words, fossil row | English words | 1 legacy row | 07-28 | **WONTFIX** | — (mapping common words would corrupt ~500 real rows) |

## B. Claim text cut wrong on the card face
| # | Defect | Reader saw | Where | First | State | Gate |
|---|---|---|---|---|---|---|
| 11 | Unmarked 115-char slice | 58% of faces cut, 33% mid-word, no ellipsis | card face | 07-28 | FIXED | `z:card-face-truncation` |
| 12 | Cut on a hanging particle **R** | "지원 규모를…" on 2,198 faces | card face | 07-31 | FIXED | **NO GATE** (the class checks marker+boundary; a particle sits *on* a boundary) |

## C. Official-evidence framing — one class found on 5 successive surfaces (**R** ×4)
| # | Defect | Reader saw | Where | First | State | Gate |
|---|---|---|---|---|---|---|
| 13 | False official confirmation on period mismatch | 2021 doc confirming a 2026 claim | reliability | 07-26 | FIXED | stamp + FIELD-VALUE-GUARD |
| 14 | Off-topic doc listed as candidate evidence | crypto approval under an emissions claim, with a score, 9/17 cards | candidate roster | 07-29 | FIXED | exclusion label (predicate = stored verdict) |
| 15 | Roster heading implied evidence **R** | "41 candidate sources" | section heading | 07-29 | FIXED | count-split heading |
| 16 | Unrelated doc asserted as rebuttal **R** | youth programme "rebutting" a library claim; "confirmed contradiction" above a hedge | contradiction | 07-30 | FIXED | same predicate, joined on title |
| 17 | Count contradicted the label **R** | "excluded" while the line above counted it as a possible rebuttal | contradiction summary | 07-30 | FIXED | reconciling sentence |
| 18 | Well-evidenced card said nothing **R** | 41 fetched-and-unmatched docs unmentioned on a *supported* card | roster gate | 07-30 | FIXED | gate removed; predicate is per-candidate |
| 19 | 13977 five-year date gap | 2021년 11월 release judged in a 2026 window | selection | 07-29 | FIXED (stamped) | MATCHER-GUARD chip |
| 20 | 13700: 88 unrelated official documents | irrelevant doc roster | selection | 07-29 | **WONTFIX** | only lever is a token-overlap threshold — permanently prohibited |

## D. Outlet tail in the headline — class recurred 2× within 24h
| # | Defect | Reader saw | Where | First | State | Gate |
|---|---|---|---|---|---|---|
| 21 | Outlet suffix in titles | "… - 뉴스1", "… - v.daum.net" on 419 titles, click depth 0 | card title | 07-31 | FIXED | `z:title-outlet-tail` |
| 22 | Tail survived in 8 other sections **R** | tail in 카드 요약/근거 문장/대조 검토/… on 36 of 39 rows | whole card | 07-31 | FIXED | `z:title-outlet-tail` (widened to joined text + SURFACE RULE) |
| 23 | Home vs detail disagreed **R** | home clean, detail still "- 뉴스1" | detail header | 07-31 | FIXED | same class (already scanned it) |
| 24 | Summaries appeared blank | no summary paragraph on 2 cards | card face | 07-31 | **NOT-A-DEFECT** — the summary was the headline verbatim; a long-standing collapse rule finally applied (8 rows already behaved so) | `z:summary-emptied-by-strip` |

## E. Layout / paint / page-level
| # | Defect | Reader saw | Where | First | State | Gate |
|---|---|---|---|---|---|---|
| 25 | Hero flicker, whole grid repainted | 2 hero cards → 1 after 240ms, titles jump to grid | home feed | 07-30 | FIXED | **NO GATE** |
| 26 | Same card twice on one page | 7 of 58 cards in both grid and domain section | home page | 07-30 | FIXED | **NO GATE** |
| 27 | Hero summary clipped mid-phrase | "…수료증이…" | CSS `-webkit-line-clamp:4`, `main.css:2591` | 07-31 | **OPEN** | none |
| 28 | Sidebar "Top 5" shows 4 | ranks 1,2,4,5 | `/api/trending` row with null representative | 07-31 | **OPEN** | none |

## F. Weekly / claim pages (the pages the outreach emails point at)
| # | Defect | Reader saw | Where | First | State | Gate |
|---|---|---|---|---|---|---|
| 29 | Heading promised "this week" but sorted all-time | wrong scope | weekly | 07-28 | FIXED | — |
| 30 | Bracket prefix in title **R** | "[금융 HOT 뉴스] …" | weekly + claim | 07-28 | FIXED | — (claim page fixed same day; weekly re-found) |
| 31 | Raw UTC timestamp **R** | "2026-07-26 19:18 UTC" | weekly | 07-28 | FIXED | — |
| 32 | Week appeared in its own archive list | self-reference | weekly | 07-28 | FIXED | — |
| 33 | Claim page titled with a broadcast segment | "[대담한K]" as the claim's name | claim | 07-28 | FIXED | — |
| 34 | All-time count beside a one-day window | "78개 매체 · 07-20 → 07-20" | weekly | 07-28 | FIXED | — |
| 35 | Child-homicide story ranked 6th | a court story on a policy-circulation page | weekly | 07-28 | FIXED | WEEKLY-CONTENT-GUARD (selection) |
| 36 | Crime story the domain classifier *did* label | same shape, passes the guard | weekly | 07-28 | **OPEN** (stated limit) | none |

## G. Content damage in stored source text — not curable by display
| # | Defect | Reader saw | Where | First | State | Gate |
|---|---|---|---|---|---|---|
| 37 | Raw clocks inside claim text | "입력 2026-07-14 10:47:59 수정…" | 카드 요약 / 주장 목록 / 공식 문서 후보 | 07-31 | **OPEN** (deliberately deferred this milestone) | none |
| 38 | Broken spacing / merged place names | "7 월 1 일", "전남광주통합특별시" | evidence excerpts, 1–2 clicks deep | 07-30 | **WONTFIX** (source/PDF extraction damage; repair needs re-analysis) | none |
| 39 | Deictic body-reference claim ("이같이 결정했다") | claim that refers to text not shown | claim_text | ≤07-27 | **WONTFIX** (re-analysis rejected) | — |
| 40 | Title-echo template stored as claim | claim == headline | claim_text | ≤07-27 | **WONTFIX** | ceiling `hero_restates_title` |
| 41 | Adjacent same-topic cards | "why is this here twice" | feed | 07-30 | **WONTFIX** — clustering would over-merge two different quarters; grouping cannot be labelled honestly | — |

## H. Ingest ceilings — accepted, watched for growth (all WONTFIX by design)
| # | Class | Baseline (mod14 / latest500) | Gate |
|---|---|---|---|
| 42 | `bullet_char` (■ ▣ ① furniture) | 60.7% / 98.6% | ceiling |
| 43 | `question_mojibake` | 1.9% / 4.0% | ceiling |
| 44 | `sentence_join` | 0.7% / 0.2% | ceiling |
| 45 | `hero_digit_start` | 0.2% / 0.8% | ceiling |
| 46 | `hero_restates_title` | 6.3% / 8.2% | ceiling |
| 47 | `empty_section` | 0.0% / 0.0% | ceiling |
| 48 | `cand_tail` (roster length) | p90 70 / 96 | ceiling |

## I. NOT A DEFECT — instrument error or misreading
| # | Reported as | Truth | First |
|---|---|---|---|
| 49 | HTML entities leaking on 3 rows | scanner decoded the bare numeric form but not the zero-padded one `escapeHtml` emits; readers always saw a correct apostrophe | 07-29 |
| 50 | Badge broken/overlapping | artifact of a 67%-zoom capture; it is a clean pill | 07-29 |
| 51 | Card page 35,000px long | artifact of expand-all; the collapsed reader view is 2,400px | 07-29 |
| 52 | Candidate roster overwhelms readers | collapsed view shows one count line + 6 rows behind an expander | 07-29 |
| 53 | Reviewer flagged event/recruitment coverage | that genre *is* policy circulation — this product exists to measure it | 07-30 |
| 54 | Blank summaries (#24) | the collapse rule working correctly | 07-31 |
| 55 | Backlog scan: bracket-prefix on 213/213 rows | the probe's own `[section]` markers | 07-31 |
| 56 | Backlog scan: spacing defect on 42 rows | regex matched across a newline between a date and the next section | 07-31 |
| 57 | Backlog scan: self-reference 0 hits | detector searched `[반박 검사]`, a label that does not exist (real one is `대조 검토`) | 07-31 |
| 58 | Tail derivation via frequency (3 attempts) | "rarely ends a sentence" selects almost every Korean noun | 07-31 |

## J. Known but unverified — UNKNOWN state
| # | Item | Source | What would settle it |
|---|---|---|---|
| 59 | Legend advertises a blue "공식자료 참고" mark no card renders | `_design_audit_day5.md:57` | render the legend + grep the card for that mark |
| 60 | Legend says "검증 완료", stronger than the product's own label | `_design_audit_day5.md:53` | read `template.html:348` against the shipped verdict vocabulary |
| 61 | Two different Korean strings for `draft_verified` | `_design_audit_day5.md:45` | diff `main.js:300` vs `:777` |
| 62 | Supportive-label guard applied on card but not detail | `_design_audit_day5.md:49` | render both surfaces for one supported row |
| 63 | `<summary>` with `display:flex` (forbidden) | `_design_audit_day5.md:29` | inspect `main.css:3973` in a browser |
| 64 | Force-select path bypasses the title reject (opinion/obituary can become the analysis target) | `_fable_engine_health_day12.md:21` | replay `_force_select_best` on rows where every candidate was rejected |
| 65 | 3rd notifier copy (topic-alert) still cannot send Korean titles | `ALERT-FIX` body, explicitly deferred | send one and read it back |
| 66 | Self-referential 대조 검토 (card's own headline as the compared document) — 38/153 sampled, 2 clicks deep | backlog scan 07-31 | check whether the labelling work already covers it |
