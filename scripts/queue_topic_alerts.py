# TOPIC-ALERT v0 — keyword surge-alert engine (internal ntfy, spread-only).
#
# Register keywords (scripts/alert_topics.json) -> detect a circulation SURGE
# between the two newest brainmap_snapshots batches -> fire an internal ntfy
# alert per (keyword, cluster). ENGINE REUSE (the whole point):
#   * compute_trending — IMPORTED from prediction_log_weekly (pure, and
#     behaviorally sync-pinned to api_server._compute_trending by its tests).
#   * The two-newest-batch SELECTs — imported from prediction_log_weekly.
#   * The graph keyword lookup — build_cluster_lookup imported from
#     b2b_briefing (same casefolded-substring matcher as the B2B filter).
#   * notify() — duplicated VERBATIM from weekly_spine.py:153-187 (it is
#     spine-local, not importable; weekly_spine.py must not be modified).
#
# VERDICT-ISOLATED (hard):
#   * Reads verdict-free snapshot counts + graph node titles ONLY. The alert
#     is spread/surge framing ("N개 매체로 확산") — NO truth/falsity/
#     probability/verdict field anywhere, no official-source status.
#   * framing reuses the whitelisted weekly string BYTE-EXACT (asserted at
#     import); every payload passes honesty_guard.validate_payload plus a
#     generated-string vocab scan before send — fail-closed per hit.
#
# WRITES: exactly ONE additive INSERT-only table, topic_alert_log (CREATE
# TABLE IF NOT EXISTS — the brainmap_snapshots/weekly_reports precedent).
# Dedupe key (keyword, cluster_stable_id, snapshot_date) suppresses repeat
# alerts across reruns. No UPDATE/DELETE, no other schema.
#
# NO email (정보통신망법 double-opt-in is a deferred legal gate). NOT wired
# into weekly_spine yet (separate step after a real --dry-run).
#
# USAGE (operator, Worker Shell or LOCAL — DATABASE_URL at the external
# Postgres; NTFY_URL/NTFY_TOPIC for real sends, print-fallback otherwise):
#   python scripts/queue_topic_alerts.py --selftest    # DB-free logic check
#   python scripts/queue_topic_alerts.py --dry-run     # compute + print only
#   python scripts/queue_topic_alerts.py               # send + log

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
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

# Engine + honesty reuse (import, never re-implement).
from b2b_briefing import build_cluster_lookup  # noqa: E402
from generate_weekly_report import FRAMING_TEXT  # noqa: E402 — the whitelisted weekly framing
from honesty_guard import (  # noqa: E402
    FORBIDDEN_LABEL_VOCAB,
    FRAMING_WHITELIST,
    validate_payload,
)
from prediction_log_weekly import (  # noqa: E402
    SELECT_NEWEST_GRAPH_SQL,
    SELECT_SNAPSHOT_KEYS_SQL,
    SELECT_SNAPSHOT_ROWS_SQL,
    compute_trending,
)

# Byte-exactness gate (the b2b_briefing precedent): drift refuses to run.
if FRAMING_TEXT not in FRAMING_WHITELIST:
    raise RuntimeError("FRAMING_TEXT drifted from honesty_guard.FRAMING_WHITELIST")

# --- Tunables (conservative v0; change only with a Phase-1-style pass) ------
SURGE_MIN_GROWTH = 2   # existing cluster: +N distinct outlets vs previous batch
NEW_MIN_OUTLETS = 3    # newly-appeared cluster: minimum outlets to count as a surge

DEFAULT_CONFIG = _SCRIPTS_DIR / "alert_topics.json"
CARD_URL = "https://tickedin.org/?result_id=%d"
BRAINMAP_URL = "https://tickedin.org/web/brainmap.html?focus=%d"

