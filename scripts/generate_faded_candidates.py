# FADED-CLAIMS Slice 1 — operator-run detection generator: find clusters that
# SPREAD WIDELY then went SILENT (no member published for N+ days) and write a
# ranked CANDIDATE shortlist for the semi-auto review flow (S2). The rule does
# the finding; the operator does the final "did it fade, or did it conclude?"
# judgment — candidates are NEVER public by themselves (status='pending').
#
# USAGE (operator, LOCAL or Worker Shell — DATABASE_URL at the external
# Postgres; USE_POSTGRES_WRITE=true required only for the real run):
#   python scripts/generate_faded_candidates.py --dry-run            # ranked print, NO write
#   python scripts/generate_faded_candidates.py --dry-run --min-outlets 7 --min-silence-days 30
#   python scripts/generate_faded_candidates.py                      # upsert candidates
#   python scripts/generate_faded_candidates.py --selftest           # offline check
#
# SAFETY:
#   * Writes ONLY the self-created faded_claim_candidates table (CREATE TABLE
#     IF NOT EXISTS — the brainmap_graph/weekly_reports precedent; no Alembic,
#     postgres_storage.py untouched). Table materializes on the first real run.
#   * VERDICT-FREE: reads brainmap_graph.graph_json + analysis_results
#     (id, title, published_at, original_url) ONLY. No verdict/score column is
#     ever selected; detection = spread + dates + title keywords.
#   * ★UPSERT PRESERVES OPERATOR WORK: re-runs match rows by cluster_stable_id
#     and NEVER overwrite an existing status ('approved'/'dismissed') or
#     reviewed_at — only the measured numbers (outlet_count, last_at,
#     silence_days, score, generated_at) refresh. New clusters -> 'pending'.
#   * HONESTY: a candidate row asserts ONLY that observed coverage stopped —
#     never that the claim is false or the policy failed/was abandoned. The
#     public copy (S3) carries that framing; nothing here is public.
#   * Fail-closed env guards; --dry-run needs only DATABASE_URL. No numpy
#     (reads the already-built graph JSON).

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# Outlet identity — REUSED, never re-derived (import, don't copy). Fallback
# only: the graph's stored cluster.outlet_count (F1A) is the primary number
# (it pooled duplicate-text rows at build time, which member rows alone
# undercount); the helper recomputes from member original_urls when a
# pre-F1A graph row lacks the field.
from build_brainmap_graph import normalize_outlet_host  # noqa: E402

DEFAULT_MIN_OUTLETS = 5
DEFAULT_MIN_SILENCE_DAYS = 21
DEFAULT_TOP_N = 25
# Forward-looking markers: a title implying follow-up coverage was expected.
# SCORE BOOST only — never a hard filter (a wide-then-silent cluster without
# a marker still deserves operator eyes). Overridable via --markers.
DEFAULT_MARKERS = ("발표", "예정", "추진", "계획", "검토", "도입", "시행")
MARKER_SCORE_BOOST = 1.25

SELECT_NEWEST_GRAPH_SQL = (
    "SELECT id, generated_at, graph_json FROM brainmap_graph "
    "ORDER BY id DESC LIMIT 1"
)
# Display/date/outlet fields ONLY — deliberately no verdict/score column.
SELECT_ROWS_SQL = (
    "SELECT id, title, published_at, original_url FROM analysis_results"
)

CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS faded_claim_candidates ("
    "id SERIAL PRIMARY KEY, "
    "cluster_stable_id TEXT UNIQUE, "
    "representative_analysis_id INTEGER, "
    "title TEXT, "
    "outlet_count INTEGER, "
    "first_at TEXT, "
    "last_at TEXT, "
    "silence_days INTEGER, "
    "marker_hit BOOLEAN, "
    "score REAL, "
    "status TEXT DEFAULT 'pending', "
    "reviewed_at TEXT, "
    "generated_at TEXT)"
)
SELECT_EXISTING_SQL = (
    "SELECT status, reviewed_at FROM faded_claim_candidates "
    "WHERE cluster_stable_id = %s"
)
INSERT_SQL = (
    "INSERT INTO faded_claim_candidates "
    "(cluster_stable_id, representative_analysis_id, title, outlet_count, "
    "first_at, last_at, silence_days, marker_hit, score, status, "
    "reviewed_at, generated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
# Refresh MEASURED fields only — status/reviewed_at are deliberately absent.
UPDATE_SQL = (
    "UPDATE faded_claim_candidates SET "
    "representative_analysis_id = %s, title = %s, outlet_count = %s, "
    "first_at = %s, last_at = %s, silence_days = %s, marker_hit = %s, "
    "score = %s, generated_at = %s "
    "WHERE cluster_stable_id = %s"
)


def marker_hit(title: str, markers=DEFAULT_MARKERS) -> bool:
    return any(marker in (title or "") for marker in markers)


def build_candidates(graph, rows_by_id, today, min_outlets=DEFAULT_MIN_OUTLETS,
                     min_silence_days=DEFAULT_MIN_SILENCE_DAYS,
                     markers=DEFAULT_MARKERS, top_n=DEFAULT_TOP_N):
    """Pure compute: graph JSON + {id: (title, published_at, original_url)} ->
    ranked candidate dicts. A cluster qualifies iff outlet_count >= min_outlets
    AND its NEWEST member publish date is >= min_silence_days old. Clusters
    with zero dated members are skipped (silence unmeasurable). Score =
    outlet_count * log(silence_days), * MARKER_SCORE_BOOST on a forward-looking
    title marker (boost only — never a gate)."""
    members_by_cluster: dict = {}
    for node in graph.get("nodes") or []:
        cid = node.get("cluster_id")
        node_id = node.get("id")
        if cid is None or node_id is None:
            continue
        members_by_cluster.setdefault(cid, []).append(node_id)

    candidates = []
    for cluster in graph.get("clusters") or []:
        cid = cluster.get("cluster_id")
        member_ids = members_by_cluster.get(cid) or []
        if cid is None or not member_ids:
            continue
        # Outlet count: stored (F1A, dup-row-pooled) first; recompute from
        # member original_urls only when an old graph row lacks it.
        outlet_count = cluster.get("outlet_count")
        if not isinstance(outlet_count, int) or outlet_count <= 0:
            hosts = set()
            for mid in member_ids:
                row = rows_by_id.get(mid)
                host = normalize_outlet_host(row[2] if row else "")
                if host:
                    hosts.add(host)
            outlet_count = len(hosts)
        if outlet_count < min_outlets:
            continue

        dated = sorted(
            value for value in (
                (rows_by_id.get(mid) or (None, None, None))[1]
                for mid in member_ids
            ) if value
        )
        if not dated:
            continue
        last_at = dated[-1]
        try:
            silence_days = (today - date.fromisoformat(last_at[:10])).days
        except ValueError:
            continue
        if silence_days < min_silence_days:
            continue

        label_title = cluster.get("label_title") or ""
        representative_id = min(member_ids)
        for mid in sorted(member_ids):
            row = rows_by_id.get(mid)
            if label_title and row and row[0] == label_title:
                representative_id = mid
                break
        hit = marker_hit(label_title, markers)
        score = outlet_count * math.log(max(silence_days, 2))
        if hit:
            score *= MARKER_SCORE_BOOST
        candidates.append({
            "cluster_stable_id": cluster.get("stable_id"),
            "representative_analysis_id": representative_id,
            "title": label_title,
            "outlet_count": outlet_count,
            "first_at": dated[0],
            "last_at": last_at,
            "silence_days": silence_days,
            "marker_hit": hit,
            "score": round(score, 3),
        })
    candidates.sort(key=lambda c: (-c["score"], c["representative_analysis_id"]))
    top = candidates[:top_n]
    for rank, candidate in enumerate(top, start=1):
        candidate["rank"] = rank
    return top


def plan_upsert(existing, candidate):
    """Pure: decide the row this candidate should end up as. ``existing`` is
    None (new cluster) or {"status", "reviewed_at"} from the current table.
    ★Operator work is NEVER lost: an existing status/reviewed_at is preserved
    verbatim; only the measured numbers refresh."""
    if existing is None:
        return {"action": "insert", "status": "pending", "reviewed_at": None}
    return {
        "action": "update",
        "status": existing.get("status") or "pending",
        "reviewed_at": existing.get("reviewed_at"),
    }


def print_shortlist(candidates):
    if not candidates:
        print("[faded] no candidates matched the thresholds.")
        return
    print("[faded] rank | score | outlets | silence | marker | last_at    | id    | title")
    for candidate in candidates:
        print("  #%-3d %8.2f %7d %8dd %7s  %s  id=%-5s %s"
              % (candidate["rank"], candidate["score"],
                 candidate["outlet_count"], candidate["silence_days"],
                 "Y" if candidate["marker_hit"] else "-",
                 (candidate["last_at"] or "")[:10],
                 candidate["representative_analysis_id"],
                 (candidate["title"] or "")[:70]))


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — synthetic graph, fixed today. No DB, no network.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    print("=== GENERATE-FADED-CANDIDATES --selftest (offline; no DB) ===")
    today = date(2026, 7, 11)
    graph = {
        "nodes": [
            {"id": 1, "cluster_id": 0}, {"id": 2, "cluster_id": 0},
            {"id": 3, "cluster_id": 0},
            {"id": 4, "cluster_id": 1}, {"id": 5, "cluster_id": 1},
            {"id": 6, "cluster_id": 2}, {"id": 7, "cluster_id": 2},
            {"id": 8, "cluster_id": 3}, {"id": 9, "cluster_id": 3},
            {"id": 10, "cluster_id": 4}, {"id": 11, "cluster_id": 4},
            {"id": 12, "cluster_id": 5}, {"id": 13, "cluster_id": 5},
        ],
        "clusters": [
            # A: wide + long-silent + marker -> kept, boosted.
            {"cluster_id": 0, "stable_id": "aaa", "outlet_count": 8,
             "label_title": "청년 지원금 도입 검토"},
            # B: wide but RECENT (5d) -> excluded by silence.
            {"cluster_id": 1, "stable_id": "bbb", "outlet_count": 9,
             "label_title": "전세 대출 발표"},
            # C: long-silent but narrow (3 outlets) -> excluded by spread.
            {"cluster_id": 2, "stable_id": "ccc", "outlet_count": 3,
             "label_title": "복지 계획 발표"},
            # D: wide + longest-silent, NO marker -> kept (marker never gates).
            {"cluster_id": 3, "stable_id": "ddd", "outlet_count": 6,
             "label_title": "전세 대출 급증"},
            # E: all members undated -> skipped (silence unmeasurable).
            {"cluster_id": 4, "stable_id": "eee", "outlet_count": 7,
             "label_title": "보험 제도"},
            # F: NO stored outlet_count -> fallback distinct-host recompute (5).
            {"cluster_id": 5, "stable_id": "fff",
             "label_title": "소상공인 지원 추진"},
        ],
    }
    rows = {
        1: ("청년 지원금 도입 검토", "2026-06-01T00:00:00+00:00", "https://a.kr/1"),
        2: ("기타", "2026-05-20T00:00:00+00:00", "https://b.kr/2"),
        3: ("기타2", None, "https://c.kr/3"),
        4: ("전세 대출 발표", "2026-07-06T00:00:00+00:00", "https://a.kr/4"),
        5: ("기타3", "2026-07-01T00:00:00+00:00", "https://b.kr/5"),
        6: ("복지 계획 발표", "2026-05-01T00:00:00+00:00", "https://a.kr/6"),
        7: ("기타4", "2026-04-01T00:00:00+00:00", "https://b.kr/7"),
        8: ("전세 대출 급증", "2026-05-12T00:00:00+00:00", "https://a.kr/8"),
        9: ("기타5", "2026-04-20T00:00:00+00:00", "https://b.kr/9"),
        10: ("보험 제도", None, "https://a.kr/10"),
        11: ("기타6", None, "https://b.kr/11"),
        12: ("소상공인 지원 추진", "2026-06-05T00:00:00+00:00", "https://m.one.kr/x"),
        13: ("기타7", "2026-06-01T00:00:00+00:00", "https://two.kr/y"),
    }
    # Give cluster F five distinct hosts across its 2 members' urls? Distinct
    # hosts come from member rows only — extend member 13's host set via more
    # members is overkill; instead lower the threshold for the F check below.
    shortlist = build_candidates(graph, rows, today, min_outlets=5,
                                 min_silence_days=21, top_n=10)
    by_sid = {c["cluster_stable_id"]: c for c in shortlist}

    a_ok = ("aaa" in by_sid and "ddd" in by_sid
            and "bbb" not in by_sid and "ccc" not in by_sid
            and "eee" not in by_sid)
    print("  [%s] (a) filter: silence>=21 & outlets>=5 kept; recent/narrow/"
          "undated excluded" % ("ok" if a_ok else "xx"))
    # A: 8*log(40)*1.25 ~ 36.9 ; D: 6*log(60) ~ 24.6 -> A first.
    b_ok = ([c["cluster_stable_id"] for c in shortlist[:2]] == ["aaa", "ddd"]
            and by_sid["aaa"]["marker_hit"] is True
            and by_sid["ddd"]["marker_hit"] is False)
    print("  [%s] (b) score ranking (marker boosts, never gates)"
          % ("ok" if b_ok else "xx"))
    c_ok = (by_sid["aaa"]["silence_days"] == 40
            and by_sid["aaa"]["last_at"] == "2026-06-01T00:00:00+00:00"
            and by_sid["aaa"]["first_at"] == "2026-05-20T00:00:00+00:00"
            and by_sid["aaa"]["representative_analysis_id"] == 1)
    print("  [%s] (c) dates/silence/representative correct" % ("ok" if c_ok else "xx"))
    # Fallback outlet computation: rerun with min_outlets=2 -> F qualifies via
    # 2 distinct recomputed hosts (one.kr + two.kr; m. stripped by the helper).
    fallback = build_candidates(graph, rows, today, min_outlets=2,
                                min_silence_days=21, top_n=10)
    f_row = next((c for c in fallback if c["cluster_stable_id"] == "fff"), None)
    d_ok = f_row is not None and f_row["outlet_count"] == 2
    print("  [%s] (d) missing stored outlet_count -> distinct-host fallback "
          "(normalize_outlet_host reused)" % ("ok" if d_ok else "xx"))
    # ★Upsert preservation: approved/dismissed + reviewed_at survive a re-run.
    kept = plan_upsert({"status": "approved", "reviewed_at": "2026-07-01T00:00:00+00:00"},
                       by_sid["aaa"])
    fresh = plan_upsert(None, by_sid["ddd"])
    dismissed = plan_upsert({"status": "dismissed", "reviewed_at": "2026-07-02T00:00:00+00:00"},
                            by_sid["aaa"])
    e_ok = (kept == {"action": "update", "status": "approved",
                     "reviewed_at": "2026-07-01T00:00:00+00:00"}
            and fresh == {"action": "insert", "status": "pending",
                          "reviewed_at": None}
            and dismissed["status"] == "dismissed")
    print("  [%s] (e) UPSERT preserves operator status/reviewed_at; new -> pending"
          % ("ok" if e_ok else "xx"))
    blob = json.dumps(shortlist, ensure_ascii=False)
    f_ok = ("verdict" not in blob and "confidence" not in blob
            and "truth" not in blob)
    print("  [%s] (f) candidate payload is verdict-free" % ("ok" if f_ok else "xx"))

    ok = all([a_ok, b_ok, c_ok, d_ok, e_ok, f_ok])
    print()
    print("SELFTEST: %s" % ("PASS (filter + ranking + dates + outlet fallback "
                            "+ upsert-preserve + verdict-free)" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_faded_candidates",
        description="Find clusters that spread widely then went silent and "
                    "upsert a ranked candidate shortlist (status='pending') "
                    "for the semi-auto review flow.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic graph; no DB).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the ranked shortlist; NO CREATE TABLE, NO write.")
    parser.add_argument("--min-outlets", type=int, default=DEFAULT_MIN_OUTLETS,
                        help="Minimum distinct-outlet spread (default %d)."
                             % DEFAULT_MIN_OUTLETS)
    parser.add_argument("--min-silence-days", type=int,
                        default=DEFAULT_MIN_SILENCE_DAYS,
                        help="Minimum days since the newest member publish "
                             "date (default %d)." % DEFAULT_MIN_SILENCE_DAYS)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="Candidates to keep (default %d)." % DEFAULT_TOP_N)
    parser.add_argument("--markers", default=",".join(DEFAULT_MARKERS),
                        help="Comma-separated forward-looking title markers "
                             "(score boost only, never a gate).")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    markers = tuple(part.strip() for part in (args.markers or "").split(",")
                    if part.strip()) or DEFAULT_MARKERS

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — point it at the external Postgres.")
        return 0
    if not args.dry_run and os.environ.get("USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — refusing to write. Set it "
              "true, or use --dry-run.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    generated_at = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date()
    print("GENERATE-FADED-CANDIDATES — min_outlets=%d min_silence=%dd top_n=%d%s"
          % (args.min_outlets, args.min_silence_days, args.top_n,
             " (DRY-RUN)" if args.dry_run else ""))
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_NEWEST_GRAPH_SQL)
            graph_row = cur.fetchone()
        if not graph_row:
            print("[faded] no brainmap_graph row — run "
                  "scripts/build_brainmap_graph.py first.")
            return 1
        graph_ref, _graph_generated_at, graph_json = graph_row
        try:
            graph = json.loads(graph_json)
        except (TypeError, ValueError):
            print("[faded] newest brainmap_graph row holds invalid JSON — aborting.")
            return 1
        with conn.cursor() as cur:
            cur.execute(SELECT_ROWS_SQL)
            rows_by_id = {row_id: (title, published_at, original_url)
                          for row_id, title, published_at, original_url
                          in cur.fetchall()}

        shortlist = build_candidates(
            graph, rows_by_id, today,
            min_outlets=args.min_outlets,
            min_silence_days=args.min_silence_days,
            markers=markers, top_n=args.top_n,
        )
        print_shortlist(shortlist)
        print("[faded] graph ref=%s | %d candidate(s)" % (graph_ref, len(shortlist)))

        if args.dry_run:
            print("[faded] DRY-RUN — no CREATE TABLE, no write.")
            return 0

        inserted = updated = 0
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            for candidate in shortlist:
                cur.execute(SELECT_EXISTING_SQL,
                            (candidate["cluster_stable_id"],))
                row = cur.fetchone()
                existing = ({"status": row[0], "reviewed_at": row[1]}
                            if row else None)
                plan = plan_upsert(existing, candidate)
                if plan["action"] == "insert":
                    cur.execute(INSERT_SQL, (
                        candidate["cluster_stable_id"],
                        candidate["representative_analysis_id"],
                        candidate["title"], candidate["outlet_count"],
                        candidate["first_at"], candidate["last_at"],
                        candidate["silence_days"], candidate["marker_hit"],
                        candidate["score"], plan["status"],
                        plan["reviewed_at"], generated_at,
                    ))
                    inserted += 1
                else:
                    # Measured fields ONLY — status/reviewed_at preserved by
                    # never appearing in UPDATE_SQL.
                    cur.execute(UPDATE_SQL, (
                        candidate["representative_analysis_id"],
                        candidate["title"], candidate["outlet_count"],
                        candidate["first_at"], candidate["last_at"],
                        candidate["silence_days"], candidate["marker_hit"],
                        candidate["score"], generated_at,
                        candidate["cluster_stable_id"],
                    ))
                    updated += 1
        conn.commit()
        print("[faded] wrote candidates: %d inserted (pending), %d refreshed "
              "(status preserved)" % (inserted, updated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
