# DEFECT LEDGER — every card-detail defect found, and its state
Built 2026-07-31 from: `git log -60` (bodies, not subjects), `scripts/card_render_audit.js`
zero/ceiling classes + `card_render_baselines.json`, `scripts/showcase_reviewer_card_probe.py`,
and the untracked `_*.md` scratch files. Inventory only — it records defects, it fixes none.

TRACKED ON PURPOSE, and one of exactly two exceptions to the `_*` scratch convention (the
other is `scripts/backlog_systemic_scan.py`, the probe that fed this file; both landed in
`373c7a81c`, 2026-07-31). It said "UNTRACKED FILE" here until 2026-08-06, which was false
from the day it was committed. The reason for the exception, so nobody helpfully undoes it:
the `_*.md` files beside it are single-session working notes and are meant to be disposable,
whereas this is the register they feed — the only place the cross-session state of every
entry below lives, and the thing that has to outlive the machine that produced it. **Do not
untrack it.**

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
| 67 | Two independent axes painted identically | `.card-watch.alert-*` and `.vt-*` carried byte-identical tint pairs (`#fff7ed`/`#c2410c`, `#fef2f2`/`#b91c1c`), so 경고 단계 and AI 검증 상태 read as one severity scale on the same card row — while the detail screen states 한쪽이 다른 쪽을 결정하지는 않습니다 | `main.css:2908-2909` vs `:2972-2974`; note at `main.js:6966` | 08-11 | **OPEN** — 2c neutralised the `.vt-*` side only; the alert axis was ruled out of scope, so the tints no longer collide but the alert badge still carries orange/red on an axis colour was never asked to encode | none — no test or gate pins either rule |

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
| 46 | `hero_restates_title` | 6.3% / **12.8%** (was 8.2%) | ceiling — see growth-watch entry below |
| 47 | `empty_section` | 0.0% / 0.0% | ceiling |
| 48 | `cand_tail` (roster length) | p90 70 / 96 | ceiling |

### Growth-watch events — a ceiling exceeded its baseline, was looked at, and was re-recorded

**`hero_restates_title`, latest500: 8.2% → 12.8%. Measured 2026-08-05 (corpus max_id=14856). NOT a defect.**

The first ceiling rise since the classifier that separates signal from disclosure was repaired on 08-04.

- **Old level** 8.2% (recorded 07-28 at max_id=14245). **New level** 12.8% (64/500). The old number was
  already stale before the alert fired: the previous 1000 rows (ids 13357–14356) were at 11.0%.
- **Cause: composition, not regression.** Rows whose *stored* title ends in an appended outlet name
  (`… - 뉴스1`, `… - go.seoul.co.kr`) went 3.3% of history → 15.7% (prev 1000) → 22.0% (latest500).
  Those rows fire at ~35% — they are aggregator/announcement stubs whose body yields no claim, so the
  hero falls back to the headline. Both sub-rates held flat throughout (tail ~35%, non-tail ~6.5%), and
  the pair reproduces every window: 7.4 / 11.0 / 12.8% against actual 7.1 / 11.0 / 12.8%. Concentrated in
  agency announcements whose headline already is the whole claim — statistics 41.9%, health 30.4%,
  finance 3.0%, SMB 0.0%.
- **Not a code change.** Nothing in the hero path moved. The 07-31 title-tail work (`7feb77e`, `12efcc1`,
  `94cc011`) is display-only — of 64 hits, **0** fire only against the raw title, so the strip
  manufactured none of them. The 08-05 export-label commit (`c1145ba`) is unrelated: `exportClaimText`
  appears in its diff only as unchanged context. Structurally, the scan renders *current* main.js over
  rows of *all* ingest dates, so a display change moves the whole curve and cannot produce a
  date-localised pattern; the pattern here is by ingest date, i.e. data.
- **Shape: drift, not a step.** By ingest day the rate runs 8.3, 24.6, 8.5, 9.0, 8.2, 7.9, 11.0, 6.2, 4.5,
  7.9, 11.2, 17.6, 13.5, 9.3, 21.8% — overdispersed (χ²=54.1, 14 df), 500-id blocks oscillating
  8.3 → 14.4 → 7.2 → 15.2%. The largest single day (07-22, 24.6%) predates every commit considered.
