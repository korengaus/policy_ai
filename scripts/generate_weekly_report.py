# WEEKLY-REPORT Slice 1 — operator-run generator: rank the week's most-
# amplified policy claims by CIRCULATION (distinct outlet_count) and store
# ONE snapshot row in the self-created `weekly_reports` table.
#
# For each cluster in the NEWEST brainmap_graph row: representative title
# (the stored label_title — build_brainmap_graph.py's highest-degree-node
# label, reused for ranking consistency), a representative analysis_id for
# the /?result_id= card link, the precomputed distinct outlet_count, and
# first/last member publish dates from analysis_results.published_at. Keeps
# clusters with ANY member published inside [week_start, week_end], sorts by
# outlet_count desc, takes Top N, writes payload_json.
#
# USAGE (operator, LOCAL machine or Worker Shell — DATABASE_URL at the
# external Postgres, USE_POSTGRES_WRITE=true):
#   python scripts/generate_weekly_report.py --dry-run          # rank, no write
#   python scripts/generate_weekly_report.py                    # last 7 days
#   python scripts/generate_weekly_report.py --week-start 2026-07-06 --week-end 2026-07-12
#   python scripts/generate_weekly_report.py --force            # regenerate a week
#   python scripts/generate_weekly_report.py --selftest         # offline check
#
# SAFETY:
#   * Writes ONLY the weekly_reports table (additive, self-created via
#     CREATE TABLE IF NOT EXISTS — the exact brainmap_graph precedent;
#     postgres_storage.py untouched, no Alembic). The table materializes on
#     this script's first real run, NOT at deploy.
#   * VERDICT-FREE: reads brainmap_graph.graph_json + analysis_results
#     (id, published_at) ONLY. No verdict_label / policy_confidence_score /
#     truth_claim / operator_review_required / has_genuine_official_support
#     column is ever selected; the ranking key is circulation, never truth.
#   * HONESTY BOUNDARY: the stored payload carries the mandatory framing
#     "확산 규모 기준 · 사실 검증 아님"; a write-time guard refuses to
#     persist if any string THIS script generates carries verdict vocabulary
#     (FORBIDDEN_LABEL_VOCAB imported from build_brainmap_graph — titles are
#     journalist-written passthrough, exactly as in the brain map).
#   * Idempotent per week_start: an existing row for that week_start SKIPS
#     the write unless --force (which appends a fresh row; the API serves
#     the newest row per week, older rows are free audit history).
#   * Fail-closed: refuses without DATABASE_URL; refuses to write without
#     USE_POSTGRES_WRITE=true (--dry-run needs only DATABASE_URL).
#     Never prints DATABASE_URL or any API key.
#   * No numpy needed — this reads the already-built graph JSON.

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
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

from build_brainmap_graph import FORBIDDEN_LABEL_VOCAB  # noqa: E402 — honesty guard, shared

DEFAULT_TOP_N = 10

SELECT_NEWEST_GRAPH_SQL = (
    "SELECT id, generated_at, graph_json FROM brainmap_graph "
    "ORDER BY id DESC LIMIT 1"
)
# id + published_at ONLY — deliberately no verdict/score column.
SELECT_PUBLISHED_SQL = "SELECT id, published_at FROM analysis_results"

# The ONLY write this script performs — an additive, self-created table
# (mirrors brainmap_graph's create-on-demand pattern verbatim).
CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS weekly_reports ("
    "id SERIAL PRIMARY KEY, "
    "week_start TEXT, "
    "week_end TEXT, "
    "generated_at TEXT, "
    "graph_build_ref INTEGER, "
    "payload_json TEXT)"
)
INSERT_SQL = (
    "INSERT INTO weekly_reports "
    "(week_start, week_end, generated_at, graph_build_ref, payload_json) "
    "VALUES (%s, %s, %s, %s, %s)"
)
SELECT_EXISTING_WEEK_SQL = (
    "SELECT id FROM weekly_reports WHERE week_start = %s LIMIT 1"
)

