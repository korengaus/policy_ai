# PREDICTION-LOG B4 Phase 2a — score-then-log weekly spread-prediction child.
#
# Builds the auditable track record: each week the top rising clusters (the
# /api/trending signal) are logged as PREDICTIONS ("this cluster will keep
# circulating"), and last week's predictions are scored by member-set overlap
# against the current graph. Rows only — no UI, no API exposure (B4).
#
# ★HARD LINE — SPREAD/circulation continuation ONLY. The schema has NO column
# that could hold a truth/falsity/veracity value: every column is an id, a
# count, a date, a set-overlap ratio, or a closed spread-outcome word
# (grew/held/faded/unmeasurable). Pinned by tests/test_prediction_log_weekly.py
# running honesty_guard._is_truth_probability_key over every DDL column name.
#
# SCORE-THEN-LOG in one run (score FIRST so a just-logged prediction is never
# scored in the same run):
#   1. SCORE: for each unscored prediction whose horizon elapsed, find the
#      newest graph's best cluster by CONTAINMENT |A∩B|/|A| >= 0.6 against the
#      stored member ids (symmetric Jaccard recorded for AUDIT only — a merge
#      gives containment 1.0 but Jaccard 0.25, and Jaccard-only matching would
#      mark exactly the most successful predictions unmeasurable). Outcome by
#      outlet comparison: grew / held / faded; below threshold (split/vanish)
#      -> unmeasurable (honest). ONE prediction_scores row per prediction.
#   2. LOG: top-5 rising clusters (growth > 0) from the two newest snapshot
#      batches (the exact _compute_trending semantics, duplicated below and
#      behaviorally sync-pinned against api_server._compute_trending by the
#      tests); member ids come from the CURRENT batch's graph_ref row.
#
# SAFETY:
#   * APPEND-ONLY: both tables are INSERT-only — no UPDATE, no DELETE, no
#     upsert anywhere in this file (prediction rows are immutable for audit;
#     the snapshot script's selftest precedent, pinned by tests).
#   * Idempotent: logging skips when rows exist for (prediction_date,
#     graph_ref); scoring skips predictions that already have a score row.
#   * VERDICT-ISOLATED: reads brainmap_snapshots (ids/counts/dates) and
#     brainmap_graph.graph_json ONLY — no verdict_label / truth_claim /
#     policy_confidence column is ever selected. Writes ONLY the two
#     self-created prediction tables (CREATE TABLE IF NOT EXISTS, the
#     brainmap_snapshots / weekly_reports precedent).
#   * Fail-closed: refuses without DATABASE_URL; refuses to write without
#     USE_POSTGRES_WRITE=true (--dry-run needs only DATABASE_URL).
#     <2 snapshot batches -> exit 0 (insufficient history, trending's posture).
#     Never prints DATABASE_URL or any API key. No numpy.
#
# USAGE (operator / future weekly_spine step 5 — wiring is slice 2b):
#   python scripts/prediction_log_weekly.py --selftest   # offline check
#   python scripts/prediction_log_weekly.py --dry-run    # compute, no writes
#   python scripts/prediction_log_weekly.py              # real (needs USE_POSTGRES_WRITE=true)

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunables (change only with a Phase-1-style design pass).
# ---------------------------------------------------------------------------
HORIZON_DAYS = 7
DEFAULT_TOP_N = 5
# Containment threshold for "same story cluster, one week later". Merges pass
# (containment stays 1.0); splits/vanishes honestly fall to unmeasurable.
CONTAINMENT_THRESHOLD = 0.6
# The ONLY direction this system ever predicts — circulation continuation.
PREDICTED_DIRECTION = "continue_spreading"
# Fixed honest framing stored on every prediction row. If these rows are EVER
# exposed via an API, this string must FIRST be added to
# honesty_guard.FRAMING_WHITELIST (+ its sync test) — see the B4 design doc.
FRAMING_TEXT = "확산 지속 예측 · 보도 확산에 대한 기록이며 사실 검증 아님"

