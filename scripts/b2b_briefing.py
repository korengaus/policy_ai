# B2B-BRIEFING v0 — per-customer weekly policy-SPREAD briefing generator.
#
# READ-ONLY: SELECTs the newest brainmap_graph row + analysis_results
# published_at (the EXACT SELECTs generate_weekly_report.py uses, imported —
# not copied) and writes NOTHING to the DB. Output = one self-contained HTML
# briefing + one audit JSON per customer under ./b2b_briefings/ (gitignored).
#
# ENGINE REUSE (the whole point):
#   * build_report (generate_weekly_report.py) produces the windowed ranked
#     cluster list — called with top_n=10**6 so the FULL qualifying ranking
#     comes back; this script re-implements NO ranking and NO windowing.
#   * The customer filter joins entries to graph_json clusters by stable_id
#     (the key build_report preserves) and reads only verdict-free node
#     fields: id / title / domain / content_nature.
#
# VERDICT-ISOLATED (hard):
#   * SPREAD + SYNDICATION only. No verdict_label, no policy_confidence, no
#     has_genuine_official_support (official-source status = v1), no
#     truth/falsity/probability field ANYWHERE.
#   * Honest framing strings are reused BYTE-EXACT and asserted against
#     honesty_guard.FRAMING_WHITELIST at import (drift = refuse to run).
#   * Every briefing data dict passes honesty_guard.validate_payload
#     (generic walker) PLUS a generated-string vocab scan before any file
#     is written — fail-closed.
#
# USAGE (operator, LOCAL machine or Worker Shell — DATABASE_URL only; this
# script never needs USE_POSTGRES_WRITE because it never writes the DB):
#   python scripts/b2b_briefing.py --selftest          # DB-free logic check
#   python scripts/b2b_briefing.py                     # all customers, last 7 days
#   python scripts/b2b_briefing.py --customer finance_ir --days 14
#   python scripts/b2b_briefing.py --week-start 2026-07-06 --week-end 2026-07-12

import argparse
import html
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

# Engine + honesty imports (REUSE, never copy). generate_weekly_report has no
# numpy dependency — safe to import anywhere the weekly engine runs.
from generate_weekly_report import (  # noqa: E402
    FRAMING_TEXT,
    SELECT_NEWEST_GRAPH_SQL,
    SELECT_PUBLISHED_SQL,
    build_report,
)
from honesty_guard import (  # noqa: E402
    FORBIDDEN_LABEL_VOCAB,
    FRAMING_WHITELIST,
    validate_payload,
)

# The syndication phrase (build_brainmap_graph.SYNDICATION_FRAMING). Defined
# as a literal here (importing build_brainmap_graph would pull numpy) but
# asserted byte-exact against the whitelist below — drift refuses to run.
SYNDICATION_NOTE = "첫 보도와 제목·주장 문구가 거의 동일"

# Footer honesty line — GENERATED copy, so it must carry none of the
# forbidden vocab (검증/confirmed/verified/truth/probability). It describes
# the briefing as a circulation summary, never a truth or outcome judgment.
FOOTER_NOTE = (
    "이 브리핑은 뉴스 유통(확산) 규모와 문구 유사성만 요약합니다. "
    "각 주장의 사실 여부나 정책의 추진·성패에 대한 판단이 아닙니다."
)

# Byte-exactness gate: the two reused framing strings MUST be the whitelisted
# bytes. A drifted literal is a bug — refuse to run at import time.
if FRAMING_TEXT not in FRAMING_WHITELIST:
    raise RuntimeError("FRAMING_TEXT drifted from honesty_guard.FRAMING_WHITELIST")
if SYNDICATION_NOTE not in FRAMING_WHITELIST:
    raise RuntimeError("SYNDICATION_NOTE drifted from honesty_guard.FRAMING_WHITELIST")

