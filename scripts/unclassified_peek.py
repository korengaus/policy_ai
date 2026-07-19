# UNCLASSIFIED-PEEK — READ-ONLY look inside the 기타-미분류 bucket (SELECT-only).
#
# WHY
# ---
# scripts/data_health_probe.py measured the unclassified bucket at ~1,674 rows
# (12.7% of the corpus). This probe answers the follow-up: WHAT is actually in
# there? The output is raw signal — a token table, outlet table, and samples —
# for a human to read and recognize MISSING policy domains (문화/체육/과학기술/
# 국방/외교/교통/에너지/...) that deserve their own collection seeds.
#
# It deliberately does NOT auto-classify. Auto-labelling here would launder a
# guess into a number; the operator eyeballs the raw signal instead.
#
# SAFETY
# ------
# Every statement is a SELECT. No INSERT / UPDATE / DELETE / DDL / commit, no
# network call, no LLM call, no URL fetch. Tables touched: `analysis_results`
# only (plus `information_schema.columns` for the claim-column detection).
# Safe to run in the Render Worker Shell.
#
# FIELD-LOCATION NOTES (carried over from data_health_probe.py)
# ------------------------------------------------------------
#   * The live PG `analysis_results` table does NOT reliably carry a top-level
#     `claim_text` column (it predates that column and is absent from
#     postgres_storage._ANALYSIS_RESULTS_ADDED_COLUMNS, so a bare SELECT of it
#     can UndefinedColumn-fail). Claim columns are DETECTED via
#     information_schema and only the present ones are selected.
#   * The unclassified label is the HYPHENATED compound "기타-미분류"
#     (domain_classifier.FALLBACK_LABEL), NOT bare "기타".
#   * Outlet identity = normalized host of original_url, same normalization as
#     scripts/build_brainmap_graph.py:140-155.
#
# TOKENIZATION CAVEAT (printed in the output too)
# -----------------------------------------------
# Korean is agglutinative: 정책은 / 정책을 / 정책이 are the SAME word wearing
# different 조사. A whitespace split alone would scatter one topic across many
# rows of the table and hide it. This probe therefore strips a short list of
# trailing particles before counting. That is a crude stemmer, not a morphological
# analyzer — it will occasionally shave a real word (e.g. a noun genuinely ending
# in 이). Counts are directional, for spotting topics, not exact term frequencies.

import collections
import json
import os
import re
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# domain_classifier.FALLBACK_LABEL — the hyphenated compound.
UNCLASSIFIED_LABEL = "기타-미분류"

# Claim-bearing columns in the frontend's buildReviewerSafeClaim order. Only the
# ones that physically exist are selected. `evidence_summary` is the stored
# stand-in for the frontend chain's `summary` step.
CLAIM_COLUMNS = ("claim_text", "claims", "evidence_summary", "title")

DETECT_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'analysis_results' AND column_name = ANY(%s)"
)

TOP_TOKENS = 40
TOP_HOSTS = 20
SAMPLE_ROWS = 15
SAMPLE_CHARS = 80
MIN_TOKEN_CHARS = 2

# Trailing particles/endings shaved before counting (longest first, one pass).
# Crude by design — see the TOKENIZATION CAVEAT above.
TRAILING_PARTICLES = (
    "으로써", "으로서", "에서는", "에서도", "에게서", "이라는", "라는",
    "으로", "에서", "에게", "부터", "까지", "보다", "처럼", "마다", "만큼",
    "이나", "이란", "라고", "이라", "와의", "과의", "에는", "에도", "의",
    "은", "는", "이", "가", "을", "를", "에", "도", "와", "과", "로", "만",
)

