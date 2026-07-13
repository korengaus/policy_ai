# CARD-CLAIM-QUALITY A2 Phase 1 — READ-ONLY size probe (pin-OUT, SELECT-only).
#
# Sizes issues ② (body-dump claim) and ③ (mid-sentence truncation) so the fix
# is data-driven. The card's 핵심 주장 renders from buildReviewerSafeClaim's
# chain claim_text -> claims[0] -> summary -> title-fallback; claim_text is the
# dominant source (an article SENTENCE picked + cleaned by
# claim_extractor.extract_verifiable_claims — NOT an LLM-normalized rewrite),
# so a quote-lead top sentence reads as a body dump straight from claim_text.
#
# Buckets on the STORED claim_text (the primary render source):
#   EMPTY      — claim_text blank -> card falls to claims[0]/summary/title
#                (the deepest ② fallback path).
#   QUOTE_LEAD — a direct-quote body sentence ("…회장은 '…'" / 「」/ curly
#                quotes with a said-verb) -> the ② body-dump look.
#   OVERSIZED  — > 220 chars OR already ends with "…"/"..." (③: the frontend
#                limitClaimSentences 220-cap would cut it, and claim_extractor
#                already truncates single sentences at 220 backend-side).
#   CLEAN      — none of the above.
# A row can be OVERSIZED AND QUOTE_LEAD; those are reported in both plus a
# combined "any-issue" figure.
#
# Joe runs once in the Render Worker Shell:
#     PYTHONPATH=. python scripts/claim_quality_size_probe.py
#
# SAFETY: SELECT-only (id, title, claim_text). No verdict column, no writes.
# Keyset pagination. Never prints DATABASE_URL. pin-OUT scripts/*.

import collections
import os
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

SELECT_SQL = (
    "SELECT id, title, claim_text FROM analysis_results "
    "WHERE id > %s ORDER BY id LIMIT 1000"
)

MAX_LEN = 220  # the frontend limitClaimSentences cap AND claim_extractor's cap.

# A said-verb near a quote = a reported-speech body sentence.
SAID_VERB = ("말했다", "밝혔다", "강조했다", "설명했다", "전했다", "덧붙였다",
             "지적했다", "주장했다", "당부했다", "약속했다", "다짐했다")
QUOTE_CHARS = "\"'“”‘’「」『』"
# name/title + 은/는 + ... + quote (the "임종룡 회장은 '…'" shape).
SPEAKER_LEAD_RE = re.compile(r"[가-힣]{2,}\s*(?:회장|장관|위원장|대표|총리|"
                             r"청장|처장|사장|부총리|의원|대통령)\s*[은는이가]")


def has_quote(text):
    q = sum(text.count(c) for c in QUOTE_CHARS)
    return q >= 2


def bucket(claim):
    text = (claim or "").strip()
    tags = []
    if not text:
        return ["EMPTY"]
    quote = has_quote(text)
    said = any(v in text for v in SAID_VERB)
    speaker = bool(SPEAKER_LEAD_RE.search(text))
    if (quote and (said or speaker)) or (speaker and said):
        tags.append("QUOTE_LEAD")
    if len(text) > MAX_LEN or text.rstrip().endswith(("...", "…")):
        tags.append("OVERSIZED")
    if not tags:
        tags.append("CLEAN")
    return tags


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    counts = collections.Counter()
    any_issue = 0
    total = 0
    samples = collections.defaultdict(list)
    lengths = []
    last_id = 0
    print("CLAIM-QUALITY SIZE PROBE — SELECT-only\n")
    with psycopg.connect(url) as conn:
        while True:
            with conn.cursor() as cur:
                cur.execute(SELECT_SQL, (last_id,))
                rows = cur.fetchall()
            if not rows:
                break
            for rid, title, claim in rows:
                last_id = max(last_id, rid)
                total += 1
                if claim:
                    lengths.append(len(claim))
                tags = bucket(claim)
                for t in tags:
                    counts[t] += 1
                    if t in ("EMPTY", "QUOTE_LEAD", "OVERSIZED") \
                            and len(samples[t]) < 8:
                        samples[t].append((rid, (claim or title or "")[:90]))
                if any(t in ("EMPTY", "QUOTE_LEAD", "OVERSIZED") for t in tags):
                    any_issue += 1

    def pct(n):
        return 100.0 * n / total if total else 0.0

    print("== buckets over %d rows (a row can carry >1 tag) ==" % total)
    for tag in ("CLEAN", "QUOTE_LEAD", "OVERSIZED", "EMPTY"):
        print("  %-11s %6d (%4.1f%%)" % (tag, counts[tag], pct(counts[tag])))
    print("  %-11s %6d (%4.1f%%)  <- ② or ③ affected"
          % ("ANY_ISSUE", any_issue, pct(any_issue)))

    if lengths:
        lengths.sort()
        n = len(lengths)
        print("\n== claim_text length distribution ==")
        print("  median=%d  p90=%d  p99=%d  max=%d  over_220=%d (%.1f%%)"
              % (lengths[n // 2], lengths[int(n * 0.9)], lengths[int(n * 0.99)],
                 lengths[-1], sum(1 for x in lengths if x > MAX_LEN),
                 100.0 * sum(1 for x in lengths if x > MAX_LEN) / n))

    for tag in ("QUOTE_LEAD", "OVERSIZED", "EMPTY"):
        print("\n== %s samples ==" % tag)
        for rid, text in samples[tag]:
            print("  id=%-6s %s" % (rid, text))

    print("\n[Probe] SELECT-only; nothing written; no verdict column read. "
          "QUOTE_LEAD sizes ②, OVERSIZED sizes ③.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
