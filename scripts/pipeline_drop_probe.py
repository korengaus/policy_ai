# PIPELINE-DROP-PROBE — THROWAWAY read-only trace of WHERE a selected, on-topic PB
# candidate disappears between selection and the stored row.
#
# DB SELECT-only. Network = the EXISTING PB provider's korea.kr/data.go.kr API ONLY
# (reused). In-process re-runs are PURE/READ-ONLY scoring/selection/shaping functions.
# Persists NOTHING. No LLM, no crawl beyond PB. No code/verdict/provider/env change.
#
# THE CONTRADICTION
# -----------------
# select_vs_window_probe (lookback=30, max_releases=20) found 6 seed rows where the
# on-topic PB doc was INSIDE the production window AND ranked within max_releases
# (REACHED-BUT-OTHER). Yet retrieval_recall_probe found those STORED rows carry NO
# policy_briefing marker. So either selection never actually ran that way at analysis
# time, or a candidate was dropped before persistence. This probe localizes it.
#
# ★★ KEY HONESTY: faithful vs reconstructed ★★
# STAGE 0 (stored-row forensics, ZERO network) is FAITHFUL — it reads what ACTUALLY
# happened in the original run. STAGES 1-4 (network re-run) reconstruct "what WOULD
# happen NOW under the CURRENT lookback", which may DIFFER from the lookback live
# when the row was analyzed (code default is 3; select_vs_window used 30). So a
# doc "REACHED" under lookback=30 may simply have been OUTSIDE the analysis-time
# window. STAGE 0 is therefore the DECISIVE measurement; stages 1-4 are a
# comparison. STAGE 5 (cap/dedup/storage over the FULL candidate pool: crawl+PB+
# FSS+law) is NOT faithfully reproducible here (the probe has no crawl lane); it is
# reported from CODE FACTS, not re-run.
#
# CODE FACTS (verified, main.py): PB candidates are appended to source_candidates at
# main.py:733 (AFTER enrich at :710), resolve_official_evidence at :769,
# evaluate_source_candidates at :773. There is NO cap/dedup of source_candidates
# between PB injection and persistence; evidence_extraction_agent caps only display
# snippets ([:2]), not source_candidates. So an injected PB marker SHOULD survive to
# the stored row -> a post-injection "drop" is not expected from the code, which is
# why STAGE 0 (was PB even enabled / did it select anything at analysis time) is the
# prime suspect.

import os
import sys
import json
import time
import math
import collections
from datetime import datetime, timedelta

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import config
from text_utils import sanitize_text
from official_evidence_resolution import (
    resolve_official_evidence,
    _classify_official_evidence,
    _claim_text,
    _PRIMARY_DOCUMENT_MARKER_FIELDS,
)
# --- PB provider REAL primitives (reused read-only; NOT reimplemented) --------
from providers.policy_briefing import (
    get_document_provider,
    _select_documents,
    _claim_tokens,
    _doc_tokens,
    to_official_source_candidates,
    date_window,
    DEFAULT_NUM_OF_ROWS,
    DATE_WINDOW_DAYS,
)


# ---------------------------------------------------------------------------
# SEED ROWS — the REACHED-BUT-OTHER rows from select_vs_window (operator-editable).
# row_id -> on-topic PB title fragment (to detect THAT doc).
# ---------------------------------------------------------------------------
SEED_ROWS = {
    68: "국민성장펀드",
    70: "포용적 금융 대전환",
    82: "실손24",
    83: "성장기업발굴협의체",
    85: "퓨리오사",
    93: "상장폐지 개혁방안",
}

PB_MARKER = "policy_briefing_news_item_id"
LAW_MARKER = "national_law_mst"
FSS_MARKER = "fss_bodo_content_id"
OFFICIAL_TYPES = {"official_government", "public_institution"}

NUM_OF_ROWS = DEFAULT_NUM_OF_ROWS
SLEEP_SECONDS = 0.5
MAX_WINDOW_FETCHES = 400
MEDIUM_BAR = 55
STRONG_BAR = 75
BODY_MIN = 300
CLAIM_PREVIEW = 80