- **KNOWN LIMIT of the detector — recorded, deliberately not fixed (its own decision, own trade-offs):**
  it compares the hero against the **raw stored title** while the reader sees the **outlet-tail-stripped**
  one, so it **under-counts**. Ten further latest500 cards restate the title a reader actually sees
  without firing. The reader-facing rate is about **14.8%**, not the 12.8% recorded.
- **What would make it a defect again:** stratify first. Tail share still rising with both sub-rates flat
  = the same composition story, number merely stale. Either **sub-rate** rising (non-tail past ~6.5%,
  tail past ~35%) = hero selection has degraded, and that is a defect regardless of the headline number.
- Tolerance left at 4.5pp and the mod14 baseline left at 6.3% (it measured 7.1%, inside tolerance).
  Reason recorded in `scripts/card_render_baselines.json` under this class's `_reason`.

**`question_mojibake`, latest500: 4.0% → 18.6%. Measured 2026-09-04 (corpus max_id=17247). NOT a defect — CLOSED.**

- **Denominator ruled out first.** `latest500` is `id > max_id - 500`, so its denominator is **fixed
  at 500** in both readings. **13 → 93** matching rows in the same 500. The rate moved because the
  corpus content moved, not because the window did.
- **Measured cause: two official documents, entering and leaving the candidate pool.** By ingest day
  the rate runs 6.4, 5.9, 1.1 (08-23…08-25), then **19.0** on **2026-08-26**, **29.1** on 08-27, and
  22.1, 25.0, 26.7, 22.2, 25.9, 20.0 through **2026-09-02**. The step is exactly the arrival of an
  **FSS 감사인 지정제도 online-briefing notice** (167 occurrences) and an **FSS 보험회사 경영실적
  release** (118): `fss.or.kr` 285 occurrences, `korea.kr` 22. A step, not drift.
- **Not article text.** **456 of 461** occurrences sit in **`source_candidates`** — attached
  official-document text — against 1 each in `claim_text`, `claims`, `normalized_claims`,
  `debug_summary`, `contradiction_checks`. No host dominates: 500 rows over **226** hosts, the top
  host carrying 6 of 98 matching rows.
- **Zero of the 461 are genuine question marks.** None has a Korean interrogative ending (까/나/요/죠)
  before the mark; none has an interrogative word (왜/어떻게/무엇/누가/언제) within 90 characters.
  The shapes are **250** lost separators in repeated `noun?noun?noun` lists, **192** lost quotation
  brackets (closing `?` followed by a particle), **19** single `noun?noun`.
- **The substitution is upstream, not ours.** The corpus contains **zero U+FFFD**, and our only lossy
  steps — `article_extractor.py:162` and `text_utils.py:101`, both
  `decode(encoding, errors="replace")` — emit **U+FFFD**, never `?`. No lossy encode exists in the
  chain (every `.encode()` is UTF-8; `article_extractor.py:119` is strict on both sides and raises).
  In one document **U+00B7 survives** while the enclosing brackets became `?`, yet **U+3141 also
  survives** in the same text; **no single encoding produces that mix**, which points at a
  publisher-side HWP-to-text conversion.
- **Already receding on its own.** The rate had fallen to **5.3%** on **2026-09-03** as those two
  documents left the pool.
- **NO BASELINE IS TO BE RAISED FOR THIS.** The 4.0% / 1.9% figures in the table above stand
  unchanged. A baseline moved to accommodate an excursion is not a baseline.

**`bullet_char`, mod14: 60.7% → 66.9%. Measured 2026-09-04. NOT a defect — CLOSED. Arithmetic, not corpus change.**

- The mod14 **denominator grew from 1,016 to 1,230** (+214) and matches grew by **210**, so the added
  rows are **~98% `bullet_char`** — which is simply the recent-ingest saturation rate (`latest500` is
  **99.6%**, 498/500; 499 of 500 rows across 225 of 226 hosts, all at 100%). The window accumulated
  recent rows; the corpus did not change. Baseline unchanged.

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

### Misreadings worth the space — a premise was acted on, measured, and withdrawn

**"Twelve probes print bare, so they are one Korean character from crashing." Measured 2026-08-06. FALSE — and nine files were changed and reverted before it was.**

- **What was believed.** `scripts/` probes call `print()` directly rather than the shared
  `_console.p`, so on the operator's cp949 console a single em-dash would raise
  `UnicodeEncodeError`, kill the run mid-report, and exit non-zero — losing exactly the
  diagnostic output someone ran the probe by hand to read.