# --- Reads (verdict-free by construction: ids/counts/dates/json only) -------
SELECT_SNAPSHOT_KEYS_SQL = (
    "SELECT snapshot_date, graph_ref, MAX(id) AS max_id "
    "FROM brainmap_snapshots "
    "GROUP BY snapshot_date, graph_ref "
    "ORDER BY max_id DESC LIMIT 2"
)
SELECT_SNAPSHOT_ROWS_SQL = (
    "SELECT cluster_stable_id, outlet_count, member_count "
    "FROM brainmap_snapshots WHERE snapshot_date = %s AND graph_ref = %s"
)
SELECT_GRAPH_BY_ID_SQL = (
    "SELECT id, graph_json FROM brainmap_graph WHERE id = %s"
)
SELECT_NEWEST_GRAPH_SQL = (
    "SELECT id, graph_json FROM brainmap_graph ORDER BY id DESC LIMIT 1"
)
SELECT_UNSCORED_SQL = (
    "SELECT p.id, p.prediction_date, p.horizon_days, p.member_ids_json, "
    "p.outlets_at_prediction FROM prediction_log p "
    "WHERE NOT EXISTS (SELECT 1 FROM prediction_scores s "
    "WHERE s.prediction_id = p.id) ORDER BY p.id"
)
SELECT_EXISTING_BATCH_SQL = (
    "SELECT id FROM prediction_log "
    "WHERE prediction_date = %s AND graph_ref = %s LIMIT 1"
)

