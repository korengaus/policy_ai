# BACKLOG-SYSTEMIC-SCAN — read-only corpus sample for SYSTEMIC display defects.
#
# WHY A SAMPLE: a systemic defect is one code path rendering wrong on many
# rows, so one fix cures the whole corpus. A per-row oddity cannot be bulk
# fixed (full re-analysis is permanently rejected). The goal is therefore to
# surface CLASSES cheaply, never to inspect every card.
#
# REUSE, NEVER REWRITE: the reader-visible text comes from
# showcase_reviewer_card_probe.render_cards (which itself drives the committed
# scripts/card_render_audit.js chain), and the reviewer prompt/model/batching
# are IMPORTED from that same probe. A second prompt that can drift from the
# first is a hazard we already carry once, so this file defines none.
#
# ONE PASS ONLY. Determinism was measured by the probe this imports; repeating
# it here would double the spend for a figure we already have.
#
# BUDGET GATE: the projected cost is computed from the ACTUAL rendered
# character count and printed BEFORE any API call. Over MAX_SPEND_USD the run
# stops and reports the projection instead. The reviewer pass additionally
# requires --run-reviewer, so the default invocation spends nothing.
#
#   PYTHONPATH=. python scripts/backlog_systemic_scan.py                 # free
#   PYTHONPATH=. python scripts/backlog_systemic_scan.py --run-reviewer  # spends
#
# SAFETY: SELECT-only (analysis_results render columns). Temp-file dump for the
# Node driver only (the C8 precedent). No DB write, no stored field touched:
# truth_claim stays False, operator_review_required stays True, verdict_label
# untouched. Credentials from the environment ONLY, never printed. pin-OUT
# scripts/* — zero log.* call sites, the 331/16 pins do not move.
import argparse
import hashlib
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from showcase_reviewer_card_probe import (  # noqa: E402  — deliberate reuse
    BATCH_SIZE, MODEL, PRICE_INTRO, SYSTEM_PROMPT, render_cards, run_reviewer,
)

TARGET_N = 150
MAX_SPEND_USD = 3.00
CHARS_PER_TOKEN = 2.0        # mixed Korean/Latin; a 1.0 worst case is printed too
KNOWN = (12064, 13977, 13833)


