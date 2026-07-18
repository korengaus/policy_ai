"""REEXTRACT-CLAIMS — re-extract stored claims with the FIXED sentence splitter,
gated on POSITIONAL SAFETY so the rewrite can never misattribute evidence.

WHY THIS SCRIPT IS GATED (read before changing anything here)
--------------------------------------------------------------
The CLAIM-DISPLAY-2 splitter fix (claim_extractor.py) only affects newly-analyzed
articles; ~13k stored rows still carry claims severed mid-sentence by the old
bare-Korean-ender split (e.g. "…지난해(1.1%)보다"). Re-extraction needs the article
body, which is DISCARDED at ingest (copyright-safe) — only original_url survives —
so each row must be re-fetched.

But the claims list is NOT a leaf. SIX stored columns key into it by POSITIONAL
INTEGER INDEX, all produced by enumerate(normalized_claims):

    evidence_snippets.claim_index        evidence_extraction_agent.py:538
    claim_evidence_map["<index>"]        evidence_extraction_agent.py:638
    contradiction_checks.claim_index     contradiction_agent.py:282
    bias_framing_analysis.claim_index    bias_framing_agent.py:235,306
    source_candidates.claim_index        source_retrieval_agent.py:272,306
    source_queries.claim_index           consumed contradiction_agent.py:165

The frontend joins the same way — main.js:1654 / :1433 / :1531 all do
`claim_index === index` over the raw claims column. So rewriting claims with a
list of DIFFERENT LENGTH OR ORDER silently attaches claim #1's new text to claim
#1's OLD evidence, contradiction verdict, and bias framing. It never errors:
main.js:1433/:1531 fall back to `|| {}` (rendering the innocuous "낮음/중립"
default) and :1654 yields an empty snippet list ("강함 0, 보통 0, 약함 0").
Corruption would present as confident misattribution or quietly missing evidence.

>>> THEREFORE: this script rewrites a row ONLY when the new claim list maps 1:1
>>> onto the old one AT THE SAME INDICES — i.e. the splitter merely un-severed
>>> the same sentences in place. Every other outcome (length change, reorder, a
>>> genuinely different sentence winning the ranking) is SKIPPED with the old
>>> claims left untouched. The gate is what makes a claims-only UPDATE safe; do
>>> not loosen it without regenerating the claim_index artifacts too.

WHAT IT WRITES (and nothing else)
----------------------------------
When the gate passes, ONE UPDATE sets exactly three columns together:
    claims, normalized_claims, claim_text
claim_text is included because it EQUALS claims[0] by construction
(verification_card.py:656-666) and IS physically present on live PG
(postgres_storage.py:244 — base create_all, not an additive ALTER). Writing
claims without claim_text would leave them disagreeing, which
artifact_evidence_linker.py:204-226 reads as a contradiction.

NO verdict / honesty / policy_alert_level / final_decision / score / *_index
column is read for writing or touched. truth_claim and operator_review_required
are untouched. The gate guarantees indices still line up, so evidence,
contradiction and bias stay correctly attached to their claims.

COST: ZERO LLM / Anthropic spend. extract_verifiable_claims (claim_extractor.py)
and normalize_claims (claim_normalizer.py) are pure regex/keyword rules — no
network, no model call. The only cost is the HTTP re-fetch.

COPYRIGHT: the re-fetched body lives in a local variable for the duration of one
row and is NEVER written to any column, file, or log. Process-then-discard, the
same posture as scripts/backfill_pilot_verify.py.

FOLLOW-UPS THIS RUN CREATES (printed again at the end of every dry-run)
-----------------------------------------------------------------------
  * Changing claim_text re-hashes the embedding key (embed_backfill.py:88-97),
    so the old vector is ORPHANED and the row has none until embed_backfill
    re-runs. Brainmap membership then shifts; the new lineage_id
    (build_brainmap_graph.assign_lineage_ids) absorbs that churn gracefully.
  * api_server.py:2372-2417 keys review-task idempotency on
    (row_id, item_index, claim_text) — changed text MINTS NEW REVIEW TASKS for
    rows a human already reviewed.

Usage (Joe runs this, after commit + push + Worker redeploy + reopen Shell):
    PYTHONPATH=. python scripts/reextract_claims.py --selftest
    PYTHONPATH=. python scripts/reextract_claims.py --dry-run --limit 200
    PYTHONPATH=. python scripts/reextract_claims.py --dry-run
    nohup env PYTHONPATH=. python scripts/reextract_claims.py --pacing 1.0 \
        > /tmp/reextract.log 2>&1 &

Exit codes: 0 = done / preconditions unmet (with guidance); 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# Per-position token overlap required to call a new claim "the same sentence,
# un-severed". 0.6 is deliberately strict: a boundary fix keeps nearly every
# token (the old text is usually a PREFIX of the new one, overlap ~1.0 against
# the shorter side), while a different sentence winning the ranking shares only
# incidental policy vocabulary and lands far below.
OVERLAP_THRESHOLD = 0.6
# Same max_claims the live pipeline passes (main.py:657) — a different value
# would change list length for reasons unrelated to the splitter fix.
MAX_CLAIMS = 3
DEFAULT_PACING_SECONDS = 1.0
DEFAULT_BATCH = 200

SELECT_SQL = (
    "SELECT id, title, original_url, claims, claim_text "
    "FROM analysis_results WHERE id > %s ORDER BY id LIMIT %s"
)
# The ONLY write. Three claim columns, keyed by id. Nothing else.
UPDATE_SQL = (
    "UPDATE analysis_results "
    "SET claims = %s, normalized_claims = %s, claim_text = %s "
    "WHERE id = %s"
)

SKIP_REASONS = (
    "fetch_fail",
    "body_too_short",
    "no_url",
    "extract_empty",
    "length_mismatch",
    "position_mismatch",
    "unchanged",
)


def p(message: str = "") -> None:
    print(message, flush=True)


def _tokens(text: str) -> set:
    """Word-ish tokens for overlap scoring: hangul/latin/digit runs, deduped.
    Punctuation and the trailing "..." of an old truncation are ignored, which
    is exactly what we want — a boundary fix changes punctuation, not words."""
    return set(re.findall(r"[0-9A-Za-z가-힣]+", (text or "").lower()))


def overlap_ratio(old: str, new: str) -> float:
    """|old ∩ new| / |smaller side|.

    Normalizing by the SMALLER side is the point: an un-severed claim is the old
    fragment PLUS the rest of its sentence, so the old tokens are a subset and
    this returns ~1.0. Jaccard would penalize exactly the repair we are looking
    for (a doubled-length sentence would score ~0.5 and be skipped)."""
    old_tokens, new_tokens = _tokens(old), _tokens(new)
    if not old_tokens or not new_tokens:
        return 0.0
    return len(old_tokens & new_tokens) / min(len(old_tokens), len(new_tokens))


def positional_gate(old_claims, new_claims, threshold=OVERLAP_THRESHOLD):
    """THE SAFETY GATE. Returns (ok: bool, reason: str, per_position: list).

    Passes only when (a) the lists are the same length AND (b) every position
    maps to the same underlying sentence (overlap >= threshold). Either failure
    means the claim_index artifacts stored on this row would no longer point at
    the right claim, so the row must be left alone.

    Pure — no DB, no IO, no network. Selftest-covered."""
    old_list = [str(c or "") for c in (old_claims or [])]
    new_list = [str(c or "") for c in (new_claims or [])]
    if not new_list:
        return False, "extract_empty", []
    if len(old_list) != len(new_list):
        return False, "length_mismatch", []
    ratios = [overlap_ratio(old, new) for old, new in zip(old_list, new_list)]
    if any(ratio < threshold for ratio in ratios):
        return False, "position_mismatch", ratios
    if old_list == new_list:
        return False, "unchanged", ratios
    return True, "ok", ratios


def _loads_list(raw):
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def reextract_row(title, url, old_claims, fetch_body, extract, normalize):
    """Pure-ish per-row logic with its collaborators INJECTED (so the selftest
    drives it with fakes and no network). Returns
    (ok, reason, new_claims, new_normalized, ratios).

    The fetched body is a local only — it is never returned, stored, or logged.
    """
    if not url:
        return False, "no_url", None, None, []
    try:
        body = fetch_body(url) or ""
    except Exception:
        # Link rot, timeout, TLS error, paywall redirect, malformed HTML — one
        # bad URL must never abort a 13k-row run.
        return False, "fetch_fail", None, None, []
    if len(body) < 100:
        # Mirrors claim_extractor.py's own floor: below it, extraction would
        # fall back to title/summary and produce a DIFFERENT kind of claim.
        return False, "body_too_short", None, None, []
    try:
        new_claims = extract(body, title or "", "", MAX_CLAIMS)
    except Exception:
        return False, "extract_empty", None, None, []

    ok, reason, ratios = positional_gate(old_claims, new_claims)
    if not ok:
        return False, reason, None, None, ratios
    try:
        new_normalized = normalize(new_claims)
    except Exception:
        return False, "extract_empty", None, None, ratios
    return True, "ok", new_claims, new_normalized, ratios


def run_backfill(conn, fetch_body, extract, normalize, *, dry_run, limit,
                 start_id, pacing, batch, samples):
    """Keyset-paginated drive loop. Commits PER BATCH so an interrupted run
    keeps its progress, and prints the id cursor so --start-id can resume."""
    counts = collections.Counter()
    shown = []
    last_id = start_id
    total = 0

    while True:
        if limit is not None and total >= limit:
            break
        this_limit = batch if limit is None else min(batch, limit - total)
        if this_limit <= 0:
            break
        with conn.cursor() as cur:
            cur.execute(SELECT_SQL, (last_id, this_limit))
            rows = cur.fetchall()
        if not rows:
            break

        with conn.cursor() as cur:
            for row_id, title, url, claims_raw, _claim_text in rows:
                last_id = max(last_id, row_id)
                total += 1
                old_claims = _loads_list(claims_raw)
                ok, reason, new_claims, new_normalized, ratios = reextract_row(
                    title, url, old_claims, fetch_body, extract, normalize)
                if not ok:
                    counts[reason] += 1
                else:
                    counts["updated" if not dry_run else "would_update"] += 1
                    if len(shown) < samples:
                        shown.append((row_id, old_claims, new_claims, ratios))
                    if not dry_run:
                        # The ONLY write: three claim columns, one row.
                        # claim_text = claims[0], keeping the invariant
                        # verification_card.py:656-666 establishes.
                        cur.execute(UPDATE_SQL, (
                            json.dumps(new_claims, ensure_ascii=False),
                            json.dumps(new_normalized, ensure_ascii=False),
                            new_claims[0],
                            row_id,
                        ))
                # Politeness: pace only when we actually hit the network.
                if reason != "no_url":
                    time.sleep(pacing)

        if not dry_run:
            conn.commit()
        p("[reextract] cursor id=%s processed=%d updated=%d skipped=%d"
          % (last_id, total,
             counts["updated"] + counts["would_update"],
             sum(counts[r] for r in SKIP_REASONS)))

    return counts, shown, last_id


def print_summary(counts, shown, last_id, dry_run):
    updated = counts["would_update"] if dry_run else counts["updated"]
    skipped = sum(counts[reason] for reason in SKIP_REASONS)
    total = updated + skipped
    p("")
    p("=== SUMMARY%s ===" % (" (DRY-RUN — nothing written)" if dry_run else ""))
    p("  rows examined            : %d" % total)
    p("  %-24s: %d  (%.1f%%)"
      % ("would update" if dry_run else "UPDATED", updated,
         100.0 * updated / total if total else 0.0))
    p("  skipped                  : %d  (%.1f%%)"
      % (skipped, 100.0 * skipped / total if total else 0.0))
    for reason in SKIP_REASONS:
        if counts[reason]:
            p("    %-22s: %d  (%.1f%%)"
              % (reason, counts[reason], 100.0 * counts[reason] / total if total else 0.0))
    p("  last id cursor           : %d   (resume with --start-id %d)"
      % (last_id, last_id))

    if shown:
        p("")
        p("=== SAMPLE old -> new (gate PASSED; eyeball that these are un-severed "
          "sentences, not different claims) ===")
        for row_id, old_claims, new_claims, ratios in shown:
            p("")
            p("  row #%s" % row_id)
            for index, (old, new) in enumerate(zip(old_claims, new_claims)):
                if old == new:
                    continue
                ratio = ratios[index] if index < len(ratios) else 0.0
                p("    [%d] overlap=%.2f" % (index, ratio))
                p("      old: %s" % old[:150])
                p("      new: %s" % new[:150])

    p("")
    p("=== FOLLOW-UPS THIS RUN CREATES (not handled here) ===")
    p("  1. claim_text changes re-hash the embedding key (embed_backfill.py:88-97):")
    p("     the old vector is orphaned and the row has NONE until embed_backfill")
    p("     re-runs. Brainmap membership then shifts; lineage_id absorbs the churn.")
    p("  2. api_server.py:2372-2417 keys review-task idempotency on")
    p("     (row_id, item_index, claim_text) — changed text MINTS NEW REVIEW TASKS")
    p("     for rows a human already reviewed.")
    p("")
    p("  Reminder: extraction + normalization are pure regex/rules — ZERO LLM cost.")
    p("  The re-fetched article body was never stored (copyright-safe).")


def run(dry_run, limit, start_id, pacing, batch, samples) -> int:
    p("=== REEXTRACT-CLAIMS%s ===" % (" (DRY-RUN)" if dry_run else ""))
    p("  positional-safety gate: same length AND per-position overlap >= %.2f"
      % OVERLAP_THRESHOLD)

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        p("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0
    if not dry_run and os.environ.get(
            "USE_POSTGRES_WRITE", "").strip().lower() != "true":
        p("USE_POSTGRES_WRITE is not 'true' — refusing to write. Set it true, "
          "or use --dry-run.")
        return 0

    import psycopg

    from article_extractor import fetch_article_body
    from claim_extractor import extract_verifiable_claims
    from claim_normalizer import normalize_claims

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    with psycopg.connect(url) as conn:
        counts, shown, last_id = run_backfill(
            conn, fetch_article_body, extract_verifiable_claims,
            normalize_claims,
            dry_run=dry_run, limit=limit, start_id=start_id, pacing=pacing,
            batch=batch, samples=samples,
        )
    print_summary(counts, shown, last_id, dry_run)
    return 0


def _selftest() -> int:
    failures = []

    def check(name, ok):
        p("  [%s] %s" % ("ok" if ok else "FAIL", name))
        if not ok:
            failures.append(name)

    # --- overlap_ratio -----------------------------------------------------
    severed = "한국은행은 올해 소비자물가 상승률이 지난해(1.1%)보다"
    repaired = ("한국은행은 올해 소비자물가 상승률이 지난해(1.1%)보다 높은 "
                "2.3%를 기록할 것으로 전망한다고 밝혔다.")
    check("un-severed sentence scores ~1.0 (old tokens are a subset)",
          overlap_ratio(severed, repaired) >= 0.99)
    check("a different sentence scores low",
          overlap_ratio(severed, "국토부는 재건축 규제를 완화하기로 했다.") < 0.3)
    check("empty side scores 0.0", overlap_ratio("", repaired) == 0.0)

    # --- positional_gate ---------------------------------------------------
    old = ["정부는 대책을 발표했다", "금융위는 지원을 확대한다"]
    ok, reason, _ = positional_gate(
        old, ["정부는 대책을 발표했다고 밝혔다", "금융위는 지원을 확대한다고 했다"])
    check("same length + 1:1 un-severed -> PASSES", ok and reason == "ok")

    ok, reason, _ = positional_gate(old, ["정부는 대책을 발표했다"])
    check("length shrink -> SKIP length_mismatch",
          not ok and reason == "length_mismatch")
    ok, reason, _ = positional_gate(
        old, ["정부는 대책을 발표했다", "금융위는 지원을 확대한다", "새 주장"])
    check("length grow -> SKIP length_mismatch",
          not ok and reason == "length_mismatch")

    # REORDER is the corrupting case the gate exists to catch: same length,
    # same texts, swapped positions -> every claim_index would misattribute.
    ok, reason, _ = positional_gate(old, [old[1], old[0]])
    check("reorder (same texts, swapped) -> SKIP position_mismatch",
          not ok and reason == "position_mismatch")

    ok, reason, _ = positional_gate(
        old, ["정부는 대책을 발표했다고 밝혔다", "국토부는 재건축 규제를 완화한다"])
    check("one position replaced by a different sentence -> SKIP",
          not ok and reason == "position_mismatch")

    ok, reason, _ = positional_gate(old, list(old))
    check("identical lists -> SKIP unchanged (no pointless write)",
          not ok and reason == "unchanged")
    ok, reason, _ = positional_gate(old, [])
    check("empty extraction -> SKIP extract_empty",
          not ok and reason == "extract_empty")

    # --- reextract_row failure handling (injected fakes; no network) --------
    def boom(_url):
        raise RuntimeError("timeout")

    ok, reason, *_ = reextract_row("t", "http://x", old, boom, None, None)
    check("fetch exception -> SKIP fetch_fail, never raises",
          not ok and reason == "fetch_fail")
    ok, reason, *_ = reextract_row("t", "", old, boom, None, None)
    check("missing url -> SKIP no_url", not ok and reason == "no_url")
    ok, reason, *_ = reextract_row("t", "http://x", old, lambda _u: "short",
                                   None, None)
    check("body under 100 chars -> SKIP body_too_short",
          not ok and reason == "body_too_short")
    ok, reason, *_ = reextract_row("t", "http://x", old, lambda _u: "x" * 200,
                                   lambda *a: (_ for _ in ()).throw(ValueError()),
                                   None)
    check("extractor exception -> SKIP extract_empty, never raises",
          not ok and reason == "extract_empty")

    body = "y" * 200
    ok, reason, new_claims, new_norm, _ = reextract_row(
        "t", "http://x", old, lambda _u: body,
        lambda *a: ["정부는 대책을 발표했다고 밝혔다", "금융위는 지원을 확대한다고 했다"],
        lambda claims: [{"claim_text": c} for c in claims])
    check("happy path returns new claims + normalized, 1:1 aligned",
          ok and len(new_claims) == 2 and len(new_norm) == 2
          and new_norm[0]["claim_text"] == new_claims[0])

    # --- write-statement audit --------------------------------------------
    upper = UPDATE_SQL.upper()
    check("UPDATE touches ONLY claims/normalized_claims/claim_text",
          upper.count("SET") == 1
          and "CLAIMS = %S" in upper and "NORMALIZED_CLAIMS = %S" in upper
          and "CLAIM_TEXT = %S" in upper
          and not any(word in upper for word in (
              "VERDICT", "HONESTY", "POLICY_ALERT_LEVEL", "FINAL_DECISION",
              "TRUTH_CLAIM", "OPERATOR_REVIEW", "SCORE", "CLAIM_INDEX",
              "EVIDENCE_SNIPPETS", "CONTRADICTION", "BIAS_FRAMING",
              "SOURCE_CANDIDATES")))
    check("no INSERT / DELETE / ALTER / DROP anywhere in this module's SQL",
          not any(word in (SELECT_SQL + UPDATE_SQL).upper()
                  for word in ("INSERT", "DELETE", "ALTER", "DROP",
                               "ON CONFLICT", "TRUNCATE")))

    p("[selftest] %s" % ("PASS" if not failures else
                         "FAIL: " + ", ".join(failures)))
    return 0 if not failures else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="reextract_claims",
        description="Re-extract stored claims with the fixed splitter, gated on "
                    "positional safety. Writes ONLY claims/normalized_claims/"
                    "claim_text, and only when the new list maps 1:1 onto the old.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE gate tests (no DB, no network).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what WOULD change; writes nothing.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N rows (small first pass).")
    parser.add_argument("--start-id", type=int, default=0,
                        help="Resume from this id cursor (see the run log).")
    parser.add_argument("--pacing", type=float, default=DEFAULT_PACING_SECONDS,
                        help="Seconds between HTTP fetches (default %.1f)."
                             % DEFAULT_PACING_SECONDS)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                        help="Rows per keyset page / commit (default %d)."
                             % DEFAULT_BATCH)
    parser.add_argument("--samples", type=int, default=10,
                        help="Sample old->new diffs to print (default 10).")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    return run(args.dry_run, args.limit, args.start_id, args.pacing,
               args.batch, args.samples)


if __name__ == "__main__":
    sys.exit(main())
