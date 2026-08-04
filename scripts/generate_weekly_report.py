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
#   python scripts/generate_weekly_report.py --force \
#       --supersede 2026-07-20 --supersede-reason "why"         # replace a PUBLISHED week
#   python scripts/generate_weekly_report.py --selftest         # offline check
#
# SAFETY:
#   * Writes ONLY the weekly_reports table (additive, self-created via
#     CREATE TABLE IF NOT EXISTS — the exact brainmap_graph precedent;
#     postgres_storage.py untouched, no Alembic). The table materializes on
#     this script's first real run, NOT at deploy.
#   * VERDICT-FREE: reads brainmap_graph.graph_json + analysis_results
#     (id, published_at, domain, content_nature) ONLY. No verdict_label /
#     policy_confidence_score / truth_claim / operator_review_required /
#     has_genuine_official_support column is ever selected; the ranking key
#     is circulation, never truth. domain/content_nature feed ONLY the
#     WEEKLY-CONTENT-GUARD strict-combo selection (both classifier fallback
#     labels together = neither classifier could place the row).
#   * HONESTY BOUNDARY: the stored payload carries the mandatory framing
#     "확산 규모 기준 · 사실 검증 아님"; a write-time guard refuses to
#     persist if any string THIS script generates carries verdict vocabulary
#     (FORBIDDEN_LABEL_VOCAB imported from build_brainmap_graph — titles are
#     journalist-written passthrough, exactly as in the brain map).
#   * Idempotent per week_start: an existing row for that week_start SKIPS
#     the write. ARCHIVE-IMMUTABILITY: --force alone no longer overrides that —
#     because the API serves the NEWEST row per week, appending is a silent
#     rewrite of a published page. Superseding additionally requires
#     --supersede <that exact week> and --supersede-reason, and records both on
#     the new row (payload_json -> supersedes). Older rows are never touched:
#     they remain intact audit history.
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
# WEEKLY-CONTENT-GUARD: the STRICT-COMBO exclusion values, imported from the
# classifiers that produce them (never hand-written here). Both are the
# classifiers' explicit fail/fallback labels: a row carrying BOTH is one that
# neither classifier could place — measured 64/14,240 corpus rows (0.45%), and
# the only archived weekly top-10 row it matches is the child-homicide court
# story (Phase 1b). The WIDE rule (기타-미분류 alone) was measured and
# REJECTED: it also removes real policy claims the domain classifier missed
# (여수시 추경 편성 — 45% of a 51-row sample read as genuine policy).
from content_nature_classifier import FALLBACK_LABEL as NATURE_FALLBACK_LABEL  # noqa: E402
from domain_classifier import FALLBACK_LABEL as DOMAIN_FALLBACK_LABEL  # noqa: E402

DEFAULT_TOP_N = 10

SELECT_NEWEST_GRAPH_SQL = (
    "SELECT id, generated_at, graph_json FROM brainmap_graph "
    "ORDER BY id DESC LIMIT 1"
)
# id + published_at ONLY — deliberately no verdict/score column.
SELECT_PUBLISHED_SQL = "SELECT id, published_at FROM analysis_results"
# WEEKLY-CONTENT-GUARD: classifier fields for the strict-combo exclusion.
# Still verdict-free: domain / content_nature are topic/format classifiers,
# never a truth or confidence signal.
SELECT_CLASSIFIER_SQL = "SELECT id, domain, content_nature FROM analysis_results"

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
# ARCHIVE-IMMUTABILITY: ORDER BY id DESC so this returns the row the archive
# actually SERVES (api_server.py:1147-1150 selects the newest id for a week).
# The skip decision is unchanged — it still turns on whether ANY row exists —
# but a refusal can now name the exact snapshot that would be replaced.
SELECT_EXISTING_WEEK_SQL = (
    "SELECT id, generated_at FROM weekly_reports WHERE week_start = %s "
    "ORDER BY id DESC LIMIT 1"
)

FRAMING_TEXT = "확산 규모 기준 · 사실 검증 아님"