DEFAULT_TOP_N = 12
DEFAULT_CONFIG = _SCRIPTS_DIR / "b2b_customers.json"
OUTPUT_DIR = _PROJECT_ROOT / "b2b_briefings"
CARD_URL = "https://tickedin.org/?result_id=%d"
BRAINMAP_URL = "https://tickedin.org/web/brainmap.html?focus=%d"

# content_nature fallback mirrors content_nature_classifier.FALLBACK_LABEL:
# a node missing the field (old graph rows) is treated as the fail-to-safe
# policy-side label, so the gate never silently drops un-labeled policy news.
CONTENT_NATURE_FALLBACK = "mixed_or_unclear"


# ---------------------------------------------------------------------------
# Graph join + customer filter (pure — selftestable without a DB)
# ---------------------------------------------------------------------------
def build_cluster_lookup(graph):
    """graph_json -> {stable_id: {label_title, outlet_count,
    near_anchor_outlet_count, members:[{id,title,domain,content_nature}]}}.
    Reads ONLY verdict-free node/cluster fields."""
    nodes_by_cluster = {}
    for node in graph.get("nodes") or []:
        cid = node.get("cluster_id")
        if cid is None or node.get("id") is None:
            continue
        nodes_by_cluster.setdefault(cid, []).append({
            "id": node.get("id"),
            "title": node.get("title") or "",
            "domain": node.get("domain"),
            "content_nature": node.get("content_nature") or CONTENT_NATURE_FALLBACK,
        })
    lookup = {}
    for cluster in graph.get("clusters") or []:
        stable_id = cluster.get("stable_id")
        if not stable_id:
            continue
        lookup[stable_id] = {
            "label_title": cluster.get("label_title") or "",
            "outlet_count": cluster.get("outlet_count"),
            "near_anchor_outlet_count": cluster.get("near_anchor_outlet_count"),
            "members": nodes_by_cluster.get(cluster.get("cluster_id")) or [],
        }
    return lookup


def _span_days(first_at, last_at):
    try:
        first = datetime.strptime(str(first_at)[:10], "%Y-%m-%d").date()
        last = datetime.strptime(str(last_at)[:10], "%Y-%m-%d").date()
        return max(1, (last - first).days + 1)
    except (TypeError, ValueError):
        return None


def filter_entries_for_customer(entries, lookup, profile):
    """Preserve build_report's ranking order; keep entries relevant to the
    profile. relevance = domain_match OR keyword_match; then the
    content_nature keep-list gates on the REPRESENTATIVE node's label;
    then the OPTIONAL exclude_keywords negative filter (2b): a cluster
    tripping an exclude term is dropped UNLESS it also matched a positive
    keyword — an explicit customer interest beats a broad exclusion, so
    genuinely-relevant crossover clusters survive. Absent/empty
    exclude_keywords = prior behavior exactly."""
    domains = set(profile.get("domains") or [])
    keywords = [k for k in (profile.get("keywords") or []) if k]
    exclude_keywords = [k for k in (profile.get("exclude_keywords") or []) if k]
    natures = set(profile.get("content_nature") or [])
    kept = []
    for entry in entries:
        cluster = lookup.get(entry.get("stable_id"))
        if not cluster:
            continue
        members = cluster["members"]
        matched_domains = sorted(
            {m["domain"] for m in members if m["domain"] in domains}
        )
        haystacks = [cluster["label_title"]] + [m["title"] for m in members]
        matched_keywords = [
            kw for kw in keywords
            if any(kw.casefold() in (h or "").casefold() for h in haystacks)
        ]
        if not matched_domains and not matched_keywords:
            continue
        if natures:
            rep_id = entry.get("representative_analysis_id")
            rep_nature = next(
                (m["content_nature"] for m in members if m["id"] == rep_id),
                CONTENT_NATURE_FALLBACK,
            )
            if rep_nature not in natures:
                continue
        excluded = any(
            any(exkw.casefold() in (h or "").casefold() for h in haystacks)
            for exkw in exclude_keywords
        )
        if excluded and not matched_keywords:
            continue
        kept.append((entry, cluster, matched_domains, matched_keywords))
    return kept