# --- The ONLY write: additive INSERT-only alert log (ensure idiom) ----------
CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS topic_alert_log ("
    "id SERIAL PRIMARY KEY, "
    "keyword TEXT, "
    "cluster_stable_id TEXT, "
    "snapshot_date TEXT, "
    "outlet_count INTEGER, "
    "growth INTEGER, "
    "created_at TEXT)"
)
SELECT_EXISTING_ALERT_SQL = (
    "SELECT id FROM topic_alert_log "
    "WHERE keyword = %s AND cluster_stable_id = %s AND snapshot_date = %s "
    "LIMIT 1"
)
INSERT_ALERT_SQL = (
    "INSERT INTO topic_alert_log "
    "(keyword, cluster_stable_id, snapshot_date, outlet_count, growth, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)


# ---------------------------------------------------------------------------
# ntfy — DUPLICATED VERBATIM from weekly_spine.py:153-187 (spine-local helper;
# weekly_spine.py is not modified). Env-driven, PRINT fallback, never crashes.
# ---------------------------------------------------------------------------
def _ntfy_endpoint():
    url = (os.environ.get("NTFY_URL") or "").strip()
    if url:
        return url
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if topic:
        return "https://ntfy.sh/%s" % topic
    return None


def notify(title, message, priority="default"):
    """Send an ntfy notification if NTFY_URL / NTFY_TOPIC is set, else PRINT.
    Best-effort: any send failure degrades to a printed warning — a
    notification problem must NEVER change the run's exit code."""
    endpoint = _ntfy_endpoint()
    banner = "[notify] %s\n%s" % (title, message)
    if not endpoint:
        print(banner)
        print("[notify] (NTFY_URL/NTFY_TOPIC unset — printed above instead of sent)")
        return False
    try:
        req = urllib.request.Request(
            endpoint,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("[notify] sent to %s: %s" % (endpoint, title))
        return True
    except Exception as exc:  # noqa: BLE001 — notify must never crash the run
        print(banner)
        print("[notify] send failed (%s) — printed above instead."
              % type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Pure pipeline (offline-testable; no DB, no network).
# ---------------------------------------------------------------------------
def entry_qualifies(entry):
    """Conservative surge test on one compute_trending entry."""
    growth = entry.get("growth") or 0
    if entry.get("is_new"):
        return (entry.get("current_outlet_count") or 0) >= NEW_MIN_OUTLETS
    return growth >= SURGE_MIN_GROWTH


def _representative_id(cluster):
    """The weekly engine's representative rule: the member whose title IS the
    cluster label (build_brainmap_graph's highest-degree pick), else min id."""
    members = cluster.get("members") or []
    label = cluster.get("label_title") or ""
    ids = [m["id"] for m in members if isinstance(m.get("id"), int)]
    if not ids:
        return None
    for m in sorted(members, key=lambda m: m.get("id") or 0):
        if label and m.get("title") == label:
            return m["id"]
    return min(ids)


def find_hits(trending, lookup, keywords):
    """Qualifying trending entries x registered keywords -> one hit per
    (keyword, stable_id). Matching = casefolded substring over label_title +
    member titles (the b2b_briefing matcher)."""
    hits = []
    for entry in trending:
        if not entry_qualifies(entry):
            continue
        cluster = lookup.get(entry.get("cluster_stable_id"))
        if not cluster:
            continue
        haystacks = [cluster.get("label_title") or ""] + [
            m.get("title") or "" for m in (cluster.get("members") or [])]
        for keyword in keywords:
            if not keyword:
                continue
            if any(keyword.casefold() in h.casefold() for h in haystacks):
                hits.append({"keyword": keyword, "entry": entry, "cluster": cluster})
    return hits


def build_alert_payload(hit):
    """Verdict-free alert payload: spread/surge counts + links only."""
    entry, cluster, keyword = hit["entry"], hit["cluster"], hit["keyword"]
    outlets = entry.get("current_outlet_count") or 0
    growth = entry.get("growth") or 0
    if entry.get("is_new"):
        message = "'%s' 관련 이슈가 새로 확산 (총 %d개 매체)" % (keyword, outlets)
    else:
        message = "'%s' 관련 이슈가 %d개 매체로 확산 (전주 대비 +%d)" % (
            keyword, outlets, growth)
    rep = _representative_id(cluster)
    return {
        "kind": "topic_surge",
        "keyword": keyword,
        "framing": FRAMING_TEXT,
        "message": message,
        "title": cluster.get("label_title") or "",
        "stable_id": entry.get("cluster_stable_id"),
        "outlet_count": outlets,
        "growth": growth,
        "is_new": bool(entry.get("is_new")),
        "card_url": CARD_URL % rep if isinstance(rep, int) else "",
        "brainmap_url": BRAINMAP_URL % rep if isinstance(rep, int) else "",
    }


def alert_honesty_ok(payload):
    """Fail-closed per-hit self-check (the b2b_briefing two-layer pattern):
    (1) honesty_guard.validate_payload — generic I1-I5 walker;
    (2) vocab scan over the strings THIS script generates (framing, message,
        kind) — whitelisted framing bytes exempt; title is journalist
        passthrough (the weekly engine's guard scope) and is NOT scanned."""
    ok, violations = validate_payload(payload)
    for text in (payload.get("framing") or "", payload.get("message") or "",
                 payload.get("kind") or ""):
        if text in FRAMING_WHITELIST:
            continue
        lowered = text.lower()
        for word in FORBIDDEN_LABEL_VOCAB:
            if word in lowered:
                ok = False
                violations.append({
                    "path": "generated", "rule": "ALERT_FORBIDDEN_VOCAB",
                    "detail": "generated string carries %r" % word,
                })
    return ok, violations


def process_hits(hits, snapshot_date, already_sent, record_sent, send):
    """Dedupe + honesty-check + send each hit. Injectable callables so the
    selftest runs with an in-memory set and a capturing send stub.
      already_sent(key) -> bool     key = (keyword, stable_id, snapshot_date)
      record_sent(key, payload)     called only after a successful dispatch
      send(payload)                 the ntfy dispatch (or a stub)
    Returns (sent_count, skipped_duplicates, honesty_failures)."""
    sent = skipped = failed = 0
    for hit in hits:
        payload = build_alert_payload(hit)
        key = (payload["keyword"], payload["stable_id"], snapshot_date)
        if already_sent(key):
            skipped += 1
            continue
        ok, violations = alert_honesty_ok(payload)
        if not ok:
            failed += 1
            print("[alert] HONESTY CHECK FAILED for %r — not sent, not logged. %r"
                  % (key, violations))
            continue
        send(payload)
        record_sent(key, payload)
        sent += 1
    return sent, skipped, failed


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — synthetic batches + graph. No DB, no network, no
# DATABASE_URL, notify stubbed (nothing is POSTed).
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    keywords = ["최저임금", "가계대출", "기초연금"]
    # Two synthetic batches: (stable_id, outlet_count, member_count) rows.
    previous_rows = [("s_surge", 2, 3), ("s_below", 2, 2)]
    current_rows = [("s_surge", 4, 5),   # growth +2 -> qualifies, keyword hit
                    ("s_below", 3, 3),   # growth +1 -> below threshold
                    ("s_new", 3, 3),     # new, outlets 3 -> qualifies, keyword hit
                    ("s_nokw", 5, 6)]    # new, outlets 5 -> qualifies, NO keyword
    graph = {
        "nodes": [
            {"id": 1, "cluster_id": "A", "title": "최저임금 인상안 발표"},
            {"id": 2, "cluster_id": "A", "title": "내년 최저임금 논의 착수"},
            {"id": 3, "cluster_id": "B", "title": "기초연금 개편 논의"},
            {"id": 4, "cluster_id": "C", "title": "가계대출 관리 방안"},
            {"id": 5, "cluster_id": "C", "title": "가계대출 증가세 지속"},
            {"id": 6, "cluster_id": "D", "title": "우주 발사체 발사 성공"},
        ],
        "clusters": [
            {"cluster_id": "A", "stable_id": "s_surge",
             "label_title": "최저임금 인상안 발표", "outlet_count": 4},
            {"cluster_id": "B", "stable_id": "s_below",
             "label_title": "기초연금 개편 논의", "outlet_count": 3},
            {"cluster_id": "C", "stable_id": "s_new",
             "label_title": "가계대출 관리 방안", "outlet_count": 3},
            {"cluster_id": "D", "stable_id": "s_nokw",
             "label_title": "우주 발사체 발사 성공", "outlet_count": 5},
        ],
    }
    snapshot_date = "2026-07-13"

    trending = compute_trending(current_rows, previous_rows, 10 ** 6)
    lookup = build_cluster_lookup(graph)
    hits = find_hits(trending, lookup, keywords)
    hit_keys = {(h["keyword"], h["entry"]["cluster_stable_id"]) for h in hits}

    # (a) growth>=2 cluster with keyword -> alert.
    a_ok = ("최저임금", "s_surge") in hit_keys
    # (b) is_new cluster with outlets>=3 and keyword -> alert.
    b_ok = ("가계대출", "s_new") in hit_keys
    # (c) below-threshold cluster -> no alert (its 기초연금 keyword matches,
    # so this proves the THRESHOLD, not the matcher, is what drops it).
    c_ok = not any(sid == "s_below" for _, sid in hit_keys)
    # (d) surging cluster with NO keyword -> no alert.
    d_ok = not any(sid == "s_nokw" for _, sid in hit_keys)

    # (e)+(f)+(g)+(h) via the injectable process loop (in-memory dedupe,
    # capturing send stub — nothing POSTed).
    seen, sent_payloads = set(), []
    sent1, skip1, fail1 = process_hits(
        hits, snapshot_date, seen.__contains__,
        lambda key, payload: seen.add(key), sent_payloads.append)
    sent2, skip2, fail2 = process_hits(
        hits, snapshot_date, seen.__contains__,
        lambda key, payload: seen.add(key), sent_payloads.append)
    e_ok = sent1 == len(hits) and sent2 == 0 and skip2 == len(hits)
    # (f) titles come from the GRAPH lookup (compute_trending's title is "").
    f_ok = all(p["title"] for p in sent_payloads)
    # (g) honesty self-check passes on every payload (0 failures recorded).
    g_ok = fail1 == 0 and fail2 == 0 and all(
        alert_honesty_ok(p)[0] for p in sent_payloads)
    # (h) no forbidden vocab in any sent message/framing/kind except the
    # whitelisted framing bytes.
    blob = json.dumps(
        [{k: p[k] for k in ("framing", "message", "kind")} for p in sent_payloads],
        ensure_ascii=False)
    for allowed in FRAMING_WHITELIST:
        blob = blob.replace(allowed, "")
    h_ok = not any(w in blob.lower() for w in FORBIDDEN_LABEL_VOCAB)
    # (i) fewer than 2 batches -> clean insufficient-history exit, no alert.
    i_ok = run_alert_pipeline([("2026-07-13", 1)], None, None, keywords) is None

    checks = {"a_growth_alert": a_ok, "b_new_alert": b_ok,
              "c_below_threshold": c_ok, "d_no_keyword": d_ok,
              "e_dedupe": e_ok, "f_graph_titles": f_ok,
              "g_honesty": g_ok, "h_vocab": h_ok, "i_insufficient": i_ok}
    for name, ok in checks.items():
        print("  %-20s %s" % (name, "ok" if ok else "FAIL"))
    ok = all(checks.values())
    print("SELFTEST: %s (%d hits, %d sent then %d deduped; self-check path: "
          "validate_payload generic walker + generated-string scan)"
          % ("PASS" if ok else "FAIL", len(hits), sent1, skip2))
    return 0 if ok else 1


def run_alert_pipeline(batch_keys, current_rows, previous_rows, keywords):
    """Guard shared by main + selftest case (i): None = insufficient history
    (fewer than 2 distinct snapshot batches); otherwise passthrough inputs."""
    if len(batch_keys) < 2:
        print("[alert] insufficient snapshot history (need 2 batches) — nothing to do.")
        return None
    return current_rows, previous_rows


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="queue_topic_alerts",
        description="Keyword surge alerts over brainmap snapshot diffs "
                    "(internal ntfy; spread-only, verdict-free).",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic fixtures; no DB, no send).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + print would-be alerts; NO send, NO log write.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="Registered keywords JSON (default scripts/alert_topics.json).")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("[alert] cannot read config %s: %s" % (args.config, exc))
        return 1
    keywords = [k for k in (config.get("keywords") or []) if k]
    if not keywords:
        print("[alert] no keywords registered in %s — nothing to do." % args.config)
        return 0

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — point it at the external Postgres.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    print("TOPIC-ALERT — %d keywords%s"
          % (len(keywords), " (DRY-RUN)" if args.dry_run else ""))
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_SNAPSHOT_KEYS_SQL)
            batch_keys = [(row[0], row[1]) for row in cur.fetchall()]
        if run_alert_pipeline(batch_keys, True, True, keywords) is None:
            return 0
        (current_date, current_ref), (prev_date, prev_ref) = batch_keys[0], batch_keys[1]
        with conn.cursor() as cur:
            cur.execute(SELECT_SNAPSHOT_ROWS_SQL, (current_date, current_ref))
            current_rows = cur.fetchall()
            cur.execute(SELECT_SNAPSHOT_ROWS_SQL, (prev_date, prev_ref))
            previous_rows = cur.fetchall()
            cur.execute(SELECT_NEWEST_GRAPH_SQL)
            graph_row = cur.fetchone()
        if not graph_row:
            print("[alert] no brainmap_graph row — run scripts/build_brainmap_graph.py first.")
            return 1
        try:
            graph = json.loads(graph_row[1])
        except (TypeError, ValueError):
            print("[alert] newest brainmap_graph row holds invalid JSON — aborting.")
            return 1

        trending = compute_trending(current_rows, previous_rows, 10 ** 6)
        lookup = build_cluster_lookup(graph)
        hits = find_hits(trending, lookup, keywords)
        qualifying = sum(1 for e in trending if entry_qualifies(e))
        print("[alert] batches %s/%s vs %s/%s: %d clusters trending, %d qualifying, %d keyword hits"
              % (current_date, current_ref, prev_date, prev_ref,
                 len(trending), qualifying, len(hits)))

        if args.dry_run:
            for hit in hits:
                payload = build_alert_payload(hit)
                print("[dry-run] would send: %s | %s | %s"
                      % (payload["message"], payload["title"][:60], payload["card_url"]))
            print("[alert] DRY-RUN — nothing sent, nothing logged (%d would-send)." % len(hits))
            return 0

        # Real mode: ensure the log table, dedupe via SELECT-exists, send, INSERT.
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()

        def already_sent(key):
            with conn.cursor() as cur:
                cur.execute(SELECT_EXISTING_ALERT_SQL, key)
                return cur.fetchone() is not None

        def record_sent(key, payload):
            with conn.cursor() as cur:
                cur.execute(INSERT_ALERT_SQL, (
                    key[0], key[1], key[2],
                    payload["outlet_count"], payload["growth"],
                    datetime.now(timezone.utc).isoformat(),
                ))
            conn.commit()

        def send(payload):
            notify(
                title="[tickedin] '%s' 확산 알림" % payload["keyword"],
                message="%s\n\n%s" % (payload["message"], payload["card_url"]),
                priority="default",
            )

        sent, skipped, failed = process_hits(
            hits, current_date, already_sent, record_sent, send)
        print("[alert] done: %d sent, %d skipped as duplicate, %d honesty-failed."
              % (sent, skipped, failed))
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
