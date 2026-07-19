# DATA-HEALTH-PROBE — READ-ONLY corpus health diagnostic (SELECT-only).
#
# WHY
# ---
# The accumulated corpus is the moat. This probe answers, IN NUMBERS, how broad
# and how clean it actually is: how many rows, over what span, how they split by
# domain, how often we captured a publish date / an outlet identity, and how
# often the stored claim LOOKS severed. Measurement only — nothing is fixed here
# and nothing is written.
#
# SAFETY
# ------
# Every statement is a SELECT. No INSERT / UPDATE / DELETE / DDL / commit, no
# network call, no LLM call, no URL fetch. Tables touched: `analysis_results`
# and `brainmap_snapshots` (plus `information_schema.columns` for the claim-
# column detection below). Safe to run in the Render Worker Shell.
#
# FIELD-LOCATION NOTES (confirmed by a Phase-1 read before writing)
# -----------------------------------------------------------------
#   * claim  — the live PG `analysis_results` table does NOT reliably carry a
#     top-level `claim_text` column (it predates that column and is absent from
#     postgres_storage._ANALYSIS_RESULTS_ADDED_COLUMNS, so create_all never
#     ALTERs it in; a bare SELECT of it can UndefinedColumn-fail). This probe
#     DETECTS which claim columns physically exist via information_schema and
#     selects only those — the same guard scripts/claim_quality_size_probe.py
#     uses. The authoritative claim source is `claims` (a JSON-serialized list
#     of strings, database.py:396).
#   * domain — real nullable TEXT column. Unclassified is the HYPHENATED
#     compound "기타-미분류" (domain_classifier.FALLBACK_LABEL), NOT bare "기타"
#     (that is a display-only label, frontend/scripts/main.js:300).
#   * publish date — lives in TWO places: the promoted `published_at` column
#     (SPREAD-F1B, NULL until scripts/backfill_published_at.py runs) and the
#     nested debug_summary JSON key `article_published_at` (written at
#     main.py:931). Both are reported so the column-vs-blob gap is VISIBLE.
#   * outlet  — no outlet column. News-outlet identity is the normalized host of
#     `original_url`, per build_brainmap_graph.normalize_outlet_host (ported
#     below rather than imported, to keep this probe dependency-light).
#   * severed — no stored flag exists anywhere. Section 6 is a HEURISTIC and is
#     labelled approximate in the printed output.
#
# Every metric is wrapped so a malformed row or a missing table degrades to a
# single "metric unavailable" line instead of aborting the whole report.

import collections
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import psycopg

# The authoritative truncation cap + sentence-ender predicate. Imported from the
# real extractor (claim_extractor deps are stdlib re + structured_logging) so the
# heuristic can never drift from the code that produced the claims.
from claim_extractor import _CLAIM_MAX_CHARS, _CLAIM_SENTENCE_END

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Claim-bearing columns in the frontend's buildReviewerSafeClaim order. Only the
# ones that physically exist are selected. `evidence_summary` is the stored
# stand-in for the frontend chain's `summary` step.
CLAIM_COLUMNS = ("claim_text", "claims", "evidence_summary", "title")

# Columns that hold a REAL claim (vs a fallback). Severance is measured only on
# these — a title/summary fallback is not a claim and would dilute the rate.
REAL_CLAIM_COLUMNS = ("claim_text", "claims")

DETECT_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'analysis_results' AND column_name = ANY(%s)"
)

# domain_classifier.FALLBACK_LABEL — the hyphenated compound.
UNCLASSIFIED_LABEL = "기타-미분류"

# Terminal punctuation a complete claim may legitimately end on.
TERMINAL_PUNCT = (".", "!", "?")
ELLIPSES = ("...", "…")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(n, total):
    """n/total as a percent string; '—' when total is 0 (never divides by zero)."""
    if not total:
        return "—"
    return "%.1f%%" % (100.0 * n / total)


def _j(s):
    """Parse a JSON TEXT column, tolerant of NULL / malformed (body2_overlap.py)."""
    try:
        return json.loads(s) if s else None
    except (TypeError, ValueError):
        return None


