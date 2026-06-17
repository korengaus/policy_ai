# CLASSIFY-PROBE — read-only measurement of a TOOL-FREE Sonnet domain classifier.
# SELECT-only on the DB, NO writes, NO verdict touch, NO production path changed.
# The only network call is the Anthropic Messages API (same client/key convention
# as hot_topics.py / llm_judge.py). Safe to run in the Render Worker Shell.
#
# QUESTION THIS PROBE ANSWERS
# ---------------------------
# A multi-domain category UI needs to label each analysis row by domain
# (finance / welfare / agriculture / labor / health / environment / SMB /
# realestate / statistics / 기타). Today `topic_classifier` only has ~8
# housing-finance values; everything else collapses to 미분류. BEFORE building
# an LLM classifier we MEASURE (measure-before-surgery) whether a TOOL-FREE
# Sonnet call accurately labels our REAL stored rows, and at what cost — so we
# don't build on a bad premise.
#
# WHAT IT DOES (read-only)
#   * SELECT a small STRATIFIED sample of stored rows (across the advisory
#     keyword-domains so finance/welfare/SMB/... are all represented), capped at
#     MAX_N to respect the Anthropic credit (auto-recharge OFF).
#   * For each row, ask claude-sonnet-4-6 TOOL-FREE (no web_search / no tools)
#     for EXACTLY ONE domain label from a fixed taxonomy.
#   * Compare the Sonnet label to the advisory keyword-domain hint (a LOOSE
#     cross-check, NOT ground truth — the keyword hint is itself imperfect).
#   * Print the FULL per-row table (title -> predicted label) for human eyeball
#     ("found != relevant": a number alone is not the answer), plus cost/tokens.
#
# WHAT IT DOES NOT
#   * No INSERT/UPDATE/DELETE, no schema change, no new column.
#   * No verdict/scoring/matcher/official-evidence touch.
#   * No web_search / no tools in the Sonnet call (token-blowup lesson).
#   * Does NOT build the classifier and does NOT classify the whole corpus.

import os
import sys
import time
import collections

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunable constants (top-of-file, commented).
# ---------------------------------------------------------------------------
# Recent-rows pool to stratify from (most recent by id). Stratification picks
# from this pool so every advisory domain is represented even if rare.
POOL_LIMIT = 500
# Max sampled rows per advisory keyword-domain (round-robin fill).
PER_DOMAIN = 6
# Max 기타/미분류 (no keyword-domain) rows to include.
UNCLASSIFIED_QUOTA = 6
# Hard cap on total API calls (cost discipline — keep small).
MAX_N = 48
# Claude pricing fallback model id (overridable via ANTHROPIC_MODEL env).
DEFAULT_MODEL = "claude-sonnet-4-6"
# Tiny output budget — we want ONE label back, nothing else.
MAX_OUTPUT_TOKENS = 24
# Truncation widths for the printed table.
TITLE_W = 52
CLAIM_SNIPPET = 240

# Fixed domain taxonomy the Sonnet call must choose from (the project's intended
# multi-domain set). 기타-미분류 is the explicit "none clearly fit" escape.
LABELS = [
    "finance", "welfare", "agriculture", "labor", "health",
    "environment", "SMB", "realestate", "statistics", "기타-미분류",
]

# Advisory keyword-domain hint (mirrors scripts/domain_usability_probe.py's
# DOMAIN_KEYWORDS). Case-insensitive substring match on title+claim+query. This
# is the LOOSE cross-check baseline — NOT ground truth. Maps to the LABELS above.
KEYWORD_DOMAINS = {
    "welfare":     ["복지", "지원금", "돌봄", "연금", "수당", "취약계층", "바우처"],
    "labor":       ["고용", "일자리", "실업", "임금", "근로", "노동"],
    "agriculture": ["농업", "축산", "농가", "농림", "식품", "농산물"],
    "health":      ["의료", "질병", "백신", "병원", "건강", "감염병"],
    "environment": ["환경", "탄소", "에너지", "기후", "온실가스", "재생에너지"],
    "finance":     ["금융", "대출", "가계부채", "DSR", "금리", "은행"],
    "SMB":         ["소상공인", "자영업", "중소기업", "새출발기금"],
    "statistics":  ["통계", "지표", "물가지수", "고용률", "실업률", "통계청"],
    # realestate has no keyword set here (housing stories tend to hit the finance
    # keywords 대출/금리). That gap is itself a signal: where Sonnet says
    # "realestate" the keyword hint will say "finance" — exactly the kind of
    # disagreement the spotlight surfaces.
}