FRAMING_TEXT = "확산 규모 기준 · 사실 검증 아님"


def build_report(graph, published_by_id, week_start, week_end,
                 top_n=DEFAULT_TOP_N):
    """Pure compute: graph JSON dict + {analysis_id: published_at} ->
    the payload dict. A cluster qualifies when ANY member's published_at
    date falls inside [week_start, week_end] (inclusive, YYYY-MM-DD compare
    on the ISO-UTC TEXT). Ranking: outlet_count desc, tie-broken by
    smallest member id (deterministic)."""
    members_by_cluster = {}
    titles_by_id = {}
    for node in graph.get("nodes") or []:
        cid = node.get("cluster_id")
        node_id = node.get("id")
        if cid is None or node_id is None:
            continue
        members_by_cluster.setdefault(cid, []).append(node_id)
        titles_by_id[node_id] = node.get("title") or ""

    entries = []
    total_considered = 0
    for cluster in graph.get("clusters") or []:
        cid = cluster.get("cluster_id")
        member_ids = members_by_cluster.get(cid) or []
        if cid is None or not member_ids:
            continue
        total_considered += 1
        dated = sorted(
            value for value in (published_by_id.get(mid) for mid in member_ids)
            if value
        )
        window_dates = [v for v in dated
                        if week_start <= v[:10] <= week_end]
        if not window_dates:
            continue
        label_title = cluster.get("label_title") or ""
        # Representative for the card link: the member whose title IS the
        # cluster label (the highest-degree node build_brainmap_graph picked);
        # fallback = smallest member id so no entry ever lacks a link.
        representative_id = min(member_ids)
        for mid in sorted(member_ids):
            if label_title and titles_by_id.get(mid) == label_title:
                representative_id = mid
                break
        entries.append({
            "stable_id": cluster.get("stable_id"),
            "title": label_title,
            "representative_analysis_id": representative_id,
            "outlet_count": cluster.get("outlet_count"),
            "size_label": cluster.get("size_label"),
            "member_count": len(member_ids),
            "window_member_count": len(window_dates),
            "first_at": dated[0] if dated else None,
            "last_at": dated[-1] if dated else None,
            "window_first_at": window_dates[0],
            "window_last_at": window_dates[-1],
        })
    entries.sort(key=lambda e: (-(e["outlet_count"] or 0),
                                e["representative_analysis_id"]))
    top = entries[:top_n]
    for rank, entry in enumerate(top, start=1):
        entry["rank"] = rank
    return {
        "week_start": week_start,
        "week_end": week_end,
        "framing": FRAMING_TEXT,
        "kind": "spread",
        "total_clusters_considered": total_considered,
        "qualifying_clusters": len(entries),
        "top": top,
    }


def honesty_guard_ok(payload):
    """Write-time honesty guard (build_brainmap_graph precedent, adapted):
    the framing DISCLAIMER deliberately contains "검증" inside a NEGATION
    ("사실 검증 아님") — so it must be BYTE-EXACT (any drift refuses the
    write), and every OTHER string this script generates must be free of
    verdict vocabulary. Titles/size_labels are journalist/graph passthrough
    data — excluded, mirroring the brain-map guard scope."""
    if payload.get("framing") != FRAMING_TEXT:
        return False
    generated_other = [payload.get("kind") or ""]
    return not any(word in text
                   for text in generated_other
                   for word in FORBIDDEN_LABEL_VOCAB)