def build_briefing_data(profile, kept, week_start, week_end, top_n):
    """The verdict-free data dict (audit JSON + HTML input). No official
    status field (v1), no truth/falsity/probability field anywhere."""
    items = []
    for rank, (entry, cluster, matched_domains, matched_keywords) in enumerate(
            kept[:top_n], start=1):
        rep = entry.get("representative_analysis_id")
        near = cluster.get("near_anchor_outlet_count")
        items.append({
            "rank": rank,
            "stable_id": entry.get("stable_id"),
            "title": entry.get("title") or "",
            "outlet_count": entry.get("outlet_count"),
            "first_at": entry.get("first_at"),
            "last_at": entry.get("last_at"),
            "span_days": _span_days(entry.get("first_at"), entry.get("last_at")),
            "near_anchor_outlet_count": near,
            # Whitelisted phrase, ONLY when >=2 outlets share near-identical
            # wording (the /api/spread rule); otherwise omitted entirely.
            "syndication_note": SYNDICATION_NOTE
                if isinstance(near, int) and near >= 2 else "",
            "matched_domains": matched_domains,
            "matched_keywords": matched_keywords,
            "representative_id": rep,
            "card_url": CARD_URL % rep if isinstance(rep, int) else "",
            "brainmap_url": BRAINMAP_URL % rep if isinstance(rep, int) else "",
        })
    return {
        "kind": "b2b_spread_briefing",
        "customer": {
            "id": profile.get("id"),
            "display_name": profile.get("display_name") or profile.get("id"),
        },
        "week": {"start": week_start, "end": week_end},
        "framing": FRAMING_TEXT,
        "items": items,
        "note": FOOTER_NOTE,
    }


def briefing_honesty_ok(data):
    """Fail-closed self-check before any file write. Two layers:
    (1) honesty_guard.validate_payload — the generic I1-I5 walker;
    (2) a vocab scan over the strings THIS script generates (framing,
        syndication_note, note, kind) — whitelisted framing bytes exempt.
    Titles are journalist passthrough (the weekly engine's guard scope) and
    are NOT vocab-scanned."""
    ok, violations = validate_payload(data)
    generated = [data.get("note") or "", data.get("kind") or "",
                 data.get("framing") or ""]
    for item in data.get("items") or []:
        generated.append(item.get("syndication_note") or "")
    for text in generated:
        if text in FRAMING_WHITELIST:
            continue
        lowered = text.lower()
        for word in FORBIDDEN_LABEL_VOCAB:
            if word in lowered:
                ok = False
                violations.append({
                    "path": "generated", "rule": "B2B_FORBIDDEN_VOCAB",
                    "detail": "generated string carries %r" % word,
                })
    return ok, violations