# Short inline stopword set: 조사/기능어 + filler nouns that dominate any policy
# corpus without indicating a topic.
STOPWORDS = {
    "은", "는", "이", "가", "을", "를", "에", "의", "도", "으로", "로", "와", "과",
    "및", "등", "관련", "대한", "위한", "통해", "따라", "따른", "대해", "이번",
    "올해", "지난", "내년", "작년", "최근", "오는", "지원", "추진", "계획",
    "발표", "확대", "강화", "예정", "방침", "정부", "국내", "우리", "모든",
    "해당", "이후", "이상", "이하", "가장", "다시", "함께", "경우", "때문",
    "위해", "밝혔다", "말했다", "설명했다", "전했다", "강조했다", "있다",
    "없다", "된다", "한다", "했다", "됐다", "이다", "그리고", "하지만",
}

# Strip punctuation/symbols from token edges; keep Korean, latin, digits, %.
_EDGE_PUNCT = re.compile(r"^[^\w가-힣%]+|[^\w가-힣%]+$")
_PURE_NUMBER = re.compile(r"^[\d,.%]+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _j(s):
    """Parse a JSON TEXT column, tolerant of NULL / malformed."""
    try:
        return json.loads(s) if s else None
    except (TypeError, ValueError):
        return None


def _pct(n, total):
    """n/total as a percent string; '—' when total is 0."""
    if not total:
        return "—"
    return "%.1f%%" % (100.0 * n / total)


def normalize_outlet_host(url):
    """Normalized host of original_url — same behaviour as
    scripts/build_brainmap_graph.py:140-155. Unparseable -> "" (excluded)."""
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
    physically exist. Returns (text, source_column); ('', '') when none."""
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


def strip_particle(token):
    """Shave ONE trailing particle when doing so leaves a token of usable
    length. Crude stemmer — see the TOKENIZATION CAVEAT at the top."""
    for suffix in TRAILING_PARTICLES:
        if token.endswith(suffix) and len(token) - len(suffix) >= MIN_TOKEN_CHARS:
            return token[: -len(suffix)]
    return token


def tokenize(text):
    """Whitespace split -> strip edge punctuation -> drop pure numbers, short
    tokens and stopwords -> shave a trailing particle -> re-check stopwords."""
    out = []
    for raw in (text or "").split():
        token = _EDGE_PUNCT.sub("", raw)
        if len(token) < MIN_TOKEN_CHARS or _PURE_NUMBER.match(token):
            continue
        if token in STOPWORDS:
            continue
        token = strip_particle(token)
        if len(token) < MIN_TOKEN_CHARS or token in STOPWORDS:
            continue
        out.append(token)
    return out


def _section(title):
    print("\n" + title)
    print("-" * len(title))


# ---------------------------------------------------------------------------
# Sections — each guarded so one bad row cannot abort the run.
# ---------------------------------------------------------------------------

def section_count(rows):
    _section("1. UNCLASSIFIED ROW COUNT")
    print("  Rows with domain = '%s': %d" % (UNCLASSIFIED_LABEL, len(rows)))
    print("  (data_health_probe.py measured ~1,674 — a drift here just means the"
          " corpus grew.)")
    return len(rows)


def section_resolution(rows, cols, total):
    _section("2. CLAIM RESOLUTION (which column the text came from)")
    resolved = []
    try:
        by_source = collections.Counter()
        for row in rows:
            text, source = resolve_claim(row, cols)
            by_source[source or "(none)"] += 1
            if text:
                resolved.append(text)
        for source, n in by_source.most_common():
            print("  %-28s %6d  (%s)" % (source, n, _pct(n, total)))
        print("\n  Texts available for the token table: %d" % len(resolved))
        if by_source.get("title"):
            print("  Note: %d rows fell through to TITLE — headline wording, not a"
                  " claim." % by_source["title"])
    except Exception as exc:  # noqa: BLE001 — a section must never abort the run
        print("  section unavailable: %s" % exc)
    return resolved


def section_tokens(resolved):
    _section("3. TOP %d TOKENS (topic signal — APPROXIMATE, see caveat)" % TOP_TOKENS)
    try:
        counts = collections.Counter()
        for text in resolved:
            counts.update(tokenize(text))
        if not counts:
            print("  section unavailable: no tokens survived filtering")
            return
        width = max((len(t) for t, _ in counts.most_common(TOP_TOKENS)), default=12)
        for rank, (token, n) in enumerate(counts.most_common(TOP_TOKENS), 1):
            print("  %2d. %-*s %5d" % (rank, width + 2, token, n))
        print("\n  Distinct tokens after filtering: %d" % len(counts))
        print("  CAVEAT: whitespace split + a crude trailing-particle shave, NOT a")
        print("  morphological analyzer. Counts are directional — good for spotting")
        print("  topics, not exact term frequencies.")
    except Exception as exc:  # noqa: BLE001
        print("  section unavailable: %s" % exc)


def section_hosts(rows, total):
    _section("4. TOP %d OUTLET HOSTS AMONG UNCLASSIFIED ROWS" % TOP_HOSTS)
    try:
        hosts = collections.Counter()
        missing = 0
        for row in rows:
            host = normalize_outlet_host(row.get("original_url"))
            if host:
                hosts[host] += 1
            else:
                missing += 1
        if not hosts:
            print("  section unavailable: no derivable outlet host on any row")
            return
        for rank, (host, n) in enumerate(hosts.most_common(TOP_HOSTS), 1):
            print("  %2d. %-32s %5d  (%s)" % (rank, host, n, _pct(n, total)))
        print("\n  Distinct hosts: %d | rows with no derivable host: %d"
              % (len(hosts), missing))
        print("  (One outlet dominating can itself be the domain gap.)")
    except Exception as exc:  # noqa: BLE001
        print("  section unavailable: %s" % exc)


def section_samples(rows, cols):
    _section("5. %d SAMPLE TEXTS (eyeball the actual content)" % SAMPLE_ROWS)
    try:
        shown = 0
        # Evenly spaced across the id-ordered set, so the sample is not just the
        # oldest 15 rows.
        step = max(1, len(rows) // SAMPLE_ROWS) if rows else 1
        for row in rows[::step]:
            if shown >= SAMPLE_ROWS:
                break
            text, source = resolve_claim(row, cols)
            if not text:
                continue
            flat = " ".join(text.split())
            if len(flat) > SAMPLE_CHARS:
                flat = flat[:SAMPLE_CHARS] + "…"
            print("  [%s] id=%s  %s" % (source, row.get("id"), flat))
            shown += 1
        if not shown:
            print("  section unavailable: no row resolved to any text")
    except Exception as exc:  # noqa: BLE001
        print("  section unavailable: %s" % exc)


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
    print("UNCLASSIFIED-PEEK — what is inside '%s' (READ-ONLY, SELECT-only)"
          % UNCLASSIFIED_LABEL)
    print("=" * 72)

    with psycopg.connect(url) as conn:
        # Detect which claim-bearing columns physically exist; the live table may
        # lack claim_text and a bare SELECT of it would UndefinedColumn-fail.
        with conn.cursor() as cur:
            cur.execute(DETECT_COLUMNS_SQL, (list(CLAIM_COLUMNS),))
            present = {r[0] for r in cur.fetchall()}
        cols = [c for c in CLAIM_COLUMNS if c in present]
        print("\nClaim columns physically present: %s"
              % (", ".join(cols) if cols else "(none)"))
        if not cols:
            print("No claim-bearing column exists — cannot peek. Columns checked: %s"
                  % (CLAIM_COLUMNS,))
            return 1

        select_cols = ["id", "original_url"]
        select_cols += [c for c in cols if c not in select_cols]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT %s FROM analysis_results WHERE domain = %%s ORDER BY id"
                % ", ".join(select_cols),
                (UNCLASSIFIED_LABEL,),
            )
            rows = [dict(zip(select_cols, r)) for r in cur.fetchall()]

        total = section_count(rows)
        resolved = section_resolution(rows, cols, total)
        section_tokens(resolved)
        section_hosts(rows, total)
        section_samples(rows, cols)

    print("\n" + "=" * 72)
    print("READ-ONLY snapshot — SELECT-only; no row was written, updated, or"
          " deleted.")
    print("Exploratory: nothing here is auto-classified. Read the token table +"
          " samples")
    print("and decide which policy domains deserve their own collection seeds.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