def _keyword_domains(text: str) -> list:
    """Advisory: domain labels whose keywords appear (case-insensitive substring)
    in text. A row may match several; order = KEYWORD_DOMAINS order."""
    hay = (text or "").lower()
    hits = []
    for domain, kws in KEYWORD_DOMAINS.items():
        if any(kw.lower() in hay for kw in kws):
            hits.append(domain)
    return hits


def _primary_keyword_domain(hits: list) -> str:
    """Single advisory label for stratification: first hit, or 기타-미분류."""
    return hits[0] if hits else "기타-미분류"


def _build_prompt(title: str, claim: str) -> str:
    """Tight single-label classification prompt. TOOL-FREE: plain text in, one
    label out. No web_search, no tools."""
    claim_snip = (claim or "").strip().replace("\n", " ")[:CLAIM_SNIPPET]
    labels = " / ".join(LABELS)
    return (
        "You are a strict single-label classifier for Korean government / "
        "policy news. Read the article and assign EXACTLY ONE domain label.\n\n"
        f"Allowed labels: {labels}\n\n"
        "Label meanings:\n"
        "- finance: 금융/대출/금리/가계부채/세제/은행 (money policy, not property)\n"
        "- realestate: 부동산/주택/전세/임대/분양 (housing as property)\n"
        "- welfare: 복지/지원금/돌봄/연금/수당/취약계층\n"
        "- labor: 고용/일자리/실업/임금/근로\n"
        "- agriculture: 농업/축산/농가/농림/식품/농산물\n"
        "- health: 의료/질병/백신/병원/건강/감염병\n"
        "- environment: 환경/탄소/에너지/기후/온실가스\n"
        "- SMB: 소상공인/자영업/중소기업\n"
        "- statistics: 통계청 지표/물가지수/고용률/실업률 (statistics as the subject)\n"
        "- 기타-미분류: use ONLY if none of the above clearly fits\n\n"
        "Reply with ONLY the single label token, nothing else.\n\n"
        f"Title: {title or ''}\n"
        f"Claim: {claim_snip}\n"
        "Label:"
    )


def _call_anthropic_tool_free(prompt: str, model: str, api_key: str):
    """TOOL-FREE Anthropic Messages call (no ``tools=``). Mirrors
    hot_topics._call_anthropic_pick (lazy import, ANTHROPIC_API_KEY). Returns the
    raw SDK message; the caller is fail-safe. No web_search, no tools."""
    from anthropic import Anthropic  # lazy import (matches hot_topics.py / llm_judge.py)

    client = Anthropic(api_key=api_key)
    return client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )


def _join_text_blocks(content_blocks) -> str:
    """Concatenate text blocks of the response (mirrors hot_topics)."""
    parts = []
    for block in content_blocks or []:
        if str(getattr(block, "type", "") or "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(parts)


def _parse_label(raw: str) -> str:
    """Robustly extract a single LABEL from the model's reply (handles stray
    text / 'Label: finance' / quotes). Returns the matched label, or
    'UNPARSEABLE' if none of the allowed labels appears."""
    s = (raw or "").strip().strip("`'\" .").lower()
    # 기타-미분류 first (Korean literal); then English labels by substring.
    if "기타" in s or "미분류" in s:
        return "기타-미분류"
    for label in LABELS:
        if label == "기타-미분류":
            continue
        if label.lower() in s:
            return label
    return "UNPARSEABLE"


def _select_sample(url: str):
    """SELECT-only stratified sample. Pull the most recent POOL_LIMIT rows, bucket
    by primary advisory keyword-domain, then round-robin up to PER_DOMAIN per
    domain (+ UNCLASSIFIED_QUOTA 기타 rows), capped at MAX_N. Returns the chosen
    rows + the per-domain selection counts."""
    pool = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, query, title, claim_text "
            "FROM analysis_results ORDER BY id DESC LIMIT %s",
            (POOL_LIMIT,),
        )
        for rid, created_at, query, title, claim_text in cur.fetchall():
            combined = "%s\n%s\n%s" % (title or "", claim_text or "", query or "")
            hits = _keyword_domains(combined)
            primary = _primary_keyword_domain(hits)
            pool.append({
                "id": rid, "created_at": created_at, "query": query,
                "title": title, "claim_text": claim_text,
                "kw_hits": hits, "kw_primary": primary,
            })

    # Bucket by primary advisory domain (preserve recency order within bucket).
    buckets = collections.OrderedDict()
    for dom in list(KEYWORD_DOMAINS.keys()) + ["기타-미분류"]:
        buckets[dom] = []
    for row in pool:
        buckets.setdefault(row["kw_primary"], []).append(row)

    chosen = []
    sel_counts = collections.Counter()
    # Round-robin across domains so coverage is even, not recency-dominated.
    quota = {d: (UNCLASSIFIED_QUOTA if d == "기타-미분류" else PER_DOMAIN)
             for d in buckets}
    cursors = {d: 0 for d in buckets}
    progressed = True
    while len(chosen) < MAX_N and progressed:
        progressed = False
        for dom, rows in buckets.items():
            if sel_counts[dom] >= quota[dom]:
                continue
            i = cursors[dom]
            if i < len(rows):
                chosen.append(rows[i])
                cursors[dom] = i + 1
                sel_counts[dom] += 1
                progressed = True
                if len(chosen) >= MAX_N:
                    break
    return chosen, sel_counts, len(pool)


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe must run in the Render Worker Shell "
              "(or locally with $env:DATABASE_URL pointed at the external DB).")
        return 0
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY not set — the tool-free Sonnet call cannot run.")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    model = os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL

    # Cost helper (reuses the project's estimator; never prints any key).
    try:
        from llm_observability import estimate_cost_usd
    except Exception:
        estimate_cost_usd = None  # type: ignore

    chosen, sel_counts, pool_n = _select_sample(url)

    print("CLASSIFY-PROBE — tool-free Sonnet domain classification (READ-ONLY)")
    print("  model:", model, "  (TOOL-FREE: no web_search, no tools)")
    print("  taxonomy:", " / ".join(LABELS))
    print()

    # ---- Sample description ----------------------------------------------
    print("=== SAMPLE ===")
    print("  pool scanned (most recent by id) :", pool_n, "(LIMIT %d)" % POOL_LIMIT)
    print("  sampled rows                     :", len(chosen),
          "(stratified round-robin; PER_DOMAIN=%d, 기타 quota=%d, MAX_N=%d)"
          % (PER_DOMAIN, UNCLASSIFIED_QUOTA, MAX_N))
    if chosen:
        days = [str(r["created_at"])[:10] for r in chosen if r["created_at"]]
        if days:
            print("  date range (created_at)          :", min(days), "->", max(days))
    print("  per advisory keyword-domain (primary bucket):")
    for dom in list(KEYWORD_DOMAINS.keys()) + ["기타-미분류"]:
        if sel_counts[dom]:
            print("      %-14s %d" % (dom, sel_counts[dom]))
    print()
    if not chosen:
        print("  No rows sampled — nothing to classify.")
        print("\n[Safety] READ-ONLY probe — SELECT-only; no rows written/updated/deleted.")
        return 0

    # ---- Per-row classification (the paid API calls) ----------------------
    results = []
    tokens_in = []
    tokens_out = []
    total_cost = 0.0
    n_unparseable = 0
    for row in chosen:
        prompt = _build_prompt(row["title"], row["claim_text"])
        sonnet = "ERROR"
        in_tok = out_tok = 0
        try:
            msg = _call_anthropic_tool_free(prompt, model, api_key)
            text = _join_text_blocks(getattr(msg, "content", None) or [])
            sonnet = _parse_label(text)
            usage = getattr(msg, "usage", None)
            in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
            out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
            if sonnet == "UNPARSEABLE":
                n_unparseable += 1
                row["_raw"] = (text or "").strip().replace("\n", " ")[:80]
        except Exception as error:  # fail-soft per row; never abort the probe
            sonnet = "ERROR"
            row["_raw"] = "%s: %s" % (type(error).__name__, str(error)[:80])
        if in_tok or out_tok:
            tokens_in.append(in_tok)
            tokens_out.append(out_tok)
            if estimate_cost_usd is not None:
                try:
                    total_cost += float(estimate_cost_usd(model, in_tok, out_tok) or 0.0)
                except Exception:
                    pass
        # Loose agreement: Sonnet's label is among the keyword-hint domains.
        kw = row["kw_hits"]
        if sonnet in ("ERROR", "UNPARSEABLE"):
            agree = "-"
        elif (sonnet == "기타-미분류" and not kw):
            agree = "Y"
        elif sonnet in kw:
            agree = "Y"
        else:
            agree = "N"
        row["sonnet"] = sonnet
        row["agree"] = agree
        results.append(row)
        time.sleep(0.05)  # gentle pacing; not a rate-limit workaround

    # ---- PER-ROW TABLE (the important part — human eyeball) ---------------
    print("=== PER-ROW (eyeball these — 'found != relevant') ===")
    print("  %-7s %-*s %-13s %-12s %s" % ("id", TITLE_W, "title", "kw-hint", "Sonnet", "agree"))
    for r in results:
        title = (r["title"] or "").replace("\n", " ")
        if len(title) > TITLE_W:
            title = title[:TITLE_W - 1] + "…"
        kw_hint = ",".join(r["kw_hits"]) if r["kw_hits"] else "기타-미분류"
        if len(kw_hint) > 13:
            kw_hint = kw_hint[:12] + "…"
        print("  %-7s %-*s %-13s %-12s %s"
              % (r["id"], TITLE_W, title, kw_hint, r["sonnet"], r["agree"]))
    print()

    # ---- Aggregate --------------------------------------------------------
    scored = [r for r in results if r["agree"] in ("Y", "N")]
    agree_y = sum(1 for r in scored if r["agree"] == "Y")
    print("=== AGGREGATE (advisory — keyword hint is NOT ground truth) ===")
    if scored:
        print("  Sonnet-vs-keyword-hint agreement : %d/%d (%.0f%%)"
              % (agree_y, len(scored), 100.0 * agree_y / len(scored)))
    dist = collections.Counter(r["sonnet"] for r in results)
    print("  Sonnet label distribution:")
    for label in LABELS + ["UNPARSEABLE", "ERROR"]:
        if dist.get(label):
            print("      %-14s %d" % (label, dist[label]))
    print("  rows Sonnet put in 기타-미분류    :", dist.get("기타-미분류", 0))
    print()

    # ---- Disagreement spotlight ------------------------------------------
    print("=== DISAGREEMENT SPOTLIGHT (read these — often Sonnet is right) ===")
    disagree = [r for r in results if r["agree"] == "N"]
    if not disagree:
        print("  (none)")
    for r in disagree:
        title = (r["title"] or "").replace("\n", " ")[:70]
        print("  id=%s  kw-hint=[%s]  Sonnet=%s"
              % (r["id"], ",".join(r["kw_hits"]) or "기타-미분류", r["sonnet"]))
        print("       %s" % title)
    print()

    # ---- Cost -------------------------------------------------------------
    print("=== COST (tool-free; no web_search surcharge) ===")
    n_calls = len(tokens_in)
    if n_calls:
        tot_in = sum(tokens_in)
        tot_out = sum(tokens_out)
        all_tok = [a + b for a, b in zip(tokens_in, tokens_out)]
        avg_tok = sum(all_tok) / len(all_tok)
        print("  calls with usage           :", n_calls)
        print("  input tokens  (sum/avg)    : %d / %.0f" % (tot_in, tot_in / n_calls))
        print("  output tokens (sum/avg)    : %d / %.0f" % (tot_out, tot_out / n_calls))
        print("  tokens/call  min/avg/max   : %d / %.0f / %d"
              % (min(all_tok), avg_tok, max(all_tok)))
        print("  TOTAL spend this run       : ~$%.4f" % total_cost)
        per_1000 = (total_cost / n_calls) * 1000 if n_calls else 0.0
        print("  estimated $/1000 rows      : ~$%.2f" % per_1000)
        print("  baseline (hot-topic pick)  : ~1,369 tokens, ~$0.005/call (per-call,")
        print("                               many titles at once — this probe is")
        print("                               1 row/call, so tokens/call are smaller).")
    else:
        print("  No usage recorded (all calls errored?). See ERROR rows above.")
    print()

    # ---- Failure / edge notes --------------------------------------------
    print("=== FAILURE / EDGE NOTES ===")
    bad = [r for r in results if r["sonnet"] in ("UNPARSEABLE", "ERROR")]
    if not bad:
        print("  No malformed / errored / refused responses.")
    for r in bad:
        print("  id=%s  %s  raw=%r" % (r["id"], r["sonnet"], r.get("_raw", "")))
    multi = [r for r in results if len(r["kw_hits"]) >= 2]
    print("  rows the keyword hint flagged as multi-domain (legitimately cross-domain"
          " candidates to eyeball): %d" % len(multi))
    print()

    print("=== VERDICT (human reads the table above, then decides) ===")
    print("  This probe does NOT auto-decide. Judge from the PER-ROW table +")
    print("  DISAGREEMENT spotlight whether the Sonnet labels are actually correct")
    print("  (the keyword agreement %% is advisory only), and from COST whether")
    print("  $/1000 is acceptable. If labels are accurate AND cheap -> the tool-free")
    print("  Sonnet classifier approach is sound to build (separate milestone).")
    print()
    print("[Safety] READ-ONLY probe — SELECT-only DB access; tool-free Anthropic")
    print("         call; no rows written/updated/deleted; no verdict field touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