- **THE DISTINGUISHING TEST: the guard is at the STREAM, not at the call site.** Printing bare
  says nothing about crash-safety. **104 of 129** bare-printing scripts call
  `sys.stdout.reconfigure(..., errors=...)` at import — 102 `errors="replace"`, 2
  `errors="backslashreplace"` — and a stream with an error handler **cannot raise**. All twelve
  targets are in that set. Any future pass must test the stream, not count `print(`.
- **The count was also wrong by an order of magnitude:** 129 scripts and 3,761 bare `print()`
  calls, not 13. Of those calls 3,206 are single-positional and 246 zero-arg, but **151 pass
  `file=sys.stderr` and must never become `p()`** (it writes stdout), plus 152 multi-arg and 4
  `end=`/`sep=`. Only 47 of 128 files are wholly drop-in.
- **Work done and discarded, so no diff survives to find.** Nine files and **263 call sites**
  were migrated to the shared helper, then reverted in full once the stream measurement landed.
  Stdout *and* stderr verified byte-identical against pre-migration baselines, 9/9. The revert
  was on evidence, not on difficulty.
- **Where this belongs if it is ever done: a different 25 files** — the ones with no stdout
  reconfigure at all (`body1_*`, `body2_*`, `r2_*`, `rel1_diag`, `minwon_rising_probe`,
  `create_admin`, `m37_snippet_a/b`, `briefing_outage_dryrun`, `badge_overlap_probe`,
  `backfill_recon_probe`, `backfill_embedding_vectors`, `check_semantic_canary_env`, and 5 more).
  **None of the twelve is among them.** ★Unguarded is NOT the same as exposed: a script whose
  output is entirely ASCII cannot raise whatever its stream is. Which of the 25 actually print
  non-ASCII is the next MEASUREMENT, not a to-do.
- **Two smaller findings, recorded not acted on.** (1) `inst_source_audit.py:166`
  (`p = (publisher or "").strip()`), `source_box_audit.py:241` (`for p, c in …`) and
  `genuine_tighten_probe.py:201` (comprehension `p`) carry a local `p` that would shadow the
  shared import — they were never cleanly migratable regardless of the premise. (2)
  `match_instability_probe.py` and `sectionpage_500.py` reconfigure stdout to
  `encoding="ascii"` **by construction**, so Korean is escaped there no matter what prints it,
  and the shared helper's Korean-preserving degrade can never help them until that changes.

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

## K. Operational baseline and infrastructure-adjacent state

### PRE-CHANGE BASELINE — recorded 2026-08-26, before any Postgres plan change

**These figures exist so that a later Postgres plan change (and any cron instance resize)
can be evaluated against something measured rather than remembered.** Source: the ntfy
completion notifications for the two daily crons and the Render dashboard's own metrics —
**not a probe.** Nothing here was re-derived by querying the database; re-deriving it after
the change would be reconstruction, not history.

- **Daily collection** (`daily-collection`, 21:00 UTC / 06:00 KST).
  08-25: **85** new rows, **59 min**, peak memory **1.50 GiB**.
  08-26: **87** new rows, **60 min**, peak memory **1.71 GiB**.
  Cron instance memory ceiling **2 GB**; overrun window **85 min**. The 08-26 peak is 86% of
  the ceiling — the headroom, not the duration, is the number to watch.
- **Daily graph** (`daily-graph`, 23:00 UTC / ~08:00 KST).
  08-25: **84** new embeddings, **22 min**, lineage carried **1223** minted **4** merged_away **0**.
  08-26: **87** new embeddings, **22 min**, lineage carried **1227** minted **5** merged_away **0**.
  Vector `missing=0` on both days.
- **Postgres.** Instance memory sustained at **~90% of a 1 GB limit**; disk **~20% of 10 GB**.
  PITR runs daily **05:39–05:41**, retention **7 days**, and as of this date **no copy of the
  database exists outside the Render account**.
- **Corpus size.** **16,430** rows on the morning of 08-26; **16,517** at the time of the
  three-surface diagnosis later the same day.

*(The `daily-graph` cron was absent from `render.yaml` until 2026-08-26 — the file documented
two of the three crons, and `scripts/b2b_readiness_audit.py` parses its spine schedule from
that file. Adding the third block is documentation only; `render.yaml` is not a Blueprint and
the dashboard remains authoritative for every schedule and command.)*