def pick_stratified(conn, target=TARGET_N):
    """Deterministic coverage sample over (domain x verdict_label) cells.

    Every non-empty cell contributes at least one row, then cells are topped up
    in descending size until `target` is reached. Ordering inside a cell is by
    md5(id) — stable across runs and decorrelated from time, so the draw is not
    dominated by one week. This is a COVERAGE sample, not a prevalence sample:
    it deliberately over-weights rare cells, so class DISCOVERY generalises but
    raw sample rates must be re-weighted before being read as corpus rates.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, COALESCE(domain,'(null)'), COALESCE(verdict_label,'(null)'), "
            "COALESCE(LEFT(created_at,7),'(null)') FROM analysis_results")
        rows = cur.fetchall()
    cells = {}
    months = {}
    for rid, dom, verdict, month in rows:
        cells.setdefault((dom, verdict), []).append(rid)
        months.setdefault(month, 0)
        months[month] += 1
    for key in cells:
        cells[key].sort(key=lambda i: hashlib.md5(str(i).encode()).hexdigest())
    picked, quota = [], {k: 1 for k in cells}
    order = sorted(cells, key=lambda k: -len(cells[k]))
    while sum(min(quota[k], len(cells[k])) for k in cells) < target:
        grew = False
        for k in order:
            if quota[k] < len(cells[k]):
                quota[k] += 1
                grew = True
                if sum(min(quota[x], len(cells[x])) for x in cells) >= target:
                    break
        if not grew:
            break
    for k in order:
        picked.extend(cells[k][:quota[k]])
    return picked[:target], cells, months, len(rows)


# ---- free mechanical layer: candidate SYSTEMIC classes over rendered text ----
HANGUL = r"[가-힣]"
DETECT = {
    # 12064: ellipsis glued to a syllable with no boundary. The card FACE path
    # (CARD-TRUNCATION-FIX) uses "…"; the DETAIL path deliberately kept ASCII "..."
    "trunc_unicode_midword": re.compile(HANGUL + "…"),
    "trunc_ascii_midword": re.compile(HANGUL + r"\.\.\."),
    # 13833: digits split from their Korean unit by whitespace
    "spaced_number_unit": re.compile(r"\d\s+(?:월|일|년|시|분|개|건|명|원|%)"),
    # machine text a reader should never meet
    "english_debug": re.compile(r"[a-z]+ [a-z]+ (?:overlap|mismatch|insufficient)"),
    "raw_enum": re.compile(r"\b(?:no_match|insufficient_[a-z_]+|mixed_or_unclear)\b"),
}


def mechanical(cards, titles):
    hits = {k: [] for k in DETECT}
    hits["self_contradiction"] = []
    for rid, text in cards.items():
        for name, rx in DETECT.items():
            if rx.search(text):
                hits[name].append(rid)
        title = (titles.get(rid) or "").strip()
        if len(title) >= 12:
            for block in re.findall(r"\[반박 검사\][^\[]*", text):
                if title[:14] in block:
                    hits["self_contradiction"].append(rid)
                    break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-reviewer", action="store_true",
                    help="spend: one reviewer pass over the sample")
    args = ap.parse_args()

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0
    import psycopg
    url = (raw.replace("postgresql+psycopg://", "postgresql://")
              .replace("postgresql+psycopg2://", "postgresql://"))
    with psycopg.connect(url) as conn:
        ids, cells, months, corpus_n = pick_stratified(conn)
        for k in KNOWN:
            if k not in ids:
                ids.append(k)
        with conn.cursor() as cur:
            cur.execute("SELECT id, title FROM analysis_results WHERE id = ANY(%s)",
                        (list(ids),))
            titles = {r[0]: r[1] for r in cur.fetchall()}
        print("SAMPLE n=%d of %d rows | cells=%d (domain x verdict) | months spanned=%d"
              % (len(ids), corpus_n, len(cells), len(months)))
        cards = render_cards(conn, ids)

    print("RENDERED %d/%d cards (Node chain, $0)" % (len(cards), len(ids)))
    total_chars = sum(len(t) for t in cards.values())
    n_batches = (len(cards) + BATCH_SIZE - 1) // BATCH_SIZE
    sys_chars = len(SYSTEM_PROMPT) * n_batches
    for ratio, tag in ((CHARS_PER_TOKEN, "expected"), (1.0, "worst case")):
        tin = (total_chars + sys_chars) / ratio
        tout = n_batches * 2000
        cost = tin / 1e6 * PRICE_INTRO[0] + tout / 1e6 * PRICE_INTRO[1]
        print("PROJECTED %-10s in=%.0fk out=%.0fk -> $%.2f  (%s, %d batches, intro $%s/$%s per MTok)"
              % (tag, tin / 1000, tout / 1000, cost, MODEL, n_batches,
                 PRICE_INTRO[0], PRICE_INTRO[1]))
    worst = ((total_chars + sys_chars) / 1.0 / 1e6 * PRICE_INTRO[0]
             + n_batches * 2000 / 1e6 * PRICE_INTRO[1])

    hits = mechanical(cards, titles)
    print("--- FREE MECHANICAL LAYER (no API) ---")
    for name, rids in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        print("  %-24s %3d/%d rows  e.g. %s"
              % (name, len(rids), len(cards), sorted(rids)[:6]))
    for k in KNOWN:
        flagged = [n for n, r in hits.items() if k in r]
        print("  KNOWN row %-6s -> %s" % (k, flagged or "no mechanical hit"))

    if not args.run_reviewer:
        print("STOPPED BEFORE SPENDING (default). Add --run-reviewer to run one pass.")
        return 0
    if worst > MAX_SPEND_USD:
        print("STOPPED: worst-case $%.2f exceeds MAX_SPEND_USD $%.2f." % (worst, MAX_SPEND_USD))
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("STOPPED: ANTHROPIC_API_KEY not set.")
        return 0
    import anthropic
    client = anthropic.Anthropic()
    ordered = sorted(cards)
    verdicts, (tin, tout) = run_reviewer(client, ordered, cards, smoke=True)
    actual = tin / 1e6 * PRICE_INTRO[0] + tout / 1e6 * PRICE_INTRO[1]
    print("ACTUAL in=%d out=%d -> $%.3f (one pass; second pass saved ~$%.3f)"
          % (tin, tout, actual, actual))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "backlog_scan_verdicts.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(verdicts, fh, ensure_ascii=False, indent=1)
    print("VERDICTS WRITTEN: %s (%d cards)" % (out, len(verdicts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