def print_ranking(payload):
    print("[weekly] window %s .. %s | clusters considered=%d qualifying=%d"
          % (payload["week_start"], payload["week_end"],
             payload["total_clusters_considered"],
             payload["qualifying_clusters"]))
    for entry in payload["top"]:
        print("  #%d [%s개 매체] %s (id=%s, 이번 주 %d건, %s→%s)"
              % (entry["rank"], entry["outlet_count"],
                 (entry["title"] or "")[:60],
                 entry["representative_analysis_id"],
                 entry["window_member_count"],
                 (entry["window_first_at"] or "")[:10],
                 (entry["window_last_at"] or "")[:10]))


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — synthetic graph + publish dates. No DB, no network.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    print("=== GENERATE-WEEKLY-REPORT --selftest (offline; no DB, no network) ===")
    graph = {
        "nodes": [
            # Cluster 0: 3 members, label on id 2, all in-window.
            {"id": 1, "cluster_id": 0, "title": "A-덜중심"},
            {"id": 2, "cluster_id": 0, "title": "A-대표제목"},
            {"id": 3, "cluster_id": 0, "title": "A-덜중심2"},
            # Cluster 1: 2 members, BIGGER outlet_count, in-window.
            {"id": 4, "cluster_id": 1, "title": "B-대표제목"},
            {"id": 5, "cluster_id": 1, "title": "B-기타"},
            # Cluster 2: OLD (out of window) — must be filtered out.
            {"id": 6, "cluster_id": 2, "title": "C-옛날"},
            {"id": 7, "cluster_id": 2, "title": "C-옛날2"},
            # Cluster 3: label_title matches NO member title -> min-id fallback.
            {"id": 8, "cluster_id": 3, "title": "D-noje1"},
            {"id": 9, "cluster_id": 3, "title": "D-noje2"},
            # Singleton — never reported.
            {"id": 10, "cluster_id": None, "title": "solo"},
        ],
        "clusters": [
            {"cluster_id": 0, "stable_id": "aaa", "outlet_count": 3,
             "label_title": "A-대표제목", "size_label": "3개 매체 보도 중"},
            {"cluster_id": 1, "stable_id": "bbb", "outlet_count": 9,
             "label_title": "B-대표제목", "size_label": "9개 매체 보도 중"},
            {"cluster_id": 2, "stable_id": "ccc", "outlet_count": 5,
             "label_title": "C-옛날", "size_label": "5개 매체 보도 중"},
            {"cluster_id": 3, "stable_id": "ddd", "outlet_count": 2,
             "label_title": "지워진-제목", "size_label": "2개 매체 보도 중"},
        ],
    }
    published = {
        1: "2026-07-06T01:00:00+00:00", 2: "2026-07-07T01:00:00+00:00",
        3: None,                          # undated member tolerated
        4: "2026-07-08T09:00:00+00:00", 5: "2026-07-05T00:00:00+00:00",
        6: "2026-01-01T00:00:00+00:00", 7: "2026-01-02T00:00:00+00:00",
        8: "2026-07-09T00:00:00+00:00", 9: "2026-07-04T00:00:00+00:00",
    }
    payload = build_report(graph, published, "2026-07-04", "2026-07-10", top_n=10)

    ranks = [(e["stable_id"], e["rank"]) for e in payload["top"]]
    a_ok = ranks == [("bbb", 1), ("aaa", 2), ("ddd", 3)]
    print("  [%s] (a) outlet_count desc ranking; out-of-window cluster excluded"
          % ("ok" if a_ok else "xx"))
    by_sid = {e["stable_id"]: e for e in payload["top"]}
    b_ok = (by_sid["aaa"]["representative_analysis_id"] == 2
            and by_sid["bbb"]["representative_analysis_id"] == 4)
    print("  [%s] (b) representative = the label-title member (card link id)"
          % ("ok" if b_ok else "xx"))
    c_ok = by_sid["ddd"]["representative_analysis_id"] == 8
    print("  [%s] (c) label-mismatch cluster falls back to smallest member id"
          % ("ok" if c_ok else "xx"))
    d_ok = (by_sid["aaa"]["window_member_count"] == 2
            and by_sid["aaa"]["first_at"] == "2026-07-06T01:00:00+00:00"
            and by_sid["bbb"]["window_first_at"] == "2026-07-05T00:00:00+00:00")
    print("  [%s] (d) undated member tolerated; first/last + window dates correct"
          % ("ok" if d_ok else "xx"))
    blob = json.dumps(payload, ensure_ascii=False)
    e_ok = (payload["framing"] == FRAMING_TEXT
            and "verdict" not in blob and "confidence" not in blob
            and "truth" not in blob and honesty_guard_ok(payload))
    print("  [%s] (e) framing present; no verdict/confidence/truth key; guard holds"
          % ("ok" if e_ok else "xx"))
    f_ok = payload["total_clusters_considered"] == 4 and payload["qualifying_clusters"] == 3
    print("  [%s] (f) considered/qualifying counts" % ("ok" if f_ok else "xx"))

    ok = all([a_ok, b_ok, c_ok, d_ok, e_ok, f_ok])
    print()
    print("SELFTEST: %s" % ("PASS (ranking + window filter + representative + "
                            "fallback + undated tolerance + honesty)" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_weekly_report",
        description="Rank the week's most-amplified claims by distinct-outlet "
                    "circulation from the newest brainmap_graph and store ONE "
                    "weekly_reports snapshot row.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic graph; no DB).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + print the ranking; NO CREATE TABLE, NO INSERT.")
    parser.add_argument("--week-start", default=None,
                        help="YYYY-MM-DD window start (default: today-6, UTC).")
    parser.add_argument("--week-end", default=None,
                        help="YYYY-MM-DD window end, inclusive (default: today, UTC).")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="Entries to keep (default %d)." % DEFAULT_TOP_N)
    parser.add_argument("--force", action="store_true",
                        help="Write even if a row for this week_start exists "
                             "(appends; the API serves the newest per week).")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    today = datetime.now(timezone.utc).date()
    week_end = args.week_end or today.isoformat()
    week_start = args.week_start or (today - timedelta(days=6)).isoformat()
    if week_start > week_end:
        print("[weekly] week_start %s is after week_end %s — aborting."
              % (week_start, week_end))
        return 1

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
    print("GENERATE-WEEKLY-REPORT — window %s..%s top_n=%d%s"
          % (week_start, week_end, args.top_n,
             " (DRY-RUN)" if args.dry_run else ""))
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_NEWEST_GRAPH_SQL)
            graph_row = cur.fetchone()
        if not graph_row:
            print("[weekly] no brainmap_graph row — run "
                  "scripts/build_brainmap_graph.py first.")
            return 1
        graph_build_ref, graph_generated_at, graph_json = graph_row
        try:
            graph = json.loads(graph_json)
        except (TypeError, ValueError):
            print("[weekly] newest brainmap_graph row holds invalid JSON — aborting.")
            return 1
        with conn.cursor() as cur:
            cur.execute(SELECT_PUBLISHED_SQL)
            published_by_id = {row_id: value for row_id, value in cur.fetchall()}

        payload = build_report(graph, published_by_id, week_start, week_end,
                               top_n=args.top_n)
        payload["generated_at"] = generated_at
        payload["graph_build_ref"] = graph_build_ref
        payload["graph_generated_at"] = str(graph_generated_at or "")
        print_ranking(payload)

        if not honesty_guard_ok(payload):
            print("[weekly] HONESTY GUARD tripped — generated strings carry "
                  "verdict vocabulary; refusing to write.")
            return 1
        if args.dry_run:
            print("[weekly] DRY-RUN — no CREATE TABLE, no INSERT.")
            return 0
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(SELECT_EXISTING_WEEK_SQL, (week_start,))
            existing = cur.fetchone()
            if existing and not args.force:
                print("[weekly] a row for week_start=%s already exists "
                      "(id=%s) — skipping (use --force to append a fresh "
                      "snapshot; the API serves the newest)."
                      % (week_start, existing[0]))
                return 0
            cur.execute(INSERT_SQL, (
                week_start, week_end, generated_at, graph_build_ref,
                json.dumps(payload, ensure_ascii=False),
            ))
        conn.commit()
        print("[weekly] wrote 1 weekly_reports row (week_start=%s, graph ref=%s)"
              % (week_start, graph_build_ref))
    return 0


if __name__ == "__main__":
    sys.exit(main())