# ---------------------------------------------------------------------------
# HTML render — self-contained inline-CSS page, mirroring web/weekly.html's
# tokens/tone (standalone, no external lib/CDN; titles escaped).
# ---------------------------------------------------------------------------
def render_briefing_html(data):
    esc = html.escape
    customer = data["customer"]["display_name"]
    week = data["week"]
    rows = []
    for item in data["items"]:
        period = "%s → %s" % ((item["first_at"] or "")[:10],
                              (item["last_at"] or "")[:10])
        span = ("%d일" % item["span_days"]) if item["span_days"] else ""
        matched = []
        if item["matched_domains"]:
            matched.append("도메인 " + ", ".join(item["matched_domains"]))
        if item["matched_keywords"]:
            matched.append("키워드 " + ", ".join(item["matched_keywords"]))
        synd = ('<div class="synd">%s · %d개 매체</div>'
                % (esc(item["syndication_note"]),
                   item["near_anchor_outlet_count"])
                if item["syndication_note"] else "")
        links = []
        if item["card_url"]:
            links.append('<a href="%s" target="_blank" rel="noopener noreferrer">상세 카드</a>' % esc(item["card_url"]))
        if item["brainmap_url"]:
            links.append('<a href="%s" target="_blank" rel="noopener noreferrer">브레인맵</a>' % esc(item["brainmap_url"]))
        rows.append("""
      <li class="item">
        <div class="rank">%d</div>
        <div class="body">
          <div class="title">%s</div>
          <div class="meta">%s개 매체 · %s%s</div>
          %s
          <div class="matched">관련: %s</div>
          <div class="links">%s</div>
        </div>
      </li>""" % (
            item["rank"], esc(item["title"]),
            esc(str(item["outlet_count"] or "?")), esc(period),
            esc(" · %s" % span if span else ""),
            synd,
            esc(" · ".join(matched) or "-"),
            " · ".join(links),
        ))
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s · 정책 확산 브리핑 — tickedin</title>
<style>
  :root {
    --ink: #0f172a; --slate: #475569; --muted: #94a3b8; --line: #e2e8f0;
    --paper: #ffffff; --canvas: #f6f8fb; --brand: #1e5fd8; --brand-ink: #1542a0;
    --shadow: 0 8px 24px rgba(15, 23, 42, 0.06); --radius-sm: 9px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--canvas); color: var(--ink); min-height: 100vh;
    font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', 'Malgun Gothic', system-ui, sans-serif; }
  header { background: var(--paper); border-bottom: 1px solid var(--line); padding: 18px 24px; }
  .brand { color: var(--brand); font-weight: 800; font-size: 20px; }
  h1 { font-size: 22px; margin-top: 8px; }
  .period { color: var(--slate); font-size: 14px; margin-top: 4px; }
  .chip { display: inline-block; margin-top: 10px; padding: 4px 10px; border: 1px solid var(--line);
    border-radius: 999px; background: var(--canvas); color: var(--slate); font-size: 12.5px; font-weight: 600; }
  .intro { color: var(--slate); font-size: 14px; margin-top: 10px; }
  main { max-width: 860px; margin: 0 auto; padding: 20px 24px 40px; }
  ol.items { list-style: none; }
  .item { display: flex; gap: 14px; background: var(--paper); border: 1px solid var(--line);
    border-radius: var(--radius-sm); box-shadow: var(--shadow); padding: 14px 16px; margin-top: 12px; }
  .rank { flex: 0 0 30px; color: var(--brand); font-size: 20px; font-weight: 800; text-align: center; }
  .title { font-size: 16px; font-weight: 700; line-height: 1.45; }
  .meta { color: var(--slate); font-size: 13.5px; margin-top: 4px; }
  .synd { color: var(--slate); font-size: 13px; margin-top: 4px; }
  .matched { color: var(--muted); font-size: 12.5px; margin-top: 4px; }
  .links { margin-top: 6px; font-size: 13px; }
  .links a { color: var(--brand); text-decoration: none; }
  .links a:hover { color: var(--brand-ink); text-decoration: underline; }
  footer { max-width: 860px; margin: 0 auto; padding: 0 24px 40px; color: var(--muted);
    font-size: 12.5px; border-top: 1px solid var(--line); padding-top: 14px; }
</style>
</head>
<body>
<header>
  <div class="brand">tickedin</div>
  <h1>%s · 정책 확산 브리핑</h1>
  <div class="period">%s ~ %s</div>
  <div class="chip">%s</div>
  <div class="intro">%s 관심 영역의 뉴스가 이번 기간 얼마나 널리 유통되었는지 보여주는 모니터링 요약입니다. 각 주장의 사실 여부에 대한 판단이 아닙니다.</div>
</header>
<main>
  <ol class="items">%s
  </ol>