### Open, measured, and NOT scheduled for repair

| # | Defect | What it costs | First | State | Gate |
|---|---|---|---|---|---|
| 67 | Query assembly is non-deterministic across processes | a past row cannot be replayed to the query that was actually sent | 08-26 | **OPEN — not scheduled** | — |
| 68 | `officialEvidenceInsufficientForExport` reads two columns the slim payload does not ship | the export guard is unusable on any card path, and would flatten every label to 사람 검토 대기 if called there | 08-28 | **OPEN — not scheduled** | — |
| 69 | `domain_classifier` fails OPEN on a provider error | a day's rows are routed to 기타-미분류 with nothing recording that no model was consulted | 08-28 | **OPEN — not scheduled** | — |
| 70 | `content_nature_classifier` fails OPEN the same way | same, into `mixed_or_unclear` | 08-28 | **OPEN — not scheduled** | — |
| 71 | `hot_topics` fails open to an empty keyword list | the day silently collects only the fixed query list | 08-28 | **OPEN — not scheduled** | — |
| 72 | Third, un-deduplicated ntfy notifier copy in `api_server.py` | latent recurrence of the three-day silent-send incident | 08-28 | **OPEN — not scheduled** (latent) | — |
| 73 | `z:title-outlet-tail` fires on 3 rows the strip cannot reach | the B2B readiness audit has been exit 1 since 08-26, so its exit code no longer gates a send | 08-26 | **OPEN — NOT SCHEDULED FOR REPAIR** | `z:title-outlet-tail` (the gate that is red) |
| 74 | Scan-window expiry: a row leaves C8's field of view about six days after ingest | a defect not caught inside that window becomes permanently invisible to this instrument | 09-04 | **OPEN — not scheduled** | — (this is the gate's own limit) |

- **The mechanism.** `source_retrieval_agent._token_variants` returns a **set**, so iteration
  order follows `PYTHONHASHSEED` and therefore differs between processes. Which terms survive
  the **6-slot and 8-slot caps** changes with that order, so the same stored claim can assemble
  a different query in two different runs.
- **Measured.** Re-invoking the committed assembly over stored text reproduced the stored query
  **byte-for-byte on 2,975 of 4,463** location-bearing claims — **67%**.
- **Contributing cause, not the whole of it.** The chrome-strip and period-gate backfills
  rewrote stored claim text *after* those queries were built, so part of the 33% is text that
  has since changed rather than ordering alone. The set-iteration defect stands independently
  of the backfills.
- **Consequence, stated plainly:** re-running a past row does **not** reproduce the query that
  was actually sent. Any retrieval post-mortem that assumes it does is reasoning about a query
  the system may never have issued.

- **The mechanism.** `officialEvidenceInsufficientForExport` (`main.js:8511-8534`) builds its
  `sources` array from `source_candidates` and `evidence_sources`, then requires the best
  `semanticScore` across them to reach **30**. Neither column is in `_SLIM_LIST_COLUMNS`
  (`postgres_storage.py:1434-1446`) — `source_candidates` was dropped by **PERF-4**,
  `evidence_sources` by **PERF-2**. On any slim payload the array is empty, `semanticScore` is
  **0**, and the final `semanticScore < 30` limb is **unconditionally true**, so the guard
  returns 사람 검토 대기 for every row regardless of its evidence.
- **Measured.** Rendering the committed chain over the 14 home pools, the guard fired on
  **650 of 650** rows. On the two rows whose stored `has_genuine_official_support` is true
  (**16438**, **16000**) the limb-by-limb comparison isolates the cause exactly: every other
  limb agrees between the slim and full shapes, and only `limb_semantic` differs
  (`nSources` 0 vs 71/26, `semantic` 0 vs 97).
- **Not a live defect today.** The guard is only ever invoked on full rows — the detail
  header, which fetches `GET /history/{id}`. Nothing currently calls it from a card path, and
  the card badge was deliberately gated on `card.hasGenuineOfficial` instead (CARD-BADGE-HONESTY)
  rather than on this guard, for exactly this reason.
- **Consequence, stated plainly:** the guard cannot be reused on any surface fed by the slim
  reader, and a future caller that reaches for it there would silently flatten every label
  rather than fail. Repair would mean conditioning the limb on the presence of the source
  arrays — a change to the guard itself, deliberately deferred.