# --- The ONLY writes: two additive, self-created, INSERT-only tables. -------
CREATE_PREDICTION_LOG_SQL = (
    "CREATE TABLE IF NOT EXISTS prediction_log ("
    "id SERIAL PRIMARY KEY, "
    "prediction_date TEXT, "
    "horizon_days INTEGER, "
    "graph_ref INTEGER, "
    "snapshot_date TEXT, "
    "cluster_stable_id TEXT, "
    "member_ids_json TEXT, "
    "outlets_at_prediction INTEGER, "
    "member_count_at_prediction INTEGER, "
    "growth_at_prediction INTEGER, "
    "is_new INTEGER, "
    "predicted_direction TEXT, "
    "framing TEXT, "
    "created_at TEXT)"
)
CREATE_PREDICTION_SCORES_SQL = (
    "CREATE TABLE IF NOT EXISTS prediction_scores ("
    "id SERIAL PRIMARY KEY, "
    "prediction_id INTEGER, "
    "scored_date TEXT, "
    "scored_graph_ref INTEGER, "
    "matched_stable_id TEXT, "
    "match_containment REAL, "
    "match_jaccard REAL, "
    "outlets_at_score INTEGER, "
    "outcome TEXT, "
    "created_at TEXT)"
)
INSERT_PREDICTION_SQL = (
    "INSERT INTO prediction_log "
    "(prediction_date, horizon_days, graph_ref, snapshot_date, "
    "cluster_stable_id, member_ids_json, outlets_at_prediction, "
    "member_count_at_prediction, growth_at_prediction, is_new, "
    "predicted_direction, framing, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
INSERT_SCORE_SQL = (
    "INSERT INTO prediction_scores "
    "(prediction_id, scored_date, scored_graph_ref, matched_stable_id, "
    "match_containment, match_jaccard, outlets_at_score, outcome, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


# ---------------------------------------------------------------------------
# Pure helpers (offline-testable; no DB, no network).
# ---------------------------------------------------------------------------
def compute_trending(current_rows, previous_rows, limit):
    """DUPLICATED from api_server._compute_trending (pure) so the cron child
    never imports the FastAPI app. Behaviorally sync-pinned by
    tests/test_prediction_log_weekly.py::TrendingSignalSyncTests — same
    inputs MUST produce identical outputs, or the pin fails."""
    current = {sid: (outlets, members)
               for sid, outlets, members in current_rows if sid}
    previous = {sid: outlets for sid, outlets, _ in previous_rows if sid}
    entries = []
    for sid, (outlets, members) in current.items():
        prev_outlets = previous.get(sid)
        is_new = prev_outlets is None
        growth = outlets if is_new else outlets - prev_outlets
        entries.append({
            "cluster_stable_id": sid,
            "representative_analysis_id": None,
            "title": "",
            "current_outlet_count": outlets,
            "previous_outlet_count": prev_outlets,
            "growth": growth,
            "is_new": is_new,
            "member_count": members,
        })
    entries.sort(key=lambda e: (-e["growth"], -e["current_outlet_count"],
                                e["cluster_stable_id"]))
    return entries[:limit]


def cluster_index_from_graph(graph):
    """graph JSON dict -> {stable_id: {"member_ids": set, "outlet_count": int}}.
    Member analysis ids come from the node list (build_brainmap_graph writes
    node.id + node.cluster_id; cluster.stable_id maps the positional id)."""
    members_by_cid = {}
    for node in (graph.get("nodes") or []):
        cid = node.get("cluster_id")
        node_id = node.get("id")
        if cid is None or node_id is None:
            continue
        members_by_cid.setdefault(cid, set()).add(node_id)
    index = {}
    for cluster in (graph.get("clusters") or []):
        stable_id = cluster.get("stable_id")
        if not stable_id:
            continue
        index[stable_id] = {
            "member_ids": members_by_cid.get(cluster.get("cluster_id")) or set(),
            "outlet_count": int(cluster.get("outlet_count") or 0),
        }
    return index


def containment_and_jaccard(predicted_ids, candidate_ids):
    """(containment |A∩B|/|A|, jaccard |A∩B|/|A∪B|) for A=predicted set.
    Empty A or B -> (0.0, 0.0)."""
    a, b = set(predicted_ids), set(candidate_ids)
    if not a or not b:
        return 0.0, 0.0
    inter = len(a & b)
    return inter / len(a), inter / len(a | b)


def best_match(predicted_ids, cluster_index,
               threshold=CONTAINMENT_THRESHOLD):
    """Best current cluster for a stored member set, by containment (PRIMARY
    — survives merges), tie-broken by jaccard then stable_id (deterministic).
    Returns (stable_id, containment, jaccard, outlet_count) or None when no
    cluster reaches the threshold (split/vanish -> honest unmeasurable)."""
    best = None
    for stable_id in sorted(cluster_index):
        info = cluster_index[stable_id]
        containment, jaccard = containment_and_jaccard(
            predicted_ids, info["member_ids"])
        key = (containment, jaccard, stable_id)
        if best is None or key > best[0]:
            best = (key, stable_id, containment, jaccard, info["outlet_count"])
    if best is None or best[2] < threshold:
        return None
    return best[1], best[2], best[3], best[4]


def spread_outcome(outlets_at_prediction, outlets_at_score):
    """Closed spread-outcome vocabulary. Outlet counts are cumulative
    distinct-outlet counts, so the honest scoreboard question is
    grew-vs-not; 'faded' stays reachable via splits."""
    if outlets_at_score > outlets_at_prediction:
        return "grew"
    if outlets_at_score < outlets_at_prediction:
        return "faded"
    return "held"


def horizon_elapsed(prediction_date, horizon_days, today):
    """True when prediction_date + horizon_days <= today (ISO date strings /
    date objects). Malformed dates -> False (never score on bad provenance)."""
    try:
        pred = (prediction_date if isinstance(prediction_date, date)
                else date.fromisoformat(str(prediction_date)[:10]))
        now = (today if isinstance(today, date)
               else date.fromisoformat(str(today)[:10]))
    except ValueError:
        return False
    return pred + timedelta(days=int(horizon_days or HORIZON_DAYS)) <= now


def build_prediction_rows(trending_entries, cluster_index, prediction_date,
                          graph_ref, snapshot_date, top_n=DEFAULT_TOP_N):
    """Pure: ranked trending entries -> prediction_log row dicts. Keeps only
    growth > 0 (a shrinking/flat cluster is not a continuation call), caps at
    top_n, and requires the cluster's member set to resolve in the SAME
    graph_ref graph (it always should — the snapshot batch was cut from it)."""
    rows = []
    for entry in trending_entries:
        if len(rows) >= top_n:
            break
        if (entry.get("growth") or 0) <= 0:
            continue
        stable_id = entry.get("cluster_stable_id") or ""
        info = cluster_index.get(stable_id)
        if not info or not info["member_ids"]:
            continue
        member_ids = sorted(info["member_ids"])
        rows.append({
            "prediction_date": prediction_date,
            "horizon_days": HORIZON_DAYS,
            "graph_ref": graph_ref,
            "snapshot_date": snapshot_date,
            "cluster_stable_id": stable_id,
            "member_ids_json": json.dumps(member_ids),
            "outlets_at_prediction": int(entry.get("current_outlet_count") or 0),
            "member_count_at_prediction": len(member_ids),
            "growth_at_prediction": int(entry.get("growth") or 0),
            "is_new": 1 if entry.get("is_new") else 0,
            "predicted_direction": PREDICTED_DIRECTION,
            "framing": FRAMING_TEXT,
        })
    return rows


def score_prediction(member_ids_json, outlets_at_prediction, cluster_index,
                     threshold=CONTAINMENT_THRESHOLD):
    """Pure: one stored prediction vs the current cluster index -> score-row
    fields (matched_stable_id, containment, jaccard, outlets_at_score,
    outcome). Unparseable/empty member sets and below-threshold overlap ->
    unmeasurable with matched_stable_id ''. Never guesses."""
    try:
        predicted_ids = json.loads(member_ids_json or "[]")
    except (TypeError, ValueError):
        predicted_ids = []
    if not isinstance(predicted_ids, list):
        predicted_ids = []
    match = best_match(predicted_ids, cluster_index, threshold)
    if match is None:
        return {"matched_stable_id": "", "match_containment": 0.0,
                "match_jaccard": 0.0, "outlets_at_score": 0,
                "outcome": "unmeasurable"}
    stable_id, containment, jaccard, outlets_now = match
    return {
        "matched_stable_id": stable_id,
        "match_containment": round(containment, 4),
        "match_jaccard": round(jaccard, 4),
        "outlets_at_score": outlets_now,
        "outcome": spread_outcome(int(outlets_at_prediction or 0), outlets_now),
    }


def ddl_column_names(create_sql):
    """Column names out of a CREATE TABLE statement (for the honesty pin)."""
    inner = create_sql[create_sql.index("(") + 1:create_sql.rindex(")")]
    names = []
    for chunk in inner.split(","):
        first = chunk.strip().split()
        if first:
            names.append(first[0].lower())
    return names


# ---------------------------------------------------------------------------
# DB phases (score first, then log).
# ---------------------------------------------------------------------------
def run_score(conn, today_iso, created_at, dry_run):
    """Score every unscored prediction whose horizon elapsed against the
    NEWEST graph. Returns (scored, skipped_not_due, missing_tables)."""
    with conn.cursor() as cur:
        try:
            cur.execute(SELECT_UNSCORED_SQL)
            unscored = cur.fetchall()
        except Exception:
            # First-ever run (tables absent). Real mode creates them right
            # before this; dry-run must not CREATE — treat as nothing to score.
            conn.rollback()
            print("[score] prediction tables absent — nothing to score yet.")
            return 0, 0, True
    if not unscored:
        print("[score] no unscored predictions.")
        return 0, 0, False

    with conn.cursor() as cur:
        cur.execute(SELECT_NEWEST_GRAPH_SQL)
        graph_row = cur.fetchone()
    if not graph_row or not graph_row[1]:
        print("[score] no brainmap_graph row — cannot score; leaving "
              "%d predictions unscored." % len(unscored))
        return 0, len(unscored), False
    scored_graph_ref = graph_row[0]
    try:
        graph = json.loads(graph_row[1])
    except (TypeError, ValueError):
        print("[score] newest graph row holds invalid JSON — leaving "
              "predictions unscored.")
        return 0, len(unscored), False
    cluster_index = cluster_index_from_graph(graph)

    scored = skipped = 0
    for pid, pred_date, horizon, member_ids_json, outlets_at_pred in unscored:
        if not horizon_elapsed(pred_date, horizon, today_iso):
            skipped += 1
            continue
        fields = score_prediction(member_ids_json, outlets_at_pred,
                                  cluster_index)
        print("[score] prediction id=%s (%s): %s (containment=%.2f "
              "jaccard=%.2f outlets %s -> %s)"
              % (pid, pred_date, fields["outcome"],
                 fields["match_containment"], fields["match_jaccard"],
                 outlets_at_pred, fields["outlets_at_score"]))
        if dry_run:
            scored += 1
            continue
        with conn.cursor() as cur:
            cur.execute(INSERT_SCORE_SQL, (
                pid, today_iso, scored_graph_ref,
                fields["matched_stable_id"], fields["match_containment"],
                fields["match_jaccard"], fields["outlets_at_score"],
                fields["outcome"], created_at,
            ))
        scored += 1
    if not dry_run and scored:
        conn.commit()
    print("[score] scored=%d not_yet_due=%d%s"
          % (scored, skipped, " (DRY-RUN — nothing written)" if dry_run else ""))
    return scored, skipped, False


def run_log(conn, today_iso, created_at, dry_run, top_n):
    """Log this week's predictions from the two newest snapshot batches.
    Returns the number of prediction rows written (0 on every clean-skip)."""
    with conn.cursor() as cur:
        try:
            cur.execute(SELECT_SNAPSHOT_KEYS_SQL)
            keys = cur.fetchall()
        except Exception:
            conn.rollback()
            print("[log] brainmap_snapshots absent — run the spine's snapshot "
                  "step first. Nothing to log.")
            return 0
    if len(keys) < 2:
        print("[log] insufficient snapshot history (%d batch%s) — need two "
              "distinct batches for a growth signal. Nothing to log."
              % (len(keys), "" if len(keys) == 1 else "es"))
        return 0

    batches = []
    with conn.cursor() as cur:
        for snapshot_date, graph_ref, _max_id in keys:
            cur.execute(SELECT_SNAPSHOT_ROWS_SQL, (snapshot_date, graph_ref))
            batches.append({
                "snapshot_date": snapshot_date,
                "graph_ref": graph_ref,
                "rows": [(r[0], r[1], r[2]) for r in cur.fetchall()],
            })
    current, previous = batches[0], batches[1]

    with conn.cursor() as cur:
        cur.execute(SELECT_GRAPH_BY_ID_SQL, (current["graph_ref"],))
        graph_row = cur.fetchone()
    if not graph_row or not graph_row[1]:
        print("[log] graph_ref=%s not found — cannot resolve member ids; "
              "nothing to log." % current["graph_ref"])
        return 0
    try:
        cluster_index = cluster_index_from_graph(json.loads(graph_row[1]))
    except (TypeError, ValueError):
        print("[log] graph_ref=%s holds invalid JSON — nothing to log."
              % current["graph_ref"])
        return 0

    entries = compute_trending(current["rows"], previous["rows"],
                               max(top_n, DEFAULT_TOP_N))
    rows = build_prediction_rows(entries, cluster_index, today_iso,
                                 current["graph_ref"],
                                 str(current["snapshot_date"]), top_n)
    if not rows:
        print("[log] no rising clusters (growth > 0) this window — nothing "
              "to log.")
        return 0
    for row in rows:
        print("[log] %s outlets=%d growth=%d members=%d%s"
              % (row["cluster_stable_id"], row["outlets_at_prediction"],
                 row["growth_at_prediction"],
                 row["member_count_at_prediction"],
                 " (new)" if row["is_new"] else ""))
    if dry_run:
        print("[log] DRY-RUN — no CREATE TABLE, no INSERT.")
        return 0

    with conn.cursor() as cur:
        cur.execute(SELECT_EXISTING_BATCH_SQL,
                    (today_iso, current["graph_ref"]))
        if cur.fetchone():
            print("[log] predictions for (%s, graph_ref=%s) already exist — "
                  "skipping duplicate batch (idempotent)."
                  % (today_iso, current["graph_ref"]))
            return 0
        for row in rows:
            cur.execute(INSERT_PREDICTION_SQL, (
                row["prediction_date"], row["horizon_days"], row["graph_ref"],
                row["snapshot_date"], row["cluster_stable_id"],
                row["member_ids_json"], row["outlets_at_prediction"],
                row["member_count_at_prediction"],
                row["growth_at_prediction"], row["is_new"],
                row["predicted_direction"], row["framing"], created_at,
            ))
    conn.commit()
    print("[log] appended %d prediction_log rows (prediction_date=%s, "
          "graph_ref=%s)." % (len(rows), today_iso, current["graph_ref"]))
    return len(rows)


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — synthetic graphs/batches. No DB, no network.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    failures = []

    def check(name, ok):
        print("  [%s] %s" % ("ok" if ok else "FAIL", name))
        if not ok:
            failures.append(name)

    print("=== PREDICTION-LOG --selftest (offline; no DB, no network) ===")

    # (a) MERGE: A(10 ids) absorbed into B(40 ids). Containment 1.0 matches;
    #     Jaccard 0.25 would have failed a 0.5 Jaccard gate (the churn trap).
    predicted = list(range(1, 11))
    merged_index = {"big": {"member_ids": set(range(1, 41)), "outlet_count": 12}}
    match = best_match(predicted, merged_index)
    containment, jaccard = containment_and_jaccard(predicted, range(1, 41))
    check("(a) merge matches via containment (1.0), jaccard only 0.25",
          match is not None and match[0] == "big" and containment == 1.0
          and abs(jaccard - 0.25) < 1e-9 and jaccard < 0.5)

    # (b) SPLIT: members scattered 5/5 -> best containment 0.5 < 0.6 ->
    #     unmeasurable (honest).
    split_index = {
        "shard1": {"member_ids": set(range(1, 6)), "outlet_count": 3},
        "shard2": {"member_ids": set(range(6, 11)), "outlet_count": 3},
    }
    fields = score_prediction(json.dumps(predicted), 5, split_index)
    check("(b) split -> unmeasurable (best containment 0.5 < 0.6)",
          fields["outcome"] == "unmeasurable"
          and fields["matched_stable_id"] == "")

    # (c) VANISH: no overlap at all -> unmeasurable.
    gone_index = {"other": {"member_ids": {900, 901}, "outlet_count": 2}}
    fields = score_prediction(json.dumps(predicted), 5, gone_index)
    check("(c) vanished -> unmeasurable", fields["outcome"] == "unmeasurable")

    # (d) outcomes by outlet comparison on an identical-membership match.
    same_index = {"same": {"member_ids": set(predicted), "outlet_count": 9}}
    grew = score_prediction(json.dumps(predicted), 5, same_index)
    held = score_prediction(json.dumps(predicted), 9, same_index)
    faded = score_prediction(json.dumps(predicted), 12, same_index)
    check("(d) outlet compare -> grew/held/faded",
          grew["outcome"] == "grew" and held["outcome"] == "held"
          and faded["outcome"] == "faded"
          and grew["match_containment"] == 1.0)

    # (e) logging filter: growth>0 only, top-N cap, member ids resolved,
    #     direction/framing constants carried.
    current = [("up", 8, 5), ("flat", 4, 3), ("new", 6, 4), ("down", 2, 2)]
    previous = [("up", 5, 4), ("flat", 4, 3), ("down", 3, 2)]
    entries = compute_trending(current, previous, 10)
    index = {sid: {"member_ids": {i, i + 1}, "outlet_count": o}
             for i, (sid, o, _m) in enumerate(current)}
    rows = build_prediction_rows(entries, index, "2026-07-13", 42,
                                 "2026-07-13", top_n=5)
    sids = [r["cluster_stable_id"] for r in rows]
    check("(e) growth>0 only, ranked (new=6 > up=3), constants carried",
          sids == ["new", "up"]
          and all(r["predicted_direction"] == PREDICTED_DIRECTION
                  and r["framing"] == FRAMING_TEXT
                  and r["horizon_days"] == HORIZON_DAYS for r in rows)
          and rows[0]["is_new"] == 1
          and json.loads(rows[0]["member_ids_json"]) == sorted(index["new"]["member_ids"]))

    # (f) horizon math: due exactly at +7d, not before; malformed -> never due.
    check("(f) horizon: due at +7d, not at +6d, malformed never",
          horizon_elapsed("2026-07-06", 7, "2026-07-13")
          and not horizon_elapsed("2026-07-07", 7, "2026-07-13")
          and not horizon_elapsed("not-a-date", 7, "2026-07-13"))

    # (g) append-only SQL: no UPDATE / DELETE / upsert anywhere.
    sql_constants = (SELECT_SNAPSHOT_KEYS_SQL, SELECT_SNAPSHOT_ROWS_SQL,
                     SELECT_GRAPH_BY_ID_SQL, SELECT_NEWEST_GRAPH_SQL,
                     SELECT_UNSCORED_SQL, SELECT_EXISTING_BATCH_SQL,
                     CREATE_PREDICTION_LOG_SQL, CREATE_PREDICTION_SCORES_SQL,
                     INSERT_PREDICTION_SQL, INSERT_SCORE_SQL)
    check("(g) append-only SQL (no UPDATE/DELETE/ON CONFLICT)",
          not any(word in statement.upper()
                  for statement in sql_constants
                  for word in ("UPDATE", "DELETE", "ON CONFLICT")))

    # (h) HONESTY: no DDL column name reads as a truth-probability field, and
    #     no verdict column is named in ANY SQL this script can execute.
    import honesty_guard
    columns = (ddl_column_names(CREATE_PREDICTION_LOG_SQL)
               + ddl_column_names(CREATE_PREDICTION_SCORES_SQL))
    flagged = [c for c in columns
               if honesty_guard._is_truth_probability_key(c)]
    verdict_columns = ("verdict_label", "truth_claim", "policy_confidence",
                       "operator_review_required",
                       "has_genuine_official_support")
    leaked = [w for w in verdict_columns
              for s in sql_constants if w in s]
    check("(h) DDL truth-shape absent (%d columns) + no verdict column in SQL"
          % len(columns), not flagged and not leaked)

    print()
    print("SELFTEST: %s" % ("PASS" if not failures
                            else "FAIL: " + ", ".join(failures)))
    return 0 if not failures else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="prediction_log_weekly",
        description="Score last week's spread predictions (containment "
                    "matching) then log this week's top rising clusters into "
                    "the append-only prediction_log. Circulation continuation "
                    "only — no truth field exists in the schema.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic data; no DB).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + print; NO CREATE TABLE, NO INSERT.")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="Max predictions per week (default %d)."
                             % DEFAULT_TOP_N)
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — point it at the external Postgres.")
        return 0
    if not args.dry_run and os.environ.get(
            "USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — refusing to write. Set it "
              "true, or use --dry-run.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    now = datetime.now(timezone.utc)
    today_iso = now.date().isoformat()
    created_at = now.isoformat()
    print("PREDICTION-LOG — score-then-log, prediction_date=%s top_n=%d%s"
          % (today_iso, args.top_n, " (DRY-RUN)" if args.dry_run else ""))
    with psycopg.connect(url) as conn:
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute(CREATE_PREDICTION_LOG_SQL)
                cur.execute(CREATE_PREDICTION_SCORES_SQL)
            conn.commit()
        run_score(conn, today_iso, created_at, args.dry_run)
        run_log(conn, today_iso, created_at, args.dry_run, args.top_n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