def _j(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def _is_dict(c):
    return isinstance(c, dict)


def _marker_counts(cands):
    pb = sum(1 for c in cands if _is_dict(c) and PB_MARKER in c)
    law = sum(1 for c in cands if _is_dict(c) and LAW_MARKER in c)
    fss = sum(1 for c in cands if _is_dict(c) and FSS_MARKER in c)
    off = sum(1 for c in cands if _is_dict(c) and c.get("source_type") in OFFICIAL_TYPES)
    return pb, law, fss, off


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe needs the DB (Worker Shell or local with $env:).")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    seed_ids = sorted(SEED_ROWS.keys())
    rows_by_id = {}
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, query, source_candidates, normalized_claims, debug_summary "
            "FROM analysis_results WHERE id = ANY(%s) ORDER BY id",
            (seed_ids,),
        )
        for rid, created_at, query, sc, nc, dbg in cur.fetchall():
            rows_by_id[int(rid)] = (created_at, query, _j(sc) or [], _j(nc) or [], _j(dbg) or {})

    lookback = config.policy_briefing_lookback_days()
    prod_max = config.policy_briefing_max_releases()
    windows = max(1, math.ceil(lookback / DATE_WINDOW_DAYS))

    print("PIPELINE-DROP-PROBE — read-only: where does a selected on-topic PB candidate disappear?")
    print("  STAGE 0 (stored forensics, ZERO network) = FAITHFUL (what actually happened).")
    print("  STAGES 1-4 (PB re-run) = RECONSTRUCTION under CURRENT lookback=%d (may differ from"
          " analysis-time lookback; code default is 3). STAGE 5 = code-facts only (not re-run)." % lookback)
    print("  instrumented real functions: get_document_provider/fetch_press_releases, _select_documents,")
    print("    to_official_source_candidates, resolve_official_evidence, _classify_official_evidence.")
    print("  config: lookback=%d -> windows=ceil(%d/%d)=%d (anchored at each row's created_at); max_releases=%d"
          % (lookback, lookback, DATE_WINDOW_DAYS, windows, prod_max))
    print("  PB is a _PRIMARY_DOCUMENT_MARKER_FIELDS member? %s"
          % (PB_MARKER in set(_PRIMARY_DOCUMENT_MARKER_FIELDS)))
    print("  ★ NOT-FAITHFULLY-REPRODUCIBLE: STAGE 1 window depends on analysis-time lookback (unknown;")
    print("    using current config); STAGE 5 cap/dedup needs the FULL candidate pool (crawl+PB+FSS+law)")
    print("    which this probe does not reconstruct -> reported from code facts, not re-run.")
    print("  seed rows: %s" % seed_ids)
    print()

    # =====================================================================
    # STAGE 0 — stored-row forensics (FAITHFUL, zero network). The decider.
    # =====================================================================
    print("=== STAGE 0 — stored-row forensics (FAITHFUL) ===")
    stage0_verdicts = collections.Counter()
    for rid in seed_ids:
        row = rows_by_id.get(rid)
        print("-" * 78)
        if row is None:
            print("  row=%s NOT FOUND" % rid)
            stage0_verdicts["NOT_FOUND"] += 1
            continue
        created_at, query, cands, claims, dbg = row
        pb, law, fss, off = _marker_counts(cands)
        pb_count_present = "policy_briefing_count" in dbg
        pb_count_val = dbg.get("policy_briefing_count")
        claim0 = _claim_text(claims[0]) if (claims and _is_dict(claims[0])) else ""
        print("  row=%s  created=%s" % (rid, str(created_at)[:10]))
        print("    claim: %s" % ((claim0 or "(empty)")[:CLAIM_PREVIEW]))
        print("    STORED source_candidates markers: PB=%d law=%d FSS=%d  official_type=%d  total=%d"
              % (pb, law, fss, off, len(cands)))
        print("    debug_summary.policy_briefing_count present=%s value=%s"
              % (pb_count_present, pb_count_val))
        # decide STAGE 0 verdict
        if pb > 0:
            verdict = "PB MARKER PRESENT in stored row (retrieval_recall may have missed it) — RECHECK"
        elif not pb_count_present:
            verdict = ("STAGE-0 DROP: PB was NOT enabled/invoked at analysis time (no "
                       "policy_briefing_count key) -> the candidate NEVER existed in that run "
                       "(config/timing, NOT a pipeline drop)")
        elif (pb_count_val or 0) == 0:
            verdict = ("STAGE-1 (analysis-time window): PB ran but selected 0 releases under the "
                       "THEN-live lookback -> doc became reachable only under a wider lookback "
                       "(window lever, EFFECT<RISK)")
        else:
            verdict = ("ANOMALY: PB injected %s release(s) at analysis time but NONE are in the stored "
                       "source_candidates -> a GENUINE post-injection drop (main.py has no "
                       "source_candidates cap/dedup -> investigate resolve/evaluate/serialization)"
                       % pb_count_val)
        print("    => %s" % verdict)
        stage0_verdicts[verdict.split(":")[0].split(" -> ")[0][:24]] += 1
    print()
    print("  STAGE-0 verdict tally: %s" % dict(stage0_verdicts))
    print()

    # =====================================================================
    # STAGES 1-4 — PB path RECONSTRUCTION under current lookback (network).
    # =====================================================================
    provider = get_document_provider("policy_briefing")
    if not getattr(provider, "available", False):
        print("=== STAGES 1-4 — SKIPPED (PB provider not available: %s) ==="
              % getattr(provider, "reason", "unknown"))
        print("  -> set POLICY_BRIEFING_ENABLED=true + DATAGOKR_SERVICE_KEY to run the re-run stages.")
        print("  (STAGE 0 above is the faithful determinant and ran without PB env.)")
        print("\n[Safety] READ-ONLY — DB SELECT-only; PB-API-only; no writes/DDL; persists nothing.")
        return 0

    window_cache = {}
    fetches = [0]

    def _fetch_window(start, end):
        key = (start, end)
        if key in window_cache:
            return window_cache[key]
        if fetches[0] >= MAX_WINDOW_FETCHES:
            window_cache[key] = []
            return []
        result = provider.fetch_press_releases(start_date=start, end_date=end, num_of_rows=NUM_OF_ROWS)
        docs = result.get("documents") or []
        window_cache[key] = docs
        fetches[0] += 1
        time.sleep(SLEEP_SECONDS)
        return docs

    def _prod_window_docs(anchor_dt):
        docs = {}
        for i in range(windows):
            wref = anchor_dt - timedelta(days=DATE_WINDOW_DAYS * i)
            start, end = date_window(reference=wref)
            for d in _fetch_window(start, end):
                did = d.get("id") or d.get("original_url") or (d.get("title") or "")
                if did and did not in docs:
                    docs[did] = d
        return list(docs.values())

    print("=== STAGES 1-4 — PB path reconstruction (under CURRENT lookback=%d; comparison only) ===" % lookback)
    drop_stages = collections.Counter()
    for rid in seed_ids:
        fragment = SEED_ROWS[rid]
        row = rows_by_id.get(rid)
        print("-" * 78)
        if row is None:
            print("  row=%s NOT FOUND" % rid)
            continue
        created_at, query, cands, claims, dbg = row
        anchor = created_at if isinstance(created_at, datetime) else None
        if anchor is None:
            try:
                anchor = datetime.strptime(str(created_at)[:10], "%Y-%m-%d")
            except Exception:
                anchor = datetime.now()
        claim0 = _claim_text(claims[0]) if (claims and _is_dict(claims[0])) else ""
        print("  row=%s  claim: %s" % (rid, (claim0 or "(empty)")[:CLAIM_PREVIEW]))
        print("    on-topic PB title fragment: %s" % fragment)

        # STAGE 1 — SELECTION
        doclist = _prod_window_docs(anchor)
        relevant = next((d for d in doclist if fragment in (d.get("title") or "")), None)
        if relevant is None:
            print("    STAGE 1 SELECTION: GONE — on-topic doc NOT in reconstructed window"
                  " (docs=%d) => CAUSE-WINDOW under current lookback" % len(doclist))
            drop_stages["STAGE1_window"] += 1
            continue
        ctoks = _claim_tokens(claims)
        rel_ov = len(_doc_tokens(relevant) & ctoks)
        ranked = _select_documents(doclist, claims, max_releases=max(1, len(doclist)))
        rel_id = relevant.get("id") or relevant.get("original_url") or (relevant.get("title") or "")
        rank = next((i + 1 for i, d in enumerate(ranked)
                     if (d.get("id") or d.get("original_url") or d.get("title")) == rel_id), None)
        sel_in_max = rank is not None and rank <= prod_max
        print("    STAGE 1 SELECTION: PRESENT — overlap=%d rank=%s (max_releases=%d) -> selected=%s"
              % (rel_ov, rank, prod_max, sel_in_max))
        if not sel_in_max:
            print("      => GONE at selection cutoff (rank > max_releases)")
            drop_stages["STAGE1_rank"] += 1
            continue

        # STAGE 2 — SHAPING / INJECTION (to_official_source_candidates)
        shaped, cnt = to_official_source_candidates(doclist, claims, max_releases=prod_max)
        pb_cand = next((c for c in shaped if c.get(PB_MARKER) == relevant.get("id")), None)
        if pb_cand is None:
            print("    STAGE 2 SHAPING: GONE — to_official_source_candidates emitted no candidate"
                  " carrying this doc's marker (injected_releases=%d)" % cnt)
            drop_stages["STAGE2_shaping"] += 1
            continue
        body = sanitize_text(pb_cand.get("raw_text") or "")
        print("    STAGE 2 SHAPING: PRESENT — marker=%s source_type=%s raw_text_len=%d official_body_length=%s"
              % (bool(pb_cand.get(PB_MARKER)), pb_cand.get("source_type"), len(body),
                 pb_cand.get("official_body_length")))

        # STAGE 3 — ENRICH / BODY (PB injected post-enrich; body is inline raw_text)
        meets_body = len(body) >= BODY_MIN
        print("    STAGE 3 BODY: raw_text_len=%d  meets ②>=%d? %s   (PB injected post-enrich:"
              " main.py:733 after enrich:710 -> enrich does NOT touch PB; body is inline)"
              % (len(body), BODY_MIN, meets_body))

        # STAGE 4 — RESOLVE / SCORING ② (resolve is per-candidate; faithful)
        resolved, _summary = resolve_official_evidence([pb_cand], claims)
        rc = resolved[0] if resolved else {}
        score = int(rc.get("official_evidence_score") or 0)
        cls = rc.get("official_evidence_classification")
        obm = bool(rc.get("official_body_match"))
        marker_survives = PB_MARKER in rc
        print("    STAGE 4 RESOLVE/②: score=%d classification=%s official_body_match=%s"
              " (clears medium>=%d? %s / strong>=%d? %s) marker_survives_resolve=%s"
              % (score, cls, obm, MEDIUM_BAR, score >= MEDIUM_BAR, STRONG_BAR, score >= STRONG_BAR,
                 marker_survives))

        # STAGE 5 — CAP / DEDUP / STORAGE: not re-run (needs full pool)
        print("    STAGE 5 CAP/STORAGE: NOT re-run (needs full candidate pool). CODE FACT: main.py has NO")
        print("      source_candidates cap/dedup between PB injection (:733) and persistence; the marker")
        print("      would be stored. So if STAGE 0 shows PB ran+selected>0 yet stored PB=0, the drop is")
        print("      the STAGE-0 anomaly path; otherwise STAGE 0 (disabled / 0-selected) explains it.")
        drop_stages["reached_stage4_no_drop_in_rerun"] += 1
    print()
    print("  distinct PB API window fetches: %d (cap %d)" % (fetches[0], MAX_WINDOW_FETCHES))
    print("  reconstruction drop-stage tally: %s" % dict(drop_stages))
    print()

    # =====================================================================
    # CLOSING
    # =====================================================================
    print("=== CLOSING — where does the PB candidate disappear? ===")
    print("  ★ DECIDER = STAGE 0 (faithful). Read its per-row verdict + tally above:")
    print("    - 'STAGE-0 DROP (PB not enabled at analysis time)' dominant -> the candidate never existed")
    print("      in the original run: a config/timing artifact, NOT a fixable pipeline drop. ⑥ stays")
    print("      effectively CLOSED for these rows (re-analysis with PB enabled would inject it now).")
    print("    - 'STAGE-1 (analysis-time window, 0 selected)' dominant -> only a wider lookback catches")
    print("      it -> window lever = EFFECT<RISK -> ⑥ CLOSED.")
    print("    - 'ANOMALY (injected>0 but stored PB=0)' present -> a GENUINE post-injection drop exists")
    print("      -> Phase 2 targets resolve/evaluate/serialization (named stage), window-free fixable.")
    print("  The STAGES 1-4 re-run only shows the doc is reachable/scorable NOW under lookback=%d; because" % lookback)
    print("  that lookback may exceed analysis-time, 'reached now' does NOT prove a pipeline drop — STAGE 0 does.")
    print("  ★ Final call DEFERRED to operator's read of the STAGE-0 verdicts + the per-stage fields above.")
    print()

    print("[Safety] READ-ONLY — DB SELECT-only; network = PB provider's korea.kr/data.go.kr API ONLY"
          " (reused); no LLM/crawl/other host; no writes/DDL; persists nothing; calls no save/analyze path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