# ---------------------------------------------------------------------------
# ARCHIVE-IMMUTABILITY — a published week may only be replaced on purpose.
#
# A stored week is a PUBLISHED record: 14 outreach emails link to archive pages,
# and api_server serves the NEWEST row per week_start, so appending a second row
# silently rewrites a page someone has already read. That has happened twice
# (2026-07-14 and 2026-07-20), and for 07-20 the top ten changed membership and
# order. The unflagged path was never the hole — it skips. `--force` was.
#
# The guard does not remove --force; it makes its effect impossible to reach by
# accident. To supersede, the operator must ALSO name the exact week
# (--supersede YYYY-MM-DD) and say why (--supersede-reason). Naming the week is
# the load-bearing part: a --force left in a saved command supersedes whatever
# week the default window resolves to, and that week changes every Monday, so a
# stale command refuses the moment the calendar moves rather than quietly
# replacing a different archive page than the operator was thinking of.
#
# FAILS CLOSED in every direction: unknown existence -> refuse (the caller
# passes existing=None only when it positively read "no row"); force without
# intent -> refuse; intent naming a different week -> refuse; blank reason ->
# refuse. The only path that writes over a published week is one where the
# operator typed that week's date and a reason.
def supersede_decision(existing, force, supersede_week, supersede_reason,
                       week_start):
    """Pure: (action, message) for a write attempt. Never touches the DB.

    ``existing`` is (id, generated_at) for the row the archive currently serves,
    or None when the week has no stored row. Actions: "write" (no published row
    to protect, or a deliberate supersession), "skip" (a row exists and no
    supersession was requested — today's behaviour, unchanged), "refuse".
    """
    if existing is None:
        return "write", ""
    existing_id, existing_generated_at = existing
    served = ("week_start=%s already has a stored row (id=%s, generated %s) and "
              "that row is what the archive serves"
              % (week_start, existing_id, existing_generated_at))
    if not force:
        return "skip", (
            "[weekly] %s — skipping. To replace it you must supersede it "
            "deliberately: --force --supersede %s --supersede-reason '<why>'."
            % (served, week_start))
    if not supersede_week:
        return "refuse", (
            "[weekly] REFUSING: %s. --force alone would append a row that "
            "silently becomes the served version of a page outreach emails "
            "link to. Re-run with --supersede %s --supersede-reason '<why>' "
            "if that is genuinely what you intend."
            % (served, week_start))
    if supersede_week != week_start:
        return "refuse", (
            "[weekly] REFUSING: --supersede %s does not match the week being "
            "written (%s). %s. This is the stale-command case: the flag names "
            "a week you are not writing, so nothing is superseded."
            % (supersede_week, week_start, served))
    if not (supersede_reason or "").strip():
        return "refuse", (
            "[weekly] REFUSING: --supersede %s requires --supersede-reason "
            "'<why>' — the reason is stored on the new row so a superseded "
            "week is discoverable from the data, not just from a shell history."
            % week_start)
    return "supersede", (
        "[weekly] SUPERSEDING %s. The previous snapshot stays in the table as "
        "audit history; the new row becomes the served version."
        % served)


