# SELECT-VS-WINDOW-PROBE — THROWAWAY read-only test: for the GENUINELY-relevant
# recall-gap rows (where pb_existence_probe found a real ON-TOPIC PB doc in a wide
# +/-30d window), was the relevant doc (a) inside production's REAL lookback window
# but dropped by _select_documents ranking [CAUSE-SELECT, fixable without widening],
# (b) selected within production's window [REACHED-BUT-OTHER, failure is downstream],
# or (c) outside production's window [CAUSE-WINDOW, widening-only = EFFECT<RISK]?
#
# DB SELECT-only. The ONLY network is the EXISTING PB provider's korea.kr/data.go.kr
# API (reused, not reimplemented). NO writes/DDL, NO crawl, NO LLM, NO other host.
#
# DECISION THIS RESOLVES
# ----------------------
# CAUSE-SELECT / REACHED-BUT-OTHER present -> a window-free lever may exist -> ⑥ NOT
# closed (a careful separate follow-up is justified). Essentially all CAUSE-WINDOW
# -> only window-widening catches these, which reintroduces the 45/45 garbage-match
# risk -> EFFECT<RISK -> ⑥ CLOSED with confidence.
#
# PRODUCTION-FAITHFUL REPLICATION (read against the REAL provider, LESSON 9)
# -------------------------------------------------------------------------
# Window: production (providers/policy_briefing.fetch_and_build_policy_briefing_
# candidates) covers config.policy_briefing_lookback_days() days back from "now" as
# windows=ceil(lookback/3) non-overlapping 3-day windows ending at now, now-3, ...
# For a HISTORICAL row, production anchored that span to the row's ANALYSIS DATE, so
# this probe anchors the SAME construction at each row's created_at (window_ref =
# created_at - 3*i; date_window(window_ref)). The probe READS the REAL lookback via
# config (env POLICY_BRIEFING_LOOKBACK_DAYS) — it does NOT hard-code a window.
# NOTE: the code DEFAULT is 3 (config.py:90); the brief assumed ~14. The probe uses
# the REAL resolved value and PRINTS it so the operator can confirm it == production.
# Selection: production injects up to config.policy_briefing_max_releases() (default
# 15) releases per claim via _select_documents (overlap>=1, ranked -overlap/-date/id)
# — NOT top-3. The probe classifies against the REAL max_releases and ALSO reports
# the relevant doc's RANK + the docs that outranked it, so the cutoff is transparent.
#
# ★ The matched PB titles + overlap scores are printed; the cause label is a HINT —
#   the operator verifies via the printed evidence.

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
from official_evidence_resolution import _claim_text
# --- PB provider REAL primitives (reused read-only; NOT reimplemented) --------
from providers.policy_briefing import (
    get_document_provider,
    _select_documents,
    _claim_tokens,
    _doc_tokens,
    date_window,
    DEFAULT_NUM_OF_ROWS,
    DATE_WINDOW_DAYS,
)


# ---------------------------------------------------------------------------
# SEED ROW SET — genuinely-relevant recall-gap rows (operator-editable).
# Each entry: row_id -> a short SUBSTRING of the on-topic PB title that
# pb_existence_probe matched (used to detect whether THAT doc appears in window).
# Operator may add/trim after seeing results.
# ---------------------------------------------------------------------------
SEED_ROWS = {
    73: "가맹본부 고금리 부당대출",   # 공정위-금융위 합동 ... 대응방안 발표
    76: "가맹본부 고금리 부당대출",   # same PB doc
    93: "상장폐지 개혁방안",          # ... 한국거래소 상장규정 개정 승인
    82: "실손24",                     # 실손24 연계 병의원 ...
    68: "국민성장펀드",               # 국민성장펀드 성과점검 및 발전방향 세미나
    83: "성장기업발굴협의체",         # 성장기업발굴협의체 설명회 개최
    85: "퓨리오사",                   # 금융위, 퓨리오사 AI 방문 ...
    70: "포용적 금융 대전환",         # 포용적 금융 대전환 회의
}