def normalize_outlet_host(url):
    """Outlet identity = normalized host of original_url. Ported verbatim in
    behaviour from scripts/build_brainmap_graph.py:140-155 so this probe's
    "derivable" count matches what the brainmap builder would actually count.
    Missing/unparseable URL -> "" and the caller EXCLUDES empties."""
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except ValueError:
        return ""
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    for prefix in ("www.", "m.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def resolve_claim(row, cols):
    """The rendered 핵심 주장, mirroring buildReviewerSafeClaim's chain:
    claim_text -> claims[0] -> summary -> title, restricted to the columns that
    physically exist. Returns (text, source_column); ('', '') when nothing
    resolves."""
    if "claim_text" in cols:
        text = (row.get("claim_text") or "").strip()
        if text:
            return text, "claim_text"
    if "claims" in cols and row.get("claims"):
        parsed = _j(row.get("claims"))
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    return item.strip(), "claims"
    for fallback in ("evidence_summary", "title"):
        if fallback in cols:
            text = (row.get(fallback) or "").strip()
            if text:
                return text, fallback
    return "", ""


def looks_severed(text):
    """APPROXIMATE. True when the claim ends in an ellipsis (the truncator's own
    marker, claim_extractor._truncate_on_boundary), or is at/over the extractor's
    cap with no sentence ending. Mirrors the ending test the extractor uses
    (_CLAIM_SENTENCE_END) so a Korean terminal syllable counts as clean.
    This measures SUSPECTED severance — it is not a measure of accuracy."""
    s = (text or "").rstrip()
    if not s:
        return False
    if s.endswith(ELLIPSES):
        return True
    if len(s) >= _CLAIM_MAX_CHARS:
        if s.endswith(TERMINAL_PUNCT):
            return False
        # A trailing Korean terminal syllable ends the sentence too; the
        # extractor's regex expects whitespace after it, so probe with a space.
        return not _CLAIM_SENTENCE_END.search(s[-4:] + " ")
    return False


def _section(title):
    print("\n" + title)
    print("-" * len(title))


# ---------------------------------------------------------------------------
# Metrics — each guarded so one bad row cannot abort the report.
# ---------------------------------------------------------------------------

def metric_totals(rows):
    _section("1. CORPUS SIZE")
    print("  Total rows: %d" % len(rows))
    return len(rows)


def metric_date_range(rows):
    _section("2. CORPUS DATE RANGE (created_at, TEXT ISO — lexicographic = chronological)")
    try:
        stamps = sorted(str(r["created_at"]) for r in rows if r.get("created_at"))
        if not stamps:
            print("  metric unavailable: no non-NULL created_at values")
            return
        print("  Earliest row: %s" % stamps[0])
        print("  Latest row:   %s" % stamps[-1])
        print("  Rows missing created_at: %d (%s)"
              % (len(rows) - len(stamps), _pct(len(rows) - len(stamps), len(rows))))
    except Exception as exc:  # noqa: BLE001 — a metric must never abort the report
        print("  metric unavailable: %s" % exc)


def metric_domain(rows, total):
    _section("3. DOMAIN DISTRIBUTION")
    try:
        counts = collections.Counter()
        nulls = 0
        for r in rows:
            value = r.get("domain")
            if value is None or not str(value).strip():
                nulls += 1
                continue
            counts[str(value).strip()] += 1
        for label, n in counts.most_common():
            print("  %-24s %6d  (%s)" % (label, n, _pct(n, total)))
        if not counts:
            print("  (no non-NULL domain values)")
        fallback = counts.get(UNCLASSIFIED_LABEL, 0)
        print("\n  Unclassified breakdown (NOT bare '기타' — that is display-only):")
        print("    domain IS NULL / empty:        %6d  (%s)" % (nulls, _pct(nulls, total)))
        print("    domain = '%s':      %6d  (%s)"
              % (UNCLASSIFIED_LABEL, fallback, _pct(fallback, total)))
        print("    combined unclassified:         %6d  (%s)"
              % (nulls + fallback, _pct(nulls + fallback, total)))
    except Exception as exc:  # noqa: BLE001
        print("  metric unavailable: %s" % exc)


def metric_publish_date(rows, total):
    _section("4. PUBLISH-DATE CAPTURE (column vs JSON blob — the gap is the point)")
    try:
        col = sum(1 for r in rows
                  if r.get("published_at") and str(r["published_at"]).strip())
        blob = 0
        unparseable = 0
        for r in rows:
            raw = r.get("debug_summary")
            if not raw:
                continue
            parsed = _j(raw)
            if parsed is None:
                unparseable += 1
                continue
            if isinstance(parsed, dict):
                value = parsed.get("article_published_at")
                if value and str(value).strip():
                    blob += 1
        print("  (a) published_at column present:              %6d  (%s)"
              % (col, _pct(col, total)))
        print("  (b) debug_summary.article_published_at present: %6d  (%s)"
              % (blob, _pct(blob, total)))
        print("  Gap (in blob but not promoted to column):    %6d rows"
              % max(0, blob - col))
        print("      -> that gap is what scripts/backfill_published_at.py would close.")
        if unparseable:
            print("  Note: %d rows had unparseable debug_summary JSON (excluded from (b))."
                  % unparseable)
    except Exception as exc:  # noqa: BLE001
        print("  metric unavailable: %s" % exc)


def metric_outlet(rows, total):
    _section("5. OUTLET IDENTITY (derivable from original_url host)")
    try:
        hosts = collections.Counter()
        derivable = 0
        for r in rows:
            host = normalize_outlet_host(r.get("original_url"))
            if host:
                derivable += 1
                hosts[host] += 1
        print("  Rows with a derivable outlet host: %6d  (%s)"
              % (derivable, _pct(derivable, total)))
        print("  Distinct outlet hosts:             %6d" % len(hosts))
        print("  (Basis for later distinct-outlet-vs-row analysis; this line is"
              " coverage only, not a syndication rate.)")
        if hosts:
            print("  Top 10 hosts by row count:")
            for host, n in hosts.most_common(10):
                print("    %-30s %6d  (%s)" % (host, n, _pct(n, total)))
    except Exception as exc:  # noqa: BLE001
        print("  metric unavailable: %s" % exc)


def metric_claim_shape(rows, cols):
    _section("6. CLAIM SHAPE — APPROXIMATE (suspected severance, NOT accuracy)")
    try:
        by_source = collections.Counter()
        real = 0
        severed = 0
        empty = 0
        for r in rows:
            text, source = resolve_claim(r, cols)
            if not text:
                empty += 1
                by_source["(none — card falls through)"] += 1
                continue
            by_source[source] += 1
            if source in REAL_CLAIM_COLUMNS:
                real += 1
                if looks_severed(text):
                    severed += 1
        print("  Claim resolved from (frontend chain order):")
        for source, n in by_source.most_common():
            print("    %-28s %6d" % (source, n))
        print("\n  Rows with NO claim at all:            %6d  (%s of all rows)"
              % (empty, _pct(empty, len(rows))))
        print("  Rows with a REAL claim (claim_text/claims): %6d" % real)
        print("  Suspected-severed:                    %6d  (%s of real claims)"
              % (severed, _pct(severed, real)))
        print("  CAVEAT: heuristic only — ends in '…'/'...', or >= %d chars with no"
              % _CLAIM_MAX_CHARS)
        print("  sentence ending. No stored severed/truncated flag exists to check"
              " against.")
    except Exception as exc:  # noqa: BLE001
        print("  metric unavailable: %s" % exc)


def metric_syndication(conn, total):
    _section("7. SYNDICATION SNAPSHOT COVERAGE — PARTIAL (NOT a corpus-wide rate)")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT snapshot_date, graph_ref FROM brainmap_snapshots "
                "ORDER BY snapshot_date DESC, graph_ref DESC LIMIT 1"
            )
            latest = cur.fetchone()
            if not latest:
                print("  metric unavailable: brainmap_snapshots is empty "
                      "(no snapshot has been taken yet)")
                return
            snapshot_date, graph_ref = latest
            cur.execute(
                "SELECT cluster_stable_id, outlet_count, member_count "
                "FROM brainmap_snapshots "
                "WHERE snapshot_date = %s AND graph_ref = %s",
                (snapshot_date, graph_ref),
            )
            snap_rows = cur.fetchall()
        clusters = len(snap_rows)
        with_outlet = sum(1 for _, oc, _m in snap_rows if isinstance(oc, int))
        members = sum(m for _c, _o, m in snap_rows if isinstance(m, int))
        multi = sum(1 for _c, oc, _m in snap_rows if isinstance(oc, int) and oc >= 2)
        print("  Latest snapshot: %s (graph_ref %s)" % (snapshot_date, graph_ref))
        print("  Clusters in that snapshot:              %6d" % clusters)
        print("  Clusters carrying an outlet_count:      %6d" % with_outlet)
        print("  Clusters spanning >= 2 outlets:         %6d" % multi)
        print("  Rows covered by those clusters:         %6d of %d total rows (%s)"
              % (members, total, _pct(members, total)))
        print("  CAVEAT: covers ONLY rows that were clustered AND snapshotted at"
              " that date.")
        print("  It is NOT a corpus-wide syndication rate — the uncovered"
              " remainder is unmeasured here.")
    except Exception as exc:  # noqa: BLE001
        print("  metric unavailable: %s" % exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe must run in the Render Worker Shell.")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    print("=" * 72)
    print("DATA-HEALTH-PROBE — corpus health snapshot (READ-ONLY, SELECT-only)")
    print("=" * 72)

    with psycopg.connect(url) as conn:
        # Detect which claim-bearing columns physically exist. The live table may
        # lack claim_text; a bare SELECT of it would UndefinedColumn-fail.
        with conn.cursor() as cur:
            cur.execute(DETECT_COLUMNS_SQL, (list(CLAIM_COLUMNS),))
            present = {r[0] for r in cur.fetchall()}
        cols = [c for c in CLAIM_COLUMNS if c in present]
        print("\nClaim columns physically present: %s"
              % (", ".join(cols) if cols else "(none)"))
        if "claim_text" not in present:
            print("  (claim_text absent on this table — claims JSON is the"
                  " authoritative source, as expected.)")

        select_cols = ["id", "created_at", "domain", "published_at",
                       "debug_summary", "original_url"]
        select_cols += [c for c in cols if c not in select_cols]
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM analysis_results ORDER BY id"
                        % ", ".join(select_cols))
            rows = [dict(zip(select_cols, r)) for r in cur.fetchall()]

        total = metric_totals(rows)
        metric_date_range(rows)
        metric_domain(rows, total)
        metric_publish_date(rows, total)
        metric_outlet(rows, total)
        metric_claim_shape(rows, cols)
        metric_syndication(conn, total)

    print("\n" + "=" * 72)
    print("READ-ONLY snapshot — SELECT-only; no row was written, updated, or"
          " deleted. Sections 6 and 7 carry the caveats printed above.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