def build_report(graph, published_by_id, week_start, week_end,
                 top_n=DEFAULT_TOP_N, classifier_by_id=None):
    """Pure compute: graph JSON dict + {analysis_id: published_at} ->
    the payload dict. A cluster qualifies when ANY member's published_at
    date falls inside [week_start, week_end] (inclusive, YYYY-MM-DD compare
    on the ISO-UTC TEXT). Ranking: outlet_count desc, tie-broken by
    smallest member id (deterministic).

    WEEKLY-CONTENT-GUARD: classifier_by_id (optional) maps analysis_id ->
    (domain, content_nature). A cluster is excluded AT SELECTION — before
    ranking, so the top-10 backfills naturally from the next eligible
    cluster — when its REPRESENTATIVE row carries BOTH fallback labels
    (domain=기타-미분류 AND content_nature=mixed_or_unclear): the strict
    combo, both classifiers explicitly failing to place the row. A missing
    row or a null/absent value in EITHER field NEVER excludes — absence is
    not evidence. No number on any surviving entry is touched; ranking
    stays sort(key=-outlet_count) exactly as before. classifier_by_id=None
    (legacy callers, selftest baseline) disables the guard entirely."""
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
        # WEEKLY-CONTENT-GUARD (see docstring): strict-combo exclusion on the
        # representative's stored classifier output. Both fields must be
        # present AND equal to their classifier's fallback label.
        if classifier_by_id is not None:
            rep_domain, rep_nature = classifier_by_id.get(
                representative_id, (None, None))
            if (rep_domain == DOMAIN_FALLBACK_LABEL
                    and rep_nature == NATURE_FALLBACK_LABEL):
                continue
        entries.append({
            "stable_id": cluster.get("stable_id"),
            # CLAIM-LINK: the DURABLE lineage id (assign_lineage_ids), straight
            # from the graph cluster — /web/claim.html?id= links use this, never
            # stable_id (a membership hash that churns as clusters grow). None on
            # rows from graphs predating lineage; weekly.html then resolves it
            # client-side from representative_analysis_id. Past stored weekly
            # rows are NEVER regenerated to backfill it.
            "lineage_id": cluster.get("lineage_id"),
            "title": label_title,
            "representative_analysis_id": representative_id,
            "outlet_count": cluster.get("outlet_count"),
            "size_label": cluster.get("size_label"),
            # WEEKLY-PAGE-ENRICH: additive circulation field — distinct outlets whose
            # title+claim cosine >= 0.95 to the cluster's earliest-published anchor
            # ("첫 보도와 문구 거의 동일"). Copied straight from the same graph cluster
            # object build_brainmap_graph already computed; None on old graphs lacking
            # it. Verdict-isolated (a syndication measurement, never a truth signal).
            "near_anchor_outlet_count": cluster.get("near_anchor_outlet_count"),
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

    # ---- WEEKLY-CONTENT-GUARD cases (situation-pinned, no dates beyond the
    # synthetic window above; the guard values come from the classifier
    # constants themselves, never re-typed here) ----
    BOTH = (DOMAIN_FALLBACK_LABEL, NATURE_FALLBACK_LABEL)
    # (g) both fields matching -> excluded, and the top backfills: with
    # top_n=2, excluding "bbb" promotes "ddd" into the list.
    cls = {4: BOTH}  # representative of cluster 1 ("bbb") is id 4
    p = build_report(graph, published, "2026-07-04", "2026-07-10",
                     top_n=2, classifier_by_id=cls)
    got = [(e["stable_id"], e["rank"]) for e in p["top"]]
    g_ok = got == [("aaa", 1), ("ddd", 2)]
    print("  [%s] (g) strict combo excludes at selection; next cluster "
          "backfills the top" % ("ok" if g_ok else "xx"))
    # (h) only ONE field matching -> kept (both sub-cases).
    p1 = build_report(graph, published, "2026-07-04", "2026-07-10",
                      top_n=10, classifier_by_id={4: (DOMAIN_FALLBACK_LABEL, "government_policy")})
    p2 = build_report(graph, published, "2026-07-04", "2026-07-10",
                      top_n=10, classifier_by_id={4: ("finance", NATURE_FALLBACK_LABEL)})
    h_ok = (len(p1["top"]) == 3 and len(p2["top"]) == 3)
    print("  [%s] (h) one matching field alone never excludes"
          % ("ok" if h_ok else "xx"))
    # (i) both null / one null / row absent -> kept. Absence is not evidence.
    p3 = build_report(graph, published, "2026-07-04", "2026-07-10",
                      top_n=10, classifier_by_id={4: (None, None)})
    p4 = build_report(graph, published, "2026-07-04", "2026-07-10",
                      top_n=10, classifier_by_id={4: (DOMAIN_FALLBACK_LABEL, None)})
    p5 = build_report(graph, published, "2026-07-04", "2026-07-10",
                      top_n=10, classifier_by_id={})
    i_ok = all(len(x["top"]) == 3 for x in (p3, p4, p5))
    print("  [%s] (i) null/absent classifier fields never exclude"
          % ("ok" if i_ok else "xx"))
    # (j) surviving entries are BYTE-IDENTICAL to the unguarded run apart
    # from rank (positions close up on backfill by design) — the guard
    # selects, it never edits a number or a field.
    strip_rank = lambda e: {k: v for k, v in e.items() if k != "rank"}
    base = {e["stable_id"]: strip_rank(e) for e in payload["top"]}
    j_ok = all(base[e["stable_id"]] == strip_rank(e) for e in p["top"])
    print("  [%s] (j) surviving rows byte-identical to the unguarded ranking "
          "(rank positions close up only)" % ("ok" if j_ok else "xx"))

    # (k) ARCHIVE-IMMUTABILITY — every path of the supersession guard, on the
    # pure decision function, so the refusals are demonstrated rather than
    # trusted. No DB is touched by any of these.
    WEEK = "2026-07-20"
    row = (6, "2026-07-28T20:30:14.519469+00:00")
    cases = [
        ("unpublished week writes",
         (None, False, None, None, WEEK), "write"),
        ("unpublished week + force still writes",
         (None, True, None, None, WEEK), "write"),
        ("published week, no force -> skip (unchanged)",
         (row, False, None, None, WEEK), "skip"),
        ("published week + force alone -> REFUSE",
         (row, True, None, None, WEEK), "refuse"),
        ("force + supersede naming a DIFFERENT week -> REFUSE",
         (row, True, "2026-07-14", "fixing a bad graph", WEEK), "refuse"),
        ("force + supersede + blank reason -> REFUSE",
         (row, True, WEEK, "   ", WEEK), "refuse"),
        ("force + supersede + reason -> supersede",
         (row, True, WEEK, "regenerated after graph rebuild", WEEK), "supersede"),
    ]
    k_ok = True
    for label, args_tuple, want in cases:
        got, msg = supersede_decision(*args_tuple)
        if got != want:
            k_ok = False
            print("    [xx] %s: got %r want %r" % (label, got, want))
    # the refusal must name the week and the row it protects, or an operator
    # cannot act on it
    _, refusal = supersede_decision(row, True, None, None, WEEK)
    k_ok = k_ok and WEEK in refusal and "id=6" in refusal and "REFUSING" in refusal
    print("  [%s] (k) supersession guard: %d paths, refusal names the week and "
          "the served row" % ("ok" if k_ok else "xx", len(cases)))

    ok = all([a_ok, b_ok, c_ok, d_ok, e_ok, f_ok, g_ok, h_ok, i_ok, j_ok, k_ok])
    print()
    print("SELFTEST: %s" % ("PASS (ranking + window filter + representative + "
                            "fallback + undated tolerance + honesty + "
                            "content-guard + archive-immutability)"
                            if ok else "FAIL"))
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
                             "(appends; the API serves the newest per week). "
                             "NOT sufficient on its own — see --supersede.")
    # ARCHIVE-IMMUTABILITY: the two flags --force cannot be used without once a
    # week is already published. Both are required, and --supersede must name
    # the week being written, so neither can be satisfied by habit.
    parser.add_argument("--supersede", default=None, metavar="YYYY-MM-DD",
                        help="State that you intend to replace the PUBLISHED "
                             "snapshot of this exact week. Must equal the week "
                             "being written. Required with --force when a row "
                             "already exists.")
    parser.add_argument("--supersede-reason", default=None, metavar="TEXT",
                        help="Why the published week is being replaced. Stored "
                             "on the new row so the supersession is "
                             "discoverable from the data.")
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
        with conn.cursor() as cur:
            cur.execute(SELECT_CLASSIFIER_SQL)
            classifier_by_id = {row_id: (domain, nature)
                                for row_id, domain, nature in cur.fetchall()}

        payload = build_report(graph, published_by_id, week_start, week_end,
                               top_n=args.top_n,
                               classifier_by_id=classifier_by_id)
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
            # ARCHIVE-IMMUTABILITY: fail closed. If we cannot positively read
            # whether this week is already published, we do not write — an
            # unreadable archive is not evidence that there is nothing to
            # protect.
            try:
                cur.execute(SELECT_EXISTING_WEEK_SQL, (week_start,))
                existing = cur.fetchone()
            except Exception as lookup_error:
                print("[weekly] REFUSING: cannot determine whether week_start=%s "
                      "is already published (%s). Refusing rather than risking a "
                      "silent supersession."
                      % (week_start, type(lookup_error).__name__))
                return 1
            action, message = supersede_decision(
                existing, args.force, args.supersede,
                args.supersede_reason, week_start)
            if message:
                print(message)
            if action == "skip":
                return 0
            if action == "refuse":
                return 1
            if action == "supersede":
                # Recorded ON THE ROW so the supersession is discoverable with a
                # SELECT, not only from a shell history. Post-hoc metadata,
                # exactly like generated_at / graph_build_ref above: nothing the
                # ranking, the window filter or any entry depends on is touched.
                payload["supersedes"] = {
                    "report_id": existing[0],
                    "generated_at": str(existing[1] or ""),
                    "reason": args.supersede_reason.strip(),
                }
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