</main>
<footer>%s</footer>
</body>
</html>
""" % (esc(customer), esc(customer), esc(week["start"]), esc(week["end"]),
       esc(data["framing"]), esc(customer), "".join(rows), esc(data["note"]))


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — synthetic graph + publish dates. No DB, no network,
# no DATABASE_URL needed.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    week_start, week_end = "2026-07-06", "2026-07-12"
    graph = {
        "nodes": [
            # Cluster A — realestate policy, 3 members, syndicated (near=2),
            # mixed member domains, keyword 재건축 in the label.
            {"id": 1, "cluster_id": "A", "title": "재건축 규제 완화 대책 발표",
             "domain": "realestate", "content_nature": "government_policy"},
            {"id": 2, "cluster_id": "A", "title": "정부, 재건축 규제 완화 추진",
             "domain": "finance", "content_nature": "government_policy"},
            {"id": 3, "cluster_id": "A", "title": "재건축 완화안 후속 보도",
             "domain": "realestate", "content_nature": "government_policy"},
            # Cluster B — finance policy, keyword 기준금리, NOT syndicated.
            {"id": 4, "cluster_id": "B", "title": "기준금리 동결 결정",
             "domain": "finance", "content_nature": "government_policy"},
            {"id": 5, "cluster_id": "B", "title": "한은 금리 동결 배경",
             "domain": "finance", "content_nature": "government_policy"},
            # Cluster C — realestate MARKET noise (rep is market_commercial).
            {"id": 6, "cluster_id": "C", "title": "강남 아파트 시세 급등",
             "domain": "realestate", "content_nature": "market_commercial"},
            {"id": 7, "cluster_id": "C", "title": "서초 매물 호가 상승",
             "domain": "realestate", "content_nature": "market_commercial"},
            # Cluster D — welfare policy (for the OR-union test).
            {"id": 8, "cluster_id": "D", "title": "기초연금 인상안 확정",
             "domain": "welfare", "content_nature": "government_policy"},
        ],
        "clusters": [
            {"cluster_id": "A", "stable_id": "aaaaaaaaaaaa",
             "label_title": "재건축 규제 완화 대책 발표", "size": 3,
             "outlet_count": 3, "size_label": "3개 매체 보도 중",
             "near_anchor_outlet_count": 2, "exact_same_text_outlet_count": 0},
            {"cluster_id": "B", "stable_id": "bbbbbbbbbbbb",
             "label_title": "기준금리 동결 결정", "size": 2,
             "outlet_count": 2, "size_label": "2개 매체 보도 중",
             "near_anchor_outlet_count": 1, "exact_same_text_outlet_count": 0},
            {"cluster_id": "C", "stable_id": "cccccccccccc",
             "label_title": "강남 아파트 시세 급등", "size": 2,
             "outlet_count": 2, "size_label": "2개 매체 보도 중",
             "near_anchor_outlet_count": 0, "exact_same_text_outlet_count": 0},
            {"cluster_id": "D", "stable_id": "dddddddddddd",
             "label_title": "기초연금 인상안 확정", "size": 1,
             "outlet_count": 1, "size_label": "1개 매체 보도 중",
             "near_anchor_outlet_count": 0, "exact_same_text_outlet_count": 0},
        ],
    }
    published = {i: "2026-07-0%dT09:00:00+00:00" % (6 + (i % 4)) for i in range(1, 9)}

    payload = build_report(graph, published, week_start, week_end, top_n=10 ** 6)
    entries = payload["top"]
    lookup = build_cluster_lookup(graph)

    def kept_ids(profile):
        kept = filter_entries_for_customer(entries, lookup, profile)
        return [e["stable_id"] for e, _, _, _ in kept]

    # (a) domain-only filter (no nature gate): realestate -> A and C.
    a_ok = kept_ids({"domains": ["realestate"], "keywords": [],
                     "content_nature": []}) == ["aaaaaaaaaaaa", "cccccccccccc"]
    # (b) keyword substring match: 기준금리 -> B only.
    b_ok = kept_ids({"domains": [], "keywords": ["기준금리"],
                     "content_nature": []}) == ["bbbbbbbbbbbb"]
    # (c) content_nature gate drops the market_commercial cluster C.
    c_ok = kept_ids({"domains": ["realestate"], "keywords": [],
                     "content_nature": ["government_policy", "mixed_or_unclear"]}
                    ) == ["aaaaaaaaaaaa"]
    # (d) OR-relevance union: welfare domain OR 재건축 keyword -> A + D.
    d_ok = set(kept_ids({"domains": ["welfare"], "keywords": ["재건축"],
                         "content_nature": []})) == {"aaaaaaaaaaaa", "dddddddddddd"}

    # (i) 2b exclude: A matches only via the finance domain (member id=2) and
    # trips exclude 재건축 with NO positive-keyword hit -> DROPPED; B stays.
    i_ok = kept_ids({"domains": ["finance"], "keywords": ["기준금리"],
                     "exclude_keywords": ["재건축"], "content_nature": []}
                    ) == ["bbbbbbbbbbbb"]
    # (ii) 2b crossover survives: A trips exclude 규제 BUT also matches the
    # positive keyword 재건축 -> KEPT (explicit interest beats exclusion).
    ii_ok = kept_ids({"domains": [], "keywords": ["재건축"],
                      "exclude_keywords": ["규제"], "content_nature": []}
                     ) == ["aaaaaaaaaaaa"]
    # (iii) backward compat: absent vs empty exclude_keywords are identical
    # (and match the pre-2b result from check (a)).
    iii_ok = (kept_ids({"domains": ["realestate"], "keywords": [],
                        "content_nature": []})
              == kept_ids({"domains": ["realestate"], "keywords": [],
                           "exclude_keywords": [], "content_nature": []})
              == ["aaaaaaaaaaaa", "cccccccccccc"])

    # (e)-(h) on a produced data dict + HTML.
    profile = {"id": "selftest", "display_name": "셀프테스트",
               "domains": ["realestate", "finance", "welfare"], "keywords": [],
               "content_nature": ["government_policy", "mixed_or_unclear"]}
    kept = filter_entries_for_customer(entries, lookup, profile)
    data = build_briefing_data(profile, kept, week_start, week_end, DEFAULT_TOP_N)
    by_sid = {i["stable_id"]: i for i in data["items"]}
    e_ok = (by_sid["aaaaaaaaaaaa"]["syndication_note"] == SYNDICATION_NOTE
            and by_sid["bbbbbbbbbbbb"]["syndication_note"] == ""
            and by_sid["dddddddddddd"]["syndication_note"] == "")
    f_ok, f_violations = briefing_honesty_ok(data)
    # (g) no forbidden vocab in ANY output string (synthetic titles are clean)
    # except the whitelisted framing bytes; check the JSON + HTML blobs with
    # the whitelisted strings removed.
    html_out = render_briefing_html(data)
    blob = json.dumps(data, ensure_ascii=False) + html_out
    for allowed in FRAMING_WHITELIST:
        blob = blob.replace(allowed, "")
    g_ok = not any(w in blob.lower() for w in FORBIDDEN_LABEL_VOCAB)
    # (h) no truth/falsity field anywhere in the data dict.
    keys_blob = json.dumps(sorted(set(
        k for item in [data] + data["items"] + [data["customer"], data["week"]]
        for k in item.keys())), ensure_ascii=False)
    h_ok = not any(bad in keys_blob.lower()
                   for bad in ("truth", "falsity", "probability", "verdict"))

    checks = {"a_domain": a_ok, "b_keyword": b_ok, "c_nature_gate": c_ok,
              "d_or_union": d_ok, "e_syndication": e_ok, "f_honesty": f_ok,
              "g_vocab": g_ok, "h_no_truth_field": h_ok,
              "i_exclude_drops": i_ok, "ii_crossover_kept": ii_ok,
              "iii_backward_compat": iii_ok}
    for name, ok in checks.items():
        print("  %-18s %s" % (name, "ok" if ok else "FAIL"))
    if not f_ok:
        print("  honesty violations: %r" % f_violations)
    ok = all(checks.values())
    print("SELFTEST: %s (%d entries ranked, %d kept for the combined profile, "
          "self-check path: validate_payload generic walker + generated-string scan)"
          % ("PASS" if ok else "FAIL", len(entries), len(data["items"])))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="b2b_briefing",
        description="Per-customer verdict-free policy-SPREAD briefing from the "
                    "weekly engine (read-only DB; writes only local HTML/JSON).",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic graph; no DB).")
    parser.add_argument("--customer", default=None,
                        help="One customer id from the config (default: all).")
    parser.add_argument("--week-start", default=None,
                        help="YYYY-MM-DD window start (default: today-6, UTC).")
    parser.add_argument("--week-end", default=None,
                        help="YYYY-MM-DD window end, inclusive (default: today, UTC).")
    parser.add_argument("--days", type=int, default=None,
                        help="Shortcut: window = last N days (overrides start).")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="Items per briefing (default %d)." % DEFAULT_TOP_N)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="Customer profiles JSON (default scripts/b2b_customers.json).")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    # Window default mirrors generate_weekly_report.main (today-6 .. today UTC).
    today = datetime.now(timezone.utc).date()
    week_end = args.week_end or today.isoformat()
    week_start = args.week_start or (
        (today - timedelta(days=(args.days - 1))).isoformat() if args.days
        else (today - timedelta(days=6)).isoformat())
    if week_start > week_end:
        print("[b2b] week_start %s is after week_end %s — aborting." % (week_start, week_end))
        return 1

    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("[b2b] cannot read config %s: %s" % (args.config, exc))
        return 1
    profiles = config.get("customers") or []
    if args.customer:
        profiles = [p for p in profiles if p.get("id") == args.customer]
        if not profiles:
            print("[b2b] no customer %r in %s" % (args.customer, args.config))
            return 1

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — point it at the external Postgres.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    print("B2B-BRIEFING — window %s..%s top_n=%d customers=%d"
          % (week_start, week_end, args.top_n, len(profiles)))
    # READ-ONLY: the exact two SELECTs the weekly engine uses; nothing written.
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_NEWEST_GRAPH_SQL)
            graph_row = cur.fetchone()
        if not graph_row:
            print("[b2b] no brainmap_graph row — run scripts/build_brainmap_graph.py first.")
            return 1
        try:
            graph = json.loads(graph_row[2])
        except (TypeError, ValueError):
            print("[b2b] newest brainmap_graph row holds invalid JSON — aborting.")
            return 1
        with conn.cursor() as cur:
            cur.execute(SELECT_PUBLISHED_SQL)
            published_by_id = {row_id: value for row_id, value in cur.fetchall()}

    payload = build_report(graph, published_by_id, week_start, week_end, top_n=10 ** 6)
    entries = payload["top"]
    lookup = build_cluster_lookup(graph)
    print("[b2b] %d windowed clusters ranked (of %d qualifying)"
          % (len(entries), payload["qualifying_clusters"]))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for profile in profiles:
        cid = profile.get("id") or "customer"
        if not (profile.get("domains") or profile.get("keywords")):
            print("[b2b] %s: profile has neither domains nor keywords — "
                  "empty briefing skipped (specify at least one relevance axis)." % cid)
            continue
        kept = filter_entries_for_customer(entries, lookup, profile)
        data = build_briefing_data(profile, kept, week_start, week_end, args.top_n)
        ok, violations = briefing_honesty_ok(data)
        if not ok:
            failures += 1
            print("[b2b] %s: HONESTY CHECK FAILED — nothing written. %r"
                  % (cid, violations))
            continue
        html_path = OUTPUT_DIR / ("%s_%s.html" % (cid, week_start))
        json_path = OUTPUT_DIR / ("%s_%s.json" % (cid, week_start))
        html_path.write_text(render_briefing_html(data), encoding="utf-8")
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[b2b] %s: %d items -> %s (+ .json)"
              % (cid, len(data["items"]), html_path))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