- **69/70/71 — the mechanism.** Three unattended paths on the daily-collection chain
  swallow a provider error and return an in-band value that is **byte-identical to a
  real answer**. `domain_classifier` returns `FALLBACK_LABEL = "기타-미분류"`
  (`domain_classifier.py:45`, returned at `:189`); `content_nature_classifier` returns
  `FALLBACK_LABEL = "mixed_or_unclear"` (`content_nature_classifier.py:43`);
  `hot_topics` returns `[]` (`hot_topics.py:490`), after which `build_query_list` yields
  only the fixed queries. All three are reached from `main.analyze_pipeline` /
  `scheduler.py` with no operator present.
- **69/70/71 — what it does NOT cost.** No verdict is corrupted: `domain` and
  `content_nature` feed **no verdict field** (`main.py:1500`, `:1513` say so in the
  source), `truth_claim` and `operator_review_required` are untouched, and a narrowed
  query list collects fewer rows rather than wrong ones.
- **69/70/71 — what it does cost.** An Anthropic credit outage would route a whole day
  into those fallbacks with **no marking on the row**. Nothing distinguishes "the model
  said 미분류" from "the model was never asked"; the only surviving evidence is a
  `logger.warning` line in Render's log retention window. `hot_topics` is visible solely
  as a lower 신규 N건 in the collection alert, with no attribution.
- **72 — the mechanism.** `api_server.py:246-265` (`_honesty_notify`) is a THIRD ntfy
  sender, never folded into `weekly_spine.notify` the way `queue_topic_alerts` was by
  NOTIFY-DEDUP. It makes **one** attempt (no retry), times out at 5s, reports failure
  only to `logger.warning`, and places the title in the `Title` header **raw** — it does
  not go through `_header_title` (`weekly_spine.py:213-236`), the RFC-2047 encoder added
  because a Korean header made urllib raise and a service logged nothing for three days.
- **72 — latent, not live.** It sits on **no cron path**, and both call sites
  (`api_server.py:346`, `:372`) pass ASCII titles, so the encoder gap cannot fire today.
  Recorded because it is the same shape as the incident, one Korean title away from
  repeating it, and because a reader looking for "the notifier" will find two correct
  copies and this one.
- **73 — what fires.** `scripts/card_render_audit.js` raises class `title-outlet-tail`
  on **3 rows — 15511, 16511, 16596** — all from one outlet, `ktin.net` /
  경인투데이뉴스. **16511 and 16596 are the same article**, `www.ktin.net/63717601`
  under `http` and `https`, collected a day apart. C8 in `scripts/b2b_readiness_audit.py:1494`
  turns red on any non-zero exit from the scan, so the audit has been **exit 1 since 2026-08-26**.
- **73 — the string.** The stored title carries the outlet name **twice** — once glued after a
  colon, once as a spaced-dash tail. `stripVerifiedOutletTail` (`main.js:882-913`) anchors on
  `/ - ([^-]{2,40})$/` and removes **only the dash copy**; the colon-glued copy survives to the card.
- **73 — COVERAGE, which is the whole of it.** The scan renders only `id % 14 === 0` or
  `id > (max_id - 500)`. **870** stored titles match the audit's tail regex; **159 (18.3%)** fall
  inside that window, **711 (81.7%)** are never rendered by C8. Rendering all 870 fires the class on
  the same 3 rows — of which **15511 is outside the window and has never been seen by C8**.
  Repairing 16511 and 16596 would turn the audit green while 15511 still renders the same string:
  **a change to what the instrument sees, not to the defect.**
- **73 — why it went red when it did.** 16511 was in-window and on screen for a full day on
  2026-08-25 with the gate green. The class needs the evidence map to hold **>=2 same-apex rows**
  (`main.js:904-910`), so one row alone reports nothing. **The gate measures whether the string
  appears twice, not whether it appears.**
- **73 — precedent.** Commit `5abde2ac45` (2026-08-20) introduced this machinery and **explicitly
  declined this shape**: an outlet name with no dash before it stays, because detecting it would need
  a registry of outlet names this project does not have and should not guess at. The colon-glued copy
  is exactly that shape; it reaches the class today only because a second dash-separated copy supplies
  the tail context. This is a **different class** from the one commit `bf217583ad` declined
  (propositionless institution-name titles, 0.04%).