# --- PB politeness / bounds --------------------------------------------------
NUM_OF_ROWS = DEFAULT_NUM_OF_ROWS     # mirror production single-page size
SLEEP_SECONDS = 0.5
MAX_WINDOW_FETCHES = 400
OUTRANK_SHOW = 5                      # how many outranking docs to print
CLAIM_PREVIEW = 80


def _j(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def _is_dict(c):
    return isinstance(c, dict)


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe needs the DB (local with $env: or Worker Shell).")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    seed_ids = sorted(SEED_ROWS.keys())
    rows_by_id = {}
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, query, normalized_claims "
            "FROM analysis_results WHERE id = ANY(%s) ORDER BY id",
            (seed_ids,),
        )
        for rid, created_at, query, nc in cur.fetchall():
            rows_by_id[int(rid)] = (created_at, query, _j(nc) or [])

    lookback = config.policy_briefing_lookback_days()
    prod_max = config.policy_briefing_max_releases()
    windows = max(1, math.ceil(lookback / DATE_WINDOW_DAYS))

    print("SELECT-VS-WINDOW-PROBE — read-only: select-drop vs out-of-window for recall-gap rows")
    print("  PB primitives reused: get_document_provider(...).fetch_press_releases + _select_documents"
          " + _claim_tokens/_doc_tokens + date_window  (REAL production primitives)")
    print("  PRODUCTION lookback (config.policy_briefing_lookback_days, env POLICY_BRIEFING_LOOKBACK_DAYS)"
          " = %d days" % lookback)
    print("    -> windows = ceil(%d/%d) = %d non-overlapping 3-day windows, anchored at each row's created_at"
          % (lookback, DATE_WINDOW_DAYS, windows))
    print("    NOTE: code default is 3; brief assumed ~14. Confirm this == Render production value.")
    print("  PRODUCTION max_releases (config.policy_briefing_max_releases) = %d  (selection cutoff, NOT 3)"
          % prod_max)
    print("  seed rows: %s" % seed_ids)
    print()

    provider = get_document_provider("policy_briefing")
    if not getattr(provider, "available", False):
        print("  PB provider NOT available: %s" % getattr(provider, "reason", "unknown"))
        print("  -> set POLICY_BRIEFING_ENABLED=true and DATAGOKR_SERVICE_KEY to run the test.")
        print("\n[Safety] READ-ONLY — DB SELECT-only; network = PB provider API only; no writes/DDL.")
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
        """Replicate production's window construction, anchored at anchor_dt:
        windows=ceil(lookback/3) non-overlapping 3-day windows ending at
        anchor, anchor-3, anchor-6, ... (mirrors fetch_and_build_policy_briefing
        _candidates, just anchored historically instead of to now)."""
        docs = {}
        for i in range(windows):
            wref = anchor_dt - timedelta(days=DATE_WINDOW_DAYS * i)
            start, end = date_window(reference=wref)
            for d in _fetch_window(start, end):
                did = d.get("id") or d.get("original_url") or (d.get("title") or "")
                if did and did not in docs:
                    docs[did] = d
        return list(docs.values())

    print("=== per seed row ===")
    labels = collections.Counter()
    for rid in seed_ids:
        fragment = SEED_ROWS[rid]
        row = rows_by_id.get(rid)
        print("-" * 78)
        if row is None:
            print("  row=%s  NOT FOUND in DB (skipped)" % rid)
            labels["NOT_FOUND"] += 1
            continue
        created_at, query, claims = row
        anchor = created_at if isinstance(created_at, datetime) else None
        if anchor is None:
            try:
                anchor = datetime.strptime(str(created_at)[:10], "%Y-%m-%d")
            except Exception:
                anchor = datetime.now()
        claim_text = _claim_text(claims[0]) if (claims and _is_dict(claims[0])) else ""

        doclist = _prod_window_docs(anchor)
        relevant = next((d for d in doclist if fragment in (d.get("title") or "")), None)
        appears = relevant is not None

        print("  row=%s  created=%s  windows(anchor +/- lookback)=%d  docs_in_prod_window=%d"
              % (rid, str(created_at)[:10], windows, len(doclist)))
        print("    claim: %s" % ((claim_text or "(empty)")[:CLAIM_PREVIEW]))
        print("    expected PB title fragment: %s" % fragment)
        print("    APPEARS_IN_PROD_WINDOW: %s" % appears)

        if not appears:
            labels["CAUSE-WINDOW"] += 1
            print("    => CAUSE-WINDOW (relevant PB doc is OUTSIDE production's %d-day window; only" % lookback)
            print("       widening catches it -> widening-only lever = EFFECT<RISK)")
            continue

        ctoks = _claim_tokens(claims)
        rel_ov = len(_doc_tokens(relevant) & ctoks)
        # full ranked order (same ranking _select_documents uses; large cap = full list)
        full_ranked = _select_documents(doclist, claims, max_releases=max(1, len(doclist)))
        rel_id = relevant.get("id") or relevant.get("original_url") or (relevant.get("title") or "")
        rank = None
        for pos, d in enumerate(full_ranked):
            did = d.get("id") or d.get("original_url") or (d.get("title") or "")
            if did == rel_id:
                rank = pos + 1
                break
        selected_prod = rank is not None and rank <= prod_max

        print("    relevant doc: overlap=%d  rank=%s of %d ranked  (prod cutoff max_releases=%d)"
              % (rel_ov, rank if rank is not None else "FILTERED(overlap<1)", len(full_ranked), prod_max))
        print("       title: %s" % (str(relevant.get("title") or "")[:90]))
        print("    docs that ranked at/above it (top %d):" % OUTRANK_SHOW)
        for pos, d in enumerate(full_ranked[:OUTRANK_SHOW], start=1):
            ov = len(_doc_tokens(d) & ctoks)
            mark = "  <-- RELEVANT" if (d.get("id") or d.get("original_url") or d.get("title")) == rel_id else ""
            print("       #%d ovlp=%d  %s%s" % (pos, ov, str(d.get("title") or "")[:70], mark))

        if selected_prod:
            labels["REACHED-BUT-OTHER"] += 1
            print("    => REACHED-BUT-OTHER (relevant doc WAS within prod max_releases=%d -> it would have"
                  " been injected; the failure is DOWNSTREAM of selection: injection/marker/matching)" % prod_max)
        else:
            labels["CAUSE-SELECT"] += 1
            print("    => CAUSE-SELECT (reachable in prod window but ranked OUTSIDE max_releases=%d / filtered"
                  " -> FIXABLE WITHOUT widening: tighten/clean selection ranking)" % prod_max)
    print()
    print("  distinct PB API window fetches: %d (cap %d)" % (fetches[0], MAX_WINDOW_FETCHES))
    print()

    # ---- aggregate + closing --------------------------------------------
    print("=== AGGREGATE (seed rows) ===")
    for lab in ("CAUSE-SELECT", "REACHED-BUT-OTHER", "CAUSE-WINDOW", "NOT_FOUND"):
        if labels.get(lab):
            print("  %-20s : %d" % (lab, labels[lab]))
    print()
    fixable = labels.get("CAUSE-SELECT", 0) + labels.get("REACHED-BUT-OTHER", 0)
    window_only = labels.get("CAUSE-WINDOW", 0)
    print("=== CLOSING — is ⑥ closed? (operator verifies via printed evidence) ===")
    print("  window-free signal (CAUSE-SELECT + REACHED-BUT-OTHER) = %d" % fixable)
    print("  window-only signal  (CAUSE-WINDOW)                    = %d" % window_only)
    print("  - CAUSE-SELECT / REACHED-BUT-OTHER present -> a window-free lever may exist -> ⑥ NOT")
    print("    closed; a careful, separate follow-up (selection tightening / downstream fix) is justified.")
    print("  - essentially all CAUSE-WINDOW -> only window-widening catches these, which reintroduces the")
    print("    45/45 garbage-match risk -> EFFECT<RISK -> ⑥ CLOSED with confidence.")
    print("  ★ The cause labels are HINTS — the operator confirms via the printed overlap scores and the")
    print("    outranking-doc titles above (a 'reached' doc must still be on-topic; an outranker may be junk).")
    print()

    print("[Safety] READ-ONLY — DB SELECT-only; network = PB provider's korea.kr/data.go.kr API ONLY"
          " (reused, not reimplemented); no crawl/LLM/other host; no writes/DDL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