- **73 — decision and why: NOT REPAIRED.** A stored-title backfill would **move the graph** —
  `build_embed_text(title, claim)` is the embedding cache key, so a rewritten title misses its cached
  vector, re-embeds at the next build, and cluster membership and `stable_id` lineage can shift.
  Moving lineage for 3 rows is out of proportion. A render-time strip would fix only rows carrying a
  **doubled** name and leave single colon-glued names untouched, and widening the anchor is the case
  `5abde2ac45` refused. Narrowing the check is forbidden. The rows are **visually untidy; they do not
  state anything false to a reader.**
- **73 — OPERATING RULE WHILE THIS STANDS — SUPERSEDED 2026-09-04. Kept verbatim; this register
  does not rewrite its own records.** The audit's **exit code is not the send gate**.
  Before a B2B send, run the audit and confirm **by eye** that the only FAIL line is
  C8 `title-outlet-tail` and the only ids are **15511, 16511, 16596**. Any other class, or any other
  id, **stops the send**. — *That instruction now has no target. Why, and what replaces it, is in the
  three bullets below.*
- **73 — WHY IT WAS SUPERSEDED: the instrument stopped seeing it. Measured 2026-09-04 (max_id 17247).**
  All three ids now fall **outside both scan windows**. `id % 14` gives **13, 5, 6** — none is 0 — and
  all three are `<= max_id - 500` (**16747**), so none is in `latest500` (**16748–17247**). The audit
  therefore exits **0**, with C8 reported as a **WARN** (`zero-classes clean=true`) and the three ids
  absent from the output entirely. The send gate was written to confirm a FAIL that can no longer occur.
- **73 — THE DEFECT IS NOT REPAIRED. Measured 2026-09-04.** Nothing was fixed; the rows left the
  window. The three ids were rendered through the **committed** chain with the window forced to cover
  them: `RENDER-SCAN FAIL: ZERO-CLASS REGRESSION [latest500] title-outlet-tail: 3 row(s) e.g. ids
  15511,16511,16596`, `zero-classes clean=false`, **EXIT=1**, 3 rows. The **stored titles are
  unchanged** — `국가데이터처… :경인투데이뉴스 - 경인투데이뉴스` on all three. The decision recorded
  above (NOT REPAIRED) still stands and is unaffected by this; only the observability changed.
- **73 — OPERATING RULE IN FORCE FROM 2026-09-04.** **The audit can no longer observe this defect.**
  Do not look for it in the audit output, and do not read C8 exit 0 as evidence that it is gone. To
  check it, render the three ids **directly** through `scripts/card_render_audit.js` with the window
  forced to cover them: select ids 15511, 16511, 16596 with the `RENDER_COLS` list
  (`b2b_readiness_audit.py:1405-1413`), write them as the scanner's input JSON, and set
  `_meta.max_id` low enough that the scanner's own cut `id > max_id - 500` reaches 15511 (**16010**
  works; the render chain itself is untouched). Class still firing = unchanged. This is the check;
  the audit's exit code is not.
- **73 — re-open condition.** A different class appears, or an id outside that set appears, or the
  pattern spreads beyond `ktin.net`.

- **74 — the mechanism.** `scripts/card_render_audit.js:2145` builds its two windows as
  `id % 14 === 0` **OR** `id > (max_id - 500)`. The first is permanent and covers exactly 1 row in
  14; the second is a **sliding** window of fixed size.
- **74 — the headline fraction is NOT the issue.** Aggregate coverage is stable: **9.9%** at
  max_id 16778 (1,661 of 16,773 rows) and **9.8%** at max_id 17247 (1,695 of 17,242). It decays only
  toward the `mod14` floor of 1/14 = **7.14%**, so watching the percentage would show nothing.
- **74 — the issue is per-row.** A row that is **not** a multiple of 14 is scanned **only** while it
  sits within 500 of `max_id`. At roughly **80 new rows per day** that is about **six days**. After
  that, **13 of every 14 rows are never scanned again**. A defect introduced and not caught inside
  that window becomes **permanently invisible to this instrument** — not fixed, not reported, not
  observable.
- **74 — first observed instance: ledger 73.** Ids 15511, 16511 and 16596 aged out of both windows
  between max_id 16778 and 17247. The class still fires on all three under a forced window (see the
  73 bullets above); the audit stopped reporting it and turned green. Recorded, **not scheduled** —
  every available remedy (an always-scan id list, a hash-stable sample, a larger window) is a change
  to the instrument, and no such change is being made on the strength of one observation.
