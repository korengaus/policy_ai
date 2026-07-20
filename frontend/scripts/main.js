    // ===== C1 — Config, DOM handles, state & label maps =====
    const API_BASE = window.location.origin;
    // M17-search-quality: storage key bumped from
    // "policy_ai_recent_analysis" to invalidate cached entries written
    // before the hot-topic card stopped silently surfacing localStorage
    // history. Old entries — overwhelmingly 전세대출 from the platform's
    // historical default query — are cleared via the one-shot
    // _M17_LEGACY_HISTORY_KEY removal at page-load init below.
    const LOCAL_HISTORY_KEY = "policy_ai_recent_analysis_v2";
    const _M17_LEGACY_HISTORY_KEY = "policy_ai_recent_analysis";
    const REVIEW_QUEUE_KEY = "policy_ai_review_queue";
    const REVIEW_ACTION_KEY = "policy_ai_reviewer_actions";
    const LOCAL_HISTORY_LIMIT = 5;
    const REVIEW_QUEUE_LIMIT = 20;
    // RECENT-VIEWED: NEW localStorage key for the detail-screen "최근 본 검증" strip
    // (click history — distinct from the home search-history LOCAL_HISTORY_KEY above).
    const RECENT_VIEWED_KEY = "policy_ai_recent_viewed_v1";
    const RECENT_VIEWED_LIMIT = 8;

    // M17-search-quality: one-shot cleanup of the legacy storage key.
    // Idempotent — removeItem on a missing key is a no-op. Wrapped in
    // try/catch because localStorage may be unavailable (Safari private
    // mode, disabled cookies); a cleanup failure must never break init.
    try {
      if (typeof localStorage !== "undefined"
          && localStorage.getItem(_M17_LEGACY_HISTORY_KEY) !== null) {
        localStorage.removeItem(_M17_LEGACY_HISTORY_KEY);
      }
    } catch (_) { /* localStorage unavailable — proceed */ }

    // Phase 2 M3: lightweight localStorage to avoid QuotaExceededError.
    // The slim shape keeps just enough to render history rows and topic cards;
    // full results are hydrated on demand via GET /history/{result_id}.
    const POLICY_AI_DEBUG = (() => {
      try {
        return /[?&]debug=1\b/.test(window.location.search || "")
          || ["localhost", "127.0.0.1", "0.0.0.0"].includes(window.location.hostname);
      } catch (_) {
        return false;
      }
    })();

    function debugLogStorageWrite(key, serialized) {
      if (!POLICY_AI_DEBUG) return;
      try {
        const bytes = serialized ? new Blob([serialized]).size : 0;
        console.debug(`[policy-ai] localStorage write key=${key} bytes=${bytes}`);
      } catch (_) {
        // size logging is best-effort only
      }
    }

    const safeStorage = {
      get(key) {
        try {
          return localStorage.getItem(key);
        } catch (error) {
          console.warn(`safeStorage.get failed for ${key}`, error);
          return null;
        }
      },
      set(key, value, options) {
        const serialized = typeof value === "string" ? value : JSON.stringify(value);
        try {
          localStorage.setItem(key, serialized);
          debugLogStorageWrite(key, serialized);
          return true;
        } catch (error) {
          const isQuota = error && (
            error.name === "QuotaExceededError"
            || error.code === 22
            || error.code === 1014
          );
          if (!isQuota) {
            console.warn(`safeStorage.set failed for ${key}`, error);
            return false;
          }
          console.warn(`safeStorage.set quota exceeded for ${key}; attempting graceful trim`);
          const trimmer = options && typeof options.onQuotaTrim === "function"
            ? options.onQuotaTrim
            : null;
          if (trimmer) {
            try {
              const trimmedSerialized = trimmer(value);
              if (typeof trimmedSerialized === "string") {
                localStorage.setItem(key, trimmedSerialized);
                debugLogStorageWrite(key, trimmedSerialized);
                return true;
              }
            } catch (innerError) {
              console.warn(`safeStorage.set trim attempt failed for ${key}`, innerError);
            }
          }
          try {
            localStorage.removeItem(key);
          } catch (_) {
            // ignore — best-effort cleanup
          }
          console.warn(`safeStorage.set giving up on ${key} after quota error; entry cleared`);
          return false;
        }
      },
      remove(key) {
        try {
          localStorage.removeItem(key);
        } catch (error) {
          console.warn(`safeStorage.remove failed for ${key}`, error);
        }
      },
    };

    function trimArrayPayload(parsedValue) {
      const arr = Array.isArray(parsedValue) ? parsedValue : [];
      const half = Math.max(1, Math.floor(arr.length / 2));
      return JSON.stringify(arr.slice(0, half));
    }

    function trimMapPayload(parsedValue) {
      const map = parsedValue && typeof parsedValue === "object" && !Array.isArray(parsedValue)
        ? parsedValue
        : {};
      const keys = Object.keys(map);
      const half = Math.max(1, Math.floor(keys.length / 2));
      const trimmed = {};
      keys.slice(0, half).forEach((key) => { trimmed[key] = map[key]; });
      return JSON.stringify(trimmed);
    }

    const queryInput = document.getElementById("query");
    const maxNewsInput = document.getElementById("maxNews");
    const analyzeBtn = document.getElementById("analyzeBtn");
    const historyBtn = document.getElementById("historyBtn");
    const clearHistoryBtn = document.getElementById("clearHistoryBtn");
    const copyReportBtn = document.getElementById("copyReportBtn");
    const downloadReportBtn = document.getElementById("downloadReportBtn");
    const downloadMarkdownBtn = document.getElementById("downloadMarkdownBtn");
    const statusLine = document.getElementById("statusLine");
    const errorBox = document.getElementById("errorBox");
    const metricsEl = document.getElementById("metrics");
    const reportActionsEl = document.getElementById("reportActions");
    const resultsEl = document.getElementById("results");
    const historyEl = document.getElementById("history");
    const reviewQueueEl = document.getElementById("reviewQueue");
    const reviewFilterEl = document.getElementById("reviewFilter");
    const selectedIssueIntroEl = document.getElementById("selectedIssueIntro");
    const hotTopicsEl = document.getElementById("hotTopics");
    // DESIGN-C3h-1d: top feed container (hero band + 오늘의 검증 row) — sits above the
    // static sort row, with the card-row + 1-col list rendering into #hotTopics below.
    const hotTopicsTopEl = document.getElementById("hotTopicsTop");
    // DESIGN-C3h-2: per-domain grouped sections container (filled on the 전체 tab only).
    const feedDomainSectionsEl = document.getElementById("feedDomainSections");
    const verifyHowEl = document.getElementById("verifyHowSection");
    // SIDEBAR-RANK-B2: weekly-stats panel numbers + range; 제보 input/button.
    // HOME-TOP5 S5a: 확산 성장 Top 5 sidebar panel (filled from /api/trending).
    const trendingPanelEl = document.getElementById("trendingPanel");
    const trendingListEl = document.getElementById("trendingList");
    const statTotalEl = document.getElementById("statTotal");
    const statOfficialEl = document.getElementById("statOfficial");
    const statDraftEl = document.getElementById("statDraft");
    const statRangeEl = document.getElementById("statRange");
    // HOME-SECTION-FIX A1: the top utility-bar counts (were static "—" dashes).
    // Fed by the SAME read-only GET /stats the sidebar panel uses.
    // MOBILE-POLISH B: now cumulative-first — 누적 검증 (cumulative_total, the
    // unbounded corpus count) then 이번 주 (total, the 7-day window). The old
    // 검증 진행 (draft) figure was retired from the banner; the sidebar's
    // #statDraft still shows it, so no number was lost.
    const utilityUpdateCountEl = document.getElementById("utilityUpdateCount");
    const utilityTotalCountEl = document.getElementById("utilityTotalCount");
    const utilityCumulativeClauseEl = document.getElementById("utilityCumulativeClause");
    const reportClaimInputEl = document.getElementById("reportClaimInput");
    const reportClaimBtnEl = document.getElementById("reportClaimBtn");
    const categoryTabsEl = document.getElementById("categoryTabs");
    const hotTopicsSortEl = document.getElementById("hotTopicsSort");
    // DESIGN-C3-2: page-number nav container (replaced the dead 더보기/접기 buttons).
    const feedPaginationEl = document.getElementById("feedPagination");
    // HOMEPAGE-TIERED: tier-2 (concise "나머지 뉴스") elements. #domainSections is
    // repurposed as the tier-2 card grid (was the retired per-domain sections).
    const tier2SectionEl = document.getElementById("tier2Section");
    const tier2GridEl = document.getElementById("domainSections");
    const tier2LoadMoreEl = document.getElementById("tier2LoadMore");
    const tier2CollapseEl = document.getElementById("tier2Collapse");

    const metricResults = document.getElementById("metricResults");
    const metricAlert = document.getElementById("metricAlert");
    const metricConfidence = document.getElementById("metricConfidence");
    const metricImpact = document.getElementById("metricImpact");

    const ALERT_RANK = { LOW: 1, WATCH: 2, MEDIUM: 3, HIGH: 4 };
    let currentHistoryId = null;
    let currentReviewId = null;
    let currentReportContext = null;
    // DISPLAY-CATEGORY B-1: active domain tab. Holds a raw English domain enum
    // (or the Korean fallback "기타-미분류"), or "전체" for no filter. Replaces
    // the old resultCategory()-based activeCategory tab filter.
    let activeDomain = "전체";
    // HOMEPAGE-TIERED: client-side sort over the ≤50-item ranked pool. No
    // server-side view/engagement counter exists (honest signals only), so the
    // default "뜨는순" is a composite proxy: 위험도(alert) → freshness → 신뢰도
    // → recency tiebreak (stable sort preserves server id-DESC order).
    let activeSort = "뜨는순";
    // DESIGN-C3-2: page-number pagination over the post-hero grid — 12 cards/page
    // (3×4). currentPage is module state (survives a card-open → detail → BACK
    // round-trip with no reload, so BACK returns to the SAME page); it resets to 1
    // only on a genuine domain-tab or sort change, and self-clamps in
    // renderHotTopics when the pool shrinks. Replaces the retired HOMEPAGE-TIERED
    // 더보기/접기 tier machinery (constants + tier1/tier2VisibleCount).
    const PAGE_SIZE = 12;
    let currentPage = 1;
    let activeTopicKey = "";
    let selectedResultIndex = null;
    // HOME-TOP5 S5b: the S5a /api/trending rows, cached by renderTrendingTop5
    // for the 오늘의 한 장 hero pick. null until the fetch lands; stays null on
    // failure (the hero then keeps its pre-S5b two-card behavior).
    let trendingHeroRows = null;
    // SEARCH-ANALYZE S-i (bug b): the last corpus-search results, cached so a
    // BACK from a card opened off the results can re-render them (the card
    // overwrote #results in place). {query, hits} or null.
    let lastSearchHitsCache = null;
    // M45: server-side recent analyses (incl. cron output) used to fill the
    // top hot-topic area when there is no live session search. Populated on
    // load from GET /history; never written to localStorage.
    let serverHotTopicResults = [];
    // STABLE-TABS S2: in-memory (no localStorage) cache of domain-scoped feed
    // results, keyed by the STORED English domain key (e.g. "realestate"). A
    // domain tab click fetches GET /history?domain=<key> once and reuses the
    // cache on re-click. domainLoadingKey / domainFetchErrorKey drive the feed's
    // loading + friendly-error states. Read-only retrieval; display only.
    const domainResultsCache = new Map();
    let domainLoadingKey = null;
    let domainFetchErrorKey = null;
    // HOME-SECTION-FIX A1: per-domain cache for the home 분야별 sections. The
    // sections used to filter the GLOBAL recent-50 pool (allCards), so a domain
    // whose rows are older than that window showed 0–1 cards (보건 showed 1;
    // education, all re-classified from old rows, showed none). Each section now
    // draws its OWN top rows from GET /history?domain=<key> — the same read-only
    // endpoint the tabs use. Display only; no localStorage.
    const domainSectionCache = new Map();
    const domainSectionLoading = new Set();
    // ===== M29-A1 — pin-safe display label maps =====
    const ALERT_LABELS = {
      WATCH: "관찰",
      LOW: "낮음",
      MEDIUM: "중간",
      HIGH: "높음",
    };
    const MARKET_SIGNAL_LABELS = {
      housing_tightening_risk: "주거금융 규제 위험",
      housing_support_pressure: "주거지원 압박",
      consumer_credit_relief: "소비자 금융 완화",
      sme_finance_support: "중소기업 금융지원",
      bank_margin_pressure: "은행 마진 압박",
      policy_uncertainty: "정책 불확실성",
      no_clear_signal: "명확한 신호 없음",
    };
    const LEVEL_LABELS = {
      none: "검증 없음",
      low: "낮음",
      medium: "보통",
      high: "높음",
    };
    const DIRECTION_LABELS = {
      positive: "긍정",
      negative: "부정",
      mixed: "혼합",
      uncertain: "불확실",
    };
    const REVIEW_ACTION_LABELS = {
      unreviewed: "미검토",
      needs_official_check: "공식 확인 필요",
      verified_watch: "관찰로 확인",
      escalated: "상위 검토로 올림",
      dismissed: "제외/종료",
    };
    const RECOMMENDATION_LABELS = {
      "Monitor official FSC/FSS follow-up before treating as confirmed policy.": "공식 금융당국 후속 발표를 확인하기 전까지 확정 정책으로 보지 말고 관찰하세요.",
      "Track official housing finance restrictions and lender implementation guidance.": "공식 주거금융 규제와 금융기관 실행 지침을 추적하세요.",
      "Track youth housing finance support measures and budget changes.": "청년 주거금융 지원 대책과 예산 변화를 추적하세요.",
      "Treat as verified product-level support and monitor borrower adoption.": "검증된 금융상품 지원으로 보고 실제 이용 확산을 확인하세요.",
      "Monitor SME finance support terms and uptake by eligible workers.": "중소기업 금융지원 조건과 대상 근로자의 이용 현황을 확인하세요.",
      "Monitor product-level rate relief and consumer eligibility conditions.": "상품별 금리 감면 내용과 대상 조건을 확인하세요.",
      "Keep on watchlist until usable official evidence is available.": "확인 가능한 공식 근거가 나올 때까지 관찰 목록에 유지하세요.",
      "No immediate action beyond routine monitoring.": "즉각적인 대응보다는 일반 모니터링을 유지하세요.",
    };
    // ===== end pin-safe display label maps =====
    // ===== DISPLAY-CATEGORY B-1 — domain category labels & section sizing =====
    // Korean DISPLAY labels for the 10 backend domain enums. The raw English
    // enum (and the Korean fallback "기타-미분류") stays the comparison key
    // everywhere; this map is used ONLY at render time. Never compare against it.
    const DOMAIN_LABELS_KO = {
      finance: "금융",
      welfare: "복지",
      agriculture: "농업",
      labor: "노동",
      health: "보건",
      environment: "환경",
      SMB: "소상공인",
      realestate: "부동산",
      statistics: "통계",
      education: "교육",
      scitech: "과학기술",
      trade: "산업·통상",
      "기타-미분류": "기타",
    };
    // Canonical tab/section order (matches domain_classifier.LABELS — education
    // joined as the 11th label in DOMAIN-LABEL 2a, then scitech + trade as the
    // 12th/13th in DOMAIN-ADD-SCITECH-TRADE, all before the fallback).
    const DOMAIN_ORDER = [
      "finance", "welfare", "agriculture", "labor", "health",
      "environment", "SMB", "realestate", "statistics", "education",
      "scitech", "trade",
      "기타-미분류",
    ];
    // TAB-ORDER: FIXED category-tab display order — corpus volume DESCENDING (from
    // a one-time GROUP BY: welfare 582 / realestate 468 / agriculture 368 /
    // finance 317 / SMB 170 / environment 157 / labor 104 / health 39 /
    // statistics 14), with 기타-미분류 pinned LAST regardless of its count. This is
    // a HARDCODED display order (not live-recomputed) so tab positions stay stable.
    // SEPARATE from DOMAIN_ORDER (which stays canonical, still driving the per-domain
    // sections + any keyed use) so this reorder touches ONLY the tab render. Every
    // entry is a real user category; the tab CLICK/fetch (English-key) is unchanged.
    // DOMAIN-LABEL 2c: education inserted FIRST per the same volume-descending
    // rule — the 2b re-classify moved 758 rows to education, the largest domain
    // (welfare was 582 at the original GROUP BY). Existing relative order kept.
    // DOMAIN-ADD-SCITECH-TRADE: scitech + trade slotted by the SAME rule using
    // the expansion dry-run's PROJECTED volumes (scitech ~464 -> just under
    // realestate 468; trade ~214 -> between finance 317 and SMB 170). Those are
    // projections from the 미분류 pool, NOT a post-backfill GROUP BY — revisit
    // this order once the backfill has actually run.
    const TAB_ORDER = [
      "education", "welfare", "realestate", "scitech", "agriculture",
      "finance", "trade", "SMB", "environment", "labor", "health",
      "statistics", "기타-미분류",
    ];
    // DESIGN-C3h-2: static per-domain section subtitles (display-only UI copy; no
    // per-card data). Keyed by the raw domain key (note "기타-미분류").
    const DOMAIN_SUBTITLE = {
      realestate: "주택·부동산 정책 뉴스 검증",
      finance: "금융 정책·제도 뉴스 검증",
      welfare: "복지 정책 뉴스 검증",
      labor: "노동·고용 정책 뉴스 검증",
      health: "보건·의료 정책 뉴스 검증",
      environment: "환경·에너지 정책 뉴스 검증",
      SMB: "소상공인 정책 뉴스 검증",
      agriculture: "농업·농촌 정책 뉴스 검증",
      statistics: "공식 통계·지표 검증",
      education: "교육 정책 뉴스 검증",
      scitech: "과학기술·AI 정책 뉴스 검증",
      trade: "산업·통상 정책 뉴스 검증",
      "기타-미분류": "기타 정책 뉴스 검증",
    };
    // Normalize a card's domain to a comparison key. Missing/empty domain falls
    // into the "기타-미분류" bucket so a card is NEVER dropped (removal-free).
    function cardDomainKey(card) {
      const d = card && card.domain;
      return (typeof d === "string" && d) ? d : "기타-미분류";
    }
    function domainDisplayLabel(domainKey) {
      return DOMAIN_LABELS_KO[domainKey] || DOMAIN_LABELS_KO["기타-미분류"];
    }
    // CARD-ICONS: per-domain tint. Byte-identical to web/brainmap.html's
    // DOMAIN_COLORS so the feed icon and the brain-map node share ONE color
    // language (a user learns "자주 = 부동산" once; it reads on both surfaces).
    const DOMAIN_COLORS = {
      finance: "#2b6cb0",
      welfare: "#b7791f",
      agriculture: "#2f855a",
      labor: "#975a16",
      health: "#c53030",
      environment: "#2c7a7b",
      SMB: "#6b46c1",
      realestate: "#b83280",
      statistics: "#4a5568",
      education: "#4c51bf",
      scitech: "#0e7490",
      trade: "#9d174d",
      "기타-미분류": "#98a2b3",
    };
    const DOMAIN_ICON_FALLBACK_COLOR = "#98a2b3";
    // CARD-ICONS: domain key → ascii <symbol> id (avoids unicode in the href).
    // Unknown / "기타-미분류" → dom-etc so a card is NEVER icon-less.
    const DOMAIN_ICON_IDS = {
      finance: "dom-finance",
      welfare: "dom-welfare",
      agriculture: "dom-agriculture",
      labor: "dom-labor",
      health: "dom-health",
      environment: "dom-environment",
      SMB: "dom-smb",
      realestate: "dom-realestate",
      statistics: "dom-statistics",
      education: "dom-education",
      scitech: "dom-scitech",
      trade: "dom-trade",
      "기타-미분류": "dom-etc",
    };
    // CARD-ICONS: the domain line-icon markup for a card. DOMAIN metadata only —
    // never a verdict; the trust badge stays the sole verdict signal. Rides
    // INSIDE .card-domain so hero cards (which hide .card-domain) auto-hide it.
    function domainIconMarkup(domainKey) {
      const symbolId = DOMAIN_ICON_IDS[domainKey] || "dom-etc";
      const color = DOMAIN_COLORS[domainKey] || DOMAIN_ICON_FALLBACK_COLOR;
      return `<svg class="domain-icon" style="color:${color}" aria-hidden="true" focusable="false"><use href="#${symbolId}"/></svg>`;
    }
    // ===== end DISPLAY-CATEGORY B-1 =====
    // Deferred from M29-A1 (pin-sensitive): values contain regression-pinned
    // phrases (검증 완료 / 사람 검토 대기) and VERDICT_LABELS is mutated below.
    // Left in place to avoid byte-risk; revisit in a later pinned-cluster slice.
    const VERDICT_LABELS = {
      draft_verified: "임시 검증 완료",
      draft_likely_true: "사실 가능성 높음",
      draft_unverified: "추가 검증 필요",
      draft_needs_context: "맥락 추가 확인 필요",
      draft_needs_official_confirmation: "공식 출처 확인 필요",
      draft_misleading: "오해 가능성 있음",
      draft_disputed: "상충 근거 확인 필요",
      draft_outdated: "최신 여부 확인 필요",
    };
    const REVIEW_STATUS_LABELS = {
      ai_draft_pending_human_review: "AI 초안, 사람 검토 대기",
    };
    const HUMAN_REVIEWED_LABEL = "사람 검토됨";
    VERDICT_LABELS.draft_needs_review = "사람 검토 대기";
    VERDICT_LABELS.draft_high_risk_review = "고위험 사람 검토 대기";

    // DESIGN-3B-1: DISPLAY-ONLY verdict_label → color map for the card-face
    // verdict dot. Pure presentation — does NOT change verdict_label, the verdict
    // path, or any score. Unknown labels fall back to grey.
    const VERDICT_DOT_COLORS = {
      draft_verified: "var(--verify)",
      draft_likely_true: "var(--verify)",
      draft_needs_context: "var(--orange)",
      draft_needs_official_confirmation: "var(--orange)",
      draft_needs_review: "var(--orange)",
      draft_high_risk_review: "var(--orange)",
      draft_misleading: "var(--red)",
      draft_disputed: "var(--red)",
      draft_unverified: "var(--muted)",
      draft_outdated: "var(--muted)",
    };
    function verdictDotColor(label) {
      return VERDICT_DOT_COLORS[String(label || "")] || "var(--muted)";
    }
    // LABEL-1 STEP 3: verdict-pill tier class, mirroring VERDICT_DOT_COLORS above
    // (same label keys). Returns vt-green / vt-orange / vt-red / vt-muted; default
    // vt-muted. Display-only — does NOT change verdict_label or any score.
    const VERDICT_TIER_CLASSES = {
      draft_verified: "vt-green",
      draft_likely_true: "vt-green",
      draft_needs_context: "vt-orange",
      draft_needs_official_confirmation: "vt-orange",
      draft_needs_review: "vt-orange",
      draft_high_risk_review: "vt-orange",
      draft_misleading: "vt-red",
      draft_disputed: "vt-red",
      draft_unverified: "vt-muted",
      draft_outdated: "vt-muted",
    };
    function verdictTierClass(label) {
      return VERDICT_TIER_CLASSES[String(label || "")] || "vt-muted";
    }
    function verdictLabelKo(label) {
      return VERDICT_LABELS[String(label || "")] || "추가 검증 필요";
    }
    // DESIGN-C3h-3: a card is "verified today" iff its analysis created_at is on or
    // after KST (UTC+9) midnight. This is the EXACT rule the former 오늘의 검증 row
    // used; now the single source of truth for the per-card "오늘 검증" recency badge
    // (a freshness marker — NOT a verdict). Display-only; created_at is in the slim
    // payload already.
    function isTodayCard(card) {
      if (!card || !card.createdAt) return false;
      const kstMidnightUtcMs =
        Math.floor((Date.now() + 9 * 3600e3) / 86400e3) * 86400e3 - 9 * 3600e3;
      return Date.parse(card.createdAt) >= kstMidnightUtcMs;
    }

    // M29-A1: SOURCE_TYPE_LABELS is also pin-safe (kept in place this slice).
    const SOURCE_TYPE_LABELS = {
      official_government: "공식 정부기관",
      public_institution: "공공기관",
      established_news: "언론 기사",
      search_fallback_news: "검색 기반 뉴스",
      unknown: "확인 필요",
    };

    // ===== C2 — Text utilities & sanitizers =====
    function repairMojibake(value) {
      const text = String(value ?? "");
      if (!/[ëìêíÃÂð]/.test(text)) return text;
      try {
        let encoded = "";
        for (const char of text) {
          const code = char.charCodeAt(0);
          if (code > 255) return text;
          encoded += `%${code.toString(16).padStart(2, "0")}`;
        }
        const repaired = decodeURIComponent(encoded);
        const oldMarkers = (text.match(/[ëìêíÃÂð]/g) || []).length;
        const newMarkers = (repaired.match(/[ëìêíÃÂð]/g) || []).length;
        return newMarkers < oldMarkers ? repaired : text;
      } catch (error) {
        return text;
      }
    }

    function sanitizeDisplayText(value) {
      return repairMojibake(value).replaceAll("\uFFFD", "").trim();
    }

    // MOBILE-POLISH F \u2014 DISPLAY-ONLY strip of a leading decorative-marker run
    // (\u25A0 \u25A1 \u25B6 \u25CF \u25C6 and the rest of the Geometric Shapes block, plus \u2605 \u2606 \u2022 \u203B) and
    // any whitespace after it. Some outlets prefix a headline with a bullet glyph
    // that renders as a broken-looking box at the head of a card title. Applied
    // at render time only \u2014 the stored payload is never mutated, and the strip is
    // anchored so a marker INSIDE a title survives. Korean/CJK sits well outside
    // these ranges. Falls back to the unstripped text when a title is nothing but
    // markers, so a headline can never render blank.
    const LEADING_TITLE_MARKER_RE = /^[\s\u2022\u203B\u25A0-\u25FF\u2605\u2606]+/;
    function stripLeadingTitleMarker(value) {
      const text = sanitizeDisplayText(value);
      return text.replace(LEADING_TITLE_MARKER_RE, "") || text;
    }

    const ARTICLE_NOISE_PATTERNS = [
      /이동\s*통신망에서\s*음성\s*재생\s*시\s*데이터\s*요금이\s*발생할\s*수\s*있습니다/gi,
      /음성\s*재생\s*시\s*데이터\s*요금/gi,
      /동영상\s*뉴스|영상으로\s*보기|사진\s*=|무단\s*전재|재배포\s*금지|저작권자/gi,
      /기사제보|카카오톡|네이버에서|구독|좋아요|댓글|공유|이\s*시각\s*추천뉴스|많이\s*본\s*뉴스|관련\s*뉴스|광고|\bAD\b/gi,
      /본문\s*내용과\s*무관한\s*사이트\s*안내문/gi,
    ];
    const ARTICLE_NOISE_SENTENCE_PATTERNS = [
      /이동\s*통신망|음성\s*재생|데이터\s*요금|동영상\s*뉴스|영상으로\s*보기/i,
      /무단\s*전재|재배포\s*금지|저작권자|기사제보|카카오톡|구독|좋아요|댓글|공유/i,
      /이\s*시각\s*추천뉴스|많이\s*본\s*뉴스|관련\s*뉴스|광고|\bAD\b|네이버에서/i,
      /^(입력|수정)\s*\d{4}[.\-년]/,
      /^[가-힣]{2,4}\s*기자$/,
    ];
    const POLICY_SIGNAL_PATTERN = /정부|국토부|금융위|공정위|경찰|검찰|법원|지자체|전세\s*사기|전세사기|사기|임대업자|대출|보증|피해자|지원|정책|법안|규제|단속|수사|기소|판결|징역|과징금|보조금|예산|금리|부동산|임대|임차|청년|금융|은행|세금|국세청|\d+(억|조|만|명|건|%|년|월|일)/;

    function splitArticleSentences(text) {
      return sanitizeDisplayText(text || "")
        .replace(/\s+/g, " ")
        .split(/(?<=[.!?。]|다\.|요\.|니다\.|했다\.|밝혔다\.|나섰다\.|된다\.)\s+|[\n\r]+/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function isArticleBoilerplateSentence(sentence) {
      const text = sanitizeDisplayText(sentence || "");
      if (!text || text.length < 8) return true;
      if (ARTICLE_NOISE_SENTENCE_PATTERNS.some((pattern) => pattern.test(text))) return true;
      if (!POLICY_SIGNAL_PATTERN.test(text) && text.length < 24) return true;
      return false;
    }

    function cleanArticleTextForPolicyAnalysis(text) {
      let cleaned = sanitizeDisplayText(text || "");
      ARTICLE_NOISE_PATTERNS.forEach((pattern) => {
        cleaned = cleaned.replace(pattern, " ");
      });
      const seen = new Set();
      const sentences = splitArticleSentences(cleaned)
        .filter((sentence) => !isArticleBoilerplateSentence(sentence))
        .filter((sentence) => {
          const key = sentence.replace(/\s+/g, " ").trim();
          if (!key || seen.has(key)) return false;
          seen.add(key);
          return true;
        });
      return sentences.join(" ").replace(/\s+/g, " ").trim();
    }

    function stripInternalDiagnosticText(value) {
      let text = cleanArticleTextForPolicyAnalysis(value || "") || sanitizeDisplayText(value || "");
      if (!text) return "";
      const lineReplacements = [
        [/Google\s*RSS[^.!?。]*(?:[.!?。]|$)/gi, "뉴스 검색 결과에서 확보한 기사입니다."],
        [/Google\s*RSS\s*실패\s*시\s*검색\s*HTML\s*fallback으로\s*확보한\s*기사입니다\.?/gi, "뉴스 검색 결과에서 확보한 기사입니다."],
        [/검색\s*HTML\s*fallback으로\s*확보한\s*기사입니다\.?/gi, "뉴스 검색 결과에서 확보한 기사입니다."],
        [/Best official document relevance below threshold[^.!?。]*(?:[.!?。]|$)/gi, "공식 자료와의 직접 일치 여부가 충분히 확인되지 않았습니다."],
        [/relevance below threshold[^.!?。]*(?:[.!?。]|$)/gi, "공식 자료와의 직접 일치 여부가 충분히 확인되지 않았습니다."],
        // DETAIL-CLEANUP A6 — two reason strings that still leaked as raw
        // English on live cards (audit id=1097). Mechanical translation of the
        // fixed backend literals; storage stays English.
        [/Best official document did not pass strengthened weakly_usable checks:?[^.!?。]*(?:[.!?。]|$)/gi, "가장 관련성 높은 공식 문서가 강화된 사용 기준을 통과하지 못했습니다."],
        [/no eligible fetched official body for top evidence/gi, "상세 본문에서 직접 일치를 확인하지 못했습니다"],
        [/insufficient material policy concept overlap/gi, "공식 자료와 기사 핵심 주장 사이의 직접 일치 여부는 추가 확인이 필요합니다."],
        [/insufficient matched query\/material concept overlap/gi, "공식 자료가 기사 핵심 주장과 직접적으로 일치하는지 추가 확인이 필요합니다."],
        [/insufficient matched[^.!?。]*(?:[.!?。]|$)/gi, "공식 자료가 기사 핵심 주장과 직접적으로 일치하는지 추가 확인이 필요합니다."],
        [/matched query\/material concept overlap/gi, "공식 자료와 기사 핵심 주장의 직접 일치 여부"],
        [/query\/material concept overlap/gi, "공식 자료와 기사 핵심 주장의 직접 일치 여부"],
        [/FSC detail press URL(?:-like explanations)?/gi, "금융위원회 보도자료 상세 페이지"],
        [/detail press URL|press URL/gi, "보도자료 상세 페이지"],
        [/\bpress_release\s*:\s*/gi, "보도자료: "],
        [/\bofficial_notice\s*:\s*/gi, "공식 공지: "],
        [/\bofficial_page\s*:\s*/gi, "공식 페이지: "],
        [/\bofficial_search\s*:\s*/gi, "공식 검색 결과: "],
        // UI-1 — leaked English score labels in the low_confidence_match
        // sentence (evidence_comparator.py: "semantic score N점, keyword
        // score N점"). Remap only the English label words; the numeric value
        // and the trailing Korean "점" are left intact.
        [/\bsemantic score\b/gi, "의미 점수"],
        [/\bkeyword score\b/gi, "키워드 점수"],
      ];
      lineReplacements.forEach(([pattern, replacement]) => {
        text = text.replace(pattern, replacement);
      });
      // UI-2 — full-phrase Korean map for the classification_reasons strings
      // emitted by official_document_classifier.py and embedded (untranslated)
      // into comparison_summary → evidence_summary by evidence_comparator.py
      // (_make_summary excluded_non_policy_page / weak_official_match branches).
      // Display-only laundering; the backend literals stay English because they
      // double as should_exclude predicates (official_document_classifier.py
      // :323-327). Ordered longest/most-specific first so a short phrase cannot
      // partial-match inside a longer one. The "insufficient material ..." /
      // "insufficient matched ..." / "FSC detail press URL" reasons are already
      // handled by the UI-1 lineReplacements above and are NOT duplicated here.
      const classificationReasonLabels = [
        [/generic policy title with weak concept\/keyword overlap/gi, "일반적 정책 제목, 핵심 개념/키워드 일치 약함"],
        [/FSC unrelated general finance\/foreign-affairs press release/gi, "금융위 일반 금융/대외 보도자료(주제 무관)"],
        [/classified as usable official evidence candidate/gi, "사용 가능한 공식 근거 후보로 분류됨"],
        [/Gov24 civil-service\/application guide page/gi, "정부24 민원/신청 안내 페이지"],
        [/insufficient core concept or query-token overlap/gi, "핵심 개념 또는 검색어 일치 부족"],
        [/Gov24 policy\/service index page/gi, "정부24 정책/서비스 목록 페이지"],
        [/document text is shorter than 300 characters/gi, "본문이 300자 미만"],
        [/generic list\/index title or URL/gi, "일반 목록/색인 제목 또는 링크"],
        [/no clear policy-document signal/gi, "명확한 정책 문서 신호 없음"],
        [/guide\/FAQ\/minwon signal/gi, "안내/FAQ/민원 신호"],
        [/main\/menu\/index URL/gi, "메인/메뉴/색인 페이지"],
        [/IBK detail\/news URL/gi, "기업은행 상세/뉴스 페이지"],
        [/error\/not-found signal/gi, "오류 또는 페이지 없음 신호"],
        [/attachment-only URL/gi, "첨부파일 전용 링크"],
        [/search page/gi, "검색 페이지"],
      ];
      classificationReasonLabels.forEach(([pattern, replacement]) => {
        text = text.replace(pattern, replacement);
      });
      // UI-2 — document_type enum values leak in TWO forms: the "{type}:" prefix
      // (excluded_non_policy_page branch) and the "유형 {type}" no-colon form
      // (weak_official_match branch). A bare word-boundary replace per enum key
      // covers BOTH ("implementation_plan: …" → "시행 계획: …" and
      // "유형 implementation_plan" → "유형 시행 계획"). UI-1's colon-suffixed
      // press_release: / official_notice: / official_page: / official_search:
      // replacements ran in lineReplacements above and already consumed those
      // colon forms, so these bare maps only catch the still-leaking no-colon
      // occurrences — no regression. Underscore is a word char, so e.g.
      // \bimplementation\b (UI-1 concept key) never matches inside
      // implementation_plan. Longer enum keys first for safety.
      const documentTypeLabels = [
        [/\bservice_index_page\b/g, "서비스 목록 페이지"],
        [/\bmenu_or_index_page\b/g, "메뉴/목록 페이지"],
        [/\bimplementation_plan\b/g, "시행 계획"],
        [/\bnon_policy_page\b/g, "정책 외 페이지"],
        [/\bunrelated_page\b/g, "관련 없는 페이지"],
        [/\battachment_only\b/g, "첨부 전용"],
        [/\bpolicy_release\b/g, "정책 발표"],
        [/\bpress_release\b/g, "보도자료"],
        [/\bofficial_notice\b/g, "공식 고시"],
        [/\bservice_page\b/g, "서비스 페이지"],
        [/\bsearch_page\b/g, "검색 페이지"],
        [/\bfaq_or_guide\b/g, "안내/FAQ"],
        [/\berror_page\b/g, "오류 페이지"],
      ];
      documentTypeLabels.forEach(([pattern, replacement]) => {
        text = text.replace(pattern, replacement);
      });
      // UI-1 — closed-set token→Korean map for the 9 policy-concept keys the
      // backend (evidence_comparator.py CONCEPT_SYNONYMS_* / _make_summary)
      // joins untranslated into the Korean comparison_summary →
      // evidence_summary string. They reach here verbatim (e.g.
      // "매칭 개념은 implementation, regulation입니다"). Word-boundary replace
      // per key so a key cannot partially match inside another word; the keys
      // are independent (none is a substring of another). Display-only — the
      // saved payload and verdict logic are untouched.
      const conceptTokenLabels = [
        [/\brental_loan\b/g, "전세대출"],
        [/\bmortgage_loan\b/g, "주택담보대출"],
        [/\binterest_rate\b/g, "금리"],
        [/\bregulation\b/g, "규제"],
        [/\bsubsidy_support\b/g, "지원"],
        [/\btarget_group\b/g, "지원 대상"],
        [/\bimplementation\b/g, "시행"],
        [/\breview_stage\b/g, "추진 단계"],
        [/\bofficial_statement\b/g, "공식 발표"],
      ];
      conceptTokenLabels.forEach(([pattern, replacement]) => {
        text = text.replace(pattern, replacement);
      });
      const hiddenFragments = [
        /\bfallback\b/gi,
        /\bdebug\b/gi,
        /\bpipeline\b/gi,
        /\braw\b/gi,
        /Best official document relevance/gi,
      ];
      hiddenFragments.forEach((pattern) => {
        text = text.replace(pattern, "");
      });
      return text.replace(/\s{2,}/g, " ").replace(/\s+([,.])/g, "$1").trim();
    }

    function userFacingReportText(value, fallback = "-") {
      const text = stripInternalDiagnosticText(value);
      if (!text || text === "-" || text === "null" || text === "undefined") return fallback;
      return text;
    }

    function publicInstitutionName(value) {
      let text = sanitizeDisplayText(value || "");
      if (!text) return text;
      const replacements = [
        // UI-4 — extend institution map to all 18 catalog source_name values
        // (official_source_search.py). Display-only; source_name stays English
        // in the backend (matching key). These NEW entries are listed BEFORE
        // the original 8 so the more-specific "IBK Industrial Bank of Korea"
        // is matched before the generic "Bank of Korea" substring below.
        // "Current article body" is the news-fallback source title
        // (evidence_extraction_agent.py:459); laundered here at display.
        [/IBK Industrial Bank of Korea/gi, "중소기업은행(IBK)"],
        [/Korea Housing Finance Corporation/gi, "한국주택금융공사"],
        [/National Tax Service/gi, "국세청"],
        [/Korean National Police Agency/gi, "경찰청"],
        [/National Assembly/gi, "국회"],
        [/Local Government/gi, "지방자치단체"],
        [/Fair Trade Commission/gi, "공정거래위원회"],
        [/Ministry of Justice/gi, "법무부"],
        [/Korea Policy Briefing/gi, "정책브리핑"],
        [/Government24/gi, "정부24"],
        [/Current article body/gi, "현재 기사 본문"],
        [/Financial Services Commission/gi, "금융위원회"],
        [/Financial Supervisory Service/gi, "금융감독원"],
        [/Ministry of Land,\s*Infrastructure and Transport/gi, "국토교통부"],
        [/Ministry of Economy and Finance/gi, "기획재정부"],
        [/Ministry of SMEs and Startups/gi, "중소벤처기업부"],
        [/Bank of Korea/gi, "한국은행"],
        [/Korea Housing\s*&\s*Urban Guarantee Corporation/gi, "주택도시보증공사(HUG)"],
        [/\bHUG\b/g, "주택도시보증공사(HUG)"],
        [/Korea Land\s*&\s*Housing Corporation/gi, "한국토지주택공사(LH)"],
        [/\bLH\b/g, "한국토지주택공사(LH)"],
      ];
      replacements.forEach(([pattern, label]) => {
        text = text.replace(pattern, label);
      });
      return text;
    }

    function escapeHtml(value) {
      return sanitizeDisplayText(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    // SEC-2 — URL scheme allowlist for href output. escapeHtml makes a value
    // safe INSIDE a quoted attribute but does NOT neutralize dangerous URL
    // schemes: javascript:/data:/vbscript: contain no chars for escapeHtml to
    // touch and stay live in an href. safeUrl gates the scheme so a scraped or
    // LLM-supplied URL can never smuggle script into an href. Compose as
    // escapeHtml(safeUrl(url)) — safeUrl decides scheme validity on the RAW
    // value, escapeHtml then makes the survivor attribute-quote safe. Never
    // throws; never blocks a legitimate http/https/mailto URL.
    function safeUrl(value) {
      if (typeof value !== "string" || !value) return "#";
      try {
        // Strip ALL ASCII control chars (tab/newline/NUL/etc.) and surrounding
        // whitespace before the scheme test — attackers split a scheme with
        // "java\tscript:" or a leading newline. A legitimate URL contains no
        // control chars, so its content is left unchanged.
        const trimmed = value.replace(/[\x00-\x1F\x7F]/g, "").trim();
        if (!trimmed) return "#";
        const lower = trimmed.toLowerCase();
        if (
          lower.startsWith("http://") ||
          lower.startsWith("https://") ||
          lower.startsWith("mailto:")
        ) {
          return trimmed;
        }
        // Bare fragment, relative path, javascript:/data:/vbscript:/file:,
        // unknown or ambiguous scheme → neutralize to "#".
        return "#";
      } catch (error) {
        return "#";
      }
    }

    // ===== C3 — AI-assist status descriptor & badge =====
    function buildAiStatusDescriptor(rawStatus) {
      const status = String(rawStatus || "").toLowerCase();
      if (status === "ok") {
        return {
          status: "ok",
          label: "AI 보조 활성",
          className: "ai-status-active",
          note: "공식 출처 우선 규칙과 AI 보조 분석이 함께 적용되었습니다.",
        };
      }
      if (status === "error") {
        return {
          status: "error",
          label: "AI 보조 오류 — 규칙 기반 분석만 적용",
          className: "ai-status-error",
          note: "AI 보조 단계에서 오류가 발생해, 이 리포트는 규칙 기반 보수 분석만 사용되었습니다.",
        };
      }
      return {
        status: "unavailable",
        label: "AI 보조 비활성 — 규칙 기반 분석만 적용",
        className: "ai-status-inactive",
        note: "AI 보조 단계가 비활성 상태이므로, 이 리포트는 규칙 기반 보수 분석만 사용되었습니다.",
      };
    }

    function getResultAiStatus(result) {
      if (!result || typeof result !== "object") {
        return { status: "unavailable", reason: "unknown", model: "", available: false };
      }
      return {
        status: result.ai_status || "unavailable",
        reason: result.ai_status_reason || "unknown",
        model: result.ai_model || "",
        available: !!result.ai_available,
      };
    }

    function getReportAiStatus(context, results) {
      const ctx = context || {};
      const ctxAi = ctx.aiStatus || ctx.ai_status;
      if (ctxAi && typeof ctxAi === "object") {
        return {
          status: ctxAi.ai_status || "unavailable",
          reason: ctxAi.ai_status_reason || "unknown",
          model: ctxAi.ai_model || "",
          available: !!ctxAi.ai_available,
        };
      }
      if (Array.isArray(results) && results.length) {
        return getResultAiStatus(results[0]);
      }
      return { status: "unavailable", reason: "unknown", model: "", available: false };
    }

    function renderAiStatusBadge(result) {
      const info = getResultAiStatus(result);
      const desc = buildAiStatusDescriptor(info.status);
      const title = info.reason ? `${desc.label} (${info.reason})` : desc.label;
      // DESIGN-DETAIL-4b FIX 2: detail-only AI-status label — flat muted text (.ai-status-tag)
      // instead of a dark filled pill (.badge). The status class still tints the TEXT
      // (green/grey/amber) but carries no background, matching the home's quiet
      // treatment of technical notes. Same label text.
      return `<span class="ai-status-tag ${desc.className}" title="${escapeHtml(title)}">${escapeHtml(desc.label)}</span>`;
    }

    // ===== C4 — Display formatters =====
    function alertClass(level) {
      const normalized = String(level || "WATCH").toUpperCase();
      if (normalized === "HIGH") return "alert-high";
      if (normalized === "MEDIUM") return "alert-medium";
      if (normalized === "LOW") return "alert-low";
      return "alert-watch";
    }

    function formatSignal(signal) {
      if (Array.isArray(signal)) {
        return signal.map((item) => MARKET_SIGNAL_LABELS[item] || formatTechnicalLabel(item)).join(", ");
      }
      return MARKET_SIGNAL_LABELS[signal] || formatTechnicalLabel(signal);
    }

    function formatAlert(level) {
      const normalized = String(level || "WATCH").toUpperCase();
      const label = ALERT_LABELS[normalized] || normalized;
      return `${label}(${normalized})`;
    }

    function formatLevel(value) {
      const normalized = String(value || "").toLowerCase();
      return LEVEL_LABELS[normalized] || value || "-";
    }

    function formatDirection(value) {
      const normalized = String(value || "").toLowerCase();
      return DIRECTION_LABELS[normalized] || value || "-";
    }

    function formatRecommendation(value) {
      return RECOMMENDATION_LABELS[value] || formatTechnicalLabel(value);
    }

    function formatVerdict(value) {
      return VERDICT_LABELS[value] || formatTechnicalLabel(value);
    }

    function formatReviewStatus(value) {
      return REVIEW_STATUS_LABELS[value] || formatTechnicalLabel(value);
    }

    function formatSourceType(value) {
      return SOURCE_TYPE_LABELS[value] || formatTechnicalLabel(value);
    }

    function formatTechnicalLabel(value) {
      const labels = {
        draft_verified: "공식 근거 확인 필요",
        draft_needs_review: "사람 검토 대기",
        draft_high_risk_review: "고위험 사람 검토 대기",
        draft_needs_official_confirmation: "공식 확인 필요",
        official_detail_missing: "공식 상세 원문 부족",
        official_detail_url_missing: "공식 상세 URL 부족",
        official_detail_not_verified: "공식 상세 자료 미확인",
        official_search_url_candidate: "공식 검색 후보",
        official_search_only: "공식 검색 결과 수준",
        official_page_not_fetchable: "공식 페이지 수집 실패",
        official_body_too_short: "공식 본문 분량 부족",
        official_topic_mismatch: "공식 자료 주제 불일치",
        official_body_mismatch: "공식 본문 불일치 가능성",
        official_body_fetch_failed: "공식 본문 수집 실패",
        official_body_fetched_unmatched: "공식 본문 직접 일치 부족",
        official_body_verified: "공식 본문 직접 근거 확인",
        official_candidate_not_fetched: "공식 후보 문서 미수집",
        official_candidate_metadata_overlap_without_body: "공식 후보 본문 미확인",
        official_candidate_without_body: "공식 후보 본문 미확보",
        official_candidate_only: "공식 후보 단계",
        official_document_excluded: "공식 문서 직접 근거 제외",
        official_detail_body_missing: "공식 상세 본문 부족",
        no_body_text: "본문 없음",
        context_only: "참고 맥락",
        official_reference: "공식 참고자료",
        direct_support: "직접 근거",
        indirect_support: "간접 근거",
        background_context: "배경 맥락",
        contradiction_candidate: "반박 후보",
        insufficient_evidence: "근거 부족",
        no_match: "직접 일치 근거 없음",
        not_enough_info: "정보 부족",
        supports: "지지",
        contradicts: "반박",
        unclear: "불명확",
        primary_evidence: "주요 근거",
        supporting_evidence: "보조 근거",
        not_reliable_enough: "신뢰도 부족",
        primary_source: "1차 출처",
        support: "보조 근거",
        contradiction: "반박 확인",
        fact_check: "팩트체크",
        update: "업데이트",
        news_context: "뉴스 맥락",
        current_news_collection: "뉴스 수집 결과",
        article_body_sentence_overlap: "기사 본문 문장 일치",
        official_candidate_without_body_match: "공식 후보 본문 매칭 미확인",
        public_policy: "공공정책",
        consumer_finance: "소비자 금융",
        household_finance: "가계 금융",
        housing: "주거",
        real_estate: "부동산",
        banking: "은행권",
        SME_finance: "중소기업 금융",
        capital_market: "자본시장",
        social: "사회",
        banks: "은행",
        young_adults: "청년층",
        renters: "임차인",
        homeowners: "주택 보유자",
        small_business_workers: "중소기업 근로자",
        SMEs: "중소기업",
        public_financial_institutions: "정책금융기관",
        investors: "투자자",
        general_consumers: "일반 소비자",
        loaded_terms: "수집된 핵심어",
        "loaded terms": "추출된 핵심 용어",
        source_retrieval: "출처 탐색",
        "source retrieval": "출처 탐색",
        evidence_matching: "근거 매칭",
        "evidence matching": "근거 매칭",
        claim_extraction: "주장 추출",
        "claim extraction": "주장 추출",
        claim_normalization: "주장 정규화",
        "claim normalization": "주장 정규화",
        bias_framing: "프레이밍/편향 검사",
        "bias/framing": "프레이밍/편향 검사",
        human_review: "사람 검토",
        "human review": "사람 검토",
        strict_staff_needs_review: "사람 검토 필요",
        needs_context: "맥락 추가 확인 필요",
        draft_needs_context: "맥락 추가 확인 필요",
        needs_review: "사람 검토 필요",
        ok: "정상",
        partial: "일부 확인",
        missing: "없음",
        needed: "필요",
        "not needed": "불필요",
        none: "없음",
        low: "낮음",
        medium: "보통",
        high: "높음",
        strong: "강함",
        weak: "약함",
        very_high: "매우 높음",
        unknown: "확인 필요",
        unknown_publisher: "발행처 확인 필요",
        null: "자료 부족",
        undefined: "자료 부족",
      };
      if (Array.isArray(value)) {
        return value.map((item) => formatTechnicalLabel(item)).join(", ");
      }
      const text = String(value ?? "").trim();
      if (!text || text === "null" || text === "undefined") return "자료 부족";
      return labels[text] || (text.includes("_") ? "추가 진단 정보" : text) || "자료 부족";
    }

    function formatDiagnosticText(value) {
      let text = userFacingReportText(value ?? "", "-");
      if (!text || text === "-" || text === "null" || text === "undefined") return "-";
      const replacements = {
        official_search_url_candidate: "공식 검색 후보",
        official_detail_missing: "상세 공식문서 미확인",
        official_detail_url_missing: "공식 상세 URL 부족",
        official_detail_not_verified: "공식 상세 자료 미확인",
        official_body_fetched_unmatched: "공식 본문 직접 일치 부족",
        official_candidate_metadata_overlap_without_body: "공식 후보 메타데이터만 일부 일치",
        // UI-4 — extraction_method enums that leaked as raw English in the
        // public "추출 방식" field (renderEvidenceSnippets). Backend literals
        // stay English (scoring predicates at evidence_extraction_agent.py
        // :239-264); this is display-only. article_body_sentence_overlap is
        // kept identical to the formatTechnicalLabel mapping for consistency.
        article_body_sentence_overlap: "기사 본문 문장 일치",
        official_body_sentence_overlap: "공식 본문 문장 일치",
        news_fallback_metadata_overlap: "뉴스 보조 메타데이터 일치",
        no_relevant_sentence_found: "관련 문장 없음",
        official_candidate_not_fetched: "공식 후보 문서 미수집",
        official_candidate_without_body: "공식 후보 본문 미확보",
        official_body_mismatch: "공식 본문 불일치 가능성",
        official_topic_mismatch: "공식 자료 주제 불일치",
        official_document_excluded: "공식 문서 직접 근거 제외",
        official_search_only: "공식 검색 결과 수준",
        no_body_text: "본문 없음",
        current_news_collection: "뉴스 수집 결과",
        context_only: "참고 맥락",
        loaded_terms: "추출된 핵심 용어",
        "loaded terms": "추출된 핵심 용어",
        no_match: "직접 일치 없음",
        strict_staff_needs_review: "사람 검토 필요",
        needs_context: "맥락 추가 확인 필요",
        source_retrieval: "출처 탐색",
        "source retrieval": "출처 탐색",
        bias_framing: "프레이밍/편향 검사",
        "bias framing": "프레이밍/편향 검사",
        contradiction: "반박/모순 검사",
        official_body_verified: "공식 본문 직접 근거 확인",
        official_reference: "공식 참고자료",
        direct_support: "직접 근거",
        indirect_support: "간접 근거",
        background_context: "배경 맥락",
        not_enough_info: "정보 부족",
        unknown_publisher: "발행처 확인 필요",
      };
      Object.entries(replacements).forEach(([raw, label]) => {
        text = text.replaceAll(raw, label);
      });
      text = text.replace(/\bofficial body fetched but direct claim match is insufficient\b/gi, "공식 본문은 수집됐지만 핵심 주장과 직접 일치가 부족합니다");
      text = text.replace(/\bofficial body direct match\b/gi, "공식 본문 직접 근거 확인");
      text = text.replace(/\bofficial body text unavailable\b/gi, "공식 본문을 확인하지 못했습니다");
      text = text.replace(/\bofficial detail evidence missing or mismatched\b/gi, "공식 상세 근거가 부족하거나 기사 주제와 직접 맞지 않습니다");
      text = text.replace(/\bNo official document candidate links found on search page\.?/gi, "공식 검색 결과에서 사용할 수 있는 상세 문서 링크를 찾지 못했습니다");
      text = text.replace(/\bofficial document topic mismatch:[^,;]+/gi, "공식 문서가 기사 핵심 주제와 직접 맞지 않습니다");
      text = text.replace(/\bConnection aborted\.?/gi, "공식 사이트 연결이 중단됐습니다");
      text = text.replace(/\bConnectionResetError\b/gi, "연결 초기화 오류");
      text = text.replace(/\b404 Client Error: Not Found for url:\s*\S+/gi, "공식 페이지를 찾을 수 없습니다");
      text = text.replace(/\b\d{3} Client Error:[^,;]+/gi, "공식 페이지 요청에 실패했습니다");
      text = text.replace(/\('공식 사이트 연결이 중단됐습니다'[^)]*\)/g, "공식 사이트 연결이 중단됐습니다");
      text = text.replace(/연결 초기화 오류\([^)]*\)/g, "연결 초기화 오류");
      text = text.replace(/공식 사이트 연결이 중단됐습니다\)+/g, "공식 사이트 연결이 중단됐습니다");
      text = text.replace(/\bNone\b/g, "");
      text = text.replace(/\bsource retrieval\b/gi, "출처 탐색");
      text = text.replace(/\bbias\/framing\b/gi, "프레이밍/편향 검사");
      text = text.replace(/\bfresh news fetch\b/gi, "새 뉴스 수집");
      text = text.replace(/\bnews cache hit\b/gi, "뉴스 캐시 사용");
      text = text.replace(/\bfresh analysis\b/gi, "새 분석 실행");
      text = text.replace(/\banalysis cache hit\b/gi, "분석 캐시 사용");
      text = text.replace(/\bterms=(\d+)/gi, "핵심어 $1개");
      text = text.replace(/\bconcepts=(\d+)/gi, "개념 $1개");
      text = text.replace(/\bnumbers=(\d+)/gi, "수치 $1개");
      text = text.replace(/\bstrong\b/gi, "강함");
      text = text.replace(/\bmedium\b/gi, "보통");
      text = text.replace(/\bweak\b/gi, "약함");
      text = text.replace(/\bok\b/gi, "정상");
      text = text.replace(/\bpartial\b/gi, "일부 확인");
      text = text.replace(/\bmissing\b/gi, "없음");
      text = text.replace(/\bno body text\b/gi, "본문 없음");
      text = text.replace(/\bcandidate only\b/gi, "후보 단계");
      text = text.replace(/\bpossible redirect\b/gi, "리다이렉트 가능성");
      return text;
    }

    // DETAIL-CLEANUP A6: raw ISO datetimes ("2026-07-05T13:20:24.917223+00:00")
    // leaked into 확인 시각 / 마지막 확인 — show just the date. Display-only;
    // non-ISO values pass through untouched.
    function formatDisplayDate(value) {
      const m = String(value ?? "").match(/^(\d{4}-\d{2}-\d{2})(?:[T ]|$)/);
      return m ? m[1] : value;
    }

    function formatReasonCounts(counts) {
      if (!counts || typeof counts !== "object") return "{}";
      const entries = Object.entries(counts);
      if (!entries.length) return "없음";
      return entries.map(([key, value]) => `${formatDiagnosticText(key)} ${value}`).join(", ");
    }

    function formatReadableValue(value) {
      const label = formatTechnicalLabel(value);
      return label === "추가 진단 정보" ? formatDiagnosticText(value) : label;
    }

    function formatList(value) {
      if (Array.isArray(value)) {
        return value.length ? value.map((item) => formatReadableValue(item)).join(", ") : "-";
      }
      return formatReadableValue(value);
    }

    function formatEvidenceSummaryLabel(summary) {
      const data = summary || {};
      return `강함 ${data.strong ?? 0}, 보통 ${data.medium ?? 0}, 약함 ${data.weak ?? 0}`;
    }

    // ===== C5 — Pipeline-section renderers =====
    // DESIGN-DETAIL-5a: shared advanced presentation primitive. The advanced
    // sub-sections used repeat(4,1fr) tile grids full of "-" cells; this renders a
    // compact key:value definition list and HIDES empty cells. A value that is
    // null/undefined/blank/"-"/"—" is the ABSENCE of a datum, so dropping its row
    // loses no data; any cell WITH a value is always shown. If every pair is empty,
    // one muted "정보 없음" line. Pairs: [label, value, opts?]; opts.html === true
    // means value is trusted HTML (e.g. an <a> link) and is not re-escaped.
    // Callers whose formatter emits a Korean placeholder for empty input
    // (e.g. formatClaimStatus(undefined) → "자료 부족") guard at the call site with
    // `raw ? format(raw) : ""` so the placeholder never becomes a kept row.
    function advIsEmptyDisplay(value) {
      if (value === null || value === undefined) return true;
      const s = String(value).trim();
      return s === "" || s === "-" || s === "—";
    }
    function advDefList(pairs) {
      const rows = (Array.isArray(pairs) ? pairs : []).filter((pair) => pair && !advIsEmptyDisplay(pair[1]));
      if (!rows.length) {
        return '<div class="adv-empty">정보 없음</div>';
      }
      // DESIGN-DETAIL-5c: label ABOVE value, both visible (the readable #6 pattern,
      // adopted for all 8 sub-sections). Replaces the 5a <dl><dt>/<dd> grid whose
      // label column collapsed so only values showed.
      return `<div class="adv-deflist">${rows.map(([label, value, opts]) => `
        <div class="adv-cell">
          <span class="adv-cell-label">${escapeHtml(label)}</span>
          <span class="adv-cell-value">${opts && opts.html ? value : escapeHtml(value)}</span>
        </div>
      `).join("")}</div>`;
    }

    function renderEvidenceSources(sources) {
      const list = Array.isArray(sources) ? sources : [];
      if (!list.length) {
        return '<div class="evidence-source-meta">표시할 근거 출처가 없습니다.</div>';
      }
      return `
        <div class="source-list">
          ${list.map((source) => {
            const sourceTitle = escapeHtml(userFacingReportText(source.title || source.url || "근거 출처", "근거 출처"));
            const sourceUrl = escapeHtml(safeUrl(source.url || ""));
            const sourceType = escapeHtml(formatSourceType(source.source_type));
            const score = escapeHtml(source.reliability_score ?? "-");
            const reason = escapeHtml(userFacingReportText(source.reliability_reason || "", ""));
            const titleHtml = source.url
              ? `<a href="${sourceUrl}" target="_blank" rel="noopener noreferrer">${sourceTitle}</a>`
              : sourceTitle;
            return `
              <div class="evidence-source">
                <div class="evidence-source-title">${titleHtml}</div>
                <!-- SCORE-CLARITY FIX C: this IS the genuine 0-5 source grade, so
                     "/5" stays — but the bare "신뢰도" collided with the claim's
                     0-100 근거 수준 shown above on the same screen. "출처 신뢰도"
                     names whose reliability this is: the SOURCE's, not the claim's. -->
                <div class="evidence-source-meta">출처 유형: ${sourceType} · 출처 신뢰도: ${score}/5</div>
                <div class="evidence-source-meta">${reason || "출처의 세부 관련성은 추가 확인이 필요합니다."}</div>
              </div>
            `;
          }).join("")}
        </div>
      `;
    }

    function renderClaimList(claims) {
      const list = (Array.isArray(claims) ? claims : [])
        .map((claim) => limitClaimSentences(cleanArticleTextForPolicyAnalysis(claim) || "", 2, CLAIM_MAX_CHARS))
        .filter(Boolean);
      if (!list.length) {
        return '<div class="evidence-source-meta">표시할 핵심 주장 목록이 없습니다.</div>';
      }

      return `<ol class="claim-list">${list.map((claim) => `
        <li>${escapeHtml(claim)}</li>
      `).join("")}</ol>`;
    }

    function formatClaimStatus(value) {
      const labels = {
        proposed: "제안",
        under_review: "검토 중",
        announced: "발표됨",
        implemented: "시행됨",
        denied: "부인됨",
        uncertain: "불확실",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function formatClaimType(value) {
      const labels = {
        policy_action: "정책 조치",
        financial_condition: "금융 조건",
        eligibility: "대상/자격",
        numerical_claim: "수치 주장",
        timeline_claim: "시점 주장",
        market_impact: "시장 영향",
        expert_opinion: "전문가 의견",
        unknown: "미분류",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function formatUncertainty(value) {
      const labels = {
        low: "낮음",
        medium: "보통",
        high: "높음",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function renderNormalizedClaims(normalizedClaims) {
      const list = Array.isArray(normalizedClaims) ? normalizedClaims : [];
      if (!list.length) {
        return '<div class="evidence-source-meta">표시할 정규화된 주장이 없습니다.</div>';
      }

      // DESIGN-DETAIL-5a: each normalized claim is a populated-only definition list.
      // Empty 주체/행동/대상/수치/시점/지역/객체 cells (raw "-"/blank) drop out; the
      // formatter fields (상태/주장 유형/불확실성) are guarded so an empty raw value
      // is NOT rendered as a "자료 부족" placeholder row. Every populated field stays.
      // DETAIL-CLEANUP A6: the extractor stores the literal "unknown" for cells it
      // could not fill — display it as 미상 (stored value untouched).
      const cell = (value) => (String(value ?? "").trim().toLowerCase() === "unknown" ? "미상" : value);
      return `<div class="normalized-claims">${list.map((claim) => `
        <div class="normalized-claim">
          <div class="normalized-claim-text">${escapeHtml(limitClaimSentences(cleanArticleTextForPolicyAnalysis(claim.claim_text) || "정책 주장 확인 필요", 2, CLAIM_MAX_CHARS))}</div>
          ${advDefList([
            ["주체", cell(claim.actor)],
            ["행동", cell(claim.action)],
            ["대상", cell(claim.target)],
            ["수치", cell(claim.quantity)],
            ["시점", cell(claim.date_or_time)],
            ["지역", cell(claim.location)],
            ["상태", claim.status ? formatClaimStatus(claim.status) : ""],
            ["주장 유형", claim.claim_type ? formatClaimType(claim.claim_type) : ""],
            ["불확실성", claim.uncertainty_level ? formatUncertainty(claim.uncertainty_level) : ""],
            ["객체", cell(claim.object)],
          ])}
        </div>
      `).join("")}</div>`;
    }

    function formatSourcePurpose(value) {
      const labels = {
        primary_source: "1차 공식 근거",
        support: "보강 근거",
        contradiction: "반박 확인",
        fact_check: "팩트체크",
        update: "후속 업데이트",
        news_context: "뉴스 맥락",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function formatReliabilityLevel(value) {
      const labels = {
        very_high: "매우 높음",
        high: "높음",
        medium: "보통",
        low: "낮음",
        unknown: "알 수 없음",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function formatVerificationRole(value) {
      const labels = {
        primary_evidence: "핵심 근거",
        supporting_evidence: "보조 근거",
        context_only: "맥락 참고",
        contradiction_check: "반박 확인",
        not_reliable_enough: "신뢰 부족",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function renderSourceReliabilitySummary(summary) {
      const data = summary || {};
      const topTitle = userFacingReportText(publicInstitutionName(data.top_source_title || "-"), "-");
      const topUrl = data.top_source_url || "";
      const mismatchReasons = Array.isArray(data.official_mismatch_reasons)
        ? formatDiagnosticText(formatList(data.official_mismatch_reasons))
        : "";
      const topHtml = topUrl
        ? `<a href="${escapeHtml(safeUrl(topUrl))}" target="_blank" rel="noopener noreferrer">${escapeHtml(topTitle)}</a>`
        : escapeHtml(topTitle);
      // DESIGN-DETAIL-5c: unified to the shared label+value deflist (counts incl. 0 kept).
      return `
        ${advDefList([
          ["최고 신뢰 출처", topHtml, { html: true }],
          ["공식 출처 후보 수", data.official_candidate_count ?? 0],
          ["본문 확보 출처", data.raw_text_available_count ?? 0],
          ["평균 출처 신뢰도", data.average_reliability_score ?? 0],
        ])}
        ${data.official_mismatch ? `<div class="evidence-source-meta">공식 출처 불일치/제외: ${escapeHtml(mismatchReasons || "공식 상세 근거 부족")}</div>` : ""}
      `;
    }

    function renderSourceQueries(sourceQueries) {
      // DISPLAY-HONESTY (②): the generated search queries (source_queries[].query, e.g.
      // "site:molit.go.kr OR site:moef.go.kr ...") are INTERNAL system queries, not
      // user-facing evidence or claim text, so they are no longer rendered. Display-only:
      // source_queries is still stored/persisted unchanged — only this sub-section stops
      // surfacing it. The enclosing "출처와 공식 근거" section keeps its reliability summary
      // and candidate list. formatSourcePurpose stays defined (used elsewhere).
      void sourceQueries;
      return "";
    }

    function formatEvidenceType(value) {
      const labels = {
        direct_support: "직접 근거",
        indirect_support: "간접 근거",
        background_context: "배경 맥락",
        contradiction_candidate: "반박 후보",
        official_reference: "공식 출처 참조",
        insufficient_evidence: "근거 부족",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function formatSupportsClaim(value) {
      const labels = {
        supports: "지지",
        contradicts: "반박",
        unclear: "불명확",
        not_enough_info: "정보 부족",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function formatExtractionConfidence(value) {
      const labels = {
        high: "높음",
        medium: "보통",
        low: "낮음",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function renderEvidenceExtractionSummary(summary) {
      const data = summary || {};
      const quality = data.evidence_quality_summary || {};
      // DESIGN-DETAIL-5a: 8 count tiles → definition list (counts incl. 0 kept).
      const cells = [
        ["근거 문장 수", data.evidence_snippet_count ?? 0],
        ["직접 근거 수", data.direct_support_count ?? 0],
        ["공식 참조 수", data.official_reference_count ?? 0],
        ["근거 부족 수", data.insufficient_evidence_count ?? 0],
        ["품질 strong", data.total_strong_evidence ?? quality.strong ?? 0],
        ["품질 medium", data.total_medium_evidence ?? quality.medium ?? 0],
        ["품질 weak", data.total_weak_evidence ?? quality.weak ?? 0],
        ["평균 품질", data.average_evidence_quality_score ?? quality.average_evidence_quality_score ?? 0],
      ];
      // DESIGN-DETAIL-5i FIX 2: if EVERY numeric field is 0 (no evidence collected at
      // all), collapse the eight 0-cells to one honest muted line. If ANY value is
      // non-zero, show the full numbers exactly as before (individual 0s among
      // non-zeros still show) — no real data is hidden, only the all-zero case folds.
      const allZero = cells.every(([, value]) => Number(value) === 0);
      if (allZero) {
        return '<div class="adv-empty">수집된 근거 문장이 없습니다</div>';
      }
      return advDefList(cells);
    }

    function formatContradictionStatus(value) {
      const labels = {
        no_contradiction: "반박 근거 없음",
        no_contradiction_found: "반박 신호 없음",
        insufficient_contradiction_evidence: "반박 판단 근거 부족",
        possible_contradiction: "반박 가능성",
        confirmed_contradiction: "확인된 모순",
        likely_contradiction: "강한 모순 가능성",
        insufficient_evidence: "근거 부족",
        needs_official_confirmation: "공식 확인 필요",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function formatContradictionRisk(value) {
      const labels = {
        high: "높음",
        medium: "보통",
        watch: "관찰",
        low: "낮음",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function renderContradictionSummary(summary) {
      const data = summary || {};
      const possibleCount = Number(data.possible_contradiction_count || 0)
        + Number(data.confirmed_contradiction_count || data.likely_contradiction_count || 0);
      // DESIGN-DETAIL-5a: counts (incl. 0) are values and stay; only an empty
      // 판정 근거/위험도 collapses out.
      return advDefList([
        ["검사한 주장", data.total_claims_checked ?? 0],
        ["반박 가능성", possibleCount],
        ["공식 확인 필요", data.needs_official_confirmation_count ?? 0],
        ["전체 모순 위험도", data.overall_contradiction_risk ? formatContradictionRisk(data.overall_contradiction_risk) : ""],
        ["후보/매칭", `${data.contradiction_candidates_searched ?? 0} / ${data.contradiction_candidates_matched ?? 0}`],
        ["판정 근거", formatDiagnosticText(data.contradiction_verdict_source || "-")],
      ]);
    }

    function renderContradictionChecks(claims, contradictionChecks) {
      const claimList = Array.isArray(claims) ? claims : [];
      const checks = Array.isArray(contradictionChecks) ? contradictionChecks : [];
      if (!checks.length) {
        return '<div class="evidence-source-meta">표시할 반박/모순 검사 결과가 없습니다.</div>';
      }

      // DESIGN-DETAIL-5a: collapse each claim to a one-line "claim #n — 검사 상태".
      // Expand to the full populated-only detail ONLY when there is an actual
      // 모순 점수, a conflict, or a missing-evidence warning — the common
      // "반박 근거 없음" rows stay one line. No data dropped: a claim with detail
      // shows all of it; a claim without shows the status it has.
      return `<div class="evidence-snippet-list">${claimList.map((claim, index) => {
        const check = checks.find((item) => Number(item.claim_index) === index) || {};
        const conflicts = Array.isArray(check.conflicting_evidence) ? check.conflicting_evidence : [];
        const statusLabel = formatContradictionStatus(check.contradiction_status);
        const hasScore = !advIsEmptyDisplay(check.contradiction_score);
        const expand = hasScore || conflicts.length > 0 || !!check.missing_evidence_warning;
        const headLine = `<div class="adv-claim-line">claim #${index + 1} — ${escapeHtml(statusLabel)}</div>`;
        if (!expand) {
          return `<div class="adv-check-item">${headLine}</div>`;
        }
        const claimText = escapeHtml(limitClaimSentences(cleanArticleTextForPolicyAnalysis(claim || check.claim_text) || "정책 주장 확인 필요", 2, CLAIM_MAX_CHARS));
        const detail = advDefList([
          ["모순 점수", check.contradiction_score],
          ["사람 검토", check.needs_human_review ? "필요" : "불필요"],
          ["확인 시각", formatDisplayDate(check.checked_at)],
        ]);
        const reason = advIsEmptyDisplay(check.contradiction_reason)
          ? ""
          : `<div class="evidence-snippet-text">${escapeHtml(formatDiagnosticText(check.contradiction_reason))}</div>`;
        const warning = check.missing_evidence_warning
          ? `<div class="evidence-source-meta">${escapeHtml(formatDiagnosticText(check.missing_evidence_warning))}</div>`
          : "";
        const conflictBlock = conflicts.length ? `
          <div class="source-list">
            ${conflicts.map((conflict) => {
              const conflictUrl = escapeHtml(safeUrl(conflict.source_url || ""));
              const conflictTitle = escapeHtml(userFacingReportText(conflict.source_title || "충돌 후보", "충돌 후보"));
              const conflictHtml = conflict.source_url
                ? `<a href="${conflictUrl}" target="_blank" rel="noopener noreferrer">${conflictTitle}</a>`
                : conflictTitle;
              return `
                <div class="evidence-source">
                  <div class="evidence-source-title">${conflictHtml}</div>
                  ${advDefList([
                    ["유형", conflict.conflict_type ? formatTechnicalLabel(conflict.conflict_type) : ""],
                    ["신뢰도", conflict.confidence ? formatTechnicalLabel(conflict.confidence) : ""],
                  ])}
                  <div class="evidence-snippet-text">${escapeHtml(conflict.evidence_text || "-")}</div>
                </div>
              `;
            }).join("")}
          </div>
        ` : "";
        return `
          <div class="evidence-snippet">
            ${headLine}
            <div class="normalized-claim-text">${claimText}</div>
            ${detail}${reason}${warning}${conflictBlock}
          </div>
        `;
      }).join("")}</div>`;
    }

    function formatFramingLevel(value) {
      const labels = {
        low: "낮음",
        medium: "보통",
        high: "높음",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function formatBiasDirection(value) {
      const labels = {
        neutral: "중립",
        pro_policy: "정책 우호",
        anti_policy: "정책 비판",
        pro_market: "시장 우호",
        anti_market: "시장 비판",
        pro_government: "정부 우호",
        anti_government: "정부 비판",
        unclear: "불명확",
      };
      return labels[value] || formatTechnicalLabel(value);
    }

    function renderBiasFramingSummary(summary) {
      const data = summary || {};
      // DESIGN-DETAIL-5a: counts (incl. 0) kept as a definition list.
      return advDefList([
        ["프레이밍 위험 수", data.high_framing_count ?? 0],
        ["감정 표현 수", data.emotional_language_count ?? 0],
        ["불확실 표현 수", data.uncertainty_language_count ?? 0],
        ["편집자 검토 필요", Number(data.editor_review_needed_count || 0) > 0 ? "필요" : "낮음"],
      ]);
    }

    function renderBiasFramingAnalysis(claims, biasFramingAnalysis) {
      const claimList = Array.isArray(claims) ? claims : [];
      const analyses = Array.isArray(biasFramingAnalysis) ? biasFramingAnalysis : [];
      if (!analyses.length) {
        return '<div class="evidence-source-meta">표시할 프레이밍/편향 검사 결과가 없습니다.</div>';
      }

      // DESIGN-DETAIL-5a: collapse each claim to a one-line "프레이밍 수준 · 편향 방향".
      // Expand to the full populated-only detail ONLY when framing > 낮음 OR any flag/
      // term exists (감정/자극/불확실 표현, 핵심 용어, 편집자 검토). Low-signal claims
      // stay one line. No data dropped — populated fields all show when expanded.
      return `<div class="evidence-snippet-list">${claimList.map((claim, index) => {
        const analysis = analyses.find((item) => Number(item.claim_index) === index) || {};
        const elevated = ["medium", "high"].includes(String(analysis.framing_level || "").toLowerCase());
        const sensational = formatList(analysis.sensational_phrases);
        const uncertainty = formatList(analysis.uncertainty_language);
        const loaded = formatList(analysis.loaded_terms);
        const hasFlags = !advIsEmptyDisplay(sensational) || !advIsEmptyDisplay(uncertainty)
          || !advIsEmptyDisplay(loaded) || !!analysis.emotional_language_detected || !!analysis.needs_editor_review;
        const framingLabel = analysis.framing_level ? formatFramingLevel(analysis.framing_level) : "낮음";
        const biasLabel = analysis.bias_direction ? formatBiasDirection(analysis.bias_direction) : "중립";
        const headLine = `<div class="adv-claim-line">claim #${index + 1} — 프레이밍 ${escapeHtml(framingLabel)} · 편향 ${escapeHtml(biasLabel)}</div>`;
        if (!elevated && !hasFlags) {
          return `<div class="adv-check-item">${headLine}</div>`;
        }
        const claimText = escapeHtml(limitClaimSentences(cleanArticleTextForPolicyAnalysis(claim || analysis.claim_text) || "정책 주장 확인 필요", 2, CLAIM_MAX_CHARS));
        const detail = advDefList([
          ["프레이밍 수준", analysis.framing_level ? formatFramingLevel(analysis.framing_level) : ""],
          ["프레이밍 점수", analysis.framing_score],
          ["편향 방향", analysis.bias_direction ? formatBiasDirection(analysis.bias_direction) : ""],
          ["편집자 검토", analysis.needs_editor_review ? "필요" : "낮음"],
          ["감정 표현", analysis.emotional_language_detected ? "감지됨" : "낮음"],
          ["자극 표현", sensational],
          ["불확실 표현", uncertainty],
          ["추출된 핵심 용어", loaded],
        ]);
        const reason = advIsEmptyDisplay(analysis.framing_reason)
          ? ""
          : `<div class="evidence-snippet-text">${escapeHtml(formatDiagnosticText(analysis.framing_reason))}</div>`;
        return `
          <div class="evidence-snippet">
            ${headLine}
            <div class="normalized-claim-text">${claimText}</div>
            ${detail}${reason}
          </div>
        `;
      }).join("")}</div>`;
    }

    function debugState(ok, count) {
      if (ok) return { label: "정상", className: "debug-ok" };
      if (Number(count || 0) > 0) return { label: "일부 확인", className: "debug-partial" };
      return { label: "없음", className: "debug-missing" };
    }


    function renderPipelineDebugSummary(summary) {
      const data = summary || {};
      const strength = data.evidence_strength_summary || {};
      const quality = data.evidence_quality_summary || {};
      const newsCacheState = data.news_cache_hit ? "news cache hit" : "fresh news fetch";
      const analysisCacheState = data.analysis_cache_hit ? "analysis cache hit" : "fresh analysis";
      const missing = Array.isArray(data.missing_steps) && data.missing_steps.length
        ? data.missing_steps.join(", ")
        : "없음";
      const zeroReasons = Array.isArray(data.evidence_zero_reasons) && data.evidence_zero_reasons.length
        ? data.evidence_zero_reasons.join(", ")
        : "없음";
      // DESIGN-DETAIL-5a: the off-brand debug box is restyled to the C3 definition-
      // list language. Step status is small TEXT (.adv-status, tinted ok/partial/
      // missing) — not pills. Each former metric tile's sub-meta is folded into its
      // value with " · " so every field is kept. The trailing diagnostic run-on
      // becomes a labeled definition list (each 사유 on its own row, empties hidden).
      const stepRow = (label, ok, count) => {
        const state = debugState(ok, count);
        return [label, `<span class="adv-status ${state.className}">${escapeHtml(state.label)}</span> · 개수 ${escapeHtml(count ?? "-")}`, { html: true }];
      };
      return `
        <div class="pipeline-debug">
          <!-- DETAIL-CLEANUP A6: the inner title + note are REMOVED — the wrapping
               collapsible (renderCollapsibleSection) already renders the same
               heading in its <summary> and the same helper reader-note, so they
               displayed twice. -->
          ${advDefList([
            stepRow("입력 수집", data.intake_ok, data.intake_ok ? 1 : 0),
            stepRow("주장 추출", data.claim_extraction_ok, data.claims_count),
            stepRow("주장 정규화", data.claim_normalization_ok, data.normalized_claims_count),
            stepRow("출처 탐색", data.source_retrieval_ok, data.evidence_candidates_count),
            stepRow("근거 매칭", data.evidence_matching_ok, data.matched_evidence_count ?? data.direct_evidence_count),
            stepRow("반박/모순 검사", data.contradiction_check_ok, data.contradiction_checks_count),
            stepRow("프레이밍/편향 검사", data.bias_framing_ok, data.framing_flags_count),
            ["사람 검토", `<span class="adv-status ${data.needs_human_review ? "debug-partial" : "debug-ok"}">${data.needs_human_review ? "필요" : "불필요"}</span> · 초안 상태: ${escapeHtml(formatVerdict(data.overall_verdict || "-"))}`, { html: true }],
          ])}
          ${advDefList([
            ["출처 구성", `공식 ${data.official_sources_count ?? 0} · 뉴스 ${data.news_sources_count ?? 0}`],
            ["근거 강도", `강함 ${strength.strong ?? 0} · 보통 ${strength.medium ?? 0} · 약함 ${strength.weak ?? 0}`],
            ["근거 품질", `강함 ${quality.strong ?? data.total_strong_evidence ?? 0} · 보통 ${quality.medium ?? data.total_medium_evidence ?? 0} · 약함 ${quality.weak ?? data.total_weak_evidence ?? 0} · 평균 ${quality.average_evidence_quality_score ?? data.average_evidence_quality_score ?? 0}`],
            ["공식 본문 확인", `후보 ${data.official_body_candidates ?? 0} · 수집 ${data.official_bodies_fetched ?? 0} · 직접 매칭 ${data.official_body_matches ?? 0}`],
            ["공식 검증 상태", `상세 ${data.official_detail_pages_fetched_count ?? 0} · 성공 ${data.official_body_success_count ?? 0} · 실패 ${data.official_body_fail_count ?? 0} · 최종 점수 반영 ${Boolean(data.official_source_used_in_final_scoring) ? "예" : "아니오"}`],
            ["공식 직접 매칭", `${officialDirectMatchLabel(data)} · 점수 ${data.official_direct_match_score ?? 0}`],
            ["공식 해소 결과", `직접 ${data.official_resolution_direct_matches ?? 0} · 맥락 ${data.official_resolution_contextual_matches ?? 0} · 약함 ${data.official_resolution_weak_candidates ?? 0} · 최고 ${data.official_resolution_top_score ?? 0}`],
            ["반박/모순 검토", `후보 ${data.contradiction_candidates_searched ?? 0} · 매칭 ${data.contradiction_candidates_matched ?? 0} · 확인된 모순 ${data.confirmed_contradictions ?? 0} · 가능성 ${data.possible_contradictions ?? 0}`],
            ["사람 검토 반영", `${formatDiagnosticText(data.human_review_feedback || "없음")} · 승인 보정 ${Boolean(data.approved_boost) ? "예" : "아니오"} · 반려 감점 ${Boolean(data.rejected_penalty) ? "예" : "아니오"}`],
            ["분석 입력 상태", `${formatDiagnosticText(newsCacheState)} · ${formatDiagnosticText(analysisCacheState)}`],
          ])}
          ${advDefList([
            ["선택된 주요 출처", userFacingReportText(publicInstitutionName(data.selected_primary_source || "-"), "-")],
            ["공식 상세문서", userFacingReportText(publicInstitutionName(data.top_official_detail_title || "-"), "-")],
            ["공식 출처 제외/불일치 사유", formatDiagnosticText(formatList(data.official_mismatch_reasons))],
            ["공식 직접 매칭 사유", formatDiagnosticText(data.official_direct_match_reason || "-")],
            ["공식 본문 실패 사유", userFacingReportText(formatReasonCounts(data.official_body_failures || {}), "없음")],
            ["근거 없음 사유", formatDiagnosticText(zeroReasons)],
            ["누락 단계", formatDiagnosticText(missing)],
          ])}
        </div>
      `;
    }

    function renderCollapsibleSection(title, body, open = false, helper = "", extraClass = "") {
      return `
        <details class="collapsible-section${extraClass ? ` ${extraClass}` : ""}" ${open ? "open" : ""}>
          <summary>${escapeHtml(title)}</summary>
          <div class="collapsible-body">${helper ? `<div class="reader-note">${escapeHtml(helper)}</div>` : ""}${body || ""}</div>
        </details>
      `;
    }

    function renderEvidenceSnippets(claims, evidenceSnippets) {
      const claimList = Array.isArray(claims) ? claims : [];
      const snippets = Array.isArray(evidenceSnippets) ? evidenceSnippets : [];
      if (!snippets.length) {
        return '<div class="evidence-source-meta">표시할 근거 문장이 없습니다.</div>';
      }

      return `<div class="evidence-snippet-list">${claimList.map((claim, index) => {
        const related = snippets.filter((snippet) => Number(snippet.claim_index) === index).slice(0, 4);
        const strongCount = related.filter((snippet) => snippet.evidence_quality_label === "strong").length;
        const mediumCount = related.filter((snippet) => snippet.evidence_quality_label === "medium").length;
        const weakCount = related.filter((snippet) => snippet.evidence_quality_label === "weak").length;
        const bestScore = related.reduce((best, snippet) => Math.max(best, Number(snippet.evidence_quality_score || 0)), 0);
        return `
          <div class="evidence-snippet">
            <div class="normalized-claim-text">claim #${index + 1}: ${escapeHtml(limitClaimSentences(cleanArticleTextForPolicyAnalysis(claim) || "기사 제목과 요약 기준으로 핵심 주장을 추가 확인해야 합니다.", 2, CLAIM_MAX_CHARS))}</div>
            <div class="evidence-source-meta">
              근거 품질: 강함 ${escapeHtml(strongCount)}, 보통 ${escapeHtml(mediumCount)}, 약함 ${escapeHtml(weakCount)}, 최고 ${escapeHtml(bestScore)}
            </div>
            ${related.length ? related.map((snippet) => {
              const sourceTitle = escapeHtml(userFacingReportText(publicInstitutionName(snippet.source_title || snippet.source_url || "출처"), "출처"));
              const sourceUrl = escapeHtml(safeUrl(snippet.source_url || ""));
              const sourceHtml = snippet.source_url
                ? `<a href="${sourceUrl}" target="_blank" rel="noopener noreferrer">${sourceTitle}</a>`
                : sourceTitle;
              // DESIGN-DETAIL-5a: evidence_text stays; the 9-cell grid → populated-only
              // definition list (empty 발행처/관련도/추출 방식 etc. drop out; formatter
              // cells guarded so empties don't render placeholder rows).
              // DESIGN-DETAIL-5d FIX 4: wrap each evidence sentence + its fields in an
              // .adv-item so a clear boundary (stronger rule + spacing + heading
              // emphasis) separates one evidence from the next (vs the thin field rules).
              return `
                <div class="adv-item">
                  <div class="evidence-snippet-text adv-item-head">${escapeHtml(userFacingReportText(snippet.evidence_text || "-", "-"))}</div>
                  ${advDefList([
                    ["출처", sourceHtml, { html: true }],
                    ["발행처", snippet.publisher ? publicInstitutionName(snippet.publisher) : ""],
                    ["근거 유형", snippet.evidence_type ? formatEvidenceType(snippet.evidence_type) : ""],
                    ["관련도", snippet.relevance_score ?? ""],
                    ["품질 점수", snippet.evidence_quality_score ?? ""],
                    ["품질 등급", snippet.evidence_quality_label ? formatTechnicalLabel(snippet.evidence_quality_label) : ""],
                    ["주장 지지", snippet.supports_claim ? formatSupportsClaim(snippet.supports_claim) : ""],
                    ["추출 신뢰도", snippet.extraction_confidence ? formatExtractionConfidence(snippet.extraction_confidence) : ""],
                    ["추출 방식", snippet.extraction_method ? formatDiagnosticText(snippet.extraction_method) : ""],
                  ])}
                </div>
              `;
            }).join("") : '<div class="evidence-source-meta">연결된 근거가 없습니다.</div>'}
          </div>
        `;
      }).join("")}</div>`;
    }

    function renderSourceCandidates(sourceCandidates) {
      // DESIGN-DETAIL-5c: tame the candidate scroll-wall WITHOUT losing any candidate
      // or field. ALL candidates are kept (no slice/truncation) — each becomes its own
      // <details> COLLAPSED by default, with a one-line at-a-glance summary
      // (출처유형 · 발행처 · 신뢰도 · 검증역할). Expanding shows the full populated-only
      // label+value list + matched sentences + risk flags. ~8×22 cells → N one-line rows.
      const list = Array.isArray(sourceCandidates) ? sourceCandidates : [];
      if (!list.length) {
        return '<div class="evidence-source-meta">표시할 출처 탐색 후보가 없습니다.</div>';
      }

      const renderCand = (source) => {
        const title = escapeHtml(userFacingReportText(publicInstitutionName(source.title || source.url || "출처 후보"), "출처 후보"));
        const url = escapeHtml(safeUrl(source.url || ""));
        const titleHtml = source.url
          ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${title}</a>`
          : title;
        const exclusionLabel = sourceExclusionLabel(source);
        const trace = sourceTraceability(source);
        const domain = sourceDomain(source.url || "");
        const publisher = source.publisher ? publicInstitutionName(source.publisher) : "";
        // One-line summary (populated bits only). Falls back to the candidate title.
        // DESIGN-DETAIL-5d FIX 2: this reliability_score is the 0-100 candidate score —
        // shown as plain "신뢰도 N" (the bogus "/5" was removed; the genuine 0-5
        // source.reliability_score lives in renderEvidenceSources / the reader card).
        const summaryBits = [
          source.source_type ? formatSourceType(source.source_type) : "",
          publisher,
          // SCORE-CLARITY FIX C: 0-100 candidate score (see the note above), so it
          // takes the 근거 수준 label — NOT a bare "신뢰도", which on this same
          // screen already means the 0-5 source grade. No "/5" here: this is 0-100.
          source.reliability_score == null ? "" : `근거 수준 ${source.reliability_score}`,
          source.verification_role ? formatVerificationRole(source.verification_role) : "",
        ].filter((b) => b && String(b).trim() && String(b).trim() !== "-");
        const summaryText = summaryBits.length
          ? summaryBits.join(" · ")
          : userFacingReportText(publicInstitutionName(source.title || "출처 후보"), "출처 후보");
        const detail = advDefList([
          ["검색어", source.query_used],
          ["목적", source.purpose ? formatSourcePurpose(source.purpose) : ""],
          ["출처 유형", source.source_type ? formatSourceType(source.source_type) : ""],
          ["수집 방식", source.retrieval_method ? formatDiagnosticText(source.retrieval_method) : ""],
          ["발행처", publisher],
          ["본문 확보", source.raw_text_available ? "예" : "아니오"],
          ["공식 본문 수집", source.official_body_fetched ? "예" : "아니오"],
          ["공식 본문 길이", source.official_body_length ?? ""],
          ["공식 본문 매칭", source.official_body_match ? "예" : "아니오"],
          ["공식 매칭 점수", source.official_body_match_score ?? ""],
          ["공식 직접 매칭", officialDirectMatchLabel(source)],
          ["공식 직접 점수", source.official_final_direct_match_score ?? source.official_body_match_score ?? ""],
          ["URL 해소 점수", source.official_url_score ?? ""],
          ["의미 매칭", source.semantic_match_score ?? ""],
          ["정책 일치도", source.policy_alignment_score ?? ""],
          ["공식 근거 점수", source.official_evidence_score ?? ""],
          ["공식 실패 사유", source.official_body_failure_reason ? formatDiagnosticText(source.official_body_failure_reason) : ""],
          // SCORE-CLARITY FIX C: same 0-100 candidate score as the summary line
          // above, so it carries the same 근거 수준 label. The adjacent 등급 is a
          // source-level grade, kept distinct as 출처 신뢰도 등급.
          ["근거 수준 점수", source.reliability_score == null ? "" : `${source.reliability_score}`],
          ["출처 신뢰도 등급", source.reliability_level ? formatReliabilityLevel(source.reliability_level) : ""],
          ["검증 역할", source.verification_role ? formatVerificationRole(source.verification_role) : ""],
          ["도메인", domain || ""],
          ["공개 표시 판단", userFacingReportText(trace.explanation, "")],
        ]);
        const matched = (Array.isArray(source.official_matched_sentences) && source.official_matched_sentences.length) ? `
          <div class="evidence-source-meta"><strong>공식 문서 매칭 문장</strong></div>
          <ul class="compact-list">
            ${source.official_matched_sentences.slice(0, 2).map((match) => `
              <li>${escapeHtml(match.sentence || "-")} <span class="adv-cell-label">(${escapeHtml(match.score ?? "-")}점)</span></li>
            `).join("")}
          </ul>
        ` : "";
        const reason = advIsEmptyDisplay(source.reliability_reason)
          ? ""
          : `<div class="evidence-source-meta">${escapeHtml(userFacingReportText(source.reliability_reason, ""))}</div>`;
        const flagText = formatDiagnosticText(formatList(source.source_risk_flags));
        const flags = advIsEmptyDisplay(flagText) ? "" : `<div class="risk-flags">${escapeHtml(flagText)}</div>`;
        return `
          <details class="adv-cand">
            <summary class="adv-cand-summary">
              <span class="adv-cand-summary-inner">
                <span class="adv-cand-trace ${escapeHtml(trace.className)}">${escapeHtml(trace.label)}</span>
                <span class="adv-cand-line">${escapeHtml(summaryText)}</span>
              </span>
            </summary>
            <div class="adv-cand-body">
              <div class="source-candidate-title">${titleHtml}</div>
              ${exclusionLabel ? `<div class="adv-cand-excl">${escapeHtml(exclusionLabel)}</div>` : ""}
              ${detail}${matched}${reason}${flags}
            </div>
          </details>
        `;
      };

      // DESIGN-DETAIL-5d FIX 1: show the first 6 candidates; the rest go inside a
      // "전체 보기" expander. ALL N candidates stay in the DOM (the overflow ones are
      // inside the nested <details>) — no truncation, just not all visible at once.
      const VISIBLE = 6;
      const head = list.slice(0, VISIBLE);
      const overflow = list.slice(VISIBLE);
      return `
        <div class="adv-cand-count">공식 출처 후보 ${list.length}개</div>
        <div class="adv-cand-list">${head.map(renderCand).join("")}</div>
        ${overflow.length ? `
          <details class="adv-cand-more">
            <summary class="adv-cand-more-summary">공식 출처 후보 ${list.length}개 전체 보기</summary>
            <div class="adv-cand-list">${overflow.map(renderCand).join("")}</div>
          </details>
        ` : ""}
      `;
    }

    // ===== C6 — Status / error / busy UI & metrics =====
    function showStatus(message, success = false) {
      // DESIGN-DETAIL-3b (FIX B): the status line (#statusLine) and the v2 progress
      // slot (#v2ProgressWrap) are DISTINCT elements both positioned at the same
      // under-search spot, so they overlap if both carry content. Clear the v2 slot
      // first → mutual exclusion (the two never coexist). analyze() drives the v2
      // slot and is never interleaved with showStatus, so no in-progress bar is lost.
      v2ResetProgress();
      statusLine.textContent = message;
      statusLine.className = success ? "status-line success" : "status-line";
      statusLine.style.display = "block";
    }

    function hideStatus() {
      statusLine.style.display = "none";
      statusLine.textContent = "";
    }

    function showError(message) {
      errorBox.textContent = message;
      errorBox.style.display = "block";
    }

    function hideError() {
      errorBox.style.display = "none";
      errorBox.textContent = "";
    }

    function setBusy(isBusy) {
      analyzeBtn.disabled = isBusy;
      historyBtn.disabled = isBusy;
      clearHistoryBtn.disabled = isBusy;
      copyReportBtn.disabled = isBusy;
      downloadReportBtn.disabled = isBusy;
      downloadMarkdownBtn.disabled = isBusy;
      queryInput.disabled = isBusy;
      maxNewsInput.disabled = isBusy;
    }

    function computeMetrics(results) {
      const count = results.length;
      const highest = results.reduce((current, result) => {
        const level = String(result.final_decision?.policy_alert_level || "LOW").toUpperCase();
        return (ALERT_RANK[level] || 0) > (ALERT_RANK[current] || 0) ? level : current;
      }, "LOW");
      const totalConfidence = results.reduce((sum, result) => {
        return sum + Number(result.policy_confidence?.policy_confidence_score || 0);
      }, 0);
      const highImpactCount = results.filter((result) => {
        return String(result.policy_impact?.impact_level || "").toLowerCase() === "high";
      }).length;

      return {
        count,
        highest: count ? highest : "-",
        averageConfidence: count ? Math.round(totalConfidence / count) : 0,
        highImpactCount,
      };
    }

    function renderMetrics(results) {
      const metrics = computeMetrics(results);
      metricResults.textContent = metrics.count;
      metricAlert.textContent = metrics.highest === "-" ? "-" : formatAlert(metrics.highest);
      metricConfidence.textContent = metrics.averageConfidence;
      metricImpact.textContent = metrics.highImpactCount;
      metricsEl.style.display = "grid";
    }

    // ===== C7 — Report context & selected-issue intro =====
    function setCurrentReportContext(query, maxNews, results, analyzedAt, aiStatus) {
      const safeResults = Array.isArray(results) ? results : [];
      currentReportContext = {
        query: query || queryInput.value.trim() || "-",
        maxNews: maxNews || Number(maxNewsInput.value || safeResults.length || 0),
        analyzedAt: analyzedAt || new Date().toISOString(),
        aiStatus: aiStatus && typeof aiStatus === "object" ? aiStatus : null,
        results: safeResults,
      };
      reportActionsEl.style.display = safeResults.length ? "flex" : "none";
    }


    function renderSelectedIssueIntro(results, selectedIndex = null) {
      if (!selectedIssueIntroEl) return;
      const safeResults = Array.isArray(results) ? results : [];
      if (!safeResults.length) {
        selectedIssueIntroEl.style.display = "";
        selectedIssueIntroEl.innerHTML = `
          <h2>선택한 이슈 검증 리포트</h2>
          <p>관심 있는 이슈의 상세 보기를 누르거나 검색어를 입력하면, 현재 확보 가능한 기사와 공식 자료를 기준으로 검증 리포트가 표시됩니다.</p>
        `;
        return;
      }
      // DETAIL-CLEANUP-V2: the former "이 이슈는 이렇게 검증되었습니다" 5-tile intro
      // duplicated the AI summary prose (now the report's prose lead) plus the core
      // indicator strip (판정 단계 / 신뢰도 / 공식 출처) and the alert/topic badges.
      // The duplicated content is removed; #selectedIssueIntro is kept (getElementById
      // target) but emptied and hidden so no empty box renders. 주제→badge,
      // 판정 단계/신뢰도/공식 출처→core strip, prose→report-summary-lead.
      selectedIssueIntroEl.innerHTML = "";
      selectedIssueIntroEl.style.display = "none";
    }

    function clearCurrentReportContext() {
      currentReportContext = null;
      reportActionsEl.style.display = "none";
      activeTopicKey = "";
      selectedResultIndex = null;
      renderSelectedIssueIntro([]);
    }

    // Phase 2 M3: in-memory cache for hydrated history records. localStorage only
    // holds slim metadata + per-result summaries; the full payload comes back from
    // /history/{result_id} on demand and lives here for the rest of the session.
    // ===== C8 — Local history & hot topics =====
    const hydratedRecordCache = new Map();

    function safeReadLocalHistory() {
      try {
        const raw = safeStorage.get(LOCAL_HISTORY_KEY);
        if (!raw) {
          return [];
        }
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) {
          return [];
        }
        return parsed.slice(0, LOCAL_HISTORY_LIMIT).map((record, index) => {
          const safeRecord = record && typeof record === "object" ? record : {};
          const stableKey = safeRecord.stable_history_key
            || buildStableHistoryKey(safeRecord.query || "", getHistoryResults(safeRecord));
          return {
            ...safeRecord,
            stable_history_key: stableKey,
            id: safeRecord.id || stableKey || `legacy-${index}-${String(safeRecord.analyzed_at || safeRecord.created_at || safeRecord.query || "record").replace(/[^A-Za-z0-9_-]/g, "")}`,
          };
        });
      } catch (error) {
        console.warn("safeReadLocalHistory parse failed; clearing storage", error);
        safeStorage.remove(LOCAL_HISTORY_KEY);
        return [];
      }
    }

    function getHistoryResults(record) {
      if (!record) return [];
      const recordKey = record.id || record.stable_history_key;
      if (recordKey && hydratedRecordCache.has(recordKey)) {
        const cached = hydratedRecordCache.get(recordKey);
        if (Array.isArray(cached?.results)) {
          return cached.results;
        }
      }
      if (Array.isArray(record?.response?.results)) {
        return record.response.results;
      }
      if (Array.isArray(record?.results)) {
        return record.results;
      }
      if (Array.isArray(record?.summary_results)) {
        return record.summary_results;
      }
      return [];
    }

    function getHistoryAnalyzedAt(record) {
      return record?.analyzed_at || record?.created_at || new Date().toISOString();
    }

    function resultCategory(result, query = "") {
      const text = [
        query,
        result?.title,
        result?.summary,
        result?.topic,
        result?.final_decision?.market_signal,
        result?.policy_impact?.affected_sectors,
      ].flat().join(" ");
      if (/전세사기/i.test(String(query || ""))) return "사회";
      if (/부동산/i.test(String(query || ""))) return "부동산";
      if (/금융위|금감원|금리|은행|DSR|연체|금융/i.test(String(query || ""))) return "금융";
      if (/전세사기/i.test(text)) return "사회";
      if (/금융위|금감원|금리|은행|대출|DSR|연체|금융|PF|채권|한국은행/i.test(text)) return "금융";
      if (/부동산|주택|전세|월세|임대|청약|양도세|주거|LH|HUG/i.test(text)) return "부동산";
      if (/소비자|청년|근로자|가계|서민|지원|혜택/i.test(text)) return "소비자";
      if (/사회|피해|사기|안전|복지/i.test(text)) return "사회";
      return "정책";
    }

    function officialStatusLabel(result) {
      const verification = result?.verification_card || result || {};
      const summary = verification.source_reliability_summary || {};
      const debug = verification.debug_summary || {};
      // LABEL-HONESTY: "공식 근거 확인" requires GENUINE verification (a real
      // primary-document match or a body-sentence match), not a relevance-passing
      // fetched page (the IBK word-overlap pattern). The backend persists
      // has_genuine_official_support; old rows lacking it fall back to a real
      // body match (official_body_matches > 0). Score is untouched — display only.
      const genuine = (typeof summary.has_genuine_official_support === "boolean")
        ? summary.has_genuine_official_support
        : (Number(debug.official_body_matches || 0) > 0);
      if (genuine) {
        return "공식 근거 확인";
      }
      if (summary.official_detail_available || Number(debug.official_body_matches || 0) > 0) {
        // Non-genuine but an official page was fetched/relevance-matched: honest,
        // non-alarmist downgrade — related material exists, not a direct verification.
        return "공식자료 참고";
      }
      if (Number(debug.official_body_candidates || summary.official_candidate_count || 0) > 0) {
        if (Number(debug.official_bodies_fetched || 0) > 0) {
          return "공식 본문 확인 제한";
        }
        return "공식 출처 확인 필요";
      }
      return "뉴스 출처 기반 보조 근거";
    }

    function topSummaryLine(result) {
      const verification = result?.verification_card || result || {};
      const decision = result?.final_decision || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      const quality = debug.evidence_quality_summary || verification.evidence_quality_summary || result?.evidence_quality_summary || {};
      // NARRATIVE-2: card-face summary leads with the per-article claim
      // (exportClaimText — already reviewer-safe, no new AI call) so hot-topic
      // cards stop looking identical. The verdict-tier evidence_summary
      // boilerplate falls to later in the chain; the evidenceQualityExplanation
      // fallback below is now effectively unreachable for the card (exportClaimText
      // always returns non-empty), so the repetitive tier text no longer surfaces
      // on the card face. topSummaryLine feeds ONLY card.summary; the detail view
      // renders its full summary via a separate path (contentLead), unaffected.
      const line = userFacingReportText(exportClaimText(result) || decision.decision_summary || verification.evidence_summary || "", "");
      if (line) return String(line).slice(0, 115);
      return evidenceQualityExplanation(quality, debug.evidence_strength_summary || {}, officialEvidenceIsGenuine(verification.source_reliability_summary || {}, debug));
    }

    // NARRATIVE-3B: strip the repeated cautious wrapper from the CARD FACE summary
    // ONLY. The detail view + export keep the cautious wording (they call
    // exportClaimText directly, untouched). Matching is by EXACT fixed literals via
    // startsWith/endsWith — never a greedy regex — so the distinctive per-article
    // middle is never removed.
    function stripCardFaceWrapper(text) {
      if (!text) return "";
      let out = String(text).trim();
      const prefixes = ["기사 제목과 요약 기준으로는 ", "보도 내용은 "];
      for (const p of prefixes) {
        if (out.startsWith(p)) { out = out.slice(p.length); break; }
      }
      const suffixes = [
        "라는 보도 내용은 기사 제목과 요약 기준으로 추가 확인이 필요합니다.",
        "기사 제목과 요약 기준으로 추가 확인이 필요합니다.",
        "추가 확인이 필요합니다.",
      ];
      for (const s of suffixes) {
        if (out.endsWith(s)) { out = out.slice(0, out.length - s.length); break; }
      }
      return out.replace(/[\s,]+$/u, "").trim();
    }

    // Loose "essentially equal" check for hiding a card-face summary that, after
    // stripping, collapses to the card title (already shown above it). Lowercase +
    // drop whitespace and punctuation so "{title}" and "{title}." compare equal.
    function normalizeForCompare(text) {
      return String(text || "")
        .toLowerCase()
        .replace(/\s+/g, "")
        .replace(/[.,!?。·…"'“”‘’()\[\]]/g, "");
    }

    // DESIGN-3B-1: card-face hashtags (DISPLAY-ONLY). Hybrid source: structured
    // tokens (normalized_claims target/object/actor + matched_concepts) when the
    // full result is in memory (fresh-search path); otherwise tokenize the slim
    // text (title primarily, then claim_text/claims[0]) since the homepage feed
    // reads the slim /history payload (no normalized_claims). Strict junk filter:
    // drop anything with a digit (dates/money/numbers), money/percent marks,
    // placeholders, single chars, and over-generic / grammatical tokens. Returns
    // up to 4 clean noun tags; [] → the caller omits the hashtag row.
    const HASHTAG_GENERIC = new Set([
      "정부", "정책", "지원", "뉴스", "기사", "기관", "관련", "발표", "금융", "확대",
      "강화", "추진", "검토", "운영", "계획", "방안", "사업", "제도", "관리", "대책",
      "당국", "공식", "내용", "결과", "오늘", "이번", "해당", "전체", "주요", "상황",
      "그리고", "그러나", "위해", "통해", "대해", "했다", "한다", "밝혔다", "전했다",
      "있다", "없다", "된다", "됐다", "이라", "라는", "으로", "에서", "에게",
    ]);
    const HASHTAG_PLACEHOLDER = new Set(["unknown", "미상", "없음", "null", "none", "기타", "미분류", "na"]);

    function _hashtagIsJunk(tok) {
      const t = String(tok || "").trim().replace(/^#/, "");
      if (t.length < 2) return true;
      if (HASHTAG_PLACEHOLDER.has(t.toLowerCase())) return true;
      if (/\d/.test(t)) return true;                 // any digit → dates/money/numbers out
      if (/[원%]/.test(t)) return true;              // money / percent blobs out
      if (!/[가-힣A-Za-z]/.test(t)) return true;     // must contain a letter
      if (HASHTAG_GENERIC.has(t)) return true;
      return false;
    }

    function deriveCardHashtags(result) {
      const verification = (result && result.verification_card) || result || {};
      const normalized = Array.isArray(result && result.normalized_claims)
        ? result.normalized_claims
        : (Array.isArray(verification.normalized_claims) ? verification.normalized_claims : []);
      const candidates = Array.isArray(result && result.source_candidates)
        ? result.source_candidates
        : (Array.isArray(verification.source_candidates) ? verification.source_candidates : []);
      let tokens = [];
      // (1) structured tokens (present only when the FULL result is in memory)
      for (const nc of normalized) {
        if (nc && typeof nc === "object") {
          for (const k of ["target", "object", "actor"]) {
            const v = String(nc[k] || "").trim();
            if (v) tokens.push(v);
          }
        }
      }
      for (const c of candidates) {
        if (c && typeof c === "object" && Array.isArray(c.matched_concepts)) {
          for (const m of c.matched_concepts) tokens.push(String(m));
        }
      }
      // (2) slim-text fallback — the TITLE only. Titles are noun-dense headlines,
      // so tokens stay clean (#공시가격 #서울 #아파트); a full claim sentence would
      // leak josa-attached / verb tokens (#정부가, #발표했다) that can't be stripped
      // safely without a morphological analyzer. Fewer-but-clean > more-but-junky.
      if (!tokens.length) {
        const text = String((result && result.title) || "");
        tokens = text.match(/[가-힣]{2,}|[A-Za-z]{3,}/g) || [];
      }
      const seen = new Set();
      const out = [];
      for (let t of tokens) {
        t = String(t).trim().replace(/\s+/g, "");
        if (_hashtagIsJunk(t)) continue;
        const key = t.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(t);
        if (out.length >= 4) break;
      }
      return out;
    }

    function topicCardFromResult(result, index, source = "current", record = null) {
      const query = record?.query || currentReportContext?.query || queryInput?.value || "";
      const confidence = result?.policy_confidence || {};
      const decision = result?.final_decision || {};
      const verification = result?.verification_card || result || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      const keyBase = source === "history"
        ? `${record?.id || record?.stable_history_key || "history"}:${index}`
        : `current:${index}:${result?.original_url || result?.title || ""}`;
      // NARRATIVE-3B: card-face summary = wrapper-stripped per-article text, hidden
      // when it collapses to the title (title-derived cards) to avoid duplicating
      // the headline already shown above. Uses the SAME title string the card renders.
      // MOBILE-POLISH F: the display-layer marker strip lands here, the single
      // point card.title is derived — covering the grid/hero/secondary cards (all
      // route through renderTopicCardHtml) and the 최근 본 rows at once. The
      // summary-collapses-to-title compare below reuses this same string, so the
      // two stay in sync. Trending / detail / search-hits build titles on their
      // own paths and call the helper directly.
      const cardTitle = stripLeadingTitleMarker(publicInstitutionName(result?.title || record?.query || "검증 뉴스"));
      const strippedSummary = stripCardFaceWrapper(topSummaryLine(result));
      const summaryNorm = normalizeForCompare(strippedSummary);
      const summaryCollapsesToTitle =
        summaryNorm.length > 0 && summaryNorm === normalizeForCompare(cardTitle);
      // DESIGN-3B-1: card-face additions (all DISPLAY-ONLY, slim-backed). The
      // genuine flag reuses the SAME predicate as officialStatusLabel (LABEL-HONESTY).
      const reliability = verification.source_reliability_summary || result?.source_reliability_summary || {};
      const hasGenuineOfficial = (typeof reliability.has_genuine_official_support === "boolean")
        ? reliability.has_genuine_official_support
        : (Number(debug.official_body_matches || 0) > 0);
      const officialDetailTitle = publicInstitutionName(
        reliability.top_official_detail_title || reliability.top_source_title || "");
      // CARD-BOX: institution string for the source box (NEW-rows-only; old rows
      // lack the key → "" → box falls back to the document title alone). Koreanized
      // via the same launderer as the title so "국"/"복"/"금" come out.
      const officialDetailInstitution = publicInstitutionName(
        reliability.top_official_institution || "");
      return {
        key: keyBase,
        source,
        index,
        recordId: record?.id || result?.result_id || "",
        humanReviewedAt: result?.human_reviewed_at || null,
        title: cardTitle,
        topic: exportTopicLabel(result, query),
        category: resultCategory(result, query),
        // DISPLAY-CATEGORY B-1: raw backend domain enum (or null). Drives the
        // domain tabs/sections; normalized via cardDomainKey() at filter time.
        domain: result?.domain ?? record?.domain ?? null,
        // NOISE-1 Part B: content_nature metadata label (from the slim payload).
        // Display-only; drives the 뜨는순 rank-down + "시장·시세" chip when
        // market_commercial AND NOT genuine. NULL/old rows → never treated.
        contentNature: result?.content_nature ?? record?.content_nature ?? null,
        alert: String(decision.policy_alert_level || record?.highest_alert || "WATCH").toUpperCase(),
        confidence: confidence.policy_confidence_score ?? record?.average_confidence ?? "-",
        officialStatus: officialStatusLabel(result),
        freshness: isFreshlyBroken(result),
        reviewStatus: formatReviewStatus(verification.review_status) || "AI 초안, 사람 검토 대기",
        summary: summaryCollapsesToTitle ? "" : strippedSummary,
        reason: decision.decision_summary || evidenceQualityExplanation(debug.evidence_quality_summary || {}, debug.evidence_strength_summary || {}),
        quality: debug.evidence_quality_summary || verification.evidence_quality_summary || {},
        verdictLabel: verification.verdict_label || result?.verdict_label || "",
        hasGenuineOfficial: hasGenuineOfficial,
        officialDetailTitle: officialDetailTitle,
        officialDetailInstitution: officialDetailInstitution,
        hashtags: deriveCardHashtags(result),
        // DESIGN-C3h-1: ISO-8601 UTC analysis timestamp (from the slim payload),
        // used only for the client-side KST-"today" filter of the 오늘의 검증 row.
        // Plain object field — does NOT change card HTML.
        createdAt: result?.created_at ?? record?.created_at ?? record?.analyzed_at ?? null,
      };
    }


    function currentTopicCards(preferredResults) {
      const currentResults = Array.isArray(preferredResults) && preferredResults.length
        ? preferredResults
        : (Array.isArray(currentReportContext?.results) ? currentReportContext.results : []);
      if (currentResults.length) {
        return currentResults.map((result, index) => topicCardFromResult(result, index, "current"));
      }
      // M17-search-quality: do NOT fall back to localStorage history.
      // The previous behaviour surfaced prior 전세대출 analyses as if
      // they were results for the user's current query, breaking trust.
      // M45: with no live session search, fall back to SERVER analyses
      // (GET /history, incl. cron output) — NOT localStorage. These are
      // the homepage's "오늘의 정책 이슈" feed. Empty server list → []
      // → renderHotTopics shows its existing placeholder.
      if (serverHotTopicResults.length) {
        return serverHotTopicResults.map((result, index) =>
          topicCardFromResult(result, index, "server"));
      }
      return [];
    }

    // HOTSORT Phase 2 — stable sort of the category-filtered card array.
    // Array.prototype.sort is stable, so equal keys preserve the incoming
    // server order (id DESC = latest-first) — that IS the "최신순" tie-break,
    // so no created_at mapping is needed. Honest signals only (no view counter).
    function sortTopicCards(cards, sortKey) {
      const list = cards.slice();
      if (sortKey === "뜨는순") {
        // HOMEPAGE-TIERED: composite "hotness" proxy (no real popularity field).
        // 위험도(alert) desc → freshness(🔥) → 신뢰도(confidence) desc, with the
        // stable sort preserving server id-DESC order as the recency tiebreak.
        const score = (c) =>
          (ALERT_RANK[c.alert] || 0) * 1000
          + (c.freshness ? 100 : 0)
          + (Number(c.confidence) || 0)
          // NOISE-1 Part B: rank-DOWN (뜨는순 only) market_commercial noise that
          // lacks genuine official support. -1_000_000 exceeds any real composite
          // (max ALERT_RANK*1000 + 100 + confidence), so demoted cards always sort
          // below non-demoted ones while keeping their relative order among
          // themselves. Still visible on scroll — NOT a hide/filter. Reuses the
          // existing hasGenuineOfficial predicate; never recomputed.
          + ((c.contentNature === "market_commercial" && !c.hasGenuineOfficial) ? -1000000 : 0);
        list.sort((a, b) => score(b) - score(a));
      } else if (sortKey === "위험도순") {
        list.sort((a, b) => (ALERT_RANK[b.alert] || 0) - (ALERT_RANK[a.alert] || 0));
      } else if (sortKey === "검토됨 우선") {
        list.sort((a, b) => (b.humanReviewedAt ? 1 : 0) - (a.humanReviewedAt ? 1 : 0));
      }
      // "최신순" -> keep server order (latest-first); no reorder.
      return list;
    }


    // HOMEPAGE-TIERED: single card renderer with a full/concise branch.
    //   opts.detailed=true  (TIER 1) → summary + reason + 4-tile meta
    //                                  (신뢰도 + 공식출처 + 리뷰 + 근거품질).
    //   opts.detailed=false (TIER 2) → concise (badges + title + 신뢰도 +
    //                                  공식출처). All omitted fields still show
    //                                  on the detail view (no data lost).
    // 신뢰도 stays the FIRST .topic-card-meta div in both branches so the
    // :first-child --verify accent + large number lands on it.
    function renderTopicCardHtml(card, opts) {
      const detailed = !!(opts && opts.detailed);
      const selected = (card.key && card.key === activeTopicKey)
        || (card.source === "current" && Number.isInteger(selectedResultIndex) && Number(card.index) === selectedResultIndex);
      // DESIGN-3B-1: small editorial card. The 4 stacked sub-boxes (신뢰도/공식 출처/
      // 리뷰/근거 품질) moved OFF the card face — they all render in the DETAIL view
      // (core-indicator-strip + verification-card + advanced section). The card
      // face now carries an at-a-glance COLORED VERDICT dot + label, editorial
      // badges, a clamped summary, conditional filtered hashtags, and a conditional
      // ✓ 공식 근거 chip (only when genuine). Tier-1 and tier-2 render identically.
      const hashtags = Array.isArray(card.hashtags) ? card.hashtags : [];
      const hashtagRow = hashtags.length
        ? `<div class="topic-card-tags">${hashtags.map((t) => `<span class="topic-card-tag">#${escapeHtml(t)}</span>`).join("")}</div>`
        : "";
      // CARD-BOX: primary-source COMPARE BOX (genuine cards only). Split into a
      // blue PILL (rendered on the verdict band, beside the verdict label) and a
      // BODY (avatar + institution·document line) rendered below. Non-genuine cards
      // render neither — and crucially their verdict label is UNAFFECTED (the pill
      // is gated on hasGenuineOfficial; the verdict band always renders).
      const genuineBox = card.hasGenuineOfficial;
      const sourcePill = genuineBox
        ? `<span class="card-source-head">✓ 공식 근거 확인</span>`
        : "";
      const sourceBody = (() => {
        if (!genuineBox) return "";
        const inst = String(card.officialDetailInstitution || "").trim();
        const t = String(card.officialDetailTitle || "").trim();
        // Avatar = institution initial when present, else title initial, else 공.
        const firstChar = inst ? Array.from(inst)[0] : (t ? Array.from(t)[0] : "공");
        // DEDUP GUARD: when institution equals the document (or is absent) — which
        // happens when document_title was empty and both collapse to source_name —
        // show the document alone, never "기관 · 기관".
        const innerText = (inst && inst !== t)
          ? `${escapeHtml(inst)} · ${escapeHtml(t)}`
          : escapeHtml(t);
        const label = t
          ? `대조한 1차 출처 · ${innerText}`
          : "공식 문서 본문과 대조";
        return `
          <div class="card-source-box">
            <div class="card-source-row">
              <span class="card-source-avatar">${escapeHtml(firstChar)}</span>
              <span class="card-source-label">${label}</span>
              <span class="card-source-check">✓</span>
            </div>
          </div>`;
      })();
      const confidenceInline = (card.confidence !== "-" && card.confidence !== null && card.confidence !== undefined)
        // SCORE-CLARITY FIX A: "신뢰도 88" read as "88% true". The number is
        // policy_confidence_score — a weighted evidence/signal composite
        // (policy_confidence.py:155-163) that is hard-clamped to <=20 whenever no
        // official document was found, REGARDLESS of whether the claim is true.
        // "근거 수준" names what it measures and cannot be read as a truth
        // percentage. Label only; the score itself is untouched.
        ? `<span class="card-confidence">근거 수준 ${escapeHtml(card.confidence)}</span>`
        : "";
      return `
        <article class="topic-card ${opts && opts.hero ? "topic-card--hero " : ""}${opts && opts.secondary ? "topic-card--secondary " : ""}${selected ? "selected" : ""}" data-topic-key="${escapeHtml(card.key)}" data-topic-source="${escapeHtml(card.source)}" data-topic-index="${escapeHtml(card.index)}" data-topic-record-id="${escapeHtml(card.recordId)}">
          <div class="topic-card-top">
            <span class="card-domain">${domainIconMarkup(cardDomainKey(card))}${escapeHtml(domainDisplayLabel(cardDomainKey(card)))}</span>
            <span class="card-watch ${alertClass(card.alert)}">${escapeHtml(formatAlert(card.alert))}</span>
            ${isTodayCard(card) ? `<span class="card-today-badge">오늘 검증</span>` : ""}
            ${(card.contentNature === "market_commercial" && !card.hasGenuineOfficial) ? `<span class="card-today-badge">시장·시세</span>` : ""}
            ${card.freshness ? `<span class="card-fresh">🔥 ${escapeHtml(FRESHNESS_BADGE_LABEL)}</span>` : ""}
            ${card.humanReviewedAt ? `<span class="review-status review-approved">${escapeHtml(HUMAN_REVIEWED_LABEL)}</span>` : ""}
          </div>
          <h3 class="topic-card-title">${escapeHtml(card.title)}</h3>
          ${card.summary ? `<div class="topic-card-summary">${escapeHtml(card.summary)}</div>` : ""}
          ${hashtagRow}
          <div class="topic-card-verdict">
            ${sourcePill}
            <span class="verdict-pill ${verdictTierClass(card.verdictLabel)}">
              <span class="verdict-dot" style="background:${verdictDotColor(card.verdictLabel)}"></span>
              <span class="verdict-text">판정 ${escapeHtml(verdictLabelKo(card.verdictLabel))}</span>
            </span>
            ${confidenceInline}
          </div>
          ${sourceBody}
        </article>
      `;
    }

    // DISPLAY-CATEGORY B-1: render the 전체 tab plus one tab per domain present
    // in the feed (canonical order). Korean labels at display; the raw enum is
    // kept in data-domain for comparison. Active selection is preserved; if the
    // active domain is no longer present it falls back to 전체 so the feed never
    // renders empty against a stale tab.
    function renderCategoryTabs() {
      if (!categoryTabsEl) return;
      // STABLE-TABS S2: render the STABLE full tab set — 전체 + every domain in
      // DOMAIN_ORDER (== domain_classifier.LABELS), ALWAYS, in canonical order.
      // Previously the tab set was derived from the recent pool (present domains),
      // so domains absent from the agriculture-flooded tail lost their tab. Tabs
      // no longer appear/disappear with the feed tail; a click loads the domain
      // from GET /history?domain=<key> (setActiveDomain). Every DOMAIN_ORDER entry
      // is a real user category, so no filtering to an "intended set" is needed.
      const tabs = [["전체", "전체"]].concat(
        TAB_ORDER.map((d) => [d, domainDisplayLabel(d)])
      );
      categoryTabsEl.innerHTML = tabs.map(([key, label]) => {
        const active = key === activeDomain ? " active" : "";
        return `<button class="category-tab${active}" type="button" data-domain="${escapeHtml(key)}">${escapeHtml(label)}</button>`;
      }).join("");
    }

    // DESIGN-C3h-2: per-domain grouped sections for the 전체 tab. Groups the full
    // all-domain card set by cardDomainKey, iterates DOMAIN_ORDER, and for each
    // domain WITH cards emits a section: a heavy black rule (.domain-section, CSS)
    // + a big serif header + a static subtitle + a "{label} 전체 →" tab-switch
    // button + the domain's top-3 by 뜨는순 as a 3-col .latest-checks-row. Reuses
    // renderTopicCardHtml + sortTopicCards verbatim (no card-render change). Returns
    // an innerHTML string (""→ no sections). Duplicates with the top feed are
    // allowed (top = 전체 인기/최신; sections = browse-by-category).
    function renderDomainSections() {
      // HOME-SECTION-FIX A1: source each section from its OWN per-domain fetch
      // (domainSectionCache), not the global recent pool — so every domain with
      // rows gets a section and its real top-3 (fixes 보건=1 and missing 교육).
      return DOMAIN_ORDER.map((d) => {
        const results = domainSectionCache.get(d);
        if (!results || !results.length) return "";
        const domainCards = results.map((result, index) =>
          topicCardFromResult(result, index, "server"));
        if (!domainCards.length) return "";
        const label = domainDisplayLabel(d);
        const top3 = sortTopicCards(domainCards, "뜨는순").slice(0, 3);
        return `<section class="domain-section">`
          + `<div class="domain-section-head">`
          + `<div class="domain-section-titles">`
          + `<h2 class="domain-section-title">${escapeHtml(label)}</h2>`
          + `<p class="domain-section-sub">${escapeHtml(DOMAIN_SUBTITLE[d] || "")}</p>`
          + `</div>`
          + `<button type="button" class="domain-section-all" data-domain="${escapeHtml(d)}">${escapeHtml(label)} 전체 →</button>`
          + `</div>`
          + `<div class="latest-checks-row">`
          + top3.map((card) => renderTopicCardHtml(card, { detailed: true })).join("")
          + `</div>`
          + `</section>`;
      }).join("");
    }

    // HOMEPAGE-TIERED: two-tier feed from ONE ranked pool. The active tab is a
    // range filter (전체 = global; specific = that domain). Both tiers use the
    // same sort (default 뜨는순) so ranking is consistent: tier-1 = global top,
    // tier-2 = ranks beyond the tier-1 cut. Nothing disappears — an item that
    // misses the tier-1 cut shows in tier-2 (and ranks high under its domain tab).
    // HOME-TOP5 S5b: resolve the #1 trending cluster's representative card from
    // the loaded server pool (the renderRecentViewed resolution). Returns null
    // when the trending fetch hasn't landed / is empty / the representative is
    // older than the recent-50 pool — callers then render the two-card hero.
    function trendingDailyPickCard() {
      const rows = Array.isArray(trendingHeroRows) ? trendingHeroRows : [];
      const rid = Number(rows[0]?.representative_analysis_id);
      if (!Number.isInteger(rid) || rid <= 0) return null;
      const idx = serverHotTopicResults.findIndex((r) => Number(r?.result_id) === rid);
      if (idx < 0) return null;
      return topicCardFromResult(serverHotTopicResults[idx], idx, "server");
    }

    function renderHotTopics(preferredResults) {
      if (!hotTopicsEl) return;
      const allCards = currentTopicCards(preferredResults);
      // HOME-TOP5 S5b: 오늘의 한 장 applies ONLY to the genuine 전체 home feed —
      // never to a domain tab's hero or a search/preferred-results render.
      const dailyPick = (activeDomain === "전체"
        && !(Array.isArray(preferredResults) && preferredResults.length))
        ? trendingDailyPickCard() : null;
      // STABLE-TABS S2: the tab SET is now stable (전체 + all DOMAIN_ORDER), so it
      // no longer depends on the loaded pool — no tabPool derivation needed.
      renderCategoryTabs();
      // STABLE-TABS S2: the active pool.
      //   전체 → the recent feed (allCards), byte-identical to before.
      //   a domain → its server-fetched cards from the in-memory cache (built via
      //     the SAME topicCardFromResult builder, so icons/badges/honesty match).
      //   a domain not yet cached → [] + a loading/error state below (domainPending).
      let filtered;
      let domainPending = false;
      if (activeDomain === "전체") {
        filtered = allCards;
      } else if (domainResultsCache.has(activeDomain)) {
        filtered = domainResultsCache.get(activeDomain)
          .map((result, index) => topicCardFromResult(result, index, "server"));
      } else {
        filtered = [];
        domainPending = true;
      }
      // DESIGN-C3h-1c: per-tab feed (every tab, filtered by activeDomain):
      //   hero      = top-2 by 뜨는순 (the band)
      //   오늘의 검증 = TODAY-verified, newest-first, ≤3 — SORT-INDEPENDENT (unchanged)
      //   card row  = first 3 of the sort-controlled pool (no header)
      //   list      = the rest of the pool, 1-col divider list + 더 보기
      // FIX 최신순: the pool is derived from `filtered` (SERVER order = id-DESC = newest)
      // MINUS the hero + today cards, THEN sorted by activeSort. "최신순" keeps input
      // order → true newest-first (the prior bug pre-sorted the pool by 뜨는순, losing
      // server order). The card row + the list are the SAME poolSorted array, chunked,
      // so BOTH obey the dropdown. Today cards are excluded from the pool so the card
      // row never duplicates the 오늘의 검증 row.
      const hot = sortTopicCards(filtered, "뜨는순");
      // DESIGN-C3h-3: the dedicated 오늘의 검증 row was removed — today cards now flow
      // into the normal sorted feed carrying the per-card "오늘 검증" badge (isTodayCard).
      // So the pool excludes ONLY the hero cards (no longer the today cards).
      // HOME-TOP5 S5b: the grid exclusion follows the ACTUAL hero. With a
      // resolved 오늘의 한 장 the ONE pick is excluded (matched by key OR
      // recordId, so a cross-source pool can't double-render it) and the two
      // 뜨는순 cards flow back into the grid; on fallback the pre-S5b 2-card
      // exclusion applies unchanged.
      const heroKeys = new Set(hot.slice(0, 2).map((c) => c.key));
      const poolBase = filtered.filter((c) => dailyPick
        ? !(c.key === dailyPick.key
            || (c.recordId && String(c.recordId) === String(dailyPick.recordId)))
        : !heroKeys.has(c.key));
      const poolSorted = sortTopicCards(poolBase, activeSort);
      // DESIGN-C3-2: ONE uniform 3-col grid, PAGE_SIZE (12) cards per page. gridPool =
      // poolSorted (filtered MINUS the 2 hero cards, already sorted by activeSort so the
      // sort dropdown still drives grid order). Only the grid pages — the hero band
      // (hot[0]/hot[1], fixed 뜨는순) is independent of currentPage.
      const gridPool = poolSorted;
      // Pages over the post-hero pool. DESIGN-C3-2-FIX (BUG 1): the clamp must NOT be
      // destructive on a NARROWED render. Opening a card calls renderResults(results) →
      // renderHotTopics(results): currentTopicCards returns that 1-card pool → gridPool
      // is empty → totalPages 1 → the old `currentPage = clamp(...)` forced currentPage
      // to 1, so browser BACK always landed on page 1. Fix: only HEAL currentPage on a
      // genuine home-feed render (no preferredResults — setActiveDomain / sort / page
      // click / popstate-home / init / async server-fill all call renderHotTopics() with
      // NO args); a narrowed/transient render leaves currentPage untouched so it survives
      // the detail round-trip. Slice + nav always use a LOCAL effectivePage, so no render
      // is ever out of range even before the heal (covers the async pool-shrink case too).
      const totalPages = Math.max(1, Math.ceil(gridPool.length / PAGE_SIZE));
      const isHomeFeedRender = !(Array.isArray(preferredResults) && preferredResults.length);
      if (isHomeFeedRender) currentPage = Math.min(Math.max(1, currentPage), totalPages);
      const effectivePage = Math.min(Math.max(1, currentPage), totalPages);
      const pageSlice = gridPool.slice((effectivePage - 1) * PAGE_SIZE, effectivePage * PAGE_SIZE);

      // Hero band → #hotTopicsTop; the uniform grid → #hotTopics.topic-card-grid
      // via the ORDINARY card builder ({detailed:true} only — no hero/secondary),
      // written straight in with NO .feed-sec-ruled / .latest-checks-row /
      // .feed-list wrappers (the container IS the 3-col grid now).
      // HOME-TOP5 S5b: with a resolved 오늘의 한 장 the hero is that ONE big card
      // (the most-CIRCULATED story by spread growth — circulation, never a
      // verdict; the standard card face keeps its own honest badges). Every
      // unresolved state falls through to the pre-S5b hero branches unchanged.
      if (dailyPick) {
        hotTopicsTopEl.innerHTML = renderTopicCardHtml(dailyPick, { detailed: true, hero: true });
        hotTopicsEl.innerHTML = pageSlice
          .map((card) => renderTopicCardHtml(card, { detailed: true }))
          .join("");
      } else if (!hot.length) {
        hotTopicsTopEl.innerHTML = "";
        // STABLE-TABS S2: a domain tab whose data is still fetching / failed shows
        // a loading or friendly-error line instead of the generic empty-state.
        if (domainPending) {
          hotTopicsEl.innerHTML = domainFetchErrorKey === activeDomain
            ? '<div class="empty-state">이 분야의 뉴스를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.</div>'
            : '<div class="empty-state">불러오는 중…</div>';
        } else {
          hotTopicsEl.innerHTML = '<div class="empty-state">검색을 실행하거나 최근 분석을 불러오면 검증 카드가 표시됩니다.</div>';
        }
      } else if (hot.length >= 2) {
        const band = `<div class="feed-hero-band">`
          + renderTopicCardHtml(hot[0], { detailed: true, hero: true })
          + renderTopicCardHtml(hot[1], { detailed: true, secondary: true })
          + `</div>`;
        hotTopicsTopEl.innerHTML = band;
        hotTopicsEl.innerHTML = pageSlice
          .map((card) => renderTopicCardHtml(card, { detailed: true }))
          .join("");
      } else {
        // <2 fallback — a single card renders as the hero alone (no grid below).
        hotTopicsTopEl.innerHTML = renderTopicCardHtml(hot[0], { detailed: true, hero: true });
        hotTopicsEl.innerHTML = "";
      }

      // DESIGN-C3-2: render the page-number nav (prev / 1..N / next) below the grid.
      // Empty when totalPages <= 1 (small pool → no nav shown). Uses effectivePage so the
      // highlighted/active page always matches the rendered slice (BUG-1-FIX).
      renderFeedPagination(totalPages, effectivePage);

      // C3-1: RETIRE the TIER-2 "나머지 뉴스" block on BOTH tabs — the domain grid now
      // shows the full domain pool, so the separate tier is redundant. Kept hidden +
      // emptied (shells preserved for C3-2). renderDomainSections stays 전체-only (C3-3
      // handles gating); #verifyHowSection is untouched.
      if (activeDomain === "전체") {
        // HOME-SECTION-FIX A1: kick off (idempotent) per-domain prefetch, then
        // render whatever has landed. Each fetch repaints the sections as it
        // resolves, so all domains fill in without blocking the feed.
        ensureDomainSectionsLoaded();
        if (feedDomainSectionsEl) feedDomainSectionsEl.innerHTML = renderDomainSections();
      } else {
        if (feedDomainSectionsEl) feedDomainSectionsEl.innerHTML = "";
      }
      // C3-3: the "이렇게 검증합니다" intro box is a site intro → 전체 tab only, hidden on
      // domain tabs. Mirrors the per-domain-section 전체-only gate above.
      if (verifyHowEl) verifyHowEl.hidden = activeDomain !== "전체";
      if (tier2SectionEl) tier2SectionEl.hidden = true;
      if (tier2GridEl) tier2GridEl.innerHTML = "";
      if (tier2LoadMoreEl) tier2LoadMoreEl.hidden = true;
      if (tier2CollapseEl) tier2CollapseEl.hidden = true;

      // HOME-TOP5 S5b: the 인기 검증 랭킹 sidebar render was retired here (the
      // S5a 확산 성장 Top 5 panel covers the sidebar-ranking role).
      // CLUSTER-SURFACE S-b: repaint the "N개 매체" chips after every grid paint
      // (innerHTML wiped any prior ones). Fire-and-forget; internally fail-silent,
      // and the 5-min HTTP cache makes repeat calls cheap.
      loadClusterSizeChips();
    }

    // DESIGN-C3-2: page-number nav for the post-hero grid. prev (‹) / 1..N / next (›).
    // The current page gets .active; prev on page 1 and next on the last page render
    // disabled (kept in place so the row doesn't jump). Empty string when totalPages
    // <= 1 so no nav shows for a small pool. Buttons carry data-page (a number) or
    // data-page-nav ("prev"/"next"); one delegated listener on #feedPagination handles
    // clicks (set currentPage → renderHotTopics → scroll the grid into view). Numbers
    // are integers → no escaping needed; the ≤50 pool means ≤5 pages, so no ellipsis.
    function renderFeedPagination(totalPages, current) {
      if (!feedPaginationEl) return;
      if (!totalPages || totalPages <= 1) {
        feedPaginationEl.innerHTML = "";
        return;
      }
      const parts = [];
      const prevDisabled = current <= 1 ? " disabled" : "";
      parts.push(
        `<button type="button" class="feed-pagination-btn feed-pagination-nav" `
        + `data-page-nav="prev"${prevDisabled} aria-label="이전 페이지">‹</button>`
      );
      for (let p = 1; p <= totalPages; p += 1) {
        const active = p === current ? " active" : "";
        const aria = p === current ? ' aria-current="page"' : "";
        parts.push(
          `<button type="button" class="feed-pagination-btn feed-pagination-page${active}" `
          + `data-page="${p}"${aria}>${p}</button>`
        );
      }
      const nextDisabled = current >= totalPages ? " disabled" : "";
      parts.push(
        `<button type="button" class="feed-pagination-btn feed-pagination-nav" `
        + `data-page-nav="next"${nextDisabled} aria-label="다음 페이지">›</button>`
      );
      feedPaginationEl.innerHTML = parts.join("");
    }

    // HOME-TOP5 S5b: renderSidebarRanking (인기 검증 랭킹 1-10) was RETIRED —
    // the S5a 확산 성장 Top 5 panel covers the sidebar-ranking role, without
    // the old rows' verdict dots. The .rank-* classes stay in main.css for the
    // trending panel and 최근 본 검증 rows.

    // RECENT-VIEWED: render the "최근 본 검증" strip on the detail screen. Reads the
    // stored result_ids, EXCLUDES the currently-open id, resolves each against the
    // serverHotTopicResults pool (skips stale ids no longer in the pool), and emits
    // .rank-row items (reusing the sidebar row shape + the unified label/verdict
    // helpers) carrying the same data-topic-* attrs so a click re-opens detail via
    // openTopicCard. Section stays [hidden] unless >=1 resolvable OTHER card exists.
    function renderRecentViewed(currentId) {
      const el = document.getElementById("recentViewed");
      if (!el) return;
      const current = Number(currentId);
      const ids = safeReadRecentViewed().filter((id) => id !== current);
      const items = ids.map((id) => {
        const idx = serverHotTopicResults.findIndex((r) => Number(r?.result_id) === id);
        if (idx < 0) return "";  // stale id (pool refreshed) -> skip
        const card = topicCardFromResult(serverHotTopicResults[idx], idx, "server");
        return `
        <div class="rank-row recent-viewed-item" data-topic-key="${escapeHtml(card.key)}" data-topic-source="${escapeHtml(card.source)}" data-topic-index="${escapeHtml(card.index)}" data-topic-record-id="${escapeHtml(card.recordId)}">
          <div class="rank-body">
            <span class="rank-domain">${escapeHtml(domainDisplayLabel(cardDomainKey(card)))}</span>
            <span class="rank-title">${escapeHtml(card.title)}</span>
            <span class="rank-verdict">
              <span class="verdict-dot" style="background:${verdictDotColor(card.verdictLabel)}"></span>
              <span class="rank-verdict-text">판정 ${escapeHtml(verdictLabelKo(card.verdictLabel))}</span>
            </span>
          </div>
        </div>`;
      }).filter(Boolean);
      if (!items.length) {
        el.innerHTML = "";
        el.hidden = true;
        return;
      }
      el.innerHTML = `<div class="recent-viewed-heading">최근 본 검증</div>`
        + `<div class="recent-viewed-strip">${items.join("")}</div>`;
      el.hidden = false;
    }

    // SIDEBAR-RANK-B2: 이번 주 검증 현황 — fetch the read-only GET /stats once and
    // fill the three numbers + the MM.DD–MM.DD range. REAL counts only (no
    // hardcoded numbers). Fail-quiet: on any error the panel keeps its "–"
    // placeholders and never throws. No write, no live-analysis trigger.
    // HOME-TOP5 S5a: 확산 성장 Top 5 — one read-only GET /api/trending fetch
    // (two-snapshot outlet-count growth) fills the sidebar panel. CIRCULATION
    // only: title + N개 매체 + ↑growth/NEW — no verdict/score/판정, no verdict
    // color. Fail-quiet (the renderWeeklyStats posture): fetch error or
    // {trending:[]} (insufficient snapshot history) keeps the panel hidden and
    // never throws. Rows use plain /?result_id= hrefs, NOT the ranking's
    // data-topic-*/openTopicCard wiring — a trending representative may be
    // older than the loaded recent-50 feed pool.
    async function renderTrendingTop5() {
      if (!trendingPanelEl || !trendingListEl) return;
      try {
        const response = await fetch(`${API_BASE}/api/trending`);
        if (!response.ok) return;
        const body = await response.json();
        const rows = Array.isArray(body?.trending) ? body.trending.slice(0, 5) : [];
        // HOME-TOP5 S5b: cache the rows for the 오늘의 한 장 hero pick and
        // repaint the (idempotent) home render now that trending has landed —
        // it falls back to the two-card hero whenever the pick can't resolve.
        trendingHeroRows = rows;
        if (rows.length) renderHotTopics();
        const items = rows.map((row, i) => {
          const rid = Number(row?.representative_analysis_id);
          // MOBILE-POLISH F: trending rows come straight off GET /api/trending,
          // bypassing topicCardFromResult — strip the leading marker here too.
          const title = stripLeadingTitleMarker(row?.title) || (rid > 0 ? `기사 #${rid}` : "");
          if (!title) return "";
          const outlets = Number(row?.current_outlet_count);
          const growth = Number(row?.growth);
          const badge = row?.is_new ? " · NEW"
            : (Number.isFinite(growth) && growth > 0 ? ` · ↑${growth}` : "");
          const meta = Number.isFinite(outlets) && outlets > 0
            ? `${outlets}개 매체${badge}`
            : badge.replace(" · ", "");
          const titleHtml = rid > 0
            ? `<a class="rank-title" href="/?result_id=${encodeURIComponent(rid)}">${escapeHtml(title)}</a>`
            : `<span class="rank-title">${escapeHtml(title)}</span>`;
          return `
        <li class="rank-row">
          <span class="rank-num">${i + 1}</span>
          <div class="rank-body">
            ${titleHtml}
            ${meta ? `<span class="rank-domain">${escapeHtml(meta)}</span>` : ""}
          </div>
        </li>`;
        }).filter(Boolean);
        if (!items.length) return;
        trendingListEl.innerHTML = items.join("");
        trendingPanelEl.hidden = false;
      } catch (error) {
        // fail-silent: the trending panel is optional; the sidebar must never break
      }
    }

    async function renderWeeklyStats() {
      if (!statTotalEl && !statOfficialEl && !statDraftEl) return;
      try {
        const response = await fetch(`${API_BASE}/stats`);
        if (!response.ok) return;
        const body = await response.json();
        if (!body || body.status !== "ok") return;
        if (statTotalEl) statTotalEl.textContent = String(body.total ?? "–");
        if (statOfficialEl) statOfficialEl.textContent = String(body.official ?? "–");
        if (statDraftEl) statDraftEl.textContent = String(body.draft ?? "–");
        // HOME-SECTION-FIX A1 / MOBILE-POLISH B: wire the top utility-bar counts
        // from the same payload — 이번 주 = total (the 7-day window), 누적 검증 =
        // cumulative_total (the unbounded corpus count). Fail-quiet leaves the "—"
        // if /stats errors. The 누적 clause stays hidden unless a real finite
        // number arrives, so an older payload without the field shows 이번 주 only
        // rather than a fabricated or dashed-out total.
        if (utilityUpdateCountEl && Number.isFinite(Number(body.total))) {
          utilityUpdateCountEl.textContent = String(body.total);
        }
        if (utilityTotalCountEl && utilityCumulativeClauseEl
            && body.cumulative_total !== null && body.cumulative_total !== undefined
            && Number.isFinite(Number(body.cumulative_total))) {
          utilityTotalCountEl.textContent = String(body.cumulative_total);
          utilityCumulativeClauseEl.hidden = false;
        }
        if (statRangeEl && body.range_start && body.range_end) {
          // ISO YYYY-MM-DD → MM.DD; fall back to the raw strings if malformed.
          const mmdd = (iso) => {
            const m = String(iso).match(/^\d{4}-(\d{2})-(\d{2})/);
            return m ? `${m[1]}.${m[2]}` : String(iso);
          };
          statRangeEl.textContent = `${mmdd(body.range_start)}–${mmdd(body.range_end)}`;
        }
      } catch (_) {
        // fail-quiet — leave the "–" placeholders.
      }
    }

    // Phase 2 M3: project each full result down to only the fields the topic
    // card / history row UI actually reads. Heavy nested arrays (evidence
    // snippets, evidence sources, source candidates, contradiction checks,
    // bias framing, claim evidence map) are dropped and rehydrated from
    // GET /history/{result_id} when a record is opened.
    function buildSlimResultSummary(result) {
      const safeResult = result && typeof result === "object" ? result : {};
      const verification = safeResult.verification_card || {};
      const summary = verification.source_reliability_summary || {};
      const fullDebug = verification.debug_summary || safeResult.debug_summary || {};
      const evidenceQuality = fullDebug.evidence_quality_summary
        || verification.evidence_quality_summary
        || safeResult.evidence_quality_summary
        || {};
      const evidenceStrength = fullDebug.evidence_strength_summary
        || verification.evidence_strength_summary
        || {};
      const slimDebug = {
        needs_human_review: fullDebug.needs_human_review,
        official_body_matches: fullDebug.official_body_matches,
        official_body_candidates: fullDebug.official_body_candidates,
        official_bodies_fetched: fullDebug.official_bodies_fetched,
        official_detail_pages_fetched_count: fullDebug.official_detail_pages_fetched_count,
        official_body_success_count: fullDebug.official_body_success_count,
        official_body_fail_count: fullDebug.official_body_fail_count,
        evidence_quality_summary: evidenceQuality,
        evidence_strength_summary: evidenceStrength,
        stable_history_key: fullDebug.stable_history_key,
        history_key: fullDebug.history_key,
        history_action: fullDebug.history_action,
        review_queue_key: fullDebug.review_queue_key,
        review_queue_action: fullDebug.review_queue_action,
      };
      const slimSummary = {
        official_detail_available: summary.official_detail_available,
        official_candidate_count: summary.official_candidate_count,
        official_evidence_status: summary.official_evidence_status,
        official_detail_status: summary.official_detail_status,
      };
      return {
        result_id: safeResult.result_id || null,
        title: safeResult.title || "",
        original_url: safeResult.original_url || "",
        topic: safeResult.topic || "",
        claim_text: verification.claim_text || safeResult.claim_text || "",
        verdict_label: verification.verdict_label || safeResult.verdict_label || "",
        review_status: verification.review_status || safeResult.review_status || "",
        evidence_summary: verification.evidence_summary || safeResult.evidence_summary || "",
        policy_confidence: {
          policy_confidence_score: safeResult.policy_confidence?.policy_confidence_score,
        },
        policy_impact: {
          impact_level: safeResult.policy_impact?.impact_level,
          impact_direction: safeResult.policy_impact?.impact_direction,
        },
        final_decision: {
          policy_alert_level: safeResult.final_decision?.policy_alert_level,
          market_signal: safeResult.final_decision?.market_signal,
          decision_summary: safeResult.final_decision?.decision_summary,
        },
        verification_card: {
          claim_text: verification.claim_text || "",
          verdict_label: verification.verdict_label || "",
          review_status: verification.review_status || "",
          evidence_summary: verification.evidence_summary || "",
          evidence_quality_summary: evidenceQuality,
          source_reliability_summary: slimSummary,
          debug_summary: slimDebug,
        },
        debug_summary: slimDebug,
        evidence_quality_summary: evidenceQuality,
        slim: true,
      };
    }

    function buildSlimHistoryRecord(record) {
      const safeRecord = record && typeof record === "object" ? record : {};
      const results = getHistoryResults(safeRecord);
      const summaryResults = results.map((result) => buildSlimResultSummary(result));
      const firstResultId = summaryResults.find((r) => r.result_id)?.result_id || null;
      return {
        id: safeRecord.id,
        stable_history_key: safeRecord.stable_history_key,
        query: safeRecord.query || "",
        max_news: safeRecord.max_news,
        analyzed_at: safeRecord.analyzed_at,
        created_at: safeRecord.created_at,
        history_action: safeRecord.history_action,
        result_id: safeRecord.result_id || firstResultId,
        highest_alert: safeRecord.highest_alert,
        average_confidence: safeRecord.average_confidence,
        high_impact_count: safeRecord.high_impact_count,
        results_count: safeRecord.results_count != null
          ? safeRecord.results_count
          : summaryResults.length,
        evidence_strength_summary: safeRecord.evidence_strength_summary,
        evidence_quality_summary: safeRecord.evidence_quality_summary,
        summary_results: summaryResults,
      };
    }

    function buildSlimReviewItem(item) {
      const safeItem = item && typeof item === "object" ? item : {};
      const results = getHistoryResults(safeItem);
      const summaryResults = results.slice(0, 2).map((result) => buildSlimResultSummary(result));
      const firstResultId = summaryResults.find((r) => r.result_id)?.result_id
        || safeItem.result_id
        || null;
      return {
        id: safeItem.id,
        stable_history_key: safeItem.stable_history_key,
        query: safeItem.query || "",
        max_news: safeItem.max_news,
        title: safeItem.title || "",
        warning_level: safeItem.warning_level,
        confidence_score: safeItem.confidence_score,
        review_status: safeItem.review_status,
        reviewer_status: safeItem.reviewer_status || "pending",
        evidence_quality_summary: safeItem.evidence_quality_summary,
        evidence_strength_summary: safeItem.evidence_strength_summary,
        created_at: safeItem.created_at,
        updated_at: safeItem.updated_at,
        result_id: firstResultId,
        summary_results: summaryResults,
      };
    }

    function safeWriteLocalHistory(records) {
      const slimmed = (records || []).slice(0, LOCAL_HISTORY_LIMIT).map(buildSlimHistoryRecord);
      const serialized = JSON.stringify(slimmed);
      safeStorage.set(LOCAL_HISTORY_KEY, serialized, {
        onQuotaTrim(currentValue) {
          // First retry: try writing only the most recent half.
          const parsed = typeof currentValue === "string" ? JSON.parse(currentValue) : currentValue;
          return trimArrayPayload(parsed);
        },
      });
    }

    // RECENT-VIEWED: read/write/push for the detail-screen click history. Mirrors the
    // safeReadLocalHistory / safeWriteLocalHistory pattern with the NEW key; the stored
    // payload is a plain array of numeric result_ids (most-recent-first).
    function safeReadRecentViewed() {
      try {
        const raw = safeStorage.get(RECENT_VIEWED_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return [];
        return parsed.map((id) => Number(id)).filter((id) => Number.isFinite(id));
      } catch (error) {
        console.warn("safeReadRecentViewed parse failed; clearing storage", error);
        safeStorage.remove(RECENT_VIEWED_KEY);
        return [];
      }
    }

    function safeWriteRecentViewed(ids) {
      const capped = (Array.isArray(ids) ? ids : []).slice(0, RECENT_VIEWED_LIMIT);
      const serialized = JSON.stringify(capped);
      safeStorage.set(RECENT_VIEWED_KEY, serialized, {
        onQuotaTrim(currentValue) {
          const parsed = typeof currentValue === "string" ? JSON.parse(currentValue) : currentValue;
          return trimArrayPayload(parsed);
        },
      });
    }

    // Record an opened card at the FRONT (dedupe-to-front, cap 8). The just-opened id
    // is stored; renderRecentViewed excludes it from what it DISPLAYS, so the current
    // card never appears in its own strip while remaining recorded for the next detail.
    function pushRecentViewed(id) {
      const numId = Number(id);
      if (!Number.isFinite(numId)) return;
      const next = [numId, ...safeReadRecentViewed().filter((existing) => existing !== numId)]
        .slice(0, RECENT_VIEWED_LIMIT);
      safeWriteRecentViewed(next);
    }

    function safeReadReviewQueue() {
      try {
        const raw = safeStorage.get(REVIEW_QUEUE_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return [];
        return parsed.map((item, index) => {
          const safeItem = item && typeof item === "object" ? item : {};
          const stableKey = safeItem.stable_history_key
            || buildStableHistoryKey(safeItem.query || "", getHistoryResults(safeItem));
          return {
            ...safeItem,
            stable_history_key: stableKey,
            id: safeItem.id || stableKey || `review-${index}`,
            reviewer_status: safeItem.reviewer_status || "pending",
          };
        });
      } catch (error) {
        console.warn("safeReadReviewQueue parse failed; clearing storage", error);
        safeStorage.remove(REVIEW_QUEUE_KEY);
        return [];
      }
    }

    function safeWriteReviewQueue(items) {
      const slimmed = (items || []).slice(0, REVIEW_QUEUE_LIMIT).map(buildSlimReviewItem);
      const serialized = JSON.stringify(slimmed);
      safeStorage.set(REVIEW_QUEUE_KEY, serialized, {
        onQuotaTrim(currentValue) {
          const parsed = typeof currentValue === "string" ? JSON.parse(currentValue) : currentValue;
          return trimArrayPayload(parsed);
        },
      });
    }

    function normalizeHistoryText(value) {
      return sanitizeDisplayText(value || "")
        .toLowerCase()
        .replace(/\s+/g, " ")
        .trim();
    }

    function canonicalHistoryUrl(value) {
      const raw = String(value || "").trim();
      if (!raw) return "";
      try {
        const url = new URL(raw, window.location.origin);
        url.hash = "";
        const removableParams = [
          "utm_source",
          "utm_medium",
          "utm_campaign",
          "utm_term",
          "utm_content",
          "fbclid",
          "gclid",
        ];
        removableParams.forEach((param) => url.searchParams.delete(param));
        const params = Array.from(url.searchParams.entries()).sort(([a], [b]) => a.localeCompare(b));
        url.search = "";
        params.forEach(([key, val]) => url.searchParams.append(key, val));
        return url.toString().replace(/\/$/, "");
      } catch (error) {
        return raw.replace(/#.*$/, "").replace(/\/$/, "");
      }
    }

    function historyDomain(value) {
      try {
        return new URL(value || "", window.location.origin).hostname.replace(/^www\./, "").toLowerCase();
      } catch (error) {
        return "";
      }
    }

    function topHistoryResult(results) {
      return Array.isArray(results) && results.length ? results[0] : {};
    }

    function buildStableHistoryKey(query, results) {
      const normalizedQuery = normalizeHistoryText(query);
      const top = topHistoryResult(results);
      const url = canonicalHistoryUrl(top.original_url || top.url || top.link || "");
      if (url) {
        return `q:${normalizedQuery}|url:${url}`;
      }
      const title = normalizeHistoryText(top.title || top.claim_text || "");
      const source = normalizeHistoryText(
        top.source
          || top.publisher
          || historyDomain(top.original_url || top.url || "")
      );
      return `q:${normalizedQuery}|title:${title || "untitled"}|source:${source || "unknown"}`;
    }

    function withHistoryDebug(responseData, stableHistoryKey, historyAction) {
      const cloned = {
        ...(responseData || {}),
        results: Array.isArray(responseData?.results)
          ? responseData.results.map((result) => {
              const nextResult = { ...(result || {}) };
              const verification = { ...(nextResult.verification_card || {}) };
              const debug = {
                ...(verification.debug_summary || nextResult.debug_summary || {}),
                stable_history_key: stableHistoryKey,
                history_key: stableHistoryKey,
                history_action: historyAction,
              };
              verification.debug_summary = debug;
              nextResult.debug_summary = debug;
              nextResult.verification_card = verification;
              return nextResult;
            })
          : [],
      };
      return cloned;
    }

    // ===== C9 — Local review queue & reviewer feedback =====
    function reviewStatusLabel(status) {
      const labels = {
        pending: "대기",
        approved: "승인됨",
        rejected: "반려됨",
        needs_more_info: "추가 확인 필요",
      };
      return labels[status] || status || "대기";
    }

    function reviewStatusClass(status) {
      if (status === "approved") return "review-approved";
      if (status === "rejected") return "review-rejected";
      if (status === "needs_more_info") return "review-needs-more";
      return "review-pending";
    }

    function reviewerActionStatusLabel(status) {
      return REVIEW_ACTION_LABELS[status] || REVIEW_ACTION_LABELS.unreviewed;
    }

    function safeReadReviewerActions() {
      try {
        const raw = safeStorage.get(REVIEW_ACTION_KEY);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
      } catch (error) {
        console.warn("safeReadReviewerActions parse failed; clearing storage", error);
        safeStorage.remove(REVIEW_ACTION_KEY);
        return {};
      }
    }

    function safeWriteReviewerActions(actions) {
      const serialized = JSON.stringify(actions || {});
      safeStorage.set(REVIEW_ACTION_KEY, serialized, {
        onQuotaTrim(currentValue) {
          const parsed = typeof currentValue === "string" ? JSON.parse(currentValue) : currentValue;
          return trimMapPayload(parsed);
        },
      });
    }

    function reviewActionKeyForResult(result, query = "") {
      return buildStableHistoryKey(query || currentReportContext?.query || queryInput?.value || "", [result]);
    }

    function getReviewerAction(result, query = "") {
      const key = reviewActionKeyForResult(result, query);
      const saved = safeReadReviewerActions()[key] || {};
      return {
        key,
        review_status: saved.review_status || "unreviewed",
        reviewer_note: saved.reviewer_note || "",
        reviewed_at: saved.reviewed_at || "",
      };
    }

    function formatReviewerSavedAt(value) {
      if (!value) return "없음";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "없음";
      const pad = (num) => String(num).padStart(2, "0");
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }

    function saveReviewerActionForKey(key, status, note) {
      if (!key) return;
      const actions = safeReadReviewerActions();
      actions[key] = {
        review_status: status || "unreviewed",
        reviewer_note: note || "",
        reviewed_at: new Date().toISOString(),
      };
      safeWriteReviewerActions(actions);
    }

    function clearReviewerActionForKey(key) {
      if (!key) return;
      const actions = safeReadReviewerActions();
      delete actions[key];
      safeWriteReviewerActions(actions);
    }

    function requiresHumanReview(results) {
      return (results || []).some((result) => {
        const verification = result?.verification_card || result || {};
        const debug = verification.debug_summary || result?.debug_summary || {};
        const verdict = verification.verdict_label || result?.verdict_label || "";
        const reviewStatus = verification.review_status || result?.review_status || "";
        return Boolean(debug.needs_human_review)
          || [
            "draft_high_risk_review",
            "draft_needs_review",
            "draft_disputed",
            "draft_needs_official_confirmation",
            "draft_needs_context",
          ].includes(verdict)
          || [
            "draft_high_risk_review",
            "draft_needs_review",
            "draft_disputed",
            "draft_needs_official_confirmation",
          ].includes(reviewStatus);
      });
    }

    function buildReviewQueueItem(query, maxNews, responseData, stableHistoryKey, existingItem) {
      const results = Array.isArray(responseData?.results) ? responseData.results : [];
      const metrics = computeMetrics(results);
      const evidence = aggregateEvidenceSummaries(results);
      const top = topHistoryResult(results);
      const verification = top?.verification_card || top || {};
      const now = new Date().toISOString();
      return {
        ...(existingItem || {}),
        id: stableHistoryKey,
        stable_history_key: stableHistoryKey,
        query,
        max_news: maxNews,
        title: top?.title || verification.claim_text || "분석 결과",
        warning_level: metrics.highest,
        confidence_score: metrics.averageConfidence,
        review_status: verification.verdict_label || verification.review_status || "draft_needs_review",
        reviewer_status: existingItem?.reviewer_status || "pending",
        evidence_quality_summary: evidence.quality,
        evidence_strength_summary: evidence.strength,
        created_at: existingItem?.created_at || now,
        updated_at: now,
        response: responseData || { status: "ok", results },
      };
    }

    function upsertReviewQueue(query, maxNews, responseData, stableHistoryKey) {
      const results = Array.isArray(responseData?.results) ? responseData.results : [];
      if (!requiresHumanReview(results)) {
        renderReviewQueue(safeReadReviewQueue());
        return { action: "skipped", stableHistoryKey };
      }

      const items = safeReadReviewQueue();
      const existing = items.find((item) => item.stable_history_key === stableHistoryKey);
      const action = existing ? "updated" : "inserted";
      const item = buildReviewQueueItem(query, maxNews, responseData, stableHistoryKey, existing);
      const nextItems = [item, ...items.filter((entry) => entry.stable_history_key !== stableHistoryKey)];
      safeWriteReviewQueue(nextItems);
      renderReviewQueue(nextItems);
      return { action, stableHistoryKey };
    }

    function withReviewDebug(responseData, stableHistoryKey, reviewQueueAction) {
      return {
        ...(responseData || {}),
        results: Array.isArray(responseData?.results)
          ? responseData.results.map((result) => {
              const nextResult = { ...(result || {}) };
              const verification = { ...(nextResult.verification_card || {}) };
              const debug = {
                ...(verification.debug_summary || nextResult.debug_summary || {}),
                review_queue_key: stableHistoryKey,
                review_queue_action: reviewQueueAction,
              };
              verification.debug_summary = debug;
              nextResult.debug_summary = debug;
              nextResult.verification_card = verification;
              return nextResult;
            })
          : [],
      };
    }

    function cloneAnalysisData(data) {
      try {
        return JSON.parse(JSON.stringify(data || { status: "ok", results: [] }));
      } catch (error) {
        return { ...(data || {}), results: Array.isArray(data?.results) ? [...data.results] : [] };
      }
    }

    function evidenceQualityLabel(score) {
      const value = Number(score || 0);
      if (value >= 75) return "strong";
      if (value >= 45) return "medium";
      return "weak";
    }

    function collectResultIdentityTokens(result) {
      const tokens = [];
      const addToken = (kind, value, weight) => {
        const raw = String(value || "").trim();
        if (!raw) return;
        tokens.push({ key: `${kind}:${raw}`, kind, weight });
      };
      const addUrl = (value) => {
        const url = canonicalHistoryUrl(value || "");
        if (url) {
          addToken("url", url, 4);
          addToken("domain", historyDomain(url), 1);
        }
      };
      const addTitle = (value) => {
        const title = normalizeHistoryText(value || "");
        if (title && title !== "untitled") addToken("title", title, 3);
      };
      const addSource = (value) => {
        const source = normalizeHistoryText(value || "");
        if (source) addToken("source", source, 1);
      };

      addTitle(result?.title || result?.claim_text || "");
      addUrl(result?.original_url || result?.url || result?.link || "");
      addSource(result?.source || result?.publisher || "");

      const verification = result?.verification_card || {};
      [
        ...(Array.isArray(verification.evidence_sources) ? verification.evidence_sources : []),
        ...(Array.isArray(result?.source_candidates) ? result.source_candidates : []),
      ].forEach((source) => {
        addTitle(source?.title || source?.source_title || "");
        addUrl(source?.url || source?.source_url || "");
        addSource(source?.publisher || source?.source_name || source?.source_type || "");
      });

      [
        ...(Array.isArray(verification.evidence_snippets) ? verification.evidence_snippets : []),
        ...(Array.isArray(result?.evidence_snippets) ? result.evidence_snippets : []),
      ].forEach((snippet) => {
        addTitle(snippet?.source_title || "");
        addUrl(snippet?.source_url || "");
        addSource(snippet?.publisher || "");
      });

      const unique = new Map();
      tokens.forEach((token) => {
        const existing = unique.get(token.key);
        if (!existing || token.weight > existing.weight) unique.set(token.key, token);
      });
      return Array.from(unique.values()).sort((a, b) => a.key.localeCompare(b.key));
    }

    function reviewFeedbackMatchScore(result, query, queueItem) {
      const currentKey = buildStableHistoryKey(query, [result]);
      if (queueItem?.stable_history_key && queueItem.stable_history_key === currentKey) {
        return 100;
      }
      const currentTokens = collectResultIdentityTokens(result);
      const currentMap = new Map(currentTokens.map((token) => [token.key, token.weight]));
      const storedResults = getHistoryResults(queueItem);
      const storedTokens = storedResults.flatMap((storedResult) => collectResultIdentityTokens(storedResult));
      let score = 0;
      storedTokens.forEach((token) => {
        if (currentMap.has(token.key)) {
          score += Math.max(Number(token.weight || 0), Number(currentMap.get(token.key) || 0));
        }
      });
      return score;
    }

    function findReviewFeedbackDecision(result, query) {
      const queue = safeReadReviewQueue()
        .filter((item) => ["approved", "rejected", "needs_more_info"].includes(item?.reviewer_status))
        .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));

      let best = null;
      queue.forEach((item) => {
        const score = reviewFeedbackMatchScore(result, query, item);
        if (score >= 3 && (!best || score > best.score)) {
          best = { item, score };
        }
      });
      return best;
    }

    function collectEvidenceSnippets(result) {
      const verification = result?.verification_card || {};
      const snippets = Array.isArray(verification.evidence_snippets)
        ? verification.evidence_snippets
        : (Array.isArray(result?.evidence_snippets) ? result.evidence_snippets : []);
      return snippets.map((snippet) => ({ ...(snippet || {}) }));
    }

    function summarizeEvidenceQuality(snippets) {
      const summary = { strong: 0, medium: 0, weak: 0, average_evidence_quality_score: 0 };
      let total = 0;
      const list = Array.isArray(snippets) ? snippets : [];
      list.forEach((snippet) => {
        const label = snippet.evidence_quality_label || evidenceQualityLabel(snippet.evidence_quality_score);
        if (label === "strong") summary.strong += 1;
        else if (label === "medium") summary.medium += 1;
        else summary.weak += 1;
        total += Number(snippet.evidence_quality_score || 0);
      });
      summary.average_evidence_quality_score = list.length ? Math.round(total / list.length) : 0;
      return summary;
    }

    function summarizeClaimEvidenceQuality(claims, snippets) {
      const claimList = Array.isArray(claims) ? claims : [];
      return claimList.map((claim, index) => {
        const related = (snippets || []).filter((snippet) => Number(snippet.claim_index) === index);
        const quality = summarizeEvidenceQuality(related);
        const bestScore = related.reduce((best, snippet) => Math.max(best, Number(snippet.evidence_quality_score || 0)), 0);
        return {
          claim_index: index,
          claim_text: claim,
          strong_evidence_count: quality.strong,
          medium_evidence_count: quality.medium,
          weak_evidence_count: quality.weak,
          best_evidence_score: bestScore,
          evidence_quality_summary: quality,
        };
      });
    }

    function clampScore(value) {
      return Math.max(0, Math.min(100, Math.round(Number(value || 0))));
    }

    function recalibrateDecisionWithReviewFeedback(result, debug, qualitySummary, reviewerStatus) {
      const decision = { ...(result.final_decision || {}) };
      const confidence = result.policy_confidence || {};
      const impact = result.policy_impact || {};
      const strength = debug.evidence_strength_summary || {};
      const totalStrength = Number(strength.strong || 0) + Number(strength.medium || 0) + Number(strength.weak || 0);
      const weightedStrength = totalStrength
        ? clampScore(((Number(strength.strong || 0) * 3) + (Number(strength.medium || 0) * 2) + Number(strength.weak || 0)) / (totalStrength * 3) * 100)
        : 0;
      const sourceTrust = clampScore(debug.source_trust_score ?? decision.source_trust_score ?? 0);
      const contradictionAdjustment = Number(debug.contradiction_adjustment ?? decision.contradiction_adjustment ?? 0);
      const humanAdjustment = reviewerStatus === "approved" ? 15 : (reviewerStatus === "rejected" ? -30 : -10);
      const finalScore = clampScore(
        Number(qualitySummary.average_evidence_quality_score || 0) * 0.35
        + weightedStrength * 0.25
        + sourceTrust * 0.20
        + Number(confidence.policy_confidence_score || 0) * 0.20
        + humanAdjustment
        + contradictionAdjustment
      );
      let alert = "LOW";
      if (reviewerStatus === "rejected") {
        alert = String(impact.impact_level || "").toLowerCase() === "high" ? "WATCH" : "LOW";
      } else if (
        reviewerStatus === "approved"
        && finalScore >= 70
        && String(impact.impact_level || "").toLowerCase() === "high"
      ) {
        alert = "HIGH";
      } else if (finalScore >= 45 || String(impact.impact_level || "").toLowerCase() === "high") {
        alert = "WATCH";
      }
      decision.policy_alert_level = alert;
      decision.final_score = finalScore;
      decision.source_trust_score = sourceTrust;
      decision.human_feedback_adjustment = humanAdjustment;
      decision.contradiction_adjustment = contradictionAdjustment;
      decision.evidence_weighted_score = weightedStrength;
      decision.evidence_quality_score = qualitySummary.average_evidence_quality_score || 0;
      decision.decision_reasons = [
        ...(Array.isArray(decision.decision_reasons) ? decision.decision_reasons : []),
        `human review feedback applied: ${reviewerStatus}`,
        `calibrated final score ${finalScore}`,
      ];
      return decision;
    }

    function applyReviewDecisionToSnippets(snippets, reviewerStatus) {
      return (snippets || []).map((snippet) => {
        const next = { ...(snippet || {}) };
        const currentScore = Number(next.evidence_quality_score || 0);
        if (reviewerStatus === "approved") {
          next.evidence_quality_score = Math.min(100, currentScore + 12);
          next.review_feedback_note = "approved_boost";
        } else if (reviewerStatus === "rejected") {
          next.evidence_quality_score = Math.min(currentScore, 35);
          next.review_feedback_note = "rejected_penalty";
        } else {
          next.evidence_quality_score = currentScore;
          next.review_feedback_note = "needs_more_info_no_boost";
        }
        next.evidence_quality_label = evidenceQualityLabel(next.evidence_quality_score);
        next.human_review_feedback = reviewerStatus;
        return next;
      });
    }

    function applyHumanReviewFeedback(responseData, query) {
      const cloned = cloneAnalysisData(responseData);
      cloned.results = Array.isArray(cloned.results) ? cloned.results.map((result) => {
        const decision = findReviewFeedbackDecision(result, query);
        if (!decision) return result;

        const reviewerStatus = decision.item.reviewer_status;
        const nextResult = { ...(result || {}) };
        const verification = { ...(nextResult.verification_card || {}) };
        const previousDebug = verification.debug_summary || nextResult.debug_summary || {};
        if (
          previousDebug.human_review_feedback === "applied"
          && previousDebug.review_feedback_status === reviewerStatus
        ) {
          return nextResult;
        }

        const snippets = applyReviewDecisionToSnippets(collectEvidenceSnippets(nextResult), reviewerStatus);
        const qualitySummary = summarizeEvidenceQuality(snippets);
        const claims = Array.isArray(nextResult.claims)
          ? nextResult.claims
          : (Array.isArray(verification.claims) ? verification.claims : []);
        const claimQuality = summarizeClaimEvidenceQuality(claims, snippets);
        const extractionSummary = {
          ...(nextResult.evidence_extraction_summary || verification.evidence_extraction_summary || {}),
          evidence_quality_summary: qualitySummary,
          total_strong_evidence: qualitySummary.strong,
          total_medium_evidence: qualitySummary.medium,
          total_weak_evidence: qualitySummary.weak,
          average_evidence_quality_score: qualitySummary.average_evidence_quality_score,
        };
        const debug = {
          ...previousDebug,
          human_review_feedback: "applied",
          approved_boost: reviewerStatus === "approved",
          rejected_penalty: reviewerStatus === "rejected",
          review_decision_source: "queue",
          review_feedback_status: reviewerStatus,
          review_feedback_key: decision.item.stable_history_key || decision.item.id || "-",
          review_feedback_match_score: decision.score,
          evidence_quality_summary: qualitySummary,
          total_strong_evidence: qualitySummary.strong,
          total_medium_evidence: qualitySummary.medium,
          total_weak_evidence: qualitySummary.weak,
          average_evidence_quality_score: qualitySummary.average_evidence_quality_score,
        };
        const calibratedDecision = recalibrateDecisionWithReviewFeedback(
          nextResult,
          debug,
          qualitySummary,
          reviewerStatus
        );
        debug.final_score = calibratedDecision.final_score;
        debug.source_trust_score = calibratedDecision.source_trust_score;
        debug.human_feedback_adjustment = calibratedDecision.human_feedback_adjustment;
        debug.contradiction_adjustment = calibratedDecision.contradiction_adjustment;

        verification.evidence_snippets = snippets;
        verification.evidence_extraction_summary = extractionSummary;
        verification.claim_evidence_quality_summary = claimQuality;
        verification.debug_summary = debug;
        nextResult.evidence_snippets = snippets;
        nextResult.evidence_extraction_summary = extractionSummary;
        nextResult.claim_evidence_quality_summary = claimQuality;
        nextResult.evidence_quality_summary = qualitySummary;
        nextResult.final_decision = calibratedDecision;
        nextResult.debug_summary = debug;
        nextResult.verification_card = verification;
        return nextResult;
      }) : [];
      return cloned;
    }

    // ===== C10 — History-row mapping & aggregation =====
    function getResultDebugSummary(result) {
      const verification = result?.verification_card || {};
      return verification.debug_summary || result?.debug_summary || {};
    }

    function addCount(target, source, key) {
      target[key] = Number(target[key] || 0) + Number(source?.[key] || 0);
    }

    function aggregateEvidenceSummaries(results) {
      const strength = { strong: 0, medium: 0, weak: 0, none: 0 };
      const quality = { strong: 0, medium: 0, weak: 0 };
      let qualityTotal = 0;
      let qualityCount = 0;

      (results || []).forEach((result) => {
        const debug = getResultDebugSummary(result);
        const strengthPart = debug.evidence_strength_summary || {};
        addCount(strength, strengthPart, "strong");
        addCount(strength, strengthPart, "medium");
        addCount(strength, strengthPart, "weak");
        addCount(strength, strengthPart, "none");

        const qualityPart = debug.evidence_quality_summary || result?.evidence_quality_summary || {};
        addCount(quality, qualityPart, "strong");
        addCount(quality, qualityPart, "medium");
        addCount(quality, qualityPart, "weak");
        const average = Number(qualityPart.average_evidence_quality_score ?? debug.average_evidence_quality_score ?? 0);
        if (average > 0) {
          qualityTotal += average;
          qualityCount += 1;
        }
      });

      quality.average_evidence_quality_score = qualityCount
        ? Math.round(qualityTotal / qualityCount)
        : 0;

      return { strength, quality };
    }

    function buildLocalHistoryRecord(query, maxNews, responseData) {
      const results = Array.isArray(responseData?.results) ? responseData.results : [];
      const metrics = computeMetrics(results);
      const evidence = aggregateEvidenceSummaries(results);
      const stableHistoryKey = buildStableHistoryKey(query, results);
      return {
        id: stableHistoryKey,
        stable_history_key: stableHistoryKey,
        query,
        max_news: maxNews,
        analyzed_at: new Date().toISOString(),
        highest_alert: metrics.highest,
        average_confidence: metrics.averageConfidence,
        high_impact_count: metrics.highImpactCount,
        results_count: metrics.count,
        evidence_strength_summary: evidence.strength,
        evidence_quality_summary: evidence.quality,
        response: responseData || { status: "ok", results },
      };
    }

    function saveLocalAnalysisHistory(query, maxNews, responseData) {
      const records = safeReadLocalHistory();
      const rawResults = Array.isArray(responseData?.results) ? responseData.results : [];
      const stableHistoryKey = buildStableHistoryKey(query, rawResults);
      const existing = records.find((item) => {
        const itemKey = item.stable_history_key || buildStableHistoryKey(item.query || "", getHistoryResults(item));
        return itemKey === stableHistoryKey;
      });
      const historyAction = existing ? "updated" : "inserted";
      const stableResponseData = withHistoryDebug(responseData, stableHistoryKey, historyAction);
      const record = buildLocalHistoryRecord(query, maxNews, stableResponseData);
      record.history_action = historyAction;
      const deduped = records.filter((item) => {
        const itemKey = item.stable_history_key || buildStableHistoryKey(item.query || "", getHistoryResults(item));
        return itemKey !== stableHistoryKey;
      });
      safeWriteLocalHistory([record, ...deduped].slice(0, LOCAL_HISTORY_LIMIT));
      currentHistoryId = record.id;
      renderHistory(safeReadLocalHistory());
      return { record, responseData: stableResponseData, historyAction, stableHistoryKey };
    }

    function renderReviewQueue(items) {
      const filter = reviewFilterEl?.value || "all";
      const allItems = Array.isArray(items) ? items : [];
      const visibleItems = allItems.filter((item) => {
        return filter === "all" || (item.reviewer_status || "pending") === filter;
      });

      if (!visibleItems.length) {
        reviewQueueEl.innerHTML = '<div class="empty-state">검토 큐에 표시할 항목이 없습니다.</div>';
        return;
      }

      reviewQueueEl.innerHTML = visibleItems.map((item, index) => {
        const quality = item.evidence_quality_summary || {};
        const status = item.reviewer_status || "pending";
        const selected = item.id && item.id === currentReviewId;
        return `
          <div class="history-row ${selected ? "selected" : ""}" data-review-id="${escapeHtml(item.id || "")}">
            <div class="history-id">Q${escapeHtml(index + 1)}</div>
            <div>
              <strong>${escapeHtml(item.query || item.title || "검토 항목")}</strong>
              <span class="review-status ${reviewStatusClass(status)}">${escapeHtml(reviewStatusLabel(status))}</span>
              ${selected ? '<span class="current-badge">현재 표시 중</span>' : ""}
              <div class="history-meta">
                <span class="label">제목:</span> ${escapeHtml(item.title || "-")}
                <br>
                <span class="label">경고:</span> ${escapeHtml(formatAlert(item.warning_level || "WATCH"))}
                &nbsp; <span class="label">신뢰도:</span> ${escapeHtml(item.confidence_score ?? "-")}
                &nbsp; <span class="label">초안 상태:</span> ${escapeHtml(formatVerdict(item.review_status))}
                <br>
                <span class="label">evidence quality:</span>
                ${escapeHtml(formatEvidenceSummaryLabel(quality))}, 평균 ${escapeHtml(quality.average_evidence_quality_score ?? 0)}
                <br>
                <span class="label">생성:</span> ${escapeHtml(item.created_at || "-")}
                &nbsp; <span class="label">갱신:</span> ${escapeHtml(item.updated_at || "-")}
              </div>
            </div>
            <div class="history-actions">
              <button class="review-action approve" type="button" data-review-action="approved" data-review-id="${escapeHtml(item.id || "")}">승인</button>
              <button class="review-action reject" type="button" data-review-action="rejected" data-review-id="${escapeHtml(item.id || "")}">반려</button>
              <button class="review-action needs-more" type="button" data-review-action="needs_more_info" data-review-id="${escapeHtml(item.id || "")}">추가 확인 필요</button>
            </div>
          </div>
        `;
      }).join("");
    }

    // Phase 2 M3: hydrate slim record/queue entries to full results via the
    // backend so the UI can render with the same shape it always has. Falls
    // back gracefully to whatever lightweight summary is in localStorage so
    // history never disappears when the network or backend is unavailable.
    function resolveResultIdFromRecord(record) {
      if (!record) return null;
      if (record.result_id) return Number(record.result_id) || null;
      const fromSummary = Array.isArray(record.summary_results)
        ? record.summary_results.find((r) => r && r.result_id)
        : null;
      if (fromSummary?.result_id) return Number(fromSummary.result_id) || null;
      const fromResults = Array.isArray(record.results)
        ? record.results.find((r) => r && r.result_id)
        : null;
      if (fromResults?.result_id) return Number(fromResults.result_id) || null;
      const fromResponse = Array.isArray(record.response?.results)
        ? record.response.results.find((r) => r && r.result_id)
        : null;
      if (fromResponse?.result_id) return Number(fromResponse.result_id) || null;
      return null;
    }

    async function hydrateRecordResults(record) {
      if (!record) return [];
      const cacheKey = record.id || record.stable_history_key;
      if (cacheKey && hydratedRecordCache.has(cacheKey)) {
        const cached = hydratedRecordCache.get(cacheKey);
        if (Array.isArray(cached?.results) && cached.results.length) {
          return cached.results;
        }
      }
      const existing = getHistoryResults(record);
      const alreadyFull = Array.isArray(existing) && existing.some((r) => !r?.slim);
      if (alreadyFull) {
        return existing;
      }
      const resultId = resolveResultIdFromRecord(record);
      if (!resultId) {
        return existing;
      }
      try {
        const response = await fetch(`${API_BASE}/history/${encodeURIComponent(resultId)}`);
        if (!response.ok) {
          console.warn(`history hydration failed: HTTP ${response.status}`);
          return existing;
        }
        const data = await response.json();
        const row = data?.result;
        if (!row) return existing;
        const fullResult = mapHistoryRowToResult(row);
        const hydrated = [fullResult];
        if (cacheKey) {
          hydratedRecordCache.set(cacheKey, { results: hydrated });
        }
        return hydrated;
      } catch (error) {
        console.warn("history hydration error", error);
        return existing;
      }
    }

    function parseMaybeJson(value) {
      if (value == null || value === "") return null;
      if (typeof value !== "string") return value;
      try {
        return JSON.parse(value);
      } catch (_) {
        return value;
      }
    }

    function mapHistoryRowToResult(row) {
      // /history/{id} returns the raw analysis_results row with JSON columns
      // still string-encoded; inflate them to the AnalyzeResult shape the UI
      // already knows how to render.
      const safeRow = row && typeof row === "object" ? row : {};
      const debug = parseMaybeJson(safeRow.debug_summary) || {};
      const evidenceSources = parseMaybeJson(safeRow.evidence_sources) || [];
      const sourceCandidates = parseMaybeJson(safeRow.source_candidates) || [];
      const evidenceSnippets = parseMaybeJson(safeRow.evidence_snippets) || [];
      const claimEvidenceMap = parseMaybeJson(safeRow.claim_evidence_map) || {};
      const contradictionChecks = parseMaybeJson(safeRow.contradiction_checks) || [];
      const contradictionSummary = parseMaybeJson(safeRow.contradiction_summary) || {};
      const biasFraming = parseMaybeJson(safeRow.bias_framing_analysis) || [];
      const biasFramingSummary = parseMaybeJson(safeRow.bias_framing_summary) || {};
      const sourceReliabilitySummary = parseMaybeJson(safeRow.source_reliability_summary) || {};
      const claims = parseMaybeJson(safeRow.claims) || [];
      const normalizedClaims = parseMaybeJson(safeRow.normalized_claims) || [];
      const sourceQueries = parseMaybeJson(safeRow.source_queries) || [];
      const missingContext = parseMaybeJson(safeRow.missing_context) || [];
      const marketSignal = parseMaybeJson(safeRow.market_signal);
      const verificationCard = {
        claim_text: safeRow.claim_text || "",
        verdict_label: safeRow.verdict_label || "",
        verdict_confidence: safeRow.verdict_confidence || 0,
        evidence_sources: evidenceSources,
        source_candidates: sourceCandidates,
        source_queries: sourceQueries,
        evidence_snippets: evidenceSnippets,
        claim_evidence_map: claimEvidenceMap,
        evidence_quality_summary: debug.evidence_quality_summary || {},
        evidence_strength_summary: debug.evidence_strength_summary || {},
        contradiction_checks: contradictionChecks,
        contradiction_summary: contradictionSummary,
        bias_framing_analysis: biasFraming,
        bias_framing_summary: biasFramingSummary,
        source_reliability_summary: sourceReliabilitySummary,
        source_reliability_score: safeRow.source_reliability_score || 0,
        source_reliability_reason: safeRow.source_reliability_reason || "",
        evidence_summary: safeRow.evidence_summary || "",
        missing_context: missingContext,
        last_checked_at: safeRow.last_checked_at || "",
        review_status: safeRow.review_status || "",
        debug_summary: debug,
      };
      return {
        result_id: safeRow.id || null,
        title: safeRow.title || "",
        original_url: safeRow.original_url || "",
        topic: safeRow.topic || "",
        claims,
        normalized_claims: normalizedClaims,
        source_candidates: sourceCandidates,
        source_queries: sourceQueries,
        evidence_snippets: evidenceSnippets,
        claim_evidence_map: claimEvidenceMap,
        evidence_quality_summary: debug.evidence_quality_summary || {},
        contradiction_checks: contradictionChecks,
        contradiction_summary: contradictionSummary,
        bias_framing_analysis: biasFraming,
        bias_framing_summary: biasFramingSummary,
        debug_summary: debug,
        policy_confidence: {
          policy_confidence_score: safeRow.policy_confidence_score,
          verification_strength: safeRow.verification_strength,
          risk_level: safeRow.risk_level,
          action_priority: safeRow.action_priority,
        },
        policy_impact: {
          impact_level: safeRow.impact_level,
          impact_direction: safeRow.impact_direction,
          market_sensitivity: safeRow.market_sensitivity,
          consumer_sensitivity: safeRow.consumer_sensitivity,
          business_sensitivity: safeRow.business_sensitivity,
        },
        final_decision: {
          policy_alert_level: safeRow.policy_alert_level,
          market_signal: marketSignal,
        },
        verification_card: verificationCard,
        claim_text: safeRow.claim_text || "",
        verdict_label: safeRow.verdict_label || "",
        verdict_confidence: safeRow.verdict_confidence || 0,
        evidence_sources: evidenceSources,
        source_reliability_score: safeRow.source_reliability_score || 0,
        source_reliability_reason: safeRow.source_reliability_reason || "",
        evidence_summary: safeRow.evidence_summary || "",
        missing_context: missingContext,
        last_checked_at: safeRow.last_checked_at || "",
        review_status: safeRow.review_status || "",
        human_reviewed_at: safeRow.human_reviewed_at || null,
        // DISPLAY-CATEGORY 2-A: carry the backend domain label (metadata
        // only; never a verdict field) so history/server cards can drive the
        // category tabs/sections. GET /history selects the whole row, so
        // safeRow.domain is present; default null when absent.
        domain: safeRow.domain ?? null,
        // DESIGN-C3h-1: carry the analysis timestamp (ISO-8601 UTC; already in the
        // slim /history payload) so the homepage can compute a client-side
        // KST-"today" filter for the 오늘의 검증 row. Display-only; not a verdict field.
        created_at: safeRow.created_at ?? null,
      };
    }

    async function loadReviewQueueItem(item, message) {
      currentReviewId = item?.id || null;
      currentHistoryId = item?.stable_history_key || item?.id || null;
      const results = await hydrateRecordResults(item);
      setCurrentReportContext(
        item?.query || queryInput.value.trim(),
        item?.max_news || results.length,
        results,
        item?.updated_at || item?.created_at || new Date().toISOString()
      );
      renderResults(results);
      // DESIGN-DETAIL-3: the loaded result renders into #results (now in
      // #detailScreen) — switch to the detail SCREEN (before showStatus so the
      // confirmation survives) so the operator sees it. Replaces the prior inline
      // render on home.
      showScreen("detail");
      renderHistory(safeReadLocalHistory());
      renderReviewQueue(safeReadReviewQueue());
      if (item?.query) {
        queryInput.value = item.query;
      }
      if (item?.max_news) {
        maxNewsInput.value = item.max_news;
      }
      showStatus(message || "검토 큐 항목을 불러왔습니다.", true);
    }

    function updateReviewQueueStatus(itemId, reviewerStatus) {
      const items = safeReadReviewQueue();
      const now = new Date().toISOString();
      const nextItems = items.map((item) => {
        if (item.id !== itemId) return item;
        return {
          ...item,
          reviewer_status: reviewerStatus,
          updated_at: now,
        };
      });
      safeWriteReviewQueue(nextItems);
      renderReviewQueue(nextItems);
      showStatus(`검토 상태를 ${reviewStatusLabel(reviewerStatus)}으로 변경했습니다.`, true);
    }

    async function loadHistoryRecord(record, message, focusIndex = null) {
      currentHistoryId = record?.id || null;
      selectedResultIndex = Number.isInteger(focusIndex) ? focusIndex : null;
      const results = await hydrateRecordResults(record);
      setCurrentReportContext(
        record?.query || queryInput.value.trim(),
        record?.max_news || results.length,
        results,
        getHistoryAnalyzedAt(record)
      );
      renderResults(results, selectedResultIndex);
      // DESIGN-DETAIL-3: switch to the detail SCREEN at its top (no jump-scroll).
      // BEFORE showStatus so the confirmation below survives (showScreen calls
      // hideStatus for non-home screens). Replaces the old resultsEl.scrollIntoView.
      showScreen("detail");
      renderHistory(safeReadLocalHistory());
      if (record?.query) {
        queryInput.value = record.query;
      }
      if (record?.max_news) {
        maxNewsInput.value = record.max_news;
      }
      showStatus(message || "선택한 분석 기록을 불러왔습니다.", true);
    }

    function deleteHistoryRecord(recordId) {
      const records = safeReadLocalHistory();
      const nextRecords = records.filter((item) => item.id !== recordId);
      safeWriteLocalHistory(nextRecords);
      if (currentHistoryId === recordId) {
        currentHistoryId = null;
      }
      if (currentReviewId === recordId) {
        currentReviewId = null;
      }
      renderHistory(nextRecords);
      showStatus("분석 기록을 삭제했습니다.", true);
    }

    function clearLocalHistory() {
      if (!confirm("최근 분석 기록을 모두 삭제할까요?")) {
        return;
      }
      safeWriteLocalHistory([]);
      currentHistoryId = null;
      renderHistory([]);
      showStatus("최근 분석 기록을 모두 삭제했습니다.", true);
    }

    // ===== C11 — Evidence-state helpers =====
    function numberValue(value, fallback = 0) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : fallback;
    }


    // DISPLAY-HONESTY (①): the SAME genuine-official predicate the "공식 근거 확인" /
    // "공식자료 참고" box uses (officialStatusLabel, ~L1908): the persisted
    // has_genuine_official_support boolean, else the old-row body-match fallback. Reused
    // to gate affirmative official-evidence COPY so it never over-claims strong official
    // evidence on a non-genuine row. Reads only — no stored field / score / verdict change.
    function officialEvidenceIsGenuine(summary, debug) {
      const s = summary || {};
      const d = debug || {};
      return (typeof s.has_genuine_official_support === "boolean")
        ? s.has_genuine_official_support
        : (Number(d.official_body_matches || 0) > 0);
    }

    function evidenceQualityExplanation(quality, strength, genuine) {
      const avg = numberValue(quality?.average_evidence_quality_score, 0);
      const strong = numberValue(quality?.strong, 0);
      const medium = numberValue(quality?.medium, 0);
      const weak = numberValue(quality?.weak, 0);
      const strongStrength = numberValue(strength?.strong, 0);
      // DISPLAY-HONESTY (①): only assert "강한 근거가 확인됐습니다" when the row is GENUINELY
      // officially supported (same predicate as the official-status box). When genuine is
      // explicitly false the affirmative is skipped and the copy reads honestly — consistent
      // with a "공식자료 참고" box. A null/undefined genuine (callers that don't pass it)
      // preserves the prior behavior byte-identically. Copy only; score/box/verdict untouched.
      if (strong > 0 && avg >= 75 && genuine !== false) {
        return "주장과 출처가 잘 연결된 강한 근거가 확인됐습니다.";
      }
      if (strongStrength > 0 || medium > 0 || avg >= 45) {
        return "일부 근거는 유용하지만, 공식 상세문서와의 직접 일치 여부를 함께 확인해야 합니다.";
      }
      if (weak > 0) {
        return "대부분 참고용 근거이므로 추가 확인이 필요합니다.";
      }
      return "검증에 사용할 수 있는 근거가 아직 충분하지 않습니다.";
    }

    function officialVerificationExplanation(sourceReliabilitySummary, debugSummary) {
      return buildOfficialEvidenceNarrative(
        buildOfficialEvidenceState(sourceReliabilitySummary || {}, debugSummary || {})
      ).summaryBullet;
    }

    // M28a — single source for the three evidence-STATE dashboard labels
    // (within-main.js de-duplication, Option A). Values are byte-identical to
    // the literals they replace; the methodology section in template.html keeps
    // its own literals (template↔main.js single-sourcing is a later slice).
    const EVIDENCE_STATE_LABELS = {
      candidateOnlyOfficial: "공식 후보만 있음",
      candidateOnlyDetail: "공식기관 후보는 있으나 상세 본문 미확인",
      semanticInsufficient: "의미 매칭 근거 부족",
    };

    function officialDirectMatchLabel(sourceReliabilitySummary, debugSummary = null) {
      return buildOfficialEvidenceState(sourceReliabilitySummary || {}, debugSummary || sourceReliabilitySummary || {}).officialDetailStatus;
    }

    function officialDirectMatchReason(sourceReliabilitySummary, debugSummary) {
      const summary = sourceReliabilitySummary || {};
      const state = buildOfficialEvidenceState(summary, debugSummary || {});
      if (state.officialEvidenceStatus === "body_unmatched" || state.officialEvidenceStatus === "candidate_only" || state.officialEvidenceStatus === "not_found") {
        return buildOfficialEvidenceNarrative(state).detailedExplanation;
      }
      if (summary.official_direct_match_reason) {
        return formatDiagnosticText(summary.official_direct_match_reason);
      }
      return buildOfficialEvidenceNarrative(state).detailedExplanation;
    }

    function officialBodyCollectionLimitation(sourceReliabilitySummary, debugSummary) {
      const state = buildOfficialEvidenceState(sourceReliabilitySummary || {}, debugSummary || {});
      if (state.officialEvidenceStatus === "body_unmatched" || state.officialEvidenceStatus === "partial_support") {
        return "공식 상세문서 본문 확인, 직접 일치 부족";
      }
      if (state.officialEvidenceStatus === "candidate_only") {
        return EVIDENCE_STATE_LABELS.candidateOnlyDetail;
      }
      if (state.officialEvidenceStatus === "not_found") {
        return "공식 후보 없음";
      }
      return formatReasonCounts(debugSummary?.official_body_failures || {});
    }

    function buildOfficialEvidenceState(sourceReliabilitySummary = {}, debugSummary = {}) {
      const summary = sourceReliabilitySummary || {};
      const debug = debugSummary || {};
      const classification = String(summary.official_direct_match_classification || debug.official_direct_match_classification || "");
      const directScore = numberValue(
        summary.official_direct_match_score
          ?? summary.official_final_direct_match_score
          ?? debug.official_direct_match_score
          ?? debug.official_final_direct_match_score,
        0
      );
      const directMatches = numberValue(debug.official_resolution_direct_matches, 0);
      const contextualMatches = numberValue(debug.official_resolution_contextual_matches, 0);
      const weakCandidates = numberValue(debug.official_resolution_weak_candidates, 0);
      const candidates = numberValue(debug.official_body_candidates ?? debug.official_sources_count ?? summary.official_candidate_count, 0);
      const fetched = numberValue(debug.official_bodies_fetched ?? debug.official_resolution_body_fetched, 0);
      const usable = numberValue(debug.official_bodies_usable, 0);
      const matched = numberValue(debug.official_body_matches, 0);
      const bestTitle = userFacingReportText(publicInstitutionName(summary.top_official_detail_title || summary.official_best_evidence_title || summary.top_source_title || ""), "");
      const bestUrl = summary.top_official_detail_url || summary.official_best_evidence_url || summary.url || "";
      const hasBody = Boolean(
        summary.official_detail_available
          || summary.official_body_fetched
          || summary.official_body_match
          || fetched > 0
          || usable > 0
          || numberValue(summary.official_body_length, 0) > 0
      );
      const hasCandidate = Boolean(
        bestUrl
          || candidates > 0
          || weakCandidates > 0
          || summary.top_source_title
          || summary.official_best_evidence_title
          || summary.title
          || classification === "weak_official_candidate_only"
          || classification === "no_usable_official_detail"
      );
      // LABEL-HONESTY: direct_support (and its emphatic "직접 뒷받침" message)
      // must require GENUINE verification. The IBK word-overlap pattern stores
      // official_direct_match_classification === "strong_official_direct_support"
      // (from relevance>=60) AND official_detail_available, so BOTH the
      // classification and the official_detail_available disjuncts leak — gate
      // the whole predicate on genuine. Old rows lacking the boolean fall back to
      // a real body match (matched = official_body_matches > 0). Non-genuine rows
      // fall through to partial_support, not direct_support — honest, and the
      // card's certainty-word stripping (hasDirectOfficialSupport) becomes more
      // cautious for IBK-pattern rows (desired). Score/verdict untouched.
      const genuine = (typeof summary.has_genuine_official_support === "boolean")
        ? summary.has_genuine_official_support
        : (matched > 0);
      const hasDirectSupport = genuine && (
        classification === "strong_official_direct_support"
        || directMatches > 0
        || matched > 0
        || (summary.official_detail_available && directScore >= 55)
      );
      const hasPartialSupport = hasDirectSupport
        || classification === "medium_official_contextual_support"
        || contextualMatches > 0
        || (hasBody && directScore >= 45);
      let officialEvidenceStatus = "not_found";
      if (hasDirectSupport) {
        officialEvidenceStatus = "direct_support";
      } else if (hasPartialSupport) {
        officialEvidenceStatus = "partial_support";
      } else if (hasBody) {
        officialEvidenceStatus = "body_unmatched";
      } else if (hasCandidate) {
        officialEvidenceStatus = "candidate_only";
      }

      const messages = {
        direct_support: {
          label: "공식 상세문서가 핵심 주장을 직접 뒷받침",
          detail: "공식 상세문서가 핵심 주장을 직접 뒷받침",
          summary: "공식기관 본문이 핵심 주장과 연결되어 공식 근거로 참고할 수 있습니다.",
          uncertainty: "다만 직접 일치 점수, 본문 수집 범위, 반박 가능성은 사람 검토로 확인해야 합니다.",
          sourceNote: "공식 상세문서 본문이 기사 핵심 주장과 연결되어 공식 근거로 참고할 수 있습니다.",
          nextAction: "공식 원문과 기사 주장 간 세부 수치·대상·시점을 최종 검토하세요.",
        },
        partial_support: {
          label: "공식 상세문서 본문 확인, 직접 일치 부족",
          detail: "공식 상세문서 본문 확인, 직접 일치 부족",
          summary: "공식 상세문서는 확보했지만 정책 키워드 또는 정책 대상 일치가 일부로 제한됩니다.",
          uncertainty: "직접 확정 근거로 쓰기 전 세부 주장과 공식 문장 일치 여부를 추가 확인해야 합니다.",
          sourceNote: "공식기관 본문이 핵심 주장과 일부 연결되어 보조 공식 근거로 참고할 수 있습니다.",
          nextAction: "관련 공식 상세문서의 본문 문장과 기사 핵심 주장을 대조하세요.",
        },
        body_unmatched: {
          label: "공식 상세문서 본문 확인, 직접 일치 부족",
          detail: "공식 상세문서 본문 확인, 직접 일치 부족",
          summary: "공식 상세문서는 확보했지만 기사 핵심 주장과 직접 일치하지 않습니다.",
          uncertainty: "공식 본문과 기사 주장 사이의 정책 대상·시점·수치 일치가 약합니다.",
          sourceNote: "공식기관 본문은 수집됐지만 핵심 주장과의 직접 일치가 부족합니다.",
          nextAction: "더 직접적인 공식 보도자료나 정책 설명자료를 추가 확인하세요.",
        },
        candidate_only: {
          label: EVIDENCE_STATE_LABELS.candidateOnlyOfficial,
          detail: EVIDENCE_STATE_LABELS.candidateOnlyDetail,
          summary: "공식기관 후보는 있으나 핵심 주장과의 직접 일치는 약합니다.",
          uncertainty: "공식기관 후보는 있으나 상세 본문 미확인 상태입니다.",
          sourceNote: "공식기관 후보는 찾았지만, 기사 핵심 주장과 직접 일치하는 상세 공식문서는 확인하지 못했습니다.",
          nextAction: "관련 공식 상세문서와 보도자료를 추가 확인하세요.",
        },
        not_found: {
          label: "공식 후보 없음",
          detail: "공식 후보 없음",
          summary: "관련 공식기관 후보를 아직 충분히 찾지 못했습니다.",
          uncertainty: "공식 출처 기반 확정 근거가 부족합니다.",
          sourceNote: "직접 일치하는 공식 상세 근거는 아직 확인되지 않았습니다.",
          nextAction: "관련 정부·공공기관의 후속 발표를 확인하세요.",
        },
      };
      const copy = messages[officialEvidenceStatus] || messages.not_found;
      return {
        officialEvidenceStatus,
        officialEvidenceLabel: copy.label,
        officialDetailStatus: copy.detail,
        officialDirectMatchScore: directScore,
        officialResolutionCounts: {
          direct: directMatches,
          contextual: contextualMatches,
          weak: weakCandidates,
          candidates,
          fetched,
          usable,
          matched,
        },
        officialLimitations: copy.uncertainty,
        officialBestEvidenceTitle: bestTitle || "확인되지 않음",
        officialBestEvidenceUrl: bestUrl || "",
        officialSummaryMessage: copy.summary,
        officialUncertaintyMessage: copy.uncertainty,
        officialSourceSummaryNote: copy.sourceNote,
        humanReviewReason: copy.nextAction,
        recommendedNextAction: copy.nextAction,
        hasSupportiveOfficialEvidence: officialEvidenceStatus === "direct_support" || officialEvidenceStatus === "partial_support",
      };
    }

    function buildOfficialEvidenceNarrative(officialEvidenceState) {
      const state = officialEvidenceState || buildOfficialEvidenceState();
      const copy = {
        direct_support: {
          summary: state.officialEvidenceLabel || "공식 상세문서가 핵심 주장을 직접 뒷받침",
          detail: "공식기관 본문이 핵심 주장과 연결되어 공식 근거로 참고할 수 있습니다. 다만 세부 수치, 대상, 시점은 사람 검토로 최종 확인해야 합니다.",
          source: "공식 상세문서 본문이 기사 핵심 주장과 직접 연결되어 주요 근거로 참고할 수 있습니다.",
          action: "공식 원문과 기사 주장 간 대상·시점·수치 일치를 최종 확인하세요.",
        },
        partial_support: {
          summary: state.officialEvidenceLabel || "공식 상세문서 본문 확인, 직접 일치 부족",
          detail: "공식 상세문서는 기사와 같은 기관 또는 정책 맥락을 다루지만, 기사 핵심 주장 전체를 직접 확인한 상태는 아닙니다.",
          source: "공식 자료가 일부 맥락을 뒷받침하지만 직접 검증 근거로 단정하기에는 제한적입니다.",
          action: "관련 공식 상세문서의 본문 문장과 기사 핵심 주장을 추가 대조하세요.",
        },
        body_unmatched: {
          summary: state.officialEvidenceLabel || "공식 상세문서 본문 확인, 직접 일치 부족",
          detail: "공식기관 본문은 수집됐지만 정책 대상, 시점, 세부 주장과 충분히 맞지 않아 확정 근거로 쓰기 어렵습니다.",
          source: "공식 상세자료와의 직접 매칭은 아직 충분하지 않습니다.",
          action: "더 직접적인 공식 보도자료나 설명자료를 추가 확인하세요.",
        },
        candidate_only: {
          summary: state.officialEvidenceLabel || EVIDENCE_STATE_LABELS.candidateOnlyOfficial,
          detail: "공식기관 후보는 있으나 상세 본문 미확인 상태입니다. 공식기관 후보는 보조 신호로만 봐야 합니다.",
          source: "공식기관 후보는 찾았지만, 기사 핵심 주장과 직접 일치하는 상세 공식문서는 확인하지 못했습니다.",
          action: "관련 공식 상세문서와 후속 보도자료를 추가 확인하세요.",
        },
        not_found: {
          summary: state.officialEvidenceLabel || "공식 후보 없음",
          detail: "현재 수집된 자료 기준으로는 기사 핵심 주장을 직접 확인할 공식 상세문서를 찾지 못했습니다.",
          source: "공식 후보 없음",
          action: "관련 정부·공공기관의 후속 발표를 확인하세요.",
        },
      };
      const selected = copy[state.officialEvidenceStatus] || copy.not_found;
      return {
        dashboardStatus: selected.summary,
        summaryBullet: selected.summary,
        detailedExplanation: selected.detail,
        sourceSummaryNote: selected.source,
        recommendedNextAction: selected.action,
      };
    }

    function publicSourceTypeLabel(source) {
      const type = source?.source_type || source?.verification_role || source?.purpose || "";
      const url = String(source?.url || source?.source_url || "");
      if (/go\.kr|gov\.kr|bok\.or\.kr|fsc\.go\.kr|fss\.or\.kr|molit\.go\.kr|korea\.kr|law\.go\.kr/i.test(url)
        || ["official_government", "public_institution", "official_reference", "primary_evidence"].includes(type)) {
        return "공식 출처";
      }
      if (["established_news", "search_fallback_news", "news_context"].includes(type)) {
        return "뉴스 출처";
      }
      return "관련 출처";
    }

    function publicSupportLabel(source) {
      const evidenceType = source?.evidence_type || source?.supports_claim || source?.verification_role || source?.purpose || "";
      const quality = source?.evidence_quality_label || source?.reliability_level || "";
      if (["direct_support", "supports", "primary_evidence", "strong", "very_high", "high"].includes(evidenceType)
        || ["strong", "very_high", "high"].includes(quality)) {
        return "강하게 뒷받침";
      }
      if (["indirect_support", "supporting_evidence", "official_reference", "medium", "support"].includes(evidenceType)
        || quality === "medium") {
        return "부분적으로 뒷받침";
      }
      return "배경 맥락 제공";
    }

    function publicSourceReason(source, sourceReliabilitySummary) {
      const support = publicSupportLabel(source);
      const type = publicSourceTypeLabel(source);
      if (type === "공식 출처" && support === "강하게 뒷받침") {
        return "공식 자료가 핵심 주장과 직접 연결되어 판단에 중요한 근거로 반영됐습니다.";
      }
      if (type === "공식 출처") {
        return "공식기관 후보이지만 기사 주장과의 직접 일치 여부는 추가 확인이 필요합니다.";
      }
      if (support === "강하게 뒷받침") {
        return "기사 내용과 주장 사이의 연결성이 높아 주요 근거로 참고했습니다.";
      }
      if (support === "부분적으로 뒷받침") {
        return "주장과 일부 맥락이 맞아 보조 근거로 참고했습니다.";
      }
      if (sourceReliabilitySummary?.official_mismatch) {
        return "공식 출처와 직접 맞는 상세 근거가 부족해 참고용으로만 반영했습니다.";
      }
      return "판단의 배경을 이해하는 데 도움이 되는 관련 자료입니다.";
    }

    function publicSourceFilterText(source, sourceReliabilitySummary = {}) {
      return [
        source?.reliability_reason,
        source?.reason,
        source?.failure_reason,
        source?.error_page_reason,
        source?.selected_document_reason,
        source?.document_type,
        source?.evidence_type,
        source?.verification_role,
        source?.purpose,
        source?.source_type,
        source?.supports_claim,
        source?.evidence_quality_label,
        sourceReliabilitySummary?.official_mismatch_reason,
        sourceReliabilitySummary?.official_mismatch_reasons,
      ].flat().filter(Boolean).join(" ");
    }

    function isOfficialLikeSource(source) {
      const type = String(source?.source_type || source?.verification_role || source?.purpose || "");
      const url = String(source?.url || source?.source_url || "");
      return /go\.kr|gov\.kr|bok\.or\.kr|fsc\.go\.kr|fss\.or\.kr|molit\.go\.kr|korea\.kr|law\.go\.kr/i.test(url)
        || ["official_government", "public_institution", "official_reference", "primary_evidence"].includes(type);
    }

    function sourceExclusionLabel(source, sourceReliabilitySummary = {}) {
      const text = publicSourceFilterText(source, sourceReliabilitySummary);
      if (/official_topic_mismatch|topic_mismatch|body_mismatch|mismatch|not_directly_related|unrelated/i.test(text)) {
        return "주제 불일치로 제외";
      }
      if (/official_detail_missing|official_detail_url_missing|official_detail_not_verified|official_document_excluded|official_search_only|official_candidate_not_fetched|official_candidate_without_body|official_candidate_metadata_overlap_without_body|no_body_text|no detail body|candidate only|candidate_only/i.test(text)) {
        return "공식 후보였으나 직접 근거에서 제외됨";
      }
      if (/search_page|list_page|generic_list_page|service_index_page|menu_or_index_page|complaint|notice|generic/i.test(text)) {
        return "상세 근거 페이지가 아니어서 제외";
      }
      if (source?.source_origin === "candidate") {
        return "공식 후보였으나 직접 근거에서 제외됨";
      }
      return "";
    }

    function isPublicSupportingSource(source, sourceReliabilitySummary = {}) {
      if (!source) return false;
      if (sourceExclusionLabel(source, sourceReliabilitySummary)) return false;
      const origin = source.source_origin || "";
      if (origin === "candidate") return false;
      const supportText = publicSourceFilterText(source, sourceReliabilitySummary);
      const hasSupportSignal = /direct_support|indirect_support|supports|supporting_evidence|primary_evidence|strong|medium/i.test(supportText);
      if (isOfficialLikeSource(source)) {
        return hasSupportSignal;
      }
      return true;
    }

    function fallbackNewsSource(result) {
      if (!result?.title && !result?.original_url) return null;
      return {
        title: result.title || "선택한 기사",
        url: result.original_url || "",
        source_type: "established_news",
        evidence_type: "news_context",
        evidence_quality_label: "medium",
        reliability_reason: "분석 대상이 된 뉴스 원문입니다.",
        source_origin: "selected_news",
      };
    }

    function publicOfficialLimitationNote(result, sourceReliabilitySummary = {}, items = []) {
      const verification = result?.verification_card || result || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      const state = buildOfficialEvidenceState(sourceReliabilitySummary, debug);
      const narrative = buildOfficialEvidenceNarrative(state);
      const hasOfficialSupporting = items.some((source) => isOfficialLikeSource(source));
      if (hasOfficialSupporting && state.hasSupportiveOfficialEvidence) return "";
      return narrative.sourceSummaryNote;
    }

    function sourceDomain(url) {
      try {
        return new URL(url || "").hostname.replace(/^www\./, "");
      } catch (_) {
        return "";
      }
    }

    function sourceSupportScore(source) {
      const score = source?.official_evidence_score
        ?? source?.official_final_direct_match_score
        ?? source?.official_body_match_score
        ?? source?.evidence_quality_score
        ?? source?.reliability_score
        ?? source?.relevance_score;
      return score === undefined || score === null || score === "" ? "-" : score;
    }

    function sourceTraceability(source, reliability = {}) {
      const exclusion = sourceExclusionLabel(source, reliability);
      if (exclusion) {
        return { label: "제외/불일치", className: "trace-excluded", explanation: exclusion };
      }
      if (source?.source_origin === "selected_news" || source?.evidence_type === "news_context") {
        return { label: "뉴스 원문", className: "trace-news", explanation: "분석 대상이 된 원문 기사입니다." };
      }
      const classification = String(source?.official_evidence_classification || source?.official_direct_match_classification || "");
      const score = numberValue(
        source?.official_evidence_score ?? source?.official_final_direct_match_score ?? source?.official_body_match_score,
        0
      );
      if (classification === "strong_official_direct_support" || (isOfficialLikeSource(source) && source?.official_body_match && score >= 75)) {
        return { label: "공식 직접 근거", className: "trace-direct", explanation: "공식 상세문서 본문이 기사 핵심 주장과 직접 연결됩니다." };
      }
      if (classification === "medium_official_contextual_support" || (isOfficialLikeSource(source) && source?.official_body_match)) {
        return { label: "공식 맥락 근거", className: "trace-context", explanation: "공식 상세문서가 같은 기관이나 정책 맥락을 일부 뒷받침합니다." };
      }
      if (isOfficialLikeSource(source)) {
        return { label: "공식 약한 후보", className: "trace-weak", explanation: "공식 후보이지만 상세 본문 또는 직접 일치가 제한적입니다." };
      }
      return {
        label: publicSupportLabel(source),
        className: "trace-context",
        explanation: publicSourceReason(source, reliability),
      };
    }

    function publicSourceCards(result) {
      const verification = result?.verification_card || result || {};
      const reliability = verification.source_reliability_summary || {};
      const evidenceSources = Array.isArray(verification.evidence_sources) ? verification.evidence_sources : [];
      const snippets = Array.isArray(verification.evidence_snippets || result?.evidence_snippets)
        ? (verification.evidence_snippets || result.evidence_snippets)
        : [];
      const candidates = Array.isArray(verification.source_candidates || result?.source_candidates)
        ? (verification.source_candidates || result.source_candidates)
        : [];
      const merged = [];
      evidenceSources.forEach((source) => merged.push({
        title: source.title || source.url,
        url: source.url,
        source_type: source.source_type,
        evidence_type: source.evidence_type,
        supports_claim: source.supports_claim,
        verification_role: source.verification_role,
        purpose: source.purpose,
        evidence_quality_label: source.evidence_quality_label,
        reliability_reason: source.reliability_reason,
        reliability_level: source.reliability_level,
        reliability_score: source.reliability_score,
        source_origin: "evidence_source",
      }));
      snippets.forEach((snippet) => merged.push({
        title: snippet.source_title || snippet.publisher || snippet.source_url,
        url: snippet.source_url,
        source_type: snippet.source_type,
        evidence_type: snippet.evidence_type,
        supports_claim: snippet.supports_claim,
        evidence_quality_label: snippet.evidence_quality_label,
        source_origin: "evidence_snippet",
      }));
      candidates.forEach((source) => merged.push({ ...source, source_origin: "candidate" }));
      const seen = new Set();
      let filtered = merged.filter((source) => isPublicSupportingSource(source, reliability));
      if (!filtered.length) {
        const fallback = fallbackNewsSource(result);
        if (fallback) filtered = [fallback];
      }
      const unique = filtered.filter((source) => {
        const key = `${source?.url || source?.source_url || ""}|${source?.title || source?.source_title || ""}`;
        if (!key.trim() || seen.has(key)) return false;
        seen.add(key);
        return true;
      }).slice(0, 3);
      return { items: unique, reliability, officialLimitation: publicOfficialLimitationNote(result, reliability, unique) };
    }

    function renderPublicSourceCards(result) {
      const { items, reliability, officialLimitation } = publicSourceCards(result);
      const officialText = officialVerificationExplanation(reliability, (result?.verification_card || result || {}).debug_summary || {});
      if (!items.length) {
        return `
          <section class="public-source-section">
            <h3>근거와 출처 요약</h3>
            <div class="reader-note">현재 공개 리포트에 표시할 수 있는 뚜렷한 출처 카드가 부족합니다. ${escapeHtml(officialLimitation || officialText)}</div>
          </section>
        `;
      }
      return `
        <section class="public-source-section">
          <h3>근거와 출처 요약</h3>
          <div class="reader-note">${escapeHtml(officialLimitation || officialText)}</div>
          <div class="public-source-grid">
            ${items.map((source) => {
              const trace = sourceTraceability(source, reliability);
              const url = source.url || source.source_url || "";
              const title = userFacingReportText(publicInstitutionName(source.title || source.source_title || source.publisher || url || "출처 정보 확인 필요"), "출처 정보 확인 필요");
              const org = publicInstitutionName(source.publisher || source.organization || sourceDomain(url) || "발행처 확인 필요");
              const score = sourceSupportScore(source);
              const reason = userFacingReportText(source.reliability_reason || source.match_reason || source.reason || trace.explanation || publicSourceReason(source, reliability), "공식 자료와 직접 일치하는지 추가 확인이 필요합니다.");
              const fetchWarning = isOfficialLikeSource(source) && (source.official_body_failure_reason || source.body_fetch_failure_reason || source.no_body_text || source.source_risk_flags?.includes?.("no_body_text"))
                ? '<div class="public-source-warning">상세 원문 수집 제한: 공식 페이지 접근 또는 본문 추출에 실패했습니다.</div>'
                : "";
              const titleHtml = url
                ? `<a href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${escapeHtml(title)}</a>`
                : escapeHtml(title);
              return `
                <article class="public-source-card">
                  <div class="public-source-card-title">${titleHtml}</div>
                  <div class="source-support-badge ${escapeHtml(trace.className)}">${escapeHtml(trace.label)}</div>
                  <div class="public-source-meta">기관/도메인: ${escapeHtml(org)}</div>
                  <div class="public-source-meta">출처 유형: ${escapeHtml(publicSourceTypeLabel(source))}</div>
                  ${(source.evidence_strength || source.evidence_quality_label || source.supports_claim) ? `<div class="public-source-meta">근거 강도: ${escapeHtml(formatTechnicalLabel(source.evidence_strength || source.evidence_quality_label || source.supports_claim))}</div>` : ""}
                  ${score !== "-" ? `<div class="public-source-meta">신뢰/관련 점수: ${escapeHtml(score)}</div>` : ""}
                  <div class="public-source-meta">링크: ${url ? `<a href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noopener noreferrer">원문 보기</a>` : "URL 없음"}</div>
                  <div class="public-source-meta">${escapeHtml(reason)}</div>
                  ${fetchWarning}
                </article>
              `;
            }).join("")}
          </div>
        </section>
      `;
    }

    // ===== C12 — Reasoning bullets & user-summary render =====
    function contradictionExplanation(summary) {
      const data = summary || {};
      const confirmed = numberValue(data.confirmed_contradiction_count ?? data.confirmed_contradictions, 0);
      const possible = numberValue(data.possible_contradiction_count ?? data.possible_contradictions, 0);
      const insufficient = numberValue(data.insufficient_evidence_count, 0);
      const officialNeed = numberValue(data.needs_official_confirmation_count, 0);
      if (confirmed > 0) {
        return "동일 대상·시점에 대해 상충되는 근거가 확인되어 사람의 검토가 필요합니다.";
      }
      if (possible > 0) {
        return "일부 상충 가능성이 있으나 같은 시점과 대상인지 추가 확인이 필요합니다.";
      }
      if (insufficient > 0 || officialNeed > 0) {
        return "반박 여부를 판단할 독립 근거가 부족해 공식 확인이 필요합니다.";
      }
      return "직접적인 반박 근거는 확인되지 않았습니다.";
    }


    function alertReasonBullets(level, decision, confidence, impact, quality, sourceReliabilitySummary, contradictionSummary, debugSummary) {
      const finalScore = numberValue(decision?.final_score ?? confidence?.policy_confidence_score, 0);
      const impactLevel = formatLevel(impact?.impact_level);
      const riskLevel = formatLevel(confidence?.risk_level);
      const bullets = [];
      if (level === "HIGH") {
        bullets.push(`영향도 ${impactLevel}, 위험도 ${riskLevel}이며 최종 점수 ${finalScore}점으로 높게 평가됐습니다.`);
        bullets.push("다만 HIGH는 실제 공식 근거와 반박 여부를 함께 확인해 해석해야 합니다.");
      } else if (level === "WATCH") {
        bullets.push(`정책 영향 가능성은 있지만 최종 점수 ${finalScore}점 기준으로 확정 판단보다 관찰이 적절합니다.`);
        bullets.push("공식 근거, 본문 직접 일치, 반박 가능성 중 일부가 아직 충분하지 않습니다.");
      } else if (level === "LOW") {
        bullets.push(`현재 근거와 영향도를 종합하면 최종 점수 ${finalScore}점으로 낮은 경고 단계입니다.`);
        bullets.push("정책 변화로 확정하기에는 직접 근거가 약하거나 영향 범위가 제한적입니다.");
      } else {
        bullets.push(`현재 단계는 ${formatAlert(level)}이며 최종 점수는 ${finalScore}점입니다.`);
      }
      bullets.push(evidenceQualityExplanation(quality || {}, debugSummary?.evidence_strength_summary || {}, officialEvidenceIsGenuine(sourceReliabilitySummary, debugSummary)));
      bullets.push(officialVerificationExplanation(sourceReliabilitySummary, debugSummary));
      bullets.push(contradictionExplanation(contradictionSummary));
      return bullets;
    }

    function uniqueBullets(items, limit = 5) {
      const seen = new Set();
      return (Array.isArray(items) ? items : [])
        .map((item) => cleanArticleTextForPolicyAnalysis(item) || sanitizeDisplayText(item))
        .filter(Boolean)
        .filter((item) => {
          const key = item.replace(/\s+/g, " ").trim();
          if (!key || seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .slice(0, limit);
    }

    function decisionReasonBullets(context, limit = 5) {
      const base = alertReasonBullets(
        context.level,
        context.decision,
        context.confidence,
        context.impact,
        context.quality,
        context.sourceReliabilitySummary,
        context.contradictionSummary,
        context.debugSummary
      );
      const additions = [];
      const officialText = officialVerificationExplanation(context.sourceReliabilitySummary, context.debugSummary);
      if (context.level === "HIGH") {
        additions.push("공식 근거와 기사 내용의 연결 강도가 높아 주요 위험 신호로 분류했습니다.");
        additions.push("사람 검토가 필요한 항목은 최종 확정 전에 출처 원문과 반박 가능성을 확인해야 합니다.");
      } else if (context.level === "WATCH") {
        additions.push("직접 공식 근거 또는 본문 일치가 충분하지 않아 HIGH로 확정하지 않았습니다.");
        additions.push("다음 단계는 관련 공식 상세문서와 후속 해명·정정 자료를 확인하는 것입니다.");
      } else if (context.level === "LOW") {
        additions.push("현재 확인된 근거만으로는 긴급한 정책 위험 신호가 크지 않습니다.");
        additions.push("향후 공식 발표나 수치가 새로 확인되면 판단 단계가 달라질 수 있습니다.");
      }
      if (officialText) additions.push(officialText);
      return uniqueBullets([...base, ...additions], limit);
    }

    function renderBulletList(items) {
      const safeItems = (Array.isArray(items) ? items : []).filter(Boolean);
      return `<ul>${safeItems.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
    }

    function policyImpactBullets(impact, decision) {
      return [
        `주요 영향 분야: ${formatList(impact?.affected_sectors) || "정보 없음"}`,
        `영향 대상: ${formatList(impact?.affected_groups) || "정보 없음"}`,
        `시장 신호: ${formatSignal(decision?.market_signal) || "정보 없음"}`,
      ];
    }

    function consumerImpactBullets(impact) {
      const sensitivity = numberValue(impact?.consumer_sensitivity, 0);
      return [
        `소비자 민감도는 ${sensitivity}점입니다.`,
        `영향 방향은 ${formatDirection(impact?.impact_direction)}입니다.`,
        `관련 대상: ${formatList(impact?.affected_groups) || "정보 없음"}`,
      ];
    }

    function financialSystemBullets(impact) {
      const market = numberValue(impact?.market_sensitivity, 0);
      const business = numberValue(impact?.business_sensitivity, 0);
      return [
        `시장 민감도는 ${market}점입니다.`,
        `기업·금융기관 민감도는 ${business}점입니다.`,
        market >= 60
          ? "금리, 대출, 은행, 부동산 시장과 연결될 가능성이 있어 후속 발표를 추적해야 합니다."
          : "금융 시스템 전반의 즉각적 영향은 제한적으로 보입니다.",
      ];
    }


    function renderReadingGuide(context) {
      const officialText = officialVerificationExplanation(context.sourceReliabilitySummary, context.debugSummary);
      const evidenceText = evidenceQualityExplanation(context.quality, context.strength, officialEvidenceIsGenuine(context.sourceReliabilitySummary, context.debugSummary));
      const contradictionText = contradictionExplanation(context.contradictionSummary);
      const confidenceScore = context.confidence?.policy_confidence_score ?? context.decision?.final_score ?? "-";
      // DETAIL-CLEANUP-V2: header removed — this guide now lives inside a collapsed
      // renderCollapsibleSection whose <summary> provides the title.
      return `
        <section class="reading-guide">
          <div class="reading-guide-grid">
            <div class="reading-guide-card">
              <strong>판정 단계</strong>
              ${escapeHtml(formatAlert(context.level))}은 현재 확보된 근거를 기준으로 얼마나 조심해서 봐야 하는지를 뜻합니다.
            </div>
            <div class="reading-guide-card">
              <!-- SCORE-CLARITY FIX A: the reading guide is public (not operator-
                   gated), so it uses the same 근거 수준 label as the verdict block
                   it explains, and says outright that it is not a truth verdict. -->
              <strong>근거 수준</strong>
              ${escapeHtml(confidenceScore)}점은 공식 자료, 기사 근거, 반박 신호를 함께 본 참고 점수이며, 진위 판정이 아닙니다.
            </div>
            <div class="reading-guide-card">
              <strong>공식 출처 상태</strong>
              ${escapeHtml(officialText)}
            </div>
            <div class="reading-guide-card">
              <strong>근거와 반박</strong>
              ${escapeHtml(evidenceText)} ${escapeHtml(contradictionText)}
            </div>
          </div>
        </section>
      `;
    }

    function renderUserSummarySections(context) {
      // DESIGN-DETAIL-4 STEP 3b: the leading "왜 ${formatAlert(level)}인가" section
      // was removed — it was byte-identical to the kept top verdict block's
      // "왜 이렇게 판단했나요" (same decisionReasonBullets(context, 3)). The 영향
      // sections + .user-explain below are unchanged.
      return `
        <div class="plain-section-grid">
          <section class="plain-section">
            <h4>주요 정책 영향</h4>
            ${renderBulletList(policyImpactBullets(context.impact, context.decision))}
          </section>
          <section class="plain-section">
            <h4>소비자 영향</h4>
            ${renderBulletList(consumerImpactBullets(context.impact))}
          </section>
          <section class="plain-section">
            <h4>금융 시스템 영향</h4>
            ${renderBulletList(financialSystemBullets(context.impact))}
          </section>
        </div>
        <div class="user-explain">
          <!-- DETAIL-CLEANUP-V2: 영향도/위험도 relocated here from the removed
               .headline-meta tiles so these fields stay visible on the page. -->
          <strong>영향도:</strong> ${escapeHtml(formatLevel(context.impact?.impact_level))} · <strong>위험도:</strong> ${escapeHtml(formatLevel(context.confidence?.risk_level))}
          <br><strong>근거 품질:</strong> ${escapeHtml(evidenceQualityExplanation(context.quality, context.strength, officialEvidenceIsGenuine(context.sourceReliabilitySummary, context.debugSummary)))}
          <br><strong>공식 출처 확인:</strong> ${escapeHtml(officialVerificationExplanation(context.sourceReliabilitySummary, context.debugSummary))}
          <br><strong>반박/모순 확인:</strong> ${escapeHtml(contradictionExplanation(context.contradictionSummary))}
        </div>
      `;
    }

    // ===== C13 — Reviewer dashboard =====
    function maxSourceNumber(sources, keys) {
      const list = Array.isArray(sources) ? sources : [];
      return list.reduce((best, source) => {
        const value = keys.reduce((current, key) => Math.max(current, numberValue(source?.[key], 0)), 0);
        return Math.max(best, value);
      }, 0);
    }

    function reviewerOfficialStatus(reliability, debug) {
      const state = buildOfficialEvidenceState(reliability, debug);
      if (state.officialEvidenceStatus === "direct_support") return "공식 직접 확인됨";
      if (state.officialEvidenceStatus === "partial_support") return "공식 맥락 일부 확인";
      if (state.officialEvidenceStatus === "body_unmatched") return "기사-공식문서 직접 일치 부족";
      if (state.officialEvidenceStatus === "candidate_only") return EVIDENCE_STATE_LABELS.candidateOnlyOfficial;
      return "공식 본문 부족";
    }

    function reviewerDetailStatus(reliability, debug) {
      return buildOfficialEvidenceState(reliability, debug).officialDetailStatus;
    }

    function reviewerSemanticStatus(reliability, debug, sources) {
      const semantic = maxSourceNumber(sources, ["semantic_match_score"]);
      const policy = maxSourceNumber(sources, ["policy_alignment_score"]);
      const directScore = numberValue(reliability?.official_direct_match_score ?? debug?.official_direct_match_score, 0);
      const score = Math.max(semantic, policy, directScore);
      if (score >= 75) return "의미 매칭 강함";
      if (score >= 55) return "의미 매칭 보통";
      if (score >= 30) return "의미 매칭 약함";
      return EVIDENCE_STATE_LABELS.semanticInsufficient;
    }

    function reviewerUncertainty(reliability, debug, contradiction, verification) {
      const state = buildOfficialEvidenceState(reliability, debug);
      const contradictionText = contradictionExplanation(contradiction);
      if (numberValue(contradiction?.possible_contradiction_count ?? contradiction?.possible_contradictions, 0) > 0) {
        return "반박 가능성이 있어 같은 대상·시점인지 사람 검토가 필요합니다.";
      }
      if (state.officialEvidenceStatus !== "not_found") {
        return state.officialUncertaintyMessage;
      }
      if (String(verification?.review_status || verification?.verdict_label || "").includes("review")) {
        return "AI 초안이므로 최종 게시 전 사람 검토가 필요합니다.";
      }
      return contradictionText || "현재 확보된 근거 기준으로 큰 모순 신호는 제한적입니다.";
    }

    function buildReviewerDashboardModel(result, context = {}) {
      const verification = result?.verification_card || result || {};
      const decision = result?.final_decision || {};
      const confidence = result?.policy_confidence || {};
      const reliability = verification.source_reliability_summary || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      const contradiction = verification.contradiction_summary || result?.contradiction_summary || {};
      const sources = verification.source_candidates || result?.source_candidates || [];
      const strength = context.strength || debug.evidence_strength_summary || {};
      const quality = context.quality || debug.evidence_quality_summary || verification.evidence_quality_summary || {};
      const finalLevel = String(decision.policy_alert_level || "WATCH").toUpperCase();
      const officialState = buildOfficialEvidenceState(reliability, debug);
      const officialStatus = reviewerOfficialStatus(reliability, debug);
      const detailStatus = reviewerDetailStatus(reliability, debug);
      const semanticStatus = reviewerSemanticStatus(reliability, debug, sources);
      const contradictionStatus = contradictionExplanation(contradiction);
      const needsReview = Boolean(
        debug.needs_human_review
        || String(verification.review_status || verification.verdict_label || "").includes("review")
        || String(verification.verdict_label || "").includes("official_confirmation")
      );
      const nextAction = officialState.recommendedNextAction
        || (reliability.official_mismatch
        ? "관련 공식 상세문서와 보도자료를 추가 확인하세요."
        : formatRecommendation(decision.action_recommendation) || "근거 문장과 공식 출처 일치 여부를 확인하세요.");
      const semanticScore = Math.max(
        maxSourceNumber(sources, ["semantic_match_score"]),
        numberValue(reliability.official_direct_match_score ?? debug.official_direct_match_score, 0)
      );
      const keywordScore = maxSourceNumber(sources, ["official_body_text_match_score", "official_body_match_score"]);
      return {
        finalJudgment: formatAlert(finalLevel),
        draftVerdict: officialEvidenceInsufficientForExport(result)
          ? "사람 검토 대기"
          : formatVerdict(verification.verdict_label),
        needsReview: needsReview ? "사람 검토 필요" : "사람 검토 불필요",
        officialStatus,
        detailStatus,
        semanticStatus,
        contradictionStatus,
        uncertainty: reviewerUncertainty(reliability, debug, contradiction, verification),
        nextAction,
        officialEvidenceState: officialState,
        chips: [
          ["공식기관 후보 수", debug.official_body_candidates ?? debug.official_sources_count ?? reliability.official_candidate_count ?? 0],
          ["상세문서 확보 수", debug.official_detail_pages_fetched_count ?? debug.official_bodies_fetched ?? 0],
          ["본문 매칭 점수", reliability.official_direct_match_score ?? debug.official_direct_match_score ?? 0],
          ["의미 매칭 점수", semanticScore],
          ["키워드 매칭 점수", keywordScore],
          ["반박 후보 수", contradiction.contradiction_candidates_searched ?? debug.contradiction_candidates_searched ?? 0],
          ["근거 강도", formatEvidenceSummaryLabel(strength)],
          ["근거 품질", formatEvidenceSummaryLabel(quality)],
          ["신뢰도 점수", confidence.policy_confidence_score ?? decision.final_score ?? "-"],
        ],
      };
    }

    function renderReviewerDecisionDashboard(result, context = {}) {
      const model = buildReviewerDashboardModel(result, context);
      const cards = [
        ["최종 판정", model.finalJudgment],
        ["AI 초안 판정", model.draftVerdict],
        ["사람 검토 필요 여부", model.needsReview],
        ["공식 근거 상태", model.officialStatus],
        ["공식 상세문서 상태", model.detailStatus],
        ["의미 매칭 상태", model.semanticStatus],
        ["반박/모순 상태", model.contradictionStatus],
        ["추천 다음 조치", model.nextAction],
      ];
      return `
        <section class="reviewer-dashboard">
          <h3>검토자 판단 대시보드</h3>
          <div class="reader-note">검수자가 최종 판정의 근거, 공식 문서 연결 상태, 남은 불확실성을 빠르게 확인하는 요약입니다.</div>
          <div class="reviewer-dashboard-grid">
            ${cards.map(([label, value]) => `
              <div class="reviewer-status-card">
                <span class="label">${escapeHtml(label)}</span>
                <strong>${escapeHtml(value || "-")}</strong>
              </div>
            `).join("")}
          </div>
          <div class="plain-section" style="margin-top: 12px;">
            <h4>현재 가장 큰 불확실성</h4>
            <div class="score-explain">${escapeHtml(model.uncertainty)}</div>
          </div>
          <div class="reviewer-chip-row">
            ${model.chips.map(([label, value]) => `<span class="reviewer-chip">${escapeHtml(label)}: ${escapeHtml(value ?? "-")}</span>`).join("")}
          </div>
        </section>
      `;
    }

    function renderReviewerActionCard(result, context = {}) {
      const query = currentReportContext?.query || queryInput?.value || "";
      const action = getReviewerAction(result, query);
      const options = Object.entries(REVIEW_ACTION_LABELS).map(([value, label]) => `
        <option value="${escapeHtml(value)}" ${action.review_status === value ? "selected" : ""}>${escapeHtml(label)}</option>
      `).join("");
      return `
        <section class="reviewer-action-card" data-review-action-key="${escapeHtml(action.key)}">
          <h3>검토자 액션</h3>
          <div class="reader-note">
            현재 검토 상태
            <span class="review-status-badge">${escapeHtml(reviewerActionStatusLabel(action.review_status))}</span>
            <br>마지막 저장: <span data-review-saved-at>${escapeHtml(formatReviewerSavedAt(action.reviewed_at))}</span>
          </div>
          <div class="reviewer-action-controls">
            <label>
              <span class="label">검토 상태</span>
              <select data-review-status>
                ${options}
              </select>
            </label>
            <label>
              <span class="label">검토 메모</span>
              <textarea data-review-note placeholder="검토자가 확인한 공식 링크, 판단 사유, 후속 확인 사항을 적어주세요.">${escapeHtml(action.reviewer_note)}</textarea>
            </label>
          </div>
          <div class="reviewer-action-buttons">
            <button class="review-save-button" type="button" data-save-review-action="${escapeHtml(action.key)}">검토 저장</button>
            <button class="review-clear-button" type="button" data-clear-review-action="${escapeHtml(action.key)}">검토 초기화</button>
          </div>
        </section>
      `;
    }

    // ================================================================
    // REVIEW-ASSIST Slice 1 — "확인 포인트" decision-support block.
    //
    // Pure presentation of fields the pipeline ALREADY computed — zero LLM,
    // zero backend, zero new judgment:
    //   (1) debug_summary.disagreement_signal (M11.0d-3a, main.py) — shown
    //       only when agreed === false, framed as a thing to CHECK;
    //   (2) the best official-source link (source_reliability_summary, same
    //       field chain buildOfficialEvidenceState reads) — so the reviewer
    //       can open the cited document and see whether it supports the claim;
    //   (3) claim ↔ evidence pairs (normalized_claims + evidence_snippets via
    //       claim_index, the same linkage the advanced evidence list uses).
    //
    // HARD CONSTRAINT (badge honesty): this is decision-SUPPORT for the human
    // "사람 검토됨" review. It must never suggest approve/reject, a leaning,
    // or a truth conclusion — it reads no verdict field, uses no red/green
    // semantics, and its copy uses check-verbs only. The words below may not
    // appear in the block's copy; tests/review_checkpoints.test.js renders
    // the block and enforces the list. Single sanctioned exception: the
    // descriptive phrase "판정 불일치", which names the pipeline's INTERNAL
    // producer disagreement, not a conclusion about the claim.
    // REVIEW-ASSIST-1 CHECKPOINTS START (markers pinned by tests/review_checkpoints.test.js)
    const REVIEWER_CHECKPOINT_FORBIDDEN_WORDS = ["추천", "승인", "기각", "검증", "사실", "거짓", "참", "판정"];
    const REVIEWER_CHECKPOINT_ALLOWED_PHRASE = "판정 불일치";

    function renderReviewerCheckpoints(result) {
      const verification = result?.verification_card || result || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      const reliability = verification.source_reliability_summary || result?.source_reliability_summary || {};
      const signal = debug.disagreement_signal || {};
      const clip = (text, max) => {
        const s = String(text || "").trim();
        return s.length > max ? `${s.slice(0, max - 1)}…` : s;
      };
      const rows = [];

      // (1) Internal disagreement — only when the pipeline itself recorded
      // agreed === false. Agreed or absent signal renders nothing (graceful).
      if (signal.agreed === false) {
        rows.push(`
          <div class="plain-section">
            <h4>확인 필요: 내부 판정 불일치</h4>
            <div class="score-explain">파이프라인 내부 단계들이 서로 다른 결과를 냈습니다. 근거를 직접 살펴봐 주세요.</div>
            <div class="score-explain">${escapeHtml(clip(signal.disagreement_description || "", 240))}</div>
          </div>
        `);
      }

      // (2) Official-source original document — click-through so the human
      // can see whether the cited document supports the claim.
      const officialUrl = reliability.top_official_detail_url || reliability.official_best_evidence_url || "";
      if (officialUrl) {
        const officialTitle = clip(
          reliability.top_official_detail_title
            || reliability.official_best_evidence_title
            || reliability.top_source_title
            || "공식 문서 열기",
          120
        );
        rows.push(`
          <div class="plain-section">
            <h4>확인 포인트: 공식 출처 원문 확인</h4>
            <div class="score-explain">
              인용된 공식 문서가 주장을 뒷받침하는지 원문에서 직접 확인해 주세요.
              <a href="${escapeHtml(safeUrl(officialUrl))}" target="_blank" rel="noopener noreferrer">${escapeHtml(officialTitle)}</a>
            </div>
          </div>
        `);
      }

      // (3) Claim ↔ evidence pairs — compact (3 claims × 2 snippets max);
      // reuses the assembled data, no refetch.
      const rawClaims = verification.normalized_claims || result?.normalized_claims
        || verification.claims || result?.claims || [];
      const snippets = verification.evidence_snippets || result?.evidence_snippets || [];
      const claimTexts = rawClaims
        .map((claim) => (typeof claim === "string" ? claim : (claim?.claim_text || claim?.text || "")))
        .filter(Boolean);
      claimTexts.slice(0, 3).forEach((claimText, index) => {
        const related = snippets.filter((snippet) => Number(snippet?.claim_index) === index).slice(0, 2);
        const evidenceHtml = related.length
          ? related.map((snippet) => {
              const url = safeUrl(snippet.source_url || "");
              const label = clip(snippet.source_title || snippet.source_url || "출처", 80);
              const link = url ? ` <a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>` : "";
              return `<div class="score-explain">근거: ${escapeHtml(clip(snippet.evidence_text || "", 200))}${link}</div>`;
            }).join("")
          : '<div class="score-explain">연결된 근거가 없습니다 — 원문에서 직접 확인해 주세요.</div>';
        rows.push(`
          <div class="plain-section">
            <h4>주장 #${index + 1} ↔ 근거 대조</h4>
            <div class="score-explain">주장: ${escapeHtml(clip(claimText, 200))}</div>
            ${evidenceHtml}
        </div>
        `);
      });

      if (!rows.length) return "";
      return `
        <section class="reviewer-dashboard" data-reviewer-checkpoints>
          <h3>확인 포인트</h3>
          <div class="reader-note">아래 항목은 검토자가 직접 살펴볼 지점을 보여줄 뿐, 어떤 결론이나 처리 방향도 제시하지 않습니다. 리뷰 결정은 전적으로 사람의 몫입니다.</div>
          ${rows.join("")}
        </section>
      `;
    }
    // REVIEW-ASSIST-1 CHECKPOINTS END



    // DESIGN-DETAIL-4 STEP 3a: renderVerificationSummaryCard (the 9-tile 검증 결과
    // 요약 카드) was REMOVED. Every tile duplicated data shown elsewhere: 최종 판정 →
    // verdict block 판정 단계 + headline badge; 신뢰도 점수 → verdict block 신뢰도;
    // 공식 근거/상세문서/의미 매칭 상태 → verdict block 공식 출처 상태 + the
    // "출처와 공식 근거" advanced collapsible + .user-explain; 반박/모순 상태 → the
    // 왜 판단 bullets + .user-explain; 사람 검토/검토 상태 → AI-card 리뷰 상태; 추천
    // 다음 조치 → the 왜 판단 next-step bullet. No data lost. (The unused
    // .verification-summary-* CSS is left in place; it is harmless dead style.)

    // ===== C14 — Result pipeline assembly & main render (HUB) =====
    function getResultPipelineParts(result) {
      const verification = result?.verification_card || result || {};
      const confidence = result?.policy_confidence || {};
      const impact = result?.policy_impact || {};
      const decision = result?.final_decision || {};
      const sourceReliabilitySummary = verification.source_reliability_summary || {};
      const evidenceExtractionSummary = verification.evidence_extraction_summary || {};
      const contradictionSummary = verification.contradiction_summary || result?.contradiction_summary || {};
      const debugSummary = verification.debug_summary || result?.debug_summary || {};
      const strength = debugSummary.evidence_strength_summary
        || evidenceExtractionSummary.evidence_strength_summary
        || {};
      const quality = debugSummary.evidence_quality_summary
        || evidenceExtractionSummary.evidence_quality_summary
        || result?.evidence_quality_summary
        || {};

      return {
        result,
        verification,
        confidence,
        impact,
        decision,
        claims: verification.claims || result?.claims || [],
        normalizedClaims: verification.normalized_claims || result?.normalized_claims || [],
        sourceQueries: verification.source_queries || result?.source_queries || [],
        sourceCandidates: verification.source_candidates || result?.source_candidates || [],
        sourceReliabilitySummary,
        evidenceSnippets: verification.evidence_snippets || result?.evidence_snippets || [],
        evidenceExtractionSummary,
        contradictionChecks: verification.contradiction_checks || result?.contradiction_checks || [],
        contradictionSummary,
        biasFramingAnalysis: verification.bias_framing_analysis || result?.bias_framing_analysis || [],
        biasFramingSummary: verification.bias_framing_summary || result?.bias_framing_summary || {},
        debugSummary,
        strength,
        quality,
        level: String(decision.policy_alert_level || "WATCH").toUpperCase(),
      };
    }

    function buildReportUserContext(parts) {
      return {
        level: parts.level,
        decision: parts.decision,
        confidence: parts.confidence,
        impact: parts.impact,
        quality: parts.quality,
        strength: parts.strength,
        sourceReliabilitySummary: parts.sourceReliabilitySummary,
        contradictionSummary: parts.contradictionSummary,
        debugSummary: parts.debugSummary,
      };
    }

    function recommendedActionForParts(parts) {
      return parts.sourceReliabilitySummary.official_mismatch
        ? "추가 공식 출처 확인 필요"
        : formatRecommendation(parts.decision.action_recommendation);
    }

    // SUMMARY-CONTENT-B: display-time cleanup for ALREADY-STORED evidence_summary
    // rows that still contain raw English concept keys (review_stage, etc.).
    // New analyses are already clean from the backend CONCEPT_LABEL_KO fix; this
    // mirrors that map so old rows display Korean too. Replaces ONLY the exact
    // known keys (word-boundary) — never mangles other text. Display-only: stored
    // data and exports of old rows are unchanged until reprocessed.
    function cleanConceptKeysForDisplay(text) {
      if (!text || typeof text !== "string") return text;
      const labels = {
        rental_loan: "전세대출",
        mortgage_loan: "주택담보대출",
        interest_rate: "금리",
        regulation: "규제",
        subsidy_support: "지원",
        target_group: "지원대상",
        implementation: "시행",
        review_stage: "검토",
      };
      return text.replace(
        /\b(rental_loan|mortgage_loan|interest_rate|regulation|subsidy_support|target_group|implementation|review_stage)\b/g,
        (match) => labels[match] || match
      );
    }

    // SPREAD-TIMELINE Slice 2 — fetch + render the circulation annotation
    // into the card detail's placeholder. CIRCULATION ONLY (유통 규모), never
    // a verdict: no truth-probability, no gauge, no verdict styling. Every
    // non-happy path renders NOTHING, silently: found:false, outlet_count<2
    // (a 1-outlet cluster can't be distinguished from a not-yet-rebuilt
    // graph, so no "단독 보도" claim), missing/invalid id, fetch/parse
    // failure. The honesty line inside the section is MANDATORY copy.
    function spreadSpanPhrase(spanDays) {
      if (spanDays === 0) return "같은 날 집중 보도";
      if (spanDays === 1) return "하루 사이 확산";
      return `${spanDays}일에 걸쳐 확산`;
    }

    // SPREAD-TIMELINE Slice 3 — tiny CSS-bar sparkline of timeline.daily so
    // the SHAPE of circulation is visible (tall first bar = everyone published
    // at once). Pure inline-styled divs — no chart lib, no canvas, no new CSS
    // class rules. Neutral brand color only (never red/green verdict
    // semantics). Zero-fills missing days between first and last so a gap
    // renders as an empty slot, not a hidden jump. Returns "" (text-only
    // section stays) only for truly bad data: no dated days, unparseable
    // dates, an implausibly long span (>60 days), or peak<=0.
    function spreadSparklineHtml(daily) {
      if (!Array.isArray(daily) || !daily.length) return "";
      const counts = new Map();
      for (const entry of daily) {
        const day = typeof entry?.date === "string" ? entry.date.slice(0, 10) : "";
        const count = Number(entry?.count);
        if (day && Number.isFinite(count) && count > 0) counts.set(day, count);
      }
      // SPARKLINE-PRESENT A6b: the hide-when-small guards (<3 distinct days, <2-day
      // span) are GONE — the centered shrink-wrapped plot below looks balanced at
      // any size, so every dated cluster (>=1 day) renders. Fail-silent stays for
      // truly bad data only: no dated days, unparseable dates, >60-day span, peak<=0.
      if (!counts.size) return "";
      const days = [...counts.keys()].sort();
      const startMs = Date.parse(`${days[0]}T00:00:00Z`);
      const endMs = Date.parse(`${days[days.length - 1]}T00:00:00Z`);
      if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return "";
      const totalDays = Math.round((endMs - startMs) / 86400000) + 1;
      if (totalDays < 1 || totalDays > 60) return "";
      let peak = 0;
      counts.forEach((count) => { peak = Math.max(peak, count); });
      if (peak <= 0) return "";
      const bars = [];
      for (let i = 0; i < totalDays; i += 1) {
        const day = new Date(startMs + i * 86400000).toISOString().slice(0, 10);
        const count = counts.get(day) || 0;
        const heightPct = count > 0 ? Math.max(6, Math.round((count / peak) * 100)) : 0;
        // flex-basis 12px gives each bar real intrinsic width (so the shrink-wrapped
        // plot's width = bar count × ~14px incl. gap); min-width:0 lets 60 bars still
        // compress into a narrow container instead of overflowing.
        bars.push(
          `<div title="${escapeHtml(day)} · ${escapeHtml(count)}건" style="flex:1 1 12px;max-width:14px;min-width:0;align-self:flex-end;height:${count > 0 ? heightPct + "%" : "2px"};background:${count > 0 ? "var(--brand)" : "var(--line)"};border-radius:2px 2px 0 0;"></div>`
        );
      }
      // Adaptive labels: 1 dated day = a point, shown honestly as ONE centered bar
      // with a single "{date} · {N}건" label; 2+ days keep first / 최다 / last, but
      // the row now spans the PLOT (min-width:max-content, centered), so the dates
      // sit under the actual first/last bars instead of the container edges.
      const labelRow = counts.size === 1
        ? `<div style="text-align:center;font-size:0.78rem;color:var(--muted);">${escapeHtml(days[0])} · ${escapeHtml(counts.get(days[0]))}건</div>`
        : `<div style="display:flex;justify-content:space-between;gap:12px;min-width:max-content;align-self:center;font-size:0.78rem;color:var(--muted);">
              <span>${escapeHtml(days[0])}</span>
              <span>최다 ${escapeHtml(peak)}건/일</span>
              <span>${escapeHtml(days[days.length - 1])}</span>
            </div>`;
      // One shrink-wrapped plot (bars + labels), centered in the section: its width
      // = bar count × ≤16px capped at 100%, so 2–5 days form a compact centered
      // cluster and 30–60 days fill the width exactly as before.
      return `
            <div style="text-align:center;margin:10px 0 4px;">
              <div class="spread-sparkline" role="img" aria-label="일별 보도량, 최다 ${escapeHtml(peak)}건" style="display:inline-flex;flex-direction:column;gap:2px;max-width:100%;vertical-align:bottom;">
                <div style="display:flex;align-items:flex-end;justify-content:center;gap:2px;height:48px;">${bars.join("")}</div>
                ${labelRow}
              </div>
            </div>`;
    }

    // SHARE-IMAGE Slice 1: the spread payload each card fetched, kept for the
    // share-image canvas (zero re-fetch at button-click time). Keyed by
    // result_id; only found+multi-outlet payloads are stored.
    const spreadDataCache = new Map();

    // SHARE-IMAGE Slice 1 — per-card share image, drawn on an offscreen
    // canvas ENTIRELY client-side (no server render, no new fetch, no new
    // font load: the same named-but-system font stack the page uses, so
    // Korean renders identically — no tofu). CIRCULATION ONLY: the image
    // carries title + "N개 매체" + dates + the honesty line + wordmark/URL
    // baked into the pixels — no verdict, no gauge, no red/green. Text and
    // shapes only (same-origin, canvas stays untainted). Download-only this
    // slice; navigator.share + sparkline bars are Slice 2.
    const SHARE_IMAGE_FONTS = 'Pretendard, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif';
    const SHARE_IMAGE_COLORS = {
      canvas: "#f6f8fb", ink: "#0f172a", slate: "#475569",
      muted: "#94a3b8", line: "#e2e8f0", brand: "#1e5fd8", brandInk: "#1542a0",
    };

    // Canvas has no auto-wrap; per-character wrapping is correct for Korean
    // (no space-delimited words needed). Returns at most maxLines lines,
    // ellipsizing the last one when the text overflows.
    function wrapCanvasText(ctx, text, maxWidth, maxLines) {
      const chars = Array.from(String(text || ""));
      const lines = [];
      let line = "";
      for (let i = 0; i < chars.length; i += 1) {
        const candidate = line + chars[i];
        if (line && ctx.measureText(candidate).width > maxWidth) {
          lines.push(line);
          line = chars[i];
          if (lines.length === maxLines) break;
        } else {
          line = candidate;
        }
      }
      if (lines.length < maxLines && line) lines.push(line);
      if ((lines.length === maxLines && line && lines[maxLines - 1] !== line)
          || chars.length && lines.join("").length < chars.length) {
        let last = lines[lines.length - 1] || "";
        while (last && ctx.measureText(last + "…").width > maxWidth) {
          last = last.slice(0, -1);
        }
        lines[lines.length - 1] = last + "…";
      }
      return lines;
    }

    function drawShareImage(title, spreadData, officialLabel = "") {
      const width = 1200;
      const height = 630;
      const margin = 72;
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext("2d");
      const palette = SHARE_IMAGE_COLORS;

      ctx.fillStyle = palette.canvas;
      ctx.fillRect(0, 0, width, height);
      ctx.textBaseline = "alphabetic";

      // Brand row — plain sans wordmark this slice (no webfont dependency).
      ctx.fillStyle = palette.brand;
      ctx.font = `800 46px ${SHARE_IMAGE_FONTS}`;
      ctx.fillText("tickedin", margin, 108);
      ctx.fillStyle = palette.muted;
      ctx.font = `600 22px ${SHARE_IMAGE_FONTS}`;
      ctx.fillText("정책 뉴스, 어디까지 퍼졌는지 확인하세요", margin + 216, 104);

      // Claim title — wrapped, max 3 lines.
      ctx.fillStyle = palette.ink;
      ctx.font = `700 50px ${SHARE_IMAGE_FONTS}`;
      let y = 210;
      for (const line of wrapCanvasText(ctx, title, width - margin * 2, 3)) {
        ctx.fillText(line, margin, y);
        y += 66;
      }
      y += 8;

      // Spread facts — only when the card actually has multi-outlet data.
      const outletCount = Number(spreadData?.cluster?.outlet_count);
      if (Number.isFinite(outletCount) && outletCount >= 2) {
        ctx.fillStyle = palette.brandInk;
        ctx.font = `800 40px ${SHARE_IMAGE_FONTS}`;
        ctx.fillText(`${outletCount}개 매체에서 보도`, margin, y);
        y += 54;
        const timeline = spreadData.timeline || {};
        const firstAt = typeof timeline.first_at === "string" ? timeline.first_at.slice(0, 10) : "";
        const spanDays = Number(timeline.span_days);
        if (Number(timeline.dated_members) > 0 && firstAt && Number.isFinite(spanDays)) {
          ctx.fillStyle = palette.slate;
          ctx.font = `500 28px ${SHARE_IMAGE_FONTS}`;
          ctx.fillText(`최초 보도 ${firstAt} · ${spreadSpanPhrase(spanDays)}`, margin, y);
          y += 40;
        }
      }

      // SHARE-IMAGE-FILL A7: 공식 근거 상태 — drawn on ALL cards, reusing the
      // detail header's officialStatusLabel (closed source-status vocabulary,
      // never a truth verdict). On spread-less cards it fills the otherwise
      // empty middle band; on spread cards it complements the lines above.
      // SINGLE neutral slate — no status-based color (color must not imply a
      // verdict). Same 28px tier as the 최초 보도 sub-line.
      if (officialLabel) {
        y += 14;
        ctx.fillStyle = palette.slate;
        ctx.font = `500 28px ${SHARE_IMAGE_FONTS}`;
        ctx.fillText(`공식 근거 상태: ${officialLabel}`, margin, y);
        y += 40;
      }

      // Footer — honesty line + URL, baked into the pixels.
      ctx.strokeStyle = palette.line;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(margin, height - 118);
      ctx.lineTo(width - margin, height - 118);
      ctx.stroke();
      ctx.fillStyle = palette.slate;
      ctx.font = `500 26px ${SHARE_IMAGE_FONTS}`;
      ctx.fillText("확산 규모를 보여줄 뿐, 사실 여부에 대한 검증이 아닙니다.", margin, height - 66);
      ctx.fillStyle = palette.brand;
      ctx.font = `800 28px ${SHARE_IMAGE_FONTS}`;
      const urlText = "tickedin.org";
      ctx.fillText(urlText, width - margin - ctx.measureText(urlText).width, height - 66);
      return canvas;
    }

    function downloadShareImage(title, resultId, officialLabel = "") {
      try {
        const canvas = drawShareImage(title, spreadDataCache.get(resultId), officialLabel);
        canvas.toBlob((blob) => {
          if (!blob) return;
          const url = URL.createObjectURL(blob);
          const anchor = document.createElement("a");
          anchor.href = url;
          anchor.download = `tickedin-${resultId || "card"}.png`;
          document.body.appendChild(anchor);
          anchor.click();
          anchor.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }, "image/png");
      } catch (error) {
        // fail-silent: the share image is optional; the card must never break
      }
    }

    async function loadSpreadAnnotations() {
      const placeholders = document.querySelectorAll(".spread-section[data-spread-id]");
      for (const section of placeholders) {
        const id = Number(section.getAttribute("data-spread-id"));
        if (!Number.isInteger(id) || id <= 0) continue;
        try {
          const response = await fetch(`${API_BASE}/api/spread/${encodeURIComponent(id)}`);
          if (!response.ok) continue;
          const data = await response.json();
          const outletCount = Number(data?.cluster?.outlet_count);
          if (!data?.found || !Number.isFinite(outletCount) || outletCount < 2) continue;
          spreadDataCache.set(id, data);
          const timeline = data.timeline || {};
          const firstAt = typeof timeline.first_at === "string" ? timeline.first_at.slice(0, 10) : "";
          const spanDays = Number(timeline.span_days);
          const timelineLine = Number(timeline.dated_members) > 0 && firstAt && Number.isFinite(spanDays)
            ? `<div class="spread-timeline-line">최초 보도 ${escapeHtml(firstAt)} · ${escapeHtml(spreadSpanPhrase(spanDays))}</div>`
            : "";
          // SYNDICATION-STAT B5d 2b: spread-structure line (circulation only,
          // never a verdict/color). Renders ONLY when near_anchor_outlet_count
          // >= 2 — a count of 1 is just the earliest report itself, and the
          // field is absent (null) on pre-2a graph rows. Wording is the honest
          // descriptive framing ("문구가 거의 동일") — never 복붙/베낌.
          const nearCount = Number(data?.cluster?.near_anchor_outlet_count);
          const exactCount = Number(data?.cluster?.exact_same_text_outlet_count);
          const exactNote = Number.isFinite(exactCount) && exactCount >= 2
            ? ` (이 중 ${escapeHtml(exactCount)}개 매체는 문구가 완전히 같습니다.)`
            : "";
          const syndicationLine = Number.isFinite(nearCount) && nearCount >= 2
            ? `<div class="spread-syndication-line">이 중 ${escapeHtml(nearCount)}개 매체는 첫 보도와 제목·주장 문구가 거의 동일합니다.${exactNote}</div>`
            : "";
          section.innerHTML = `
            <h3>이슈 확산 현황</h3>
            <div>이 주장과 유사한 내용이 ${escapeHtml(outletCount)}개 매체에서 보도되었습니다.</div>
            ${syndicationLine}
            ${timelineLine}
            ${spreadSparklineHtml(timeline.daily)}
            <div class="spread-map-link"><a href="/web/brainmap.html?focus=${encodeURIComponent(id)}" target="_blank" rel="noopener noreferrer">브레인맵에서 유사 보도 보기 →</a></div>
            <div class="reader-note">확산 정보는 유통 규모를 보여줄 뿐, 사실 여부에 대한 검증이 아닙니다.</div>
          `;
          section.hidden = false;
        } catch (error) {
          // fail-silent: spread info is optional context, never an error state
        }
      }
    }

    // CLUSTER-SURFACE S-a: hydrate the detail card's sibling-coverage placeholder
    // after the innerHTML pass (mirrors loadSpreadAnnotations). Verdict-free:
    // titles + /?result_id links + the circulation honesty note only.
    // TEMPORAL-MAP v1 — weekly-snapshot trajectory sparkline. One bar per
    // snapshot point (outlet_count over snapshot dates). Reuses the spread
    // sparkline's inline-bar styling; no library. Measurement only.
    function topicTimelineSparklineHtml(points) {
      if (!Array.isArray(points) || points.length < 2) return "";
      let peak = 0;
      for (const point of points) peak = Math.max(peak, Number(point?.outlets) || 0);
      if (peak <= 0) return "";
      const bars = points.map((point) => {
        const outlets = Number(point?.outlets) || 0;
        const day = typeof point?.date === "string" ? point.date.slice(5, 10) : "";
        const heightPct = outlets > 0 ? Math.max(6, Math.round((outlets / peak) * 100)) : 0;
        return `<div title="${escapeHtml(day)} · ${escapeHtml(outlets)}개 매체" style="flex:1 1 14px;max-width:18px;min-width:0;align-self:flex-end;height:${outlets > 0 ? heightPct + "%" : "2px"};background:${outlets > 0 ? "var(--brand)" : "var(--line)"};border-radius:2px 2px 0 0;"></div>`;
      });
      // TEMPORAL-MAP Phase 4 FIX 2: structure mirrors spreadSparklineHtml so the
      // two charts read as sibling plots — a centered, shrink-wrapped inline-flex
      // column (bars + label row), 48px bar row, and a min-width:max-content
      // label row so the dates sit under the actual first/last bars. Both charts
      // are fully inline-styled (no CSS class rules exist for either), so this is
      // markup-only. Middle label is circulation vocab: peak outlet count.
      const firstDay = timelineDateLabel(points[0]?.date);
      const lastDay = timelineDateLabel(points[points.length - 1]?.date);
      return `
            <div style="text-align:center;margin:10px 0 4px;">
              <div class="topic-timeline-sparkline" role="img" aria-label="주간 스냅샷별 매체 수, 최다 ${escapeHtml(peak)}개 매체" style="display:inline-flex;flex-direction:column;gap:2px;max-width:100%;vertical-align:bottom;">
                <div style="display:flex;align-items:flex-end;justify-content:center;gap:2px;height:48px;">${bars.join("")}</div>
                <div style="display:flex;justify-content:space-between;gap:12px;min-width:max-content;align-self:center;font-size:0.78rem;color:var(--muted);">
                  <span>${escapeHtml(firstDay)}</span>
                  <span>최다 ${escapeHtml(peak)}개 매체</span>
                  <span>${escapeHtml(lastDay)}</span>
                </div>
              </div>
            </div>`;
    }

    // TEMPORAL-MAP v1: MM/DD display form of a snapshot date ("2026-07-06" ->
    // "7/6"). Falls back to the raw string when it doesn't parse.
    function timelineDateLabel(value) {
      const match = /^\d{4}-(\d{2})-(\d{2})/.exec(String(value || ""));
      if (!match) return String(value || "");
      return `${Number(match[1])}/${Number(match[2])}`;
    }

    async function loadTopicTimeline() {
      const placeholders = document.querySelectorAll(".topic-timeline-section[data-timeline-id]");
      for (const section of placeholders) {
        const id = Number(section.getAttribute("data-timeline-id"));
        if (!Number.isInteger(id) || id <= 0) continue;
        try {
          const response = await fetch(`${API_BASE}/api/topic-timeline/${encodeURIComponent(id)}`);
          if (!response.ok) continue;
          const data = await response.json();
          const points = Array.isArray(data?.points) ? data.points : [];
          // >=2 points required: a single snapshot is not a trajectory.
          if (!data?.found || points.length < 2) continue;
          // TEMPORAL-MAP Phase 4 FIX 1: VARIANCE GATE. Measured live, 652 of 653
          // lineages have an identical outlet_count across every snapshot — the
          // only batches so far span 7/11-7/13, a static 3-day window. A flat
          // chart sitting beside the varying 이슈 확산 현황 plot reads as broken,
          // so a trajectory that did not move renders NOTHING (same posture as
          // found:false). As weekly snapshots accumulate, real movement appears
          // and the section starts showing on its own — honest at every stage.
          const outletSeries = points.map((point) => Number(point?.outlets) || 0);
          if (Math.max(...outletSeries) === Math.min(...outletSeries)) continue;
          const first = points[0];
          const last = points[points.length - 1];
          const firstLabel = timelineDateLabel(first?.date);
          const lastLabel = timelineDateLabel(last?.date);
          const firstOutlets = Number(first?.outlets) || 0;
          const lastOutlets = Number(last?.outlets) || 0;
          if (!firstLabel || !lastLabel || firstOutlets <= 0 || lastOutlets <= 0) continue;
          // Factual direction only (counts up/down/flat) — no sentiment, no
          // 여론/국민정서, and sequence is never framed as 반박.
          const delta = Number(data?.latest_delta) || 0;
          const deltaLine = delta > 0
            ? `<div class="spread-timeline-line">최근 스냅샷에서 ${escapeHtml(delta)}개 매체 증가</div>`
            : (delta < 0
              ? `<div class="spread-timeline-line">최근 스냅샷에서 ${escapeHtml(Math.abs(delta))}개 매체 감소</div>`
              : "");
          section.innerHTML = `
            <h3>확산 추이</h3>
            <div>${escapeHtml(firstLabel)} ${escapeHtml(firstOutlets)}개 매체 → ${escapeHtml(lastLabel)} ${escapeHtml(lastOutlets)}개 매체</div>
            ${deltaLine}
            ${topicTimelineSparklineHtml(points)}
            <div class="evidence-source-meta">주간 스냅샷 기준 · 최초 관측 ${escapeHtml(timelineDateLabel(data?.first_seen))}</div>
            <div class="reader-note">확산 추이는 유통 규모의 변화를 보여줄 뿐, 사실 여부에 대한 검증이 아닙니다.</div>
          `;
          section.hidden = false;
        } catch (error) {
          // fail-silent: trajectory is optional context, never an error state
        }
      }
    }

    async function loadClusterMembers() {
      const placeholders = document.querySelectorAll(".cluster-members-section[data-cluster-id]");
      for (const section of placeholders) {
        const id = Number(section.getAttribute("data-cluster-id"));
        if (!Number.isInteger(id) || id <= 0) continue;
        try {
          const response = await fetch(`${API_BASE}/api/cluster/${encodeURIComponent(id)}/members`);
          if (!response.ok) continue;
          const data = await response.json();
          const members = Array.isArray(data?.members) ? data.members : [];
          if (!data?.found || !members.length) continue;
          const items = members
            .filter((m) => Number(m?.analysis_id) > 0)
            .map((m) => `<li><a href="/?result_id=${encodeURIComponent(Number(m.analysis_id))}">${escapeHtml(m.title || `기사 #${Number(m.analysis_id)}`)}</a></li>`)
            .join("");
          if (!items) continue;
          section.innerHTML = `
            <h3>이 주장을 보도한 다른 기사들</h3>
            <ul class="cluster-members-list">${items}</ul>
            <div class="reader-note">${escapeHtml(data.note || "같은 주장을 다룬 다른 보도 — 검증이 아닙니다")}</div>
          `;
          section.hidden = false;
        } catch (error) {
          // fail-silent: sibling coverage is optional context, never an error state
        }
      }
    }

    // CLUSTER-SURFACE S-b: after the home grid paints, ONE batch fetch patches a
    // small "N개 매체" circulation chip onto cards whose cluster spans >=2 outlets.
    // Pure post-render DOM patch (the grid markup is untouched); cards without a
    // returned count get no chip; any failure leaves the feed exactly as rendered.
    async function loadClusterSizeChips() {
      try {
        const cards = document.querySelectorAll(".topic-card[data-topic-record-id]");
        const byId = new Map();
        for (const card of cards) {
          const rid = Number(card.getAttribute("data-topic-record-id"));
          if (!Number.isInteger(rid) || rid <= 0) continue;
          if (!byId.has(rid)) byId.set(rid, []);
          byId.get(rid).push(card);
        }
        if (!byId.size) return;
        const ids = Array.from(byId.keys()).slice(0, 60);
        const response = await fetch(`${API_BASE}/api/cluster-sizes?ids=${encodeURIComponent(ids.join(","))}`);
        if (!response.ok) return;
        const data = await response.json();
        const sizes = data && data.sizes ? data.sizes : {};
        for (const [rid, ridCards] of byId) {
          const count = Number(sizes[rid]);
          if (!Number.isFinite(count) || count < 2) continue;
          for (const card of ridCards) {
            const top = card.querySelector(".topic-card-top");
            if (!top || top.querySelector(".card-outlet-chip")) continue;
            const chip = document.createElement("span");
            // Reuse the quiet .card-domain chip styling; the marker class only
            // guards against double-patching on overlapping hydrations.
            chip.className = "card-domain card-outlet-chip";
            chip.textContent = `${count}개 매체`;
            top.appendChild(chip);
          }
        }
      } catch (error) {
        // fail-silent: the circulation chip is optional; the feed must never break
      }
    }

    function renderResults(results, focusIndex = selectedResultIndex) {
      if (!results.length) {
        metricsEl.style.display = "none";
        // M17-search-quality: when a search was just attempted (currentReportContext
        // holds the query that produced zero results), surface a clear
        // "관련 기사를 찾지 못했습니다" message. Otherwise (initial landing /
        // explicit clear) keep the generic prompt. Read the query BEFORE
        // clearCurrentReportContext nulls the context.
        const justSearched = !!(currentReportContext && currentReportContext.query);
        clearCurrentReportContext();
        resultsEl.innerHTML = justSearched
          ? '<div class="empty-state">관련 기사를 찾지 못했습니다. 다른 검색어로 다시 시도해 주세요.</div>'
          : '<div class="empty-state">관심 있는 이슈의 상세 보기를 누르거나 검색어를 입력하면 검증 리포트가 표시됩니다.</div>';
        renderHotTopics();
        return;
      }

      renderMetrics(results);
      renderHotTopics(results);
      const hasFocus = Number.isInteger(focusIndex) && results[focusIndex];
      const displayResults = hasFocus ? [results[focusIndex]] : results;
      // DESIGN-DETAIL-4 / CARD-TOPFIX 3a: on ANY single-card detail, hide the
      // aggregate 4-tile #metrics strip — its 판정/신뢰도 values are folded into
      // the top verdict block and over a 1-card context it only restates them.
      // displayResults.length === 1 covers BOTH the focused-history path (hasFocus)
      // AND the public card-open path (loadServerResultById inflates one id with
      // selectedResultIndex null, so hasFocus alone missed it). renderMetrics set
      // #metrics to display:grid just above; we only hide it here. The element/id
      // is kept, so the multi-card full-report path still shows the strip.
      if (displayResults.length === 1) metricsEl.style.display = "none";
      renderSelectedIssueIntro(results, hasFocus ? focusIndex : 0);
      resultsEl.innerHTML = displayResults.map((result) => {
        const parts = getResultPipelineParts(result);
        const {
          verification,
          confidence,
          impact,
          decision,
          claims,
          normalizedClaims,
          sourceQueries,
          sourceCandidates,
          sourceReliabilitySummary,
          evidenceSnippets,
          evidenceExtractionSummary,
          contradictionChecks,
          contradictionSummary,
          biasFramingAnalysis,
          biasFramingSummary,
          debugSummary,
          strength,
          quality,
          level,
        } = parts;
        // MOBILE-POLISH F: strip before escaping (the detail header builds its own
        // title; this var also feeds data-share-title for the share image).
        const title = escapeHtml(stripLeadingTitleMarker(publicInstitutionName(result.title || "제목 없음")));
        const url = escapeHtml(safeUrl(result.original_url || "#"));
        const topic = exportTopicLabel(result, currentReportContext?.query || queryInput?.value || "");
        // DETAIL-CLEANUP-V2: topSource*/recommendedAction/sourceTrustScore consts were
        // consumed only by the removed duplicate .result-summary-grid (box 7). Those
        // data still render elsewhere — 최고 신뢰 출처 in the "출처와 공식 근거"
        // collapsible, 추천 다음 조치 in the 검증 결과 요약 카드, 경고 단계 in the
        // alert badge + core indicator strip. finalScore is kept (used by the
        // "검증 점수 상세" collapsible).
        const finalScore = decision.final_score ?? confidence.policy_confidence_score ?? "-";
        const userContext = buildReportUserContext(parts);
        // SUMMARY-CONTENT-A: the top of the report leads with the news CONTENT
        // (what the government announced), built from the claim sentences, then a
        // smaller VERIFICATION note (how it was checked) below it.
        //  - contentLead: join the first 2-3 claims. Prefer normalizedClaims
        //    (each .claim_text); fall back to the claims string array. Empty → "".
        //  - verifyNote: the existing verification/judgment line. When contentLead
        //    exists we DROP the exportClaimText (claim #1) fallback so the second
        //    line doesn't repeat the first claim; on sparse rows (no contentLead
        //    AND no summary) we keep exportClaimText so something still shows.
        // CLAIM-QUALITY FIX 3: contentLead reads the SAME claims/normalized_claims
        // arrays that 핵심 주장 (above) resolves from, so its first element was
        // routinely the same sentence rendered twice, adjacently, in the main body.
        // Drop any lead entry that overlaps the rendered 핵심 주장 — what remains is
        // the genuinely ADDITIONAL claims #2/#3, so the block only renders when it
        // says something new. The full arrays still appear in the 고급 검증 정보
        // collapsible (핵심 주장과 정규화 / 근거 문장), which is that section's job.
        const heroClaimText = exportClaimText(result);
        const contentLeadClaims = ((Array.isArray(normalizedClaims) && normalizedClaims.length)
          ? normalizedClaims.slice(0, 3).map((claim) => claim && claim.claim_text).filter(Boolean)
          : (Array.isArray(claims) ? claims : []).slice(0, 3).filter(Boolean))
          .filter((claim) => !claimTextsOverlap(claim, heroClaimText));
        const contentLead = contentLeadClaims.join(" ");
        // DETAIL-CLEANUP A6: route through userFacingReportText so the raw
        // English reason-code tails (press_release:/weakly_usable/…) that
        // decision_summary can carry are translated/stripped at display —
        // this line previously bypassed the launderer. "" fallback keeps the
        // truthy guard below rendering nothing on sparse rows.
        // CLAIM-QUALITY FIX 3: the evidence_summary and exportClaimText fallbacks are
        // dropped — both re-showed text the main body already carries (근거 요약 in the
        // 근거 요약과 부족한 맥락 sub-section, and 핵심 주장 directly above). The note
        // now renders ONLY the distinct judgment line; when a row has no
        // decision_summary it renders nothing rather than an echo. No data is lost:
        // evidence_summary is still shown in full in the advanced collapsible.
        const verifyNote = userFacingReportText(
          cleanConceptKeysForDisplay(decision.decision_summary || ""), "");
        const verificationDetails = [
          // DISPLAY-CATEGORY ⑩: ② 최종점수 and ③ 초안 신뢰도 are demoted off the
          // headline into the advanced collapsible. Values are PRESERVED, only
          // relocated — the headline keeps a single number (① 신뢰도). On the
          // public/history feed these can coincide with ① by construction.
          renderCollapsibleSection(
            "검증 점수 상세",
            advDefList([
              ["최종 점수", finalScore],
              // SCORE-CLARITY FIX A: verdict_confidence is a straight copy of
              // policy_confidence_score (main.py:968), so it carries the same
              // 근거 수준 label rather than a second "신뢰도" reading.
              ["초안 근거 수준", verification.verdict_confidence ?? ""],
            ]),
            false,
            "최종 점수와 초안 근거 수준은 화면 상단의 근거 수준을 보조하는 내부 참고 값입니다. 근거 수준과 같을 수 있습니다."
          ),
          renderCollapsibleSection(
            "핵심 주장과 정규화",
            `${renderClaimList(claims)}${renderNormalizedClaims(normalizedClaims)}`,
            false,
            "기사에서 검증 가능한 문장을 뽑고, 주체·행동·대상 같은 구조로 정리한 정보입니다."
          ),
          renderCollapsibleSection(
            "근거 문장",
            renderEvidenceSnippets(claims, evidenceSnippets),
            false,
            "주장과 직접 또는 간접적으로 연결된 기사·출처 문장입니다."
          ),
          renderCollapsibleSection(
            "반박/모순 검사",
            `${renderContradictionSummary(contradictionSummary)}${renderContradictionChecks(claims, contradictionChecks)}`,
            false,
            "같은 대상과 시점에 대해 상충되는 근거가 있는지 보수적으로 확인합니다."
          ),
          renderCollapsibleSection(
            "프레이밍/편향 검사",
            `${renderBiasFramingSummary(biasFramingSummary)}${renderBiasFramingAnalysis(claims, biasFramingAnalysis)}`,
            false,
            "제목과 본문에 감정적 표현, 불확실 표현, 과장된 프레이밍이 있는지 확인합니다."
          ),
          renderCollapsibleSection(
            "출처와 공식 근거",
            `${renderSourceReliabilitySummary(sourceReliabilitySummary)}${renderSourceCandidates(sourceCandidates)}${renderSourceQueries(sourceQueries)}`,
            false,
            "공식기관 후보와 언론 출처가 실제 주장 검증에 얼마나 도움이 되는지 보여줍니다."
          ),
          renderCollapsibleSection(
            "근거 요약과 부족한 맥락",
            `${renderEvidenceExtractionSummary(evidenceExtractionSummary)}${renderEvidenceSources(verification.evidence_sources)}${advDefList([
              ["근거 요약", userFacingReportText(cleanConceptKeysForDisplay(verification.evidence_summary), "-")],
              ["부족한 맥락", buildSafeMissingContext(result)],
              ["마지막 확인", formatDisplayDate(verification.last_checked_at)],
            ])}`,
            false,
            "현재 리포트가 어떤 근거에 기대고 있으며, 추가 확인이 필요한 부분은 무엇인지 정리합니다."
          ),
          // DETAIL-CLEANUP A6: the stage-by-stage pipeline summary is OPERATOR
          // tooling (literally titled 검수자용). Gate it behind the SAME
          // operatorToolsFlagSet() condition the 검토자 액션 / 판단 대시보드
          // blocks already use — Joe still sees it in reviewer mode; the
          // public card doesn't render it.
          operatorToolsFlagSet() ? renderCollapsibleSection(
            "검수자용 검증 단계 요약",
            renderPipelineDebugSummary(debugSummary),
            false,
            "검수자가 분석 단계별 작동 여부와 공식 근거 확보 상태를 확인하는 요약입니다."
          ) : "",
        ].join("");
        const advancedVerificationDetails = renderCollapsibleSection(
          "고급 검증 정보 보기",
          verificationDetails,
          // MOBILE-POLISH J: the OUTER container opens by default so the rich
          // analysis is not missed behind a closed summary. Its 8 inner
          // sub-sections are separate renderCollapsibleSection calls that each
          // still pass open=false, so they stay individually collapsed (headers
          // only). The site-wide default at renderCollapsibleSection stays false.
          true,
          "핵심 판단을 뒷받침하는 주장 추출, 근거 매칭, 반박 검사, 프레이밍 검사, 출처 후보, 내부 점검 정보를 한곳에 모았습니다.",
          // DESIGN-DETAIL-5d FIX 3b: mark the OUTER advanced container so CSS un-boxes
          // only it (not the top-level reader reading-guide, which shares the class).
          "adv-outer"
        );

        return `
          <article class="result-card">
            <div class="headline-card">
              <!-- DESIGN-DETAIL-4b FIX 2: the detail's top labels reuse the HOME card
                   label classes so they read as the SAME flat, text-only, background-
                   free family — NOT filled pills. .card-watch = the home's tier-tinted
                   warning tag; .card-domain = the home's quiet brand-tinted domain
                   text. Same text, same classes as renderTopicCardHtml. -->
              <div class="platform-kicker">
                <span class="card-watch ${alertClass(level)}">${escapeHtml(formatAlert(level))}</span>
                <span class="card-domain">${escapeHtml(topic)}</span>
                <span class="card-domain">검증 뉴스</span>
                ${renderAiStatusBadge(result)}
              </div>
              <h2 class="result-title">
                <a href="${url}" target="_blank" rel="noopener noreferrer">${title}</a>
              </h2>
              <div class="ai-status-note">${escapeHtml(buildAiStatusDescriptor(getResultAiStatus(result).status).note)}</div>
            </div>

            <!-- CARD-AISUMMARY: the 핵심 주장 claim summary MOVED here from the
                 verification-card (bottom) — the always-read "what's claimed"
                 now leads layer 1, before the verdict panel. Byte-identical
                 markup; a move, not a copy. Claim summary, not a verdict. -->
            <div class="verification-item full">
              <span class="label">핵심 주장</span><br>
              ${escapeHtml(exportClaimText(result))}
            </div>

            <!-- DESIGN-DETAIL-4 STEP 1: the consolidated AI VERDICT BLOCK — the first
                 thing the reader sees. Verdict indicators (판정 단계 / AI 초안 판정 /
                 신뢰도 / 공식 출처 상태) as a clean flat row, then the single
                 "왜 이렇게 판단했나요" bullets right below: conclusion → why. This
                 ABSORBS the former core-indicator-strip AND the standalone reasoning
                 section (both removed). safeAiDraftVerdictForExport is surfaced HERE
                 so the duplicate AI-card 초안 판정 tile (STEP 3c) is removed with no
                 data loss; decisionReasonBullets keeps the next-step guidance that
                 made the 9-tile grid's 추천 다음 조치 safe to drop. -->
            <section class="verdict-block">
              <div class="verdict-indicators">
                <div class="verdict-indicator">
                  <span class="verdict-label">판정 단계</span>
                  <span class="verdict-value">${escapeHtml(formatAlert(level))}</span>
                </div>
                <div class="verdict-indicator">
                  <span class="verdict-label">AI 초안 판정</span>
                  <span class="verdict-value">${escapeHtml(safeAiDraftVerdictForExport(result))}</span>
                </div>
                <!-- SCORE-CLARITY FIX A+B: relabelled 신뢰도 -> 근거 수준 (see the
                     card-grid note), with a one-line caveat under the value so the
                     0-100 number cannot be read as a truth percentage. Wording is
                     aligned with the 내부 참고 값 hint on 검증 점수 상세 below. -->
                <div class="verdict-indicator">
                  <span class="verdict-label">근거 수준</span>
                  <span class="verdict-value">${escapeHtml(confidence.policy_confidence_score ?? "-")}</span>
                  <span class="verdict-note">수집된 근거의 양이며 진위 판정이 아닙니다</span>
                </div>
                <div class="verdict-indicator">
                  <span class="verdict-label">공식 출처 상태</span>
                  <span class="verdict-value">${escapeHtml(officialStatusLabel(result))}</span>
                </div>
              </div>
              <div class="verdict-reasoning">
                <h3>왜 이렇게 판단했나요?</h3>
                ${renderBulletList(decisionReasonBullets(userContext, 3))}
              </div>
            </section>

            <!-- CARD-3LAYER S4b: 리뷰 상태 + 사람 검토됨 badge and the fixed honesty
                 line, HOISTED byte-identical from the verification-card below —
                 layer 1 shows claim + verdict + status badge + honesty framing
                 before anything dense. Markup/strings unchanged, only moved. -->
            <div class="verification-intro">
              <div class="summary-tile">
                <span class="label">리뷰 상태</span>
                <strong>${escapeHtml(formatReviewStatus(verification.review_status))}</strong>
                ${result.human_reviewed_at ? `<span class="review-status review-approved">${escapeHtml(HUMAN_REVIEWED_LABEL)}</span>` : ""}
              </div>
            </div>
            <div class="reader-note">
              현재 수집된 기사와 공식 자료 기준의 검증 초안입니다. 절대적 결론이 아니라, 확인 가능한 근거를 바탕으로 한 판단입니다.
            </div>

            <!-- CARD-TOPFIX 3b: reading guide MOVED here (was after the spread
                 placeholder, too late for a first-time reader) — right after the
                 verdict block, before the dense sections. Kept collapsed,
                 content unchanged. -->
            ${renderCollapsibleSection("이 리포트는 이렇게 읽으면 됩니다", renderReadingGuide(userContext), false, "처음 보는 분을 위한 안내입니다. 판정 단계·근거 수준·공식 출처·근거를 어떻게 읽으면 되는지 설명합니다.")}

            <!-- SPREAD-TIMELINE Slice 2 / CARD-3LAYER S4a: circulation annotation
                 placeholder (유통 정보만 — 판정 아님), PROMOTED here as the endgame
                 layer 2 — prominent right after the verdict/guide block instead of
                 buried late. Stays hidden until /api/spread/{id} returns found +
                 outlet_count>=2; found:false / singleton / fetch failure all render
                 NOTHING (fail-silent). Hydrated AFTER the innerHTML pass by
                 loadSpreadAnnotations() via [data-spread-id] — position-independent.
                 Detail view only, and only when the card knows its analysis id. -->
            ${(hasFocus || displayResults.length === 1) && Number(result.result_id) > 0 ? `<section class="public-source-section spread-section" data-spread-id="${Number(result.result_id)}" hidden></section>` : ""}

            <!-- CLUSTER-SURFACE S-a: sibling-coverage placeholder (같은 클러스터의
                 다른 보도 — 유통 정보만, 판정 아님), right after the spread section
                 as part of the layer-2 cluster context. Stays hidden until
                 /api/cluster/{id}/members returns found + members; found:false /
                 empty / fetch failure all render NOTHING (fail-silent). Hydrated
                 AFTER the innerHTML pass by loadClusterMembers() — position-
                 independent. Same gate as the spread placeholder. -->
            ${(hasFocus || displayResults.length === 1) && Number(result.result_id) > 0 ? `<section class="public-source-section cluster-members-section" data-cluster-id="${Number(result.result_id)}" hidden></section>` : ""}

            <!-- TEMPORAL-MAP v1: cluster trajectory placeholder (확산 추이 —
                 유통 정보만, 판정 아님). Hidden until /api/topic-timeline/{id}
                 returns found + >=2 points; found:false / singleton / single
                 point / fetch failure all render NOTHING (fail-silent).
                 Hydrated AFTER the innerHTML pass by loadTopicTimeline() via
                 [data-timeline-id] — position-independent. Same gate as the
                 spread placeholder. Measurement only: dates + outlet counts;
                 no sentiment, no 여론, no verdict vocabulary. -->
            ${(hasFocus || displayResults.length === 1) && Number(result.result_id) > 0 ? `<section class="public-source-section topic-timeline-section" data-timeline-id="${Number(result.result_id)}" hidden></section>` : ""}

            <!-- STEP 2 item 3: the news CONTENT lead (the article's own claim quote)
                 + the muted verification note, just under the verdict so the reader
                 sees conclusion → the claim it's about. Each renders only if truthy. -->
            <!-- CLAIM-DISPLAY-2 FIX A: this block is claims #2/#3 — the members of
                 claims[0..2] that SURVIVED the claimTextsOverlap() de-dup, i.e. the
                 ones that genuinely differ from 핵심 주장 (claim #1) above. Unlabeled,
                 an anonymous blue-barred paragraph read as a mystery "AI 요약"; it is
                 not AI-generated at all (same regex-extracted claims column). The
                 heading reuses the 핵심 주장 markup (.verification-item.full + .label)
                 so the two read as one family, and sits INSIDE the same contentLead
                 conditional so it can never orphan when the de-dup empties the block.
                 Kept as a SIBLING of .report-summary-lead, not a wrapper, so the
                 report-summary-lead + report-verify-note adjacency rule
                 (styles/main.css:881) still matches. -->
            ${contentLead ? `<div class="verification-item full"><span class="label">그 밖의 핵심 주장</span></div>
            <div class="report-summary-lead">${escapeHtml(contentLead)}</div>` : ""}
            ${verifyNote ? `<div class="report-verify-note">${escapeHtml(verifyNote)}</div>` : ""}

            <!-- STEP 2 item 4: 정책 영향 / 소비자 영향 / 금융 시스템 영향 + the
                 .user-explain line. The duplicate "왜 관찰(WATCH)인가" sub-block was
                 removed from renderUserSummarySections (STEP 3b — byte-identical to
                 the kept top reasoning above). -->
            ${renderUserSummarySections(userContext)}

            <!-- STEP 2 item 5: 근거와 출처 요약. -->
            ${renderPublicSourceCards(result)}

            <!-- STEP 2 items 6+7: the AI 종합 검증 판단 residual card. STEP 3c removed
                 the duplicate 초안 판정 tile (now led by the top verdict block); kept:
                 리뷰 상태 + 핵심 주장 + the advanced collapsible. STEP 5: the advanced
                 collapsible (고급 검증 정보 보기 + its 8 sub-collapsibles) is KEPT and
                 COLLAPSED with internals UNTOUCHED — presentation redesign is DETAIL-5. -->
            <section class="verification-card">
              <h3>AI 종합 검증 판단</h3>
              <!-- CARD-3LAYER S4b: the honesty reader-note and the 리뷰 상태 +
                   사람 검토됨 badge tile moved UP into layer 1 (right after the
                   verdict block); CARD-AISUMMARY moved the 핵심 주장 summary up
                   under the headline — moves, not removals. -->
              ${operatorToolsFlagSet() ? renderReviewerDecisionDashboard(result, userContext) : ""}
              ${operatorToolsFlagSet() ? renderReviewerCheckpoints(result) : ""}
              ${operatorToolsFlagSet() ? renderReviewerActionCard(result, userContext) : ""}
              <div class="news-section-title">상세 검증 정보</div>
              <div class="reader-note">더 자세한 주장별 근거, 반박 가능성, 표현 방식 점검은 아래 고급 정보에서 펼쳐볼 수 있습니다.</div>
              ${advancedVerificationDetails}
            </section>
            <!-- CARD-3LAYER S4a: 이미지로 공유 button — split out of the (moved)
                 spread placeholder's conditional, byte-identical markup/condition;
                 stays low near the footer while the spread section sits up top. -->
            ${(hasFocus || displayResults.length === 1) && Number(result.result_id) > 0 ? `<div class="report-error-link"><button type="button" class="secondary" data-share-image="${Number(result.result_id)}" data-share-title="${title}" data-share-official="${escapeHtml(officialStatusLabel(result))}">이미지로 공유</button></div>` : ""}
            <div class="source-link">
              <a class="source-button" href="${url}" target="_blank" rel="noopener noreferrer">원문 보기</a>
            </div>
            <div class="report-error-link">
              <a href="mailto:contact@tickedin.org?subject=${encodeURIComponent('[tickedin 오류신고] 분석 오류 제보')}">이 분석에 오류가 있나요? 신고하기</a>
            </div>
          </article>
        `;
      }).join("");
      // SPREAD-TIMELINE Slice 2: hydrate the detail card's spread placeholder
      // after the innerHTML pass. Fire-and-forget; internally fail-silent.
      loadSpreadAnnotations();
      // CLUSTER-SURFACE S-a: hydrate the sibling-coverage placeholder the same way.
      loadClusterMembers();
      loadTopicTimeline();
    }

    function renderHistory(rows) {
      const records = Array.isArray(rows) ? rows.slice(0, LOCAL_HISTORY_LIMIT) : [];
      // HOME-EMPTY-HIDE: the 최근 분석 기록 section shows only when records
      // exist (template starts it [hidden]; the empty state added home length).
      // Toggled BOTH ways so clearing history re-hides it.
      const recentSectionEl = historyEl ? historyEl.closest("section.recent-analyses") : null;
      if (recentSectionEl) recentSectionEl.hidden = !records.length;
      if (!records.length) {
        historyEl.innerHTML = '<div class="empty-state">아직 저장된 분석 기록이 없습니다.</div>';
        renderHotTopics();
        return;
      }

      historyEl.innerHTML = records.map((row, index) => {
        const strength = row.evidence_strength_summary || {};
        const quality = row.evidence_quality_summary || {};
        const analyzedAt = row.analyzed_at || row.created_at || "-";
        const alert = row.highest_alert || row.policy_alert_level || "LOW";
        const avgConfidence = row.average_confidence ?? row.policy_confidence_score ?? "-";
        const highImpactCount = row.high_impact_count ?? 0;
        const selected = row.id && row.id === currentHistoryId;
        const topResult = getHistoryResults(row)[0] || {};
        const topic = exportTopicLabel(topResult, row.query || "");
        const reviewerAction = getReviewerAction(topResult, row.query || "");
        return `
        <div class="history-row ${selected ? "selected" : ""}" data-history-id="${escapeHtml(row.id || "")}">
          <div class="history-id">#${escapeHtml(index + 1)}</div>
          <div>
            <strong>${escapeHtml(row.query || row.title || "분석 기록")}</strong>
            <div class="history-meta">
              <span class="label">뉴스 개수:</span> ${escapeHtml(row.max_news ?? "-")}
              &nbsp; <span class="label">최고 경고:</span> ${escapeHtml(formatAlert(alert))}
              &nbsp; <span class="label">평균 신뢰도:</span> ${escapeHtml(avgConfidence)}
              &nbsp; <span class="label">고영향 뉴스:</span> ${escapeHtml(highImpactCount)}
              &nbsp; <span class="label">주제:</span> ${escapeHtml(topic)}
              <br>
              <span class="label">근거 강도:</span>
              강함 ${escapeHtml(strength.strong ?? 0)}, 보통 ${escapeHtml(strength.medium ?? 0)}, 약함 ${escapeHtml(strength.weak ?? 0)}
              <br>
              <span class="label">근거 품질:</span>
              강함 ${escapeHtml(quality.strong ?? 0)}, 보통 ${escapeHtml(quality.medium ?? 0)}, 약함 ${escapeHtml(quality.weak ?? 0)}, 평균 ${escapeHtml(quality.average_evidence_quality_score ?? 0)}
              <br>
              <span class="label">분석 시간:</span> ${escapeHtml(analyzedAt)}
              <br>
              <span class="review-status-badge">검토: ${escapeHtml(reviewerActionStatusLabel(reviewerAction.review_status))}</span>
            </div>
          </div>
          <div class="history-actions">
            <button class="history-delete" type="button" data-delete-history-id="${escapeHtml(row.id || "")}">삭제</button>
          </div>
        </div>
      `;
      }).join("");
      renderHotTopics();
    }

    // ===== C15 — Export builders =====
    function plain(value, fallback = "-") {
      if (value === null || value === undefined || value === "") {
        return fallback;
      }
      if (Array.isArray(value)) {
        return value.length ? value.join(", ") : fallback;
      }
      return String(value);
    }


    function getEvidenceSummaryForReport(result) {
      const verification = result?.verification_card || result || {};
      const extraction = verification.evidence_extraction_summary || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      return {
        strength: debug.evidence_strength_summary || extraction.evidence_strength_summary || {},
        quality: debug.evidence_quality_summary || extraction.evidence_quality_summary || result?.evidence_quality_summary || {},
      };
    }

    function publicSourceNotesForReport(result) {
      const { items, reliability, officialLimitation } = publicSourceCards(result);
      if (officialEvidenceInsufficientForExport(result)) {
        return [
          "뉴스 원문은 분석 대상이지만, 공식 검증 근거로는 사용하지 않았습니다.",
          "확인된 공식 후보는 배경 맥락 수준이며, 직접 근거로 사용하기 어렵습니다.",
          publicExportOfficialLimitation(result),
        ];
      }
      if (!items.length) {
        return [officialLimitation || "표시할 수 있는 주요 출처 카드가 부족합니다."];
      }
      const notes = items.slice(0, 3).map((source) => {
        const title = userFacingReportText(publicInstitutionName(source.title || source.source_title || source.publisher || source.url || source.source_url || "출처 정보 확인 필요"), "출처 정보 확인 필요");
        const type = publicSourceTypeLabel(source);
        const trace = sourceTraceability(source, reliability);
        const support = trace.label || publicSupportLabel(source);
        const reason = userFacingReportText(source.reliability_reason || source.match_reason || trace.explanation || publicSourceReason(source, reliability), "공식 자료와 직접 일치하는지 추가 확인이 필요합니다.");
        const url = source.url || source.source_url || "";
        return `${title} - ${type}, ${support}. ${reason}${url ? ` (${url})` : " (URL 없음)"}`;
      });
      if (officialLimitation) {
        notes.push(officialLimitation);
      }
      return notes;
    }

    function hasExcludedOfficialDetailText(value) {
      const text = plain(value, "");
      const exclusionSignal = /official_topic_mismatch|official_detail_missing|official_detail_url_missing|official_body_mismatch|official_document_excluded|official_candidate_not_fetched|official_candidate_without_body|official_candidate_metadata_overlap_without_body|insufficient matched query|concept overlap|not_directly_related|unrelated|excluded|mismatch|불일치|제외|직접 일치가 부족/i.test(text);
      const officialDetailSignal = /FSC detail press URL|보도자료 상세 페이지|금융위원회 보도자료|국토교통부|금융감독원|한국은행|Financial Services Commission|Financial Supervisory Service|Bank of Korea|Ministry of Land/i.test(text);
      return exclusionSignal || (officialDetailSignal && /후보|제외|불일치|부족|미확인|수집 실패|직접 일치/i.test(text));
    }

    function publicExportOfficialLimitation(result) {
      const verification = result?.verification_card || result || {};
      const reliability = verification.source_reliability_summary || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      const state = buildOfficialEvidenceState(reliability, debug);
      return buildOfficialEvidenceNarrative(state).sourceSummaryNote
        || "공식 자료 후보는 있었지만, 기사 핵심 주장과 직접 일치하지 않아 공개 근거에서는 제외했습니다.";
    }

    function officialEvidenceInsufficientForExport(result) {
      const verification = result?.verification_card || result || {};
      const reliability = verification.source_reliability_summary || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      const state = buildOfficialEvidenceState(reliability, debug);
      const directScore = numberValue(state.officialDirectMatchScore, 0);
      const directCount = numberValue(debug.official_resolution_direct_matches, 0);
      const contextCount = numberValue(debug.official_resolution_contextual_matches, 0);
      const sources = [
        ...(verification.source_candidates || result?.source_candidates || []),
        ...(verification.evidence_sources || []),
      ];
      const semanticScore = sources.reduce((best, source) => Math.max(
        best,
        numberValue(source?.semantic_match_score, 0),
        numberValue(source?.policy_alignment_score, 0),
        numberValue(source?.official_evidence_score, 0),
        numberValue(source?.official_final_direct_match_score, 0)
      ), 0);
      return ["body_unmatched", "candidate_only", "not_found"].includes(state.officialEvidenceStatus)
        || directScore <= 0
        || (directCount === 0 && contextCount === 0)
        || semanticScore < 30;
    }

    function safeAiDraftVerdictForExport(result) {
      if (officialEvidenceInsufficientForExport(result)) return "사람 검토 대기";
      const verification = result?.verification_card || result || {};
      const label = formatVerdict(verification.verdict_label);
      if (/임시\s*검증\s*완료|검증\s*완료|공식\s*확인/i.test(label) && !hasDirectOfficialSupport(result)) {
        return "사람 검토 대기";
      }
      return label || "사람 검토 대기";
    }

    function getSafeAiDraftEvidenceSummary(result) {
      if (officialEvidenceInsufficientForExport(result)) {
        return "공식 상세자료 후보는 확인했지만, 기사 핵심 주장과 직접 일치하는 공식 근거는 아직 충분하지 않습니다.";
      }
      return publicExportEvidenceSummary(result);
    }

    function getSafeAiDraftMissingContext(result) {
      if (officialEvidenceInsufficientForExport(result)) {
        const extra = hasContradictionConcern(result) ? " 같은 시점과 대상인지도 추가 확인이 필요합니다." : "";
        return `더 직접적인 공식 보도자료나 정책 설명자료 확인이 필요합니다.${extra}`;
      }
      return publicExportMissingContext(result);
    }

    function publicExportEvidenceSummary(result) {
      const verification = result?.verification_card || result || {};
      const summary = userFacingReportText(plain(verification.evidence_summary, ""), "");
      if (officialEvidenceInsufficientForExport(result)) {
        return "공식 상세자료 후보는 확인했지만, 기사 핵심 주장과 직접 일치하는 공식 근거는 아직 충분하지 않습니다.";
      }
      if (!summary) {
        return publicExportOfficialLimitation(result);
      }
      if (hasExcludedOfficialDetailText(summary)) {
        return publicExportOfficialLimitation(result);
      }
      return sanitizePublicExportText(summary);
    }

    function publicExportMissingContext(result) {
      return buildSafeMissingContext(result);
    }

    function publicExportTopSource(result) {
      const { items, officialLimitation } = publicSourceCards(result);
      const source = items[0];
      if (!source) {
        return officialLimitation || "공식 상세 근거 부족";
      }
      const title = userFacingReportText(publicInstitutionName(source.title || source.source_title || source.publisher || source.url || source.source_url || "출처 정보 확인 필요"), "출처 정보 확인 필요");
      return sanitizePublicExportText(title);
    }

    function sanitizePublicExportText(value) {
      let text = userFacingReportText(value || "", "");
      const replacements = {
        official_topic_mismatch: "공식 자료 주제 불일치",
        official_detail_missing: "공식 상세 자료 부족",
        official_detail_url_missing: "공식 상세 URL 부족",
        official_candidate_not_fetched: "공식 후보 문서 미수집",
        official_body_mismatch: "공식 본문 불일치 가능성",
        official_body_fetched_unmatched: "공식 본문 직접 일치 부족",
        official_search_url_candidate: "공식 검색 후보",
        official_candidate_metadata_overlap_without_body: "공식 후보 본문 미확인",
        official_candidate_without_body: "공식 후보 본문 미확보",
        official_document_excluded: "공식 문서 직접 근거 제외",
        official_search_only: "공식 검색 결과 수준",
        current_news_collection: "뉴스 수집 결과",
        strict_staff_needs_review: "사람 검토 필요",
        needs_context: "맥락 추가 확인 필요",
        no_match: "직접 일치 없음",
        no_body_text: "본문 없음",
        context_only: "맥락 참고용",
        loaded_terms: "추출된 핵심 용어",
        press_release: "보도자료",
        "insufficient matched query/material concept overlap": "기사 주제와 공식 자료의 직접 일치가 부족함",
        "unrelated general finance/foreign-affairs press release": "기사 주제와 직접 관련성이 낮은 일반 보도자료",
      };
      Object.entries(replacements).forEach(([raw, label]) => {
        text = text.replaceAll(raw, label);
      });
      text = text.replace(/unrelated\s+general\s+finance\/foreign-affairs\s+press\s+release/gi, "기사 주제와 직접 관련성이 낮은 일반 보도자료");
      text = text.replace(/unrelated\s+general\s+finance/gi, "기사 주제와 직접 관련성이 낮은 일반 금융 자료");
      text = text.replace(/foreign-affairs/gi, "대외 협력 관련");
      text = text.replace(/insufficient\s+matched\s+query\/material\s+concept\s+overlap/gi, "기사 주제와 공식 자료의 직접 일치가 부족함");
      text = text.replace(/official[_\s-]*search[_\s-]*url[_\s-]*candidate/gi, "공식 검색 후보");
      text = text.replace(/official[_\s-]*body[_\s-]*fetched[_\s-]*unmatched/gi, "공식 본문 직접 일치 부족");
      text = text.replace(/official[_\s-]*candidate[_\s-]*metadata[_\s-]*overlap[_\s-]*without[_\s-]*body/gi, "공식 후보 메타데이터만 일부 일치");
      text = text.replace(/current[_\s-]*news[_\s-]*collection/gi, "뉴스 수집 결과");
      text = text.replace(/strict[_\s-]*staff[_\s-]*needs[_\s-]*review/gi, "사람 검토 필요");
      text = text.replace(/needs[_\s-]*context/gi, "맥락 추가 확인 필요");
      text = text.replace(/\bno[_\s-]*match\b/gi, "직접 일치 없음");
      text = text.replace(/\bloaded\s+terms\b/gi, "추출된 핵심 용어");
      text = text.replace(/\bsource\s+retrieval\b/gi, "출처 탐색");
      text = text.replace(/\bbias\s+framing\b/gi, "프레이밍/편향 검사");
      text = text.replace(/\bcontradiction\b/gi, "반박/모순 검사");
      text = text.replace(/\bpress\s+release\b/gi, "보도자료");
      text = text.replace(/https?:\/\/(?:www\.)?fsc\.go\.kr\/no\d+\/?\S*/gi, "금융위원회 보도자료 상세 페이지");
      text = text.replace(/\bFSC detail press URL\b/gi, "금융위원회 보도자료 상세 페이지");
      text = text.replace(/\bFSC detail press URL-like explanations\b/gi, "금융위원회 보도자료 상세 페이지 관련 설명");
      text = text.replace(/금융위원회 보도자료 상세 페이지-like explanations/gi, "금융위원회 보도자료 상세 페이지 관련 설명");
      text = text.replace(/\bdetail press URL\b/gi, "상세 보도자료 페이지");
      text = text.replace(/\bpress URL\b/gi, "보도자료 페이지");
      text = text.replace(/\bFSC\b/g, "금융위원회");
      text = text.replace(/Google\s*RSS\s*실패/gi, "뉴스 검색 결과");
      text = text.replace(/Google\s*RSS[^.!?。]*(?:[.!?。]|$)/gi, "뉴스 검색 결과에서 확보한 기사입니다.");
      text = text.replace(/검색\s*HTML\s*fallback/gi, "뉴스 검색 결과");
      text = text.replace(/검색\s*HTML[^.!?。]*(?:[.!?。]|$)/gi, "뉴스 검색 결과에서 확보한 기사입니다.");
      text = text.replace(/\bfallback\b/gi, "");
      text = text.replace(/Best official document relevance[^.!?。]*(?:[.!?。]|$)/gi, "공식 자료와의 직접 일치 여부가 충분히 확인되지 않았습니다.");
      text = text.replace(/relevance below threshold[^.!?。]*(?:[.!?。]|$)/gi, "공식 자료와의 직접 일치 여부가 충분히 확인되지 않았습니다.");
      text = text.replace(/insufficient\s+material\s+policy\s+concept\s+overlap/gi, "공식 자료와 기사 핵심 주장 사이의 직접 일치 여부는 추가 확인이 필요합니다.");
      text = text.replace(/insufficient\s+matched[^.!?。]*(?:[.!?。]|$)/gi, "공식 자료가 기사 핵심 주장과 직접적으로 일치하는지 추가 확인이 필요합니다.");
      text = text.replace(/matched\s+query\/material\s+concept\s+overlap/gi, "공식 자료와 기사 핵심 주장의 직접 일치 여부");
      text = text.replace(/query\/material\s+concept\s+overlap/gi, "공식 자료와 기사 핵심 주장의 직접 일치 여부");
      text = text.replace(/\b(?:press_release|official_notice|official_page|official_search)\s*:\s*/gi, "");
      text = text.replace(/\b(?:debug|pipeline|raw)\b/gi, "");
      text = text.replace(/https?:\/\/\S+/gi, (url) => {
        if (/go\.kr|gov\.kr|bok\.or\.kr|fss\.or\.kr|molit\.go\.kr|korea\.kr/i.test(url)) {
          return "공식기관 상세 페이지";
        }
        return url;
      });
      text = publicInstitutionName(text);
      return text || "자료 부족";
    }

    function exportLine(lines, value = "") {
      lines.push(sanitizePublicExportText(value));
    }

    function exportTopicLabel(result, query) {
      const primaryText = [
        query,
        result?.title,
        result?.summary,
        result?.final_decision?.decision_summary,
        result?.verification_card?.claim_text,
      ].filter(Boolean).join(" ");
      const topicText = sanitizePublicExportText(result?.topic || "");
      const allText = [
        primaryText,
        topicText,
      ].filter(Boolean).join(" ");
      const primaryHasRealEstate = /부동산|양도세|양도소득세|다주택|주택|분양|재건축|재개발|청약|토지|임대|전세사기|종부세|공시가격|세무조사/.test(primaryText);
      const primaryHasJeonseLoan = /전세대출|버팀목|전세자금|주담대|주택담보대출/.test(primaryText);

      if (/전세사기/.test(allText)) return "전세사기";
      if (/부동산/.test(String(query || ""))) return "부동산";
      if (primaryHasRealEstate && !primaryHasJeonseLoan) return "부동산";
      if (/전세대출|전세자금|버팀목/.test(primaryText)) return "전세대출";
      if (/금융위|금감원|금리|은행|대출|DSR|가계부채|연체율|한국은행/.test(primaryText)) return "금융/정책";
      if (primaryHasRealEstate) return "부동산";
      if (/전세대출/.test(topicText) && !primaryHasJeonseLoan) return "확인 필요";
      if (/부동산/.test(topicText)) return "부동산";
      if (/금융|정책|금리|은행|대출/.test(topicText)) return "금융/정책";
      return topicText && topicText !== "자료 부족" ? topicText : "확인 필요";
    }

    function selectedResultsForExport() {
      const context = currentReportContext || {};
      const results = Array.isArray(context.results) ? context.results : [];
      if (Number.isInteger(selectedResultIndex) && results[selectedResultIndex]) {
        return [results[selectedResultIndex]];
      }
      return results;
    }

    function keywordSetForClaimAlignment(text) {
      const normalized = cleanArticleTextForPolicyAnalysis(text || "").toLowerCase();
      const matches = normalized.match(/[가-힣a-z0-9]{2,}/g) || [];
      const stopwords = new Set([
        "있다", "했다", "한다", "대한", "관련", "기준", "통해", "위해",
        "지원", "정책", "검토", "추진", "발표", "정부", "기사", "뉴스",
      ]);
      return new Set(matches.filter((word) => !stopwords.has(word)));
    }

    function claimLooksAlignedWithResult(result, claimText) {
      const claimWords = keywordSetForClaimAlignment(claimText);
      if (!claimWords.size) return false;
      const contextWords = keywordSetForClaimAlignment([
        result?.title,
        result?.topic,
        result?.summary,
        result?.final_decision?.decision_summary,
        result?.verification_card?.evidence_summary,
      ].filter(Boolean).join(" "));
      let overlap = 0;
      claimWords.forEach((word) => {
        if (contextWords.has(word)) overlap += 1;
      });
      return overlap >= 2;
    }

    function isGenericClaimPlaceholder(value) {
      const text = sanitizeDisplayText(value || "");
      return !text
        || /핵심\s*정책\s*주장만\s*표시|확인된\s*핵심\s*정책\s*주장|내용\s*확인\s*필요|추가\s*확인해야\s*합니다|자료\s*부족/i.test(text)
        || /fallback|debug|pipeline|insufficient matched|relevance below threshold/i.test(text);
    }

    function isGenericClaimPlaceholder(value) {
      const text = sanitizeDisplayText(value || "");
      return !text
        || /핵심\s*정책\s*주장만\s*표시|선택한\s*기사에서\s*확인된\s*핵심|내용\s*확인\s*필요|추가\s*확인해야\s*합니다|자료\s*부족/i.test(text)
        || /fallback|debug|pipeline|insufficient matched|relevance below threshold|concept overlap|official_detail|official_topic/i.test(text);
    }

    function officialEvidenceStateForResult(result) {
      const verification = result?.verification_card || result || {};
      return buildOfficialEvidenceState(
        verification.source_reliability_summary || {},
        verification.debug_summary || result?.debug_summary || {}
      );
    }

    // FRESHNESS Phase 2 — conservative "freshly-broken" gate. Distinguishes a
    // just-published issue with no official primary source yet (🔥 fresh,
    // confirmation pending) from an old article the matcher simply missed
    // (⚠️ unconfirmed). Returns true ONLY when ALL FOUR conditions hold:
    //   (1) a real article publish date is present in debug_summary
    //       (article_published_at — added backend-side only for trusted sources),
    //   (2) the collection source is trusted: google_rss / naver_api. HTML
    //       fallback synthesizes published=NOW (always looks fresh) and is
    //       already excluded backend-side; re-checked here as defense in depth,
    //   (3) the publish date is within the freshness window, AND
    //   (4) Phase-1's "no official primary source found" state
    //       (officialEvidenceStatus === "not_found").
    // FAIL-SAFE: any missing/unparseable date, untrusted source, old date, or
    // non-not_found state → false → NO badge, leaving the existing ⚠️ /
    // official-state line untouched. An old-unmatched card is never mislabeled.
    const FRESHNESS_WINDOW_DAYS = 7; // matches the relaxed recent window (news_collector.py:1133); do NOT widen to 30
    const TRUSTED_FRESHNESS_SOURCES = new Set(["google_rss", "naver_api"]);
    const FRESHNESS_BADGE_LABEL = "새로 부상한 이슈 · 공식 확인 진행 중";

    function isFreshlyBroken(result) {
      const verification = result?.verification_card || result || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      // (2) trusted source
      if (!TRUSTED_FRESHNESS_SOURCES.has(String(debug.article_source || ""))) return false;
      // (1) real date present
      const rawDate = debug.article_published_at;
      if (!rawDate) return false;
      // Date.parse handles BOTH ISO-8601 (Naver API published_at) and
      // RFC822/RFC1123 (Google RSS published) — the only formats the two
      // trusted sources emit, each carrying an explicit tz offset (mirrors the
      // parsedate_to_datetime tz handling at news_collector.py:719-727).
      const parsedMs = Date.parse(rawDate);
      if (Number.isNaN(parsedMs)) return false; // unparseable → unknown → no badge
      // (3) within window. Upper bound rejects OLD articles (the core fail-safe);
      // a small negative lower bound tolerates clock skew / tz-parse jitter
      // without ever admitting a genuinely old date.
      const ageDays = (Date.now() - parsedMs) / 86400000;
      if (!(ageDays <= FRESHNESS_WINDOW_DAYS && ageDays >= -1)) return false;
      // (4) no official primary source found (Phase-1 not_found gate)
      return officialEvidenceStateForResult(result).officialEvidenceStatus === "not_found";
    }

    function officialDirectScoreForResult(result) {
      return numberValue(officialEvidenceStateForResult(result).officialDirectMatchScore, 0);
    }

    function hasDirectOfficialSupport(result) {
      return officialEvidenceStateForResult(result).officialEvidenceStatus === "direct_support";
    }

    function needsHumanReviewForResult(result) {
      const verification = result?.verification_card || result || {};
      const decision = result?.final_decision || {};
      const debug = verification.debug_summary || result?.debug_summary || {};
      const reviewText = [
        verification.review_status,
        verification.verdict_label,
        decision.policy_alert_level,
        debug.needs_human_review,
      ].join(" ");
      return /review|official_confirmation|needs|WATCH|관찰|검토|확인/i.test(reviewText);
    }

    function stripCertaintyWords(text) {
      return String(text || "")
        .replace(/공식\s*확인(?:됐다|되었다|됨)?/g, "추가 확인이 필요")
        .replace(/검증(?:됐다|되었다|됨)/g, "추가 확인이 필요")
        .replace(/입증(?:됐다|되었다|됨)/g, "추가 확인이 필요")
        .replace(/확인(?:됐다|되었다|됨)/g, "확인할 필요가 있음")
        .replace(/확정(?:됐다|되었다|됨)/g, "확정 전 확인이 필요")
        .replace(/\s+/g, " ")
        .trim();
    }

    // CLAIM-QUALITY FIX 2: the display cap, raised 220 -> 360 and kept in lockstep
    // with _CLAIM_MAX_CHARS in claim_extractor.py so the backend and frontend
    // truncation layers agree instead of each shaving the claim independently.
    const CLAIM_MAX_CHARS = 360;

    // Cut on a SENTENCE boundary when one sits inside the cap (a complete
    // sentence needs no ellipsis), else on a WORD boundary — never mid-word or
    // mid-syllable, which is what read as 문장이 끊김.
    // CLAIM-DISPLAY-3 — DISPLAY-ONLY polish for claims the OLD splitter severed.
    // The ~89% of stored rows the positional-safety re-extraction cannot repair
    // keep their severed text; this only reformats the render string. Stored
    // claims / claim_text / normalized_claims are untouched, so no claim_index
    // shifts and evidence_snippets / contradiction_checks / bias_framing_analysis
    // / source_candidates / source_queries stay correctly attached.
    const CLAIM_TERMINAL_PUNCT = /[.!?…]$/;
    const CLAIM_VERB_ENDER = /[다요죠음임됨함]$/;
    // A Korean sentence never ENDS on a josa/connective — it binds a noun to the
    // rest of the clause — so one at end-of-string means the text was cut
    // mid-clause. Ordered longest-first, and this test MUST run BEFORE the
    // verb-ender test: "보다" ends in 다, so checking the ender first would
    // "complete" the exact fragment we are trying to catch ("…지난해(1.1%)보다").
    const CLAIM_DANGLING_JOSA =
      /(?:이라고|라고|에게|에서|으로|부터|까지|보다|와|과|의|를|을|은|는|로|며|고)$/;
    // Minimum surviving length for a josa-trim to be worth doing — roughly one
    // full policy clause. Below it, an awkward tail beats a near-empty claim.
    const CLAIM_TRIM_FLOOR_CHARS = 40;

    function polishClaimEnding(text) {
      const value = String(text || "").trim();
      if (!value) return value;
      // Already ends cleanly -> BYTE-IDENTICAL. This is what makes the change a
      // no-op on clean claims, so it composes with re-extraction instead of
      // fighting it: repaired rows simply stop matching anything below.
      if (CLAIM_TERMINAL_PUNCT.test(value)) return value;

      if (CLAIM_DANGLING_JOSA.test(value)) {
        // SEVERED: trim back to the last clean boundary and mark the cut with
        // "…". Purely subtractive — never adds or alters meaning.
        // The (?=\s|$) guard is load-bearing: without it the "." inside a decimal
        // ("지난해(1.1%)보다") reads as a sentence end and the claim gets cut
        // mid-number to "지난해(1…". Policy claims are dense with decimals.
        const boundary = /[.!?…](?=\s|$)|[다요죠음임됨함](?=\s)|[,，、·]/g;
        let lastEnd = 0;
        let match;
        while ((match = boundary.exec(value)) !== null) {
          lastEnd = match.index + match[0].length;
        }
        // CLAIM-DISPLAY-3 Phase 2b: an ABSOLUTE floor, not the >=half rule.
        // The common severed shape is "one complete sentence. + severed
        // fragment", where the clean boundary sits well before the halfway
        // point — so a >=half guard declined to trim on exactly the population
        // this targets, making the fix inert. A floor instead asks the only
        // question that matters: does enough substantial text survive? Trimming
        // DISCARDS the severed fragment (incomplete anyway) in favour of a clean
        // sentence; the "…" honestly signals that more existed.
        const trimmed = value.slice(0, lastEnd)
          .replace(/[,，、·\s]+$/, "")
          .replace(/\.+$/, "")
          .trim();
        // CLAIM-DISPLAY-3 Phase 3: FRAGMENT-ONLY case. When the claim is a single
        // severed sentence there is no earlier clause to fall back to — the
        // boundary scan finds nothing (lastEnd 0) and the old splitter stored
        // only the fragment ("한은은 … 지난해(1.1%)보다"). Display cannot recover
        // words that were never stored, so the honest move is to MARK the cut:
        // a bare "…" reads as an excerpt rather than a glitch. Adds no meaning.
        // The real repair is re-extraction, which restores the missing clause.
        // Double-ellipsis is structurally impossible here: the CLAIM_TERMINAL_
        // PUNCT early-return above already sent any "…"/"."/"!"/"?" ending back
        // byte-identical before reaching this branch.
        if (trimmed.length < CLAIM_TRIM_FLOOR_CHARS) return `${value}…`;
        return `${trimmed}…`;
      }

      // COMPLETE sentence missing only its full stop — the old splitter consumed
      // no punctuation, so a large share of stored claims end "…밝혔다" and are
      // NOT truncated. A "…" here would falsely mark them as cut off; a bare
      // period is cosmetic and changes no meaning.
      if (CLAIM_VERB_ENDER.test(value)) return `${value}.`;

      // Unrecognized shape (headline-style noun ending, etc.) -> leave alone.
      return value;
    }

    function truncateClaimOnBoundary(text, maxLength) {
      const value = String(text || "");
      if (value.length <= maxLength) return polishClaimEnding(value);
      const window = value.slice(0, maxLength);
      // Constructed per call: a shared /g regex would carry lastIndex between calls.
      // CLAIM-DISPLAY-3: same (?=\s|$) decimal guard as polishClaimEnding — this
      // regex had the identical latent bug, cutting a >cap claim mid-number at
      // the "." of "1.1%" whenever that fell near the boundary.
      const sentenceEnd = /[.!?…](?=\s|$)|[다요죠음임됨함](?=\s)/g;
      let lastEnd = 0;
      let match;
      while ((match = sentenceEnd.exec(window)) !== null) {
        lastEnd = match.index + match[0].length;
      }
      // Only honour the sentence boundary if it keeps at least half the budget,
      // otherwise an early period would gut the claim.
      if (lastEnd >= Math.floor(maxLength / 2)) {
        return polishClaimEnding(window.slice(0, lastEnd).trim());
      }
      return `${window.replace(/\s+\S*$/, "").trim()}...`;
    }

    // CLAIM-QUALITY FIX 3: do two claim strings say the same thing? Compared on
    // letters/digits/hangul only, so the hedge prefix ("보도 내용은 "), punctuation
    // and a trailing "..." don't hide a duplicate. Containment (not equality) is
    // the test because the hero claim is a sanitized/truncated form of the same
    // sentence. The 20-char floor keeps short stubs from matching everything.
    function claimTextsOverlap(a, b) {
      const key = (value) => String(value || "").replace(/[^0-9a-z가-힣]/gi, "").toLowerCase();
      const left = key(a);
      const right = key(b);
      if (!left || !right) return false;
      const shorter = left.length <= right.length ? left : right;
      const longer = left.length <= right.length ? right : left;
      if (shorter.length < 20) return false;
      return longer.includes(shorter);
    }

    function limitClaimSentences(text, maxSentences = 2, maxLength = CLAIM_MAX_CHARS) {
      const cleaned = cleanArticleTextForPolicyAnalysis(userFacingReportText(text, "")) || "";
      const sentences = splitArticleSentences(cleaned)
        .map((sentence) => sentence.trim())
        .filter(Boolean)
        .filter((sentence, index, arr) => arr.indexOf(sentence) === index);
      const limited = (sentences.length ? sentences.slice(0, maxSentences).join(" ") : cleaned)
        .replace(/\s+/g, " ")
        .trim();
      return truncateClaimOnBoundary(limited, maxLength);
    }

    function claimLooksSuspicious(text) {
      const value = String(text || "");
      const numberCount = (value.match(/\d+/g) || []).length;
      const commaCount = (value.match(/[,，]/g) || []).length;
      return /사원\s*전원\s*소환|전원\s*소환|\d+\s*개\s*사원|무더기\s*소환/i.test(value)
        // CLAIM-QUALITY FIX 2 follow-on: this "long AND number-dense = body dump"
        // guard was calibrated against the old 220 cap (180/220 ≈ 0.82 of budget).
        // With the cap at CLAIM_MAX_CHARS=360 the untouched 180 would suppress
        // ordinary long policy claims — which return "" and fall back to a SHORTER
        // candidate, re-creating the very problem this change fixes. Rescaled to
        // the same fraction of the new budget; the heuristic's intent is unchanged.
        || (value.length > 300 && numberCount >= 4)
        || commaCount >= 4;
    }

    function sanitizeClaimText(claimText, result = {}) {
      let claim = limitClaimSentences(claimText, 2, CLAIM_MAX_CHARS);
      if (!claim || isGenericClaimPlaceholder(claim)) return "";
      if (claimLooksSuspicious(claim)) return "";
      if (!hasDirectOfficialSupport(result) || officialDirectScoreForResult(result) <= 0) {
        claim = stripCertaintyWords(claim);
      }
      claim = sanitizePublicExportText(claim);
      if (!claim || isGenericClaimPlaceholder(claim)) return "";
      return limitClaimSentences(claim, 2, CLAIM_MAX_CHARS);
    }

    function cautiousClaimPrefix(result) {
      if (hasDirectOfficialSupport(result) && !needsHumanReviewForResult(result)) return "";
      return needsHumanReviewForResult(result) ? "기사 제목과 요약 기준으로는 " : "보도 내용은 ";
    }

    // CARD-CLAIM-QUALITY A2 ② — quote-lead detection + wrapper strip (DISPLAY
    // ONLY). Mirrors the measured probe heuristic (scripts/claim_quality_size_
    // probe.py): a quote-lead is a reported-speech ARTICLE sentence ("X 회장은
    // '…'고 말했다") that claim_extractor picked as the top sentence, so it renders
    // as a body dump instead of a clean 핵심 주장 (measured 839 rows / 11%). The
    // fix only SELECTs a cleaner existing field or TRIMS the wrapper — it never
    // rewrites, summarizes, or fabricates.
    const CLAIM_QUOTE_CHARS = "\"'“”‘’＂「」『』";
    const CLAIM_SAID_VERBS = /(말했다|밝혔다|강조했다|설명했다|전했다|덧붙였다|지적했다|주장했다|당부했다|약속했다|다짐했다)/;
    const CLAIM_SPEAKER_LEAD = /[가-힣]{2,}\s*(회장|장관|위원장|대표|총리|청장|처장|사장|부총리|의원|대통령)\s*[은는이가]/;
    const CLAIM_QUOTED_SPAN = new RegExp(
      "[" + CLAIM_QUOTE_CHARS + "]([^" + CLAIM_QUOTE_CHARS + "]{6,})[" + CLAIM_QUOTE_CHARS + "]", "g");

    function claimIsQuoteLead(text) {
      const value = String(text || "");
      if (!value) return false;
      const quoteCount = [...value].filter((ch) => CLAIM_QUOTE_CHARS.includes(ch)).length;
      const hasQuote = quoteCount >= 2;
      const said = CLAIM_SAID_VERBS.test(value);
      const speaker = CLAIM_SPEAKER_LEAD.test(value);
      return (hasQuote && (said || speaker)) || (speaker && said);
    }

    function stripQuoteLeadWrapper(text) {
      const value = String(text || "");
      let best = "";
      let match;
      CLAIM_QUOTED_SPAN.lastIndex = 0;
      while ((match = CLAIM_QUOTED_SPAN.exec(value)) !== null) {
        const inner = (match[1] || "").trim();
        if (inner.length > best.length) best = inner;
      }
      // Only accept a substantial quoted statement (the reported content); a
      // short quoted term is not a claim -> "" signals "no clean strip".
      return best.length >= 10 ? best : "";
    }

    function buildReviewerSafeClaim(result) {
      const verification = result?.verification_card || result || {};
      const candidates = [
        verification.claim_text,
        Array.isArray(verification.claims) ? verification.claims[0] : "",
        Array.isArray(result?.claims) ? result.claims[0] : "",
        result?.summary,
      ]
        .map((item) => sanitizeClaimText(item, result))
        .filter((item) => item && claimLooksAlignedWithResult(result, item));
      let claim = candidates[0] || sanitizeClaimText(fallbackClaimFromTitle(result), result);
      // CARD-CLAIM-QUALITY A2 ②: only when the resolved claim is a quote-lead,
      // (a) prefer the structured normalized claim (contentLead's source, when
      // the detail payload carries it) if it is clean + aligned + not itself a
      // quote-lead, else (b) strip the wrapper to the quoted statement. If
      // neither yields clean aligned text, keep the original (a quote lead is
      // still truthful). CLEAN claims skip this block entirely (byte-identical).
      if (claim && claimIsQuoteLead(claim)) {
        const normalized = Array.isArray(verification.normalized_claims)
          ? verification.normalized_claims
          : (Array.isArray(result?.normalized_claims) ? result.normalized_claims : []);
        const normalizedText = sanitizeClaimText(
          (normalized[0] && normalized[0].claim_text) || "", result);
        if (normalizedText
            && !claimIsQuoteLead(normalizedText)
            && claimLooksAlignedWithResult(result, normalizedText)) {
          claim = normalizedText;
        } else {
          const stripped = sanitizeClaimText(stripQuoteLeadWrapper(claim), result);
          if (stripped && claimLooksAlignedWithResult(result, stripped)) {
            claim = stripped;
          }
        }
      }
      if (!claim) {
        claim = "기사 제목과 요약 기준으로 핵심 주장을 추가 확인해야 합니다.";
      }
      const prefix = cautiousClaimPrefix(result);
      // CARD-CLAIM-QUALITY fix ①: phrase-aware dedup. The title fallback can embed
      // "…기사 제목과 요약 기준으로 추가 확인이 필요합니다" mid/end of the claim,
      // which the startsWith check missed → the prefix was glued on a second time
      // (double hedge). Skip the prefix whenever the hedge phrase already appears
      // ANYWHERE; the start-strip is kept for a claim that leads with a stale prefix.
      if (prefix && !claim.startsWith(prefix) && !claim.includes("기사 제목과 요약 기준으로")) {
        claim = `${prefix}${claim.replace(/^(기사 제목과 요약 기준으로는|보도 내용은)\s*/g, "")}`;
      }
      if (!hasDirectOfficialSupport(result) || officialDirectScoreForResult(result) <= 0) {
        claim = stripCertaintyWords(claim);
      }
      return limitClaimSentences(claim, 2, CLAIM_MAX_CHARS);
    }

    function hasContradictionConcern(result) {
      const verification = result?.verification_card || result || {};
      const summary = verification.contradiction_summary || result?.contradiction_summary || {};
      const checks = verification.contradiction_checks || result?.contradiction_checks || [];
      return numberValue(summary.possible_contradiction_count, 0) > 0
        || numberValue(summary.likely_contradiction_count, 0) > 0
        || numberValue(summary.confirmed_contradiction_count, 0) > 0
        || (Array.isArray(checks) && checks.some((check) => /possible|likely|confirmed|contradiction/i.test(String(check.contradiction_status || ""))));
    }

    function buildSafeMissingContext(result) {
      const verification = result?.verification_card || result || {};
      let context = userFacingReportText(formatList(verification.missing_context), "");
      if (hasExcludedOfficialDetailText(context)) {
        context = publicExportOfficialLimitation(result);
      }
      if (hasContradictionConcern(result) && !/같은 시점과 대상인지 추가 확인 필요/.test(context)) {
        context = `${context && context !== "-" ? `${context} ` : ""}같은 시점과 대상인지 추가 확인 필요`;
      }
      return sanitizePublicExportText(context || "추가 확인이 필요한 맥락은 현재 공개 리포트에 구체적으로 표시되지 않았습니다.");
    }

    // CARD-CLAIM-QUALITY fix ①: the earlier duplicate definition of
    // fallbackClaimFromTitle (cleanArticleTextForPolicyAnalysis-based) was dead —
    // this later definition shadowed it. Removed; this one is the live path.
    function fallbackClaimFromTitle(result) {
      const title = userFacingReportText(result?.title || "", "")
        .replace(/\[[^\]]+\]/g, "")
        .replace(/[“”"']/g, "")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 160);
      if (!title) {
        return "기사 제목과 요약 기준으로 핵심 주장을 추가 확인해야 합니다.";
      }
      if (/전세\s*사기|전세사기/.test(title)) {
        const amount = (title.match(/\d+\s*억\s*원?대?|\d+\s*억\s*대/) || [""])[0];
        const sentence = (title.match(/징역\s*\d+\s*년/) || [""])[0];
        if (sentence) {
          return `전세사기 사건 관련 임대업자가 사기 혐의로 ${sentence}${amount ? `을 선고받았다는 보도입니다` : "을 선고받았다는 보도입니다"}.`;
        }
        return `${title}와 관련한 보도 내용은 공식 자료와 추가 대조가 필요합니다.`;
      }
      if (/생보업계|생명보험/.test(title) && /소비자/.test(title)) {
        const count = (title.match(/\d+\s*개사/) || [""])[0];
        return `생명보험업계${count ? ` ${count}` : ""}가 소비자 기준의 의사결정 강화를 다짐했다는 보도입니다.`;
      }
      if (/금융위|금융위원회/.test(title)) {
        return `${title.replace(/[“”"']/g, "")} 관련 금융당국 보도 내용은 공식 상세자료와 추가 대조가 필요합니다.`;
      }
      const cleaned = title.replace(/\[[^\]]+\]/g, "").replace(/[“”"']/g, "").replace(/\s+/g, " ").trim();
      if (/[다요]\.$/.test(cleaned)) return cleaned;
      return `${cleaned}라는 보도 내용은 기사 제목과 요약 기준으로 추가 확인이 필요합니다.`;
    }

    function exportClaimText(result) {
      return buildReviewerSafeClaim(result);
    }

    function formatEvidenceCounts(summary) {
      return `강함 ${plain(summary.strong, 0)}, 보통 ${plain(summary.medium, 0)}, 약함 ${plain(summary.weak, 0)}`;
    }

    function officialMatchedSentenceNotes(result) {
      const verification = result?.verification_card || result || {};
      const sources = verification.source_candidates || result?.source_candidates || [];
      return (Array.isArray(sources) ? sources : [])
        .filter((source) => {
          const classification = source.official_evidence_classification || source.official_direct_match_classification || "";
          return source.official_body_match
            && ["strong_official_direct_support", "medium_official_contextual_support"].includes(classification)
            && Array.isArray(source.official_matched_sentences);
        })
        .flatMap((source) => source.official_matched_sentences.slice(0, 2).map((match) => ({
          sentence: match.sentence,
          score: match.score,
        })))
        .filter((item) => item.sentence)
        .slice(0, 3);
    }

    function buildReportText() {
      const context = currentReportContext || {};
      const results = selectedResultsForExport();
      const metrics = computeMetrics(results);
      const aggregate = aggregateEvidenceSummaries(results);
      const lines = [];

      exportLine(lines, "정책 AI 검증 리포트");
      exportLine(lines, "=".repeat(32));
      exportLine(lines, `검색어: ${plain(context.query)}`);
      exportLine(lines, `뉴스 개수: ${plain(context.maxNews || results.length, 0)}`);
      exportLine(lines, `분석 시간: ${plain(context.analyzedAt)}`);
      exportLine(lines, `최고 경고 단계: ${metrics.highest === "-" ? "-" : formatAlert(metrics.highest)}`);
      exportLine(lines, `평균 신뢰도: ${plain(metrics.averageConfidence, 0)}`);
      exportLine(lines, `고영향 뉴스 수: ${plain(metrics.highImpactCount, 0)}`);
      exportLine(lines, `전체 근거 강도: ${formatEvidenceCounts(aggregate.strength)}`);
      exportLine(lines, `전체 근거 품질: ${formatEvidenceCounts(aggregate.quality)}, 평균 ${plain(aggregate.quality.average_evidence_quality_score, 0)}`);
      const reportAiStatus = getReportAiStatus(context, results);
      const reportAiDesc = buildAiStatusDescriptor(reportAiStatus.status);
      exportLine(lines, `AI 보조 상태: ${reportAiDesc.label}`);
      lines.push("");

      if (!results.length) {
        lines.push("분석 결과가 없습니다.");
        return lines.join("\n");
      }

      results.forEach((result, index) => {
        const parts = getResultPipelineParts(result);
        const { verification, confidence, impact, decision, sourceReliabilitySummary: reliability, debugSummary: debug, contradictionSummary: contradiction, level } = parts;
        const evidence = getEvidenceSummaryForReport(result);
        const recommendation = recommendedActionForParts(parts);
        const contextForReport = buildReportUserContext({
          ...parts,
          quality: evidence.quality,
          strength: evidence.strength,
        });
        const reviewerModel = buildReviewerDashboardModel(result, contextForReport);
        const reviewerAction = getReviewerAction(result, context.query || "");

        lines.push(`[${index + 1}] ${sanitizePublicExportText(plain(result.title, "제목 없음"))}`);
        lines.push("-".repeat(32));
        lines.push(`원문: ${sanitizePublicExportText(plain(result.original_url))}`);
        lines.push(`주제: ${exportTopicLabel(result, context.query)}`);
        exportLine(lines, `경고 단계: ${formatAlert(level)}`);
        exportLine(lines, `최종 점수: ${plain(decision.final_score ?? confidence.policy_confidence_score)}`);
        exportLine(lines, `신뢰도 점수: ${plain(confidence.policy_confidence_score)}`);
        exportLine(lines, `영향도: ${formatLevel(impact.impact_level)}`);
        exportLine(lines, `위험도: ${formatLevel(confidence.risk_level)}`);
        exportLine(lines, `영향 방향: ${formatDirection(impact.impact_direction)}`);
        lines.push(`권장 조치: ${sanitizePublicExportText(recommendation)}`);
        lines.push(`시장 신호: ${sanitizePublicExportText(formatSignal(decision.market_signal))}`);
        lines.push(`최고 신뢰 출처: ${publicExportTopSource(result)}`);
        lines.push("");
        exportLine(lines, "[검증 결과 요약 카드]");
        exportLine(lines, `- 최종 판정: ${reviewerModel.finalJudgment}`);
        exportLine(lines, `- 신뢰도 점수: ${plain(confidence.policy_confidence_score ?? decision.final_score, "확인되지 않음")}`);
        exportLine(lines, `- 공식 근거 상태: ${reviewerModel.officialStatus}`);
        exportLine(lines, `- 공식 상세문서 상태: ${reviewerModel.detailStatus}`);
        exportLine(lines, `- 의미 매칭 상태: ${reviewerModel.semanticStatus}`);
        exportLine(lines, `- 반박/모순 상태: ${reviewerModel.contradictionStatus}`);
        exportLine(lines, `- 사람 검토 필요 여부: ${reviewerModel.needsReview}`);
        exportLine(lines, `- 추천 다음 조치: ${reviewerModel.nextAction}`);
        lines.push("");
        exportLine(lines, "[검토자 판단 대시보드]");
        exportLine(lines, `- 최종 판정: ${reviewerModel.finalJudgment}`);
        exportLine(lines, `- AI 초안 판정: ${reviewerModel.draftVerdict}`);
        exportLine(lines, `- 사람 검토 필요 여부: ${reviewerModel.needsReview}`);
        exportLine(lines, `- 공식 근거 상태: ${reviewerModel.officialStatus}`);
        exportLine(lines, `- 공식 상세문서 상태: ${reviewerModel.detailStatus}`);
        exportLine(lines, `- 의미 매칭 상태: ${reviewerModel.semanticStatus}`);
        exportLine(lines, `- 반박/모순 상태: ${reviewerModel.contradictionStatus}`);
        exportLine(lines, `- 현재 가장 큰 불확실성: ${reviewerModel.uncertainty}`);
        exportLine(lines, `- 추천 다음 조치: ${reviewerModel.nextAction}`);
        lines.push("");
        exportLine(lines, "[검토자 액션]");
        exportLine(lines, `- 검토 상태: ${reviewerActionStatusLabel(reviewerAction.review_status)}`);
        exportLine(lines, `- 검토 메모: ${sanitizePublicExportText(reviewerAction.reviewer_note || "없음")}`);
        exportLine(lines, `- 마지막 저장: ${formatReviewerSavedAt(reviewerAction.reviewed_at)}`);
        lines.push("");
        exportLine(lines, "핵심 요약");
        decisionReasonBullets(contextForReport, 4).forEach((item) => exportLine(lines, `- ${item}`));
        lines.push("");
        exportLine(lines, "왜 이렇게 판단했나요?");
        exportLine(lines, `- 왜 ${formatAlert(level)}인가: ${decisionReasonBullets(contextForReport, 1)[0] || "현재 수집된 근거 기준으로 판단했습니다."}`);
        exportLine(lines, `- 주요 정책 영향: ${policyImpactBullets(impact, decision).join(" / ")}`);
        exportLine(lines, `- 소비자 영향: ${consumerImpactBullets(impact).join(" / ")}`);
        exportLine(lines, `- 금융 시스템 영향: ${financialSystemBullets(impact).join(" / ")}`);
        exportLine(lines, `- 근거 품질: ${evidenceQualityExplanation(evidence.quality, evidence.strength, officialEvidenceIsGenuine(reliability, debug))}`);
        exportLine(lines, `- 공식 출처 확인: ${officialVerificationExplanation(reliability, debug)}`);
        exportLine(lines, `- 공식 상세문서 상태: ${officialDirectMatchLabel(reliability, debug)}`);
        exportLine(lines, `- 공식 직접 매칭 점수: ${plain(reliability.official_direct_match_score, 0)}`);
        exportLine(lines, `- 공식 매칭 사유: ${officialDirectMatchReason(reliability, debug)}`);
        exportLine(lines, `- 공식 해소 결과: 직접 ${plain(debug.official_resolution_direct_matches, 0)}, 맥락 ${plain(debug.official_resolution_contextual_matches, 0)}, 약한 후보 ${plain(debug.official_resolution_weak_candidates, 0)}`);
        if (reliability.top_official_detail_url) {
          exportLine(lines, `- 공식 상세문서 URL: ${reliability.top_official_detail_url}`);
        }
        if (debug.official_body_failures && Object.keys(debug.official_body_failures).length) {
          exportLine(lines, `- 공식 본문 수집 제한: ${officialBodyCollectionLimitation(reliability, debug)}`);
        }
        exportLine(lines, `- 반박/모순 확인: ${contradictionExplanation(contradiction)}`);
        const officialSentences = officialMatchedSentenceNotes(result);
        if (officialSentences.length) {
          exportLine(lines, "- 공식 문서에서 연결된 문장:");
          officialSentences.forEach((item) => exportLine(lines, `  · ${item.sentence} (${plain(item.score, 0)}점)`));
        }
        lines.push("");
        exportLine(lines, "근거와 출처 요약");
        publicSourceNotesForReport(result).forEach((item) => lines.push(`- ${sanitizePublicExportText(item)}`));
        lines.push("");
        lines.push(`AI 초안 판정: ${sanitizePublicExportText(safeAiDraftVerdictForExport(result))}`);
        lines.push(`핵심 주장: ${sanitizePublicExportText(plain(exportClaimText(result)))}`);
        lines.push(`근거 요약: ${getSafeAiDraftEvidenceSummary(result)}`);
        lines.push(`부족한 맥락: ${getSafeAiDraftMissingContext(result)}`);
        exportLine(lines, `마지막 확인 시간: ${plain(verification.last_checked_at)}`);
        exportLine(lines, `근거 강도: ${formatEvidenceCounts(evidence.strength)}`);
        exportLine(lines, `근거 품질: ${formatEvidenceCounts(evidence.quality)}, 평균 ${plain(evidence.quality.average_evidence_quality_score, 0)}`);
        lines.push("");
      });

      return lines.join("\n");
    }

    // ===== C16 — Report download & copy =====
    async function copyReport() {
      try {
        const reportText = buildReportText();
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(reportText);
        } else {
          const textarea = document.createElement("textarea");
          textarea.value = reportText;
          textarea.setAttribute("readonly", "");
          textarea.style.position = "fixed";
          textarea.style.left = "-9999px";
          document.body.appendChild(textarea);
          textarea.select();
          document.execCommand("copy");
          document.body.removeChild(textarea);
        }
        showStatus("리포트를 복사했습니다.", true);
      } catch (error) {
        showError(`리포트 복사 실패: ${error.message}`);
      }
    }

    function safeFilenamePart(value) {
      return String(value || "report")
        .replace(/[\\/:*?"<>|]/g, "-")
        .replace(/\s+/g, "-")
        .replace(/-+/g, "-")
        .slice(0, 40) || "report";
    }

    function formatTimestampForFilename(dateValue) {
      const date = dateValue ? new Date(dateValue) : new Date();
      const safeDate = Number.isNaN(date.getTime()) ? new Date() : date;
      const pad = (value) => String(value).padStart(2, "0");
      return `${safeDate.getFullYear()}${pad(safeDate.getMonth() + 1)}${pad(safeDate.getDate())}-${pad(safeDate.getHours())}${pad(safeDate.getMinutes())}`;
    }

    function downloadReport() {
      try {
        const reportText = buildReportText();
        const query = currentReportContext?.query || queryInput.value.trim() || "analysis";
        const timestamp = formatTimestampForFilename(currentReportContext?.analyzedAt);
        const filename = `policy-analysis-report-${safeFilenamePart(query)}-${timestamp}.txt`;
        const blob = new Blob([reportText], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
        showStatus("리포트 다운로드를 시작했습니다.", true);
      } catch (error) {
        showError(`리포트 다운로드 실패: ${error.message}`);
      }
    }

    function downloadMarkdownReport() {
      try {
        const reportText = buildReportText();
        const query = currentReportContext?.query || queryInput.value.trim() || "analysis";
        const timestamp = formatTimestampForFilename(currentReportContext?.analyzedAt);
        const filename = `policy-analysis-report-${safeFilenamePart(query)}-${timestamp}.md`;
        const blob = new Blob([reportText], { type: "text/markdown;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
        showStatus("Markdown 리포트 다운로드를 시작했습니다.", true);
      } catch (error) {
        showError(`Markdown 리포트 다운로드 실패: ${error.message}`);
      }
    }

    function readAnalyzeRequestFromInputs() {
      return {
        query: queryInput.value.trim(),
        maxNews: Number(maxNewsInput.value || 2),
      };
    }

    // ===== C17 — Legacy async-job polling =====
    const JOB_POLL_INTERVAL_MS = 2000;
    const JOB_MAX_POLL_ATTEMPTS = 600;

    function describeJobStage(stage, percent) {
      const mapped = ({
        queued: "대기열에 등록됨",
        running: "실행 시작",
        pipeline_started: "검증 파이프라인 실행 중",
        saving_result: "결과 저장 중",
        completed: "완료",
        failed: "실패",
        timeout: "시간 초과",
      })[stage];
      const hasPct = Number.isFinite(Number(percent));
      const pct = hasPct ? Number(percent) : 0;
      if (!mapped) {
        // Unmapped stage → generic Korean (never leak the raw English key).
        return hasPct ? `검증 중 · ${pct}%` : "검증 중";
      }
      return `${mapped} (${pct}%)`;
    }

    // =====================================================================
    // ===== C18 — V2 SSE async client =====
    // M15.0c — V2 client (begin)
    //
    // Functional equivalent of frontend/scripts/v2_client.js; kept inline
    // in main.js to avoid touching the M11.5 / M13.2a / M13.2b single-JS-
    // file build pipeline (see docs/V2_ASYNC_API.md "Frontend integration"
    // section). Re-promoting to a separate file is a follow-up that
    // requires updating frontend/build_index.py + tests/test_frontend_build.py.
    //
    // Public surface: requestPolicyAnalysisV2({query, maxNews}, onProgress)
    // — used by requestPolicyAnalysis below as the topmost preference.
    // =====================================================================

    const V2_ENDPOINT = "/v2/analyze";
    const V2_JOB_STATUS_ENDPOINT = (id) => `/v2/jobs/${encodeURIComponent(id)}`;
    const V2_JOB_STREAM_ENDPOINT = (id) => `/v2/jobs/${encodeURIComponent(id)}/stream`;
    const V2_POLL_INTERVAL_MS = 2000;
    const V2_MAX_POLL_ATTEMPTS = 300; // 10-minute cap (mirrors legacy)
    const V2_FALLBACK_AFTER_SILENCE_MS = 10000;
    const V2_TERMINAL_STATUSES = new Set(["finished", "failed", "stopped", "canceled"]);

    // Stage → Korean label. The first 5 are emitted by
    // pipeline_worker.report_progress today (M15.0b). The later
    // fine-grained labels are aspirational for M15.0d when the
    // pipeline gets per-news parallelism; including them here lets
    // M15.0d ship without a frontend follow-up.
    const V2_STAGE_LABELS_KO = {
      queued: "대기열에 등록됨",
      pipeline_started: "검증 파이프라인 실행 중",
      saving_results: "결과 저장 중",
      completed: "완료",
      failed: "실패",
      news_collection: "뉴스 수집 중",
      article_extraction: "기사 본문 추출 중",
      claim_extraction: "주장 추출 중",
      official_source_search: "공식 출처 검색 중",
      evidence_extraction: "증거 추출 중",
      verification_card: "검증 카드 구성 중",
      ai_reasoning: "AI 추론 중",
      calibration: "최종 보정 중",
    };

    function v2StageLabel(stage, percent) {
      const hasPct = Number.isFinite(Number(percent));
      const pct = hasPct ? Number(percent) : 0;
      const mapped = V2_STAGE_LABELS_KO[stage];
      if (!mapped) {
        // Any unmapped/new backend stage → generic Korean. Never leak the raw
        // English stage key (e.g. "news_item_parallel_started") to the UI.
        return hasPct ? `검증 중 · ${pct}%` : "검증 중";
      }
      return `${mapped} (${pct}%)`;
    }

    function v2SetProgressVisible(visible) {
      const wrap = document.getElementById("v2ProgressWrap");
      if (!wrap) return;
      // The slot is always laid out with a reserved height; toggling only the
      // .is-active class fades the inner bar+text, so the header never reflows.
      wrap.classList.toggle("is-active", !!visible);
    }

    function v2UpdateProgress(percent, label) {
      const bar = document.getElementById("v2ProgressBar");
      const text = document.getElementById("v2ProgressText");
      if (bar) {
        const pct = Math.max(0, Math.min(100, Number(percent) || 0));
        bar.style.width = `${pct}%`;
        bar.setAttribute("aria-valuenow", String(pct));
      }
      if (text) {
        text.textContent = label || "진행 중";
      }
    }

    function v2ResetProgress() {
      v2UpdateProgress(0, "");
      v2SetProgressVisible(false);
    }

    async function v2PostAnalyze({ query, maxNews }) {
      const response = await fetch(`${API_BASE}${V2_ENDPOINT}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, max_news: maxNews }),
      });
      if (!response.ok) {
        let body = "";
        try { body = await response.text(); } catch (_) {}
        const err = new Error(`V2 enqueue failed: HTTP ${response.status} ${body}`);
        err.status = response.status;
        throw err;
      }
      return response.json();
    }

    async function v2FetchJobStatus(jobId) {
      const response = await fetch(`${API_BASE}${V2_JOB_STATUS_ENDPOINT(jobId)}`);
      if (!response.ok) {
        const err = new Error(`V2 status fetch failed: HTTP ${response.status}`);
        err.status = response.status;
        throw err;
      }
      return response.json();
    }

    async function v2FetchHistoryRow(resultId) {
      const response = await fetch(`${API_BASE}/history/${encodeURIComponent(resultId)}`);
      if (!response.ok) {
        throw new Error(`/history/${resultId} fetch failed: HTTP ${response.status}`);
      }
      const body = await response.json();
      return body.result || null;
    }

    // M45: fetch the newest server-side analyses (GET /history list) and map
    // each full row through the existing mapHistoryRowToResult adapter — the
    // list endpoint returns the SAME row shape as /history/{id}. Used to fill
    // the homepage hot-topic area with cron/server output. Fail-open: any
    // error or empty body returns [] so the area falls back to its existing
    // placeholder (mirrors loadServerResultById's graceful handling).
    async function getServerRecentAnalyses(limit = 50) {
      try {
        const response = await fetch(`${API_BASE}/history?limit=${encodeURIComponent(limit)}`);
        if (!response.ok) return [];
        const body = await response.json();
        const rows = Array.isArray(body?.results) ? body.results : [];
        return rows.map(mapHistoryRowToResult);
      } catch (_) {
        return [];
      }
    }

    // STABLE-TABS S2: fetch ONE domain's recent rows (S1 endpoint) and map them
    // through the SAME mapHistoryRowToResult adapter the 전체 feed uses. `domainKey`
    // is the STORED English domain key (e.g. "realestate"). Returns null on any
    // failure (→ friendly-error state), a results[] on success (possibly empty).
    async function getServerDomainAnalyses(domainKey, limit = 50) {
      try {
        const response = await fetch(
          `${API_BASE}/history?domain=${encodeURIComponent(domainKey)}&limit=${encodeURIComponent(limit)}`
        );
        if (!response.ok) return null;
        const body = await response.json();
        const rows = Array.isArray(body?.results) ? body.results : [];
        return rows.map(mapHistoryRowToResult);
      } catch (_) {
        return null;
      }
    }

    // STABLE-TABS S2: ensure a domain's cards are cached, then re-render if that
    // domain is still active. No-op when already cached or a fetch is in flight
    // (in-memory only; no localStorage). Fire-and-forget — the feed shows a
    // loading line until this resolves.
    function ensureDomainLoaded(domainKey) {
      if (domainResultsCache.has(domainKey) || domainLoadingKey === domainKey) return;
      domainLoadingKey = domainKey;
      if (domainFetchErrorKey === domainKey) domainFetchErrorKey = null;
      getServerDomainAnalyses(domainKey).then((results) => {
        if (domainLoadingKey === domainKey) domainLoadingKey = null;
        if (results === null) {
          domainFetchErrorKey = domainKey;
        } else {
          domainResultsCache.set(domainKey, results);
        }
        if (activeDomain === domainKey) renderHotTopics();
      });
    }

    // HOME-SECTION-FIX A1: ensure every DOMAIN_ORDER domain has its top rows
    // cached for the home 분야별 sections, then repaint. Fire-and-forget and
    // fail-quiet: a domain that errors just has no section (never blocks the
    // others). Reuses getServerDomainAnalyses — the SAME read-only /history
    // adapter the tabs use. A small limit gives 뜨는순 a real pool to rank while
    // keeping the per-domain payloads light. Repaint writes ONLY the sections
    // container (never calls renderHotTopics), so there is no re-entrancy loop.
    function ensureDomainSectionsLoaded() {
      for (const domainKey of DOMAIN_ORDER) {
        if (domainSectionCache.has(domainKey) || domainSectionLoading.has(domainKey)) continue;
        domainSectionLoading.add(domainKey);
        getServerDomainAnalyses(domainKey, 12).then((results) => {
          domainSectionLoading.delete(domainKey);
          if (results !== null) domainSectionCache.set(domainKey, results);
          if (activeDomain === "전체" && feedDomainSectionsEl) {
            feedDomainSectionsEl.innerHTML = renderDomainSections();
          }
        });
      }
    }

    // After a V2 job finishes, the SSE "completed" event carries the
    // `pipeline_worker._build_summary_payload` shape — which only
    // includes `saved_result_ids`, not the full per-news results.
    // Fetch each /history/{id} row and map it through the existing
    // `mapHistoryRowToResult` adapter (defined above near L2538) to
    // synthesize the AnalyzeResponse shape the renderer expects.
    async function v2InflateResults(savedResultIds, summary) {
      const ids = Array.isArray(savedResultIds) ? savedResultIds : [];
      const results = [];
      for (const id of ids) {
        if (typeof id !== "number" || Number.isNaN(id)) continue;
        try {
          const row = await v2FetchHistoryRow(id);
          if (row) results.push(mapHistoryRowToResult(row));
        } catch (err) {
          console.warn(`V2: failed to inflate history row ${id}:`, err);
        }
      }
      return {
        status: "ok",
        results,
        news_collection_debug: (summary && summary.news_collection_debug) || {},
        ai_status: (summary && summary.ai_status_summary) || {},
      };
    }

    // SSE stream wrapper. Falls back to polling if EventSource is
    // unavailable OR if no events arrive in V2_FALLBACK_AFTER_SILENCE_MS.
    function v2StreamProgress(jobId, onProgress, onComplete, onError) {
      if (typeof EventSource === "undefined") {
        return v2PollJobUntilTerminal(jobId, onProgress, onComplete, onError);
      }
      let source;
      let closed = false;
      let silenceTimer = null;
      const close = () => {
        if (closed) return;
        closed = true;
        try { source && source.close(); } catch (_) {}
        if (silenceTimer) clearTimeout(silenceTimer);
      };
      try {
        source = new EventSource(`${API_BASE}${V2_JOB_STREAM_ENDPOINT(jobId)}`);
      } catch (_) {
        return v2PollJobUntilTerminal(jobId, onProgress, onComplete, onError);
      }
      let receivedAny = false;
      silenceTimer = setTimeout(() => {
        if (!receivedAny && !closed) {
          console.warn("V2 SSE: no events in 10s, falling back to polling");
          close();
          v2PollJobUntilTerminal(jobId, onProgress, onComplete, onError);
        }
      }, V2_FALLBACK_AFTER_SILENCE_MS);
      const safeParse = (raw) => {
        try { return JSON.parse(raw); } catch (_) { return { raw }; }
      };
      const handle = (event, kind) => {
        receivedAny = true;
        const payload = safeParse(event.data || "");
        if (kind === "progress" || kind === "status") {
          try { onProgress && onProgress(payload, kind); } catch (_) {}
        } else if (kind === "completed") {
          try { onComplete && onComplete(payload); } catch (_) {}
          close();
        } else if (
          kind === "failed" || kind === "timeout"
          || kind === "unavailable" || kind === "not_found"
        ) {
          const reason = (payload && (payload.error || payload.reason)) || kind;
          try { onError && onError(new Error(`V2 stream ${kind}: ${reason}`)); } catch (_) {}
          close();
        }
      };
      source.addEventListener("progress", (e) => handle(e, "progress"));
      source.addEventListener("status", (e) => handle(e, "status"));
      source.addEventListener("completed", (e) => handle(e, "completed"));
      source.addEventListener("failed", (e) => handle(e, "failed"));
      source.addEventListener("timeout", (e) => handle(e, "timeout"));
      source.addEventListener("unavailable", (e) => handle(e, "unavailable"));
      source.addEventListener("not_found", (e) => handle(e, "not_found"));
      source.addEventListener("error", () => {
        if (closed) return;
        if (!receivedAny) {
          close();
          v2PollJobUntilTerminal(jobId, onProgress, onComplete, onError);
        }
      });
      return close;
    }

    async function v2PollJobUntilTerminal(jobId, onProgress, onComplete, onError) {
      let cancelled = false;
      let lastStatus = null;
      const close = () => { cancelled = true; };
      (async () => {
        for (let i = 0; i < V2_MAX_POLL_ATTEMPTS; i += 1) {
          if (cancelled) return;
          await new Promise((r) => setTimeout(r, V2_POLL_INTERVAL_MS));
          if (cancelled) return;
          let status;
          try {
            status = await v2FetchJobStatus(jobId);
          } catch (err) {
            if (err.status === 404) {
              try { onError && onError(new Error("job not found")); } catch (_) {}
              return;
            }
            continue;
          }
          const current = status && status.status;
          if (current !== lastStatus) {
            try { onProgress && onProgress(status, "status"); } catch (_) {}
            lastStatus = current;
          }
          if (V2_TERMINAL_STATUSES.has(current)) {
            if (current === "finished") {
              try { onComplete && onComplete(status); } catch (_) {}
            } else {
              try { onError && onError(new Error(`job ${current}`)); } catch (_) {}
            }
            return;
          }
        }
        try { onError && onError(new Error("polling timed out")); } catch (_) {}
      })();
      return close;
    }

    async function requestPolicyAnalysisV2({ query, maxNews }, onProgress) {
      const job = await v2PostAnalyze({ query, maxNews });
      const jobId = job && job.job_id;
      if (!jobId) {
        throw new Error("V2 /v2/analyze did not return job_id");
      }
      v2SetProgressVisible(true);
      v2UpdateProgress(2, v2StageLabel("queued", 2));
      return new Promise((resolve, reject) => {
        let resolved = false;
        const finish = (result) => {
          if (resolved) return;
          resolved = true;
          resolve(result);
        };
        const fail = (err) => {
          if (resolved) return;
          resolved = true;
          reject(err);
        };
        const onProgressInner = (payload, kind) => {
          if (kind === "progress") {
            const pct = Number(payload.percent);
            const stage = payload.stage || "";
            v2UpdateProgress(pct, v2StageLabel(stage, pct));
          }
          if (typeof onProgress === "function") {
            try { onProgress({ kind, payload }); } catch (_) {}
          }
        };
        const onCompleteInner = async (payload) => {
          try {
            v2UpdateProgress(100, v2StageLabel("completed", 100));
            const summary = (payload && payload.result) || {};
            const savedIds = summary.saved_result_ids || [];
            const inflated = await v2InflateResults(savedIds, summary);
            finish(inflated);
          } catch (err) {
            fail(err);
          }
        };
        const onErrorInner = (err) => fail(err);
        try {
          v2StreamProgress(jobId, onProgressInner, onCompleteInner, onErrorInner);
        } catch (err) {
          fail(err);
        }
      });
    }

    // =====================================================================
    // M15.0c — V2 client (end)
    // =====================================================================

    async function requestPolicyAnalysisLegacy({ query, maxNews }) {
      const response = await fetch(`${API_BASE}/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, max_news: maxNews }),
      });

      if (!response.ok) {
        const body = await response.text();
        throw new Error(body || `HTTP ${response.status}`);
      }

      return response.json();
    }

    async function requestPolicyAnalysisAsync({ query, maxNews }, onProgress) {
      const createResp = await fetch(`${API_BASE}/jobs/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, max_news: maxNews }),
      });
      if (!createResp.ok) {
        const body = await createResp.text();
        throw new Error(body || `HTTP ${createResp.status}`);
      }
      const job = await createResp.json();
      const jobId = job.job_id;
      if (!jobId) {
        throw new Error("job_id not returned");
      }

      for (let attempt = 0; attempt < JOB_MAX_POLL_ATTEMPTS; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, JOB_POLL_INTERVAL_MS));
        const statusResp = await fetch(`${API_BASE}/jobs/${encodeURIComponent(jobId)}`);
        if (!statusResp.ok) {
          throw new Error(`status check failed: HTTP ${statusResp.status}`);
        }
        const statusJson = await statusResp.json();
        if (typeof onProgress === "function") {
          onProgress(statusJson);
        }
        const jobStatus = statusJson.job_status;
        if (jobStatus === "completed") {
          const resultResp = await fetch(`${API_BASE}/jobs/${encodeURIComponent(jobId)}/result`);
          if (!resultResp.ok) {
            throw new Error(`result fetch failed: HTTP ${resultResp.status}`);
          }
          const resultJson = await resultResp.json();
          if (resultJson.result) {
            if (resultJson.result_source === "stored_result") {
              console.info("Job result reconstructed from SQLite (cache miss).");
            }
            return resultJson.result;
          }
          throw new Error(
            resultJson.error_message || "job completed but no result payload available"
          );
        }
        if (jobStatus === "failed" || jobStatus === "timeout") {
          throw new Error(statusJson.error_message || `job ${jobStatus}`);
        }
      }
      throw new Error("job polling timed out");
    }

    async function requestPolicyAnalysis({ query, maxNews }, onProgress) {
      // M15.0c: three-tier fallback chain.
      //   1. V2 (RQ worker + SSE) — preferred when Redis + worker available
      //   2. V1 async (/jobs/analyze + polling) — process-local fallback
      //   3. Sync /analyze — last resort, always works (but blocks 48-174s)
      try {
        return await requestPolicyAnalysisV2({ query, maxNews }, onProgress);
      } catch (v2Error) {
        console.warn("V2 flow failed, falling back to legacy async:", v2Error);
        // Keep the reserved slim slot visible with a generic label for the
        // non-SSE fallback paths (legacy polling refines it per poll; sync
        // keeps this label for its single blocking request).
        v2SetProgressVisible(true);
        v2UpdateProgress(0, "검증 중…");
      }
      try {
        return await requestPolicyAnalysisAsync({ query, maxNews }, onProgress);
      } catch (asyncError) {
        console.warn("Async job flow failed, falling back to /analyze:", asyncError);
        return requestPolicyAnalysisLegacy({ query, maxNews });
      }
    }

    // ===== C19 — Analysis orchestration & event wiring (HUB) =====
    function stabilizeAnalysisResponseForRender(data, query, maxNews) {
      if (!(data.results || []).length) return data;

      const feedbackData = applyHumanReviewFeedback(data, query);
      const historyResult = saveLocalAnalysisHistory(query, maxNews, feedbackData);
      const reviewResult = upsertReviewQueue(
        query,
        maxNews,
        historyResult.responseData,
        historyResult.stableHistoryKey
      );
      const renderData = withReviewDebug(
        historyResult.responseData,
        historyResult.stableHistoryKey,
        reviewResult.action
      );
      // Seed the in-memory hydration cache with the just-completed full payload
      // so reopening this record in the same session doesn't trigger a refetch.
      // localStorage itself only holds the slim shape written above.
      if (historyResult.stableHistoryKey && Array.isArray(renderData.results)) {
        hydratedRecordCache.set(historyResult.stableHistoryKey, {
          results: renderData.results,
        });
      }
      return renderData;
    }

    function renderAnalysisResponse(query, maxNews, renderData) {
      const results = renderData.results || [];
      setCurrentReportContext(query, maxNews, results, new Date().toISOString(), renderData.ai_status);
      renderResults(results);
      // DESIGN-DETAIL-3: a search/analyze produces a report (detail content) that
      // renders into #results (now in #detailScreen). Switch to the detail SCREEN so
      // it's visible, and push a history entry so BACK returns to the home feed
      // (mirrors the card-open flow). On zero results stay on home — the "찾지 못했습니다"
      // message lives in the always-visible v2 progress slot below, not #results.
      if (results.length) {
        pushDetailHistoryState(window.scrollY || 0);
        showScreen("detail");
      } else {
        showScreen("home");
      }
      renderHistory(safeReadLocalHistory());
      renderReviewQueue(safeReadReviewQueue());
      // Terminal message lives in the reserved slim slot (not the legacy banner)
      // so the header height stays identical between running and done — no jump.
      v2SetProgressVisible(true);
      if (!results.length) {
        v2UpdateProgress(100, "검증 가능한 결과를 찾지 못했습니다");
      } else {
        v2UpdateProgress(100, "분석 완료");
      }
    }

    async function analyze() {
      const { query, maxNews } = readAnalyzeRequestFromInputs();

      if (!query) {
        alert("검색어를 입력해주세요.");
        return;
      }

      hideError();
      // DESIGN-PROGRESS: progress shows ONLY in the reserved slim-bar slot
      // (#v2ProgressWrap), never the legacy #statusLine banner — the slot has a
      // permanent reserved height, so activating it paints without growing the
      // header. v2ResetProgress() here clears any prior run's terminal message.
      hideStatus();
      v2ResetProgress();
      v2SetProgressVisible(true);
      v2UpdateProgress(0, "검증 중…");
      setBusy(true);
      selectedResultIndex = null;
      activeTopicKey = "";

      try {
        const data = await requestPolicyAnalysis(
          { query, maxNews },
          (statusJson) => {
            // The V2 (SSE) path updates the slim bar itself with real stage
            // labels; its wrapper events carry a `kind` string — skip those so
            // we don't clobber the real-stage label. The legacy polling path
            // reports {current_stage, progress_percent}; reflect it coarsely in
            // the same slot.
            if (statusJson && typeof statusJson.kind === "string") return;
            v2SetProgressVisible(true);
            v2UpdateProgress(
              Number(statusJson?.progress_percent) || 0,
              describeJobStage(statusJson?.current_stage, statusJson?.progress_percent),
            );
          }
        );
        const renderData = stabilizeAnalysisResponseForRender(data, query, maxNews);
        renderAnalysisResponse(query, maxNews, renderData);
      } catch (error) {
        v2ResetProgress();
        showError(`분석 실패: ${error.message}`);
        hideStatus();
      } finally {
        setBusy(false);
      }
    }

    async function loadHistory() {
      hideError();
      showStatus("최근 분석 불러오는 중...");
      setBusy(true);

      try {
        const records = safeReadLocalHistory();
        renderHistory(records);
        if (!records.length) {
          renderResults([]);
          showStatus("저장된 분석 기록이 없습니다.", true);
          return;
        }
        // DESIGN-DETAIL-3: loadHistoryRecord switches to the detail screen; push a
        // back-entry here too (this button path doesn't go through openTopicCard /
        // the history-list opener that already push), so BACK returns to the feed.
        pushDetailHistoryState(window.scrollY || 0);
        await loadHistoryRecord(records[0] || {}, "가장 최근 분석 기록을 불러왔습니다.");
      } catch (error) {
        showError(`최근 분석 불러오기 실패: ${error.message}`);
        hideStatus();
      } finally {
        setBusy(false);
      }
    }

    // SEARCH-TO-ANALYZE Slice 2 — corpus-first search. The 분석하기 button now
    // tries GET /api/search over EXISTING cards first: a hit renders an
    // instant result list (zero LLM cost); a miss shows an opt-in offer that
    // calls the UNCHANGED analyze() flow (its v2 progress UX preserved).
    // Honesty: hits show only the existing draft badge (review_status) — no
    // new ranking, no new label; the offer copy says "no prior analysis
    // exists", never anything about truth.
    function renderSearchHitsView(query, hits) {
      metricsEl.style.display = "none";
      resultsEl.innerHTML = `
        <div class="empty-state">'${escapeHtml(query)}' 관련 기존 분석 ${hits.length}건을 찾았습니다. 제목을 누르면 전체 검증 카드가 열립니다.</div>
        ${hits.map((hit, index) => `
          <div class="history-row" data-search-hit-id="${Number(hit.result_id) || 0}" role="button" tabindex="0">
            <div class="history-id">#${escapeHtml(index + 1)}</div>
            <div>
              <strong>${escapeHtml(stripLeadingTitleMarker(hit.title) || "제목 없음")}</strong>
              <div class="history-meta">
                ${escapeHtml(hit.snippet || "")}
                <br>
                <span class="review-status-badge">검토: ${escapeHtml(reviewerActionStatusLabel(hit.review_status))}</span>
                ${hit.published_at ? `&nbsp; <span class="label">보도일:</span> ${escapeHtml(String(hit.published_at).slice(0, 10))}` : ""}
              </div>
            </div>
          </div>
        `).join("")}
      `;
      resultsEl.querySelectorAll("[data-search-hit-id]").forEach((row) => {
        const id = Number(row.getAttribute("data-search-hit-id"));
        if (!(id > 0)) return;
        const open = () => {
          // SEARCH-ANALYZE S-i (bug b): fresh detail entry ON TOP of the
          // search entry (the double-push guard only collapses detail→detail,
          // so search→detail pushes cleanly) — BACK now returns to the results.
          pushDetailHistoryState(window.scrollY || 0);
          loadServerResultById(id);
        };
        row.addEventListener("click", open);
        row.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            open();
          }
        });
      });
    }

    // SEARCH-ANALYZE S-i (bug b): the search-results view gets its OWN history
    // entry kind ({tickedinSearch}) instead of the shared detail entry — a card
    // opened from the results then pushes a detail entry on top, so BACK pops
    // card → results → home instead of skipping to home. A repeat search from
    // the results view refreshes the single search entry in place (no stacking).
    // detailHistoryActive is reused as the "on a tracked non-home entry" flag —
    // the results render on the detail SCREEN, and the popstate home-branch
    // already keys on it.
    function pushSearchHistoryState(scrollY) {
      if (!window.history || !window.history.pushState) return;
      const y = (typeof scrollY === "number" && isFinite(scrollY)) ? scrollY : (window.scrollY || 0);
      try {
        if (window.history.state && window.history.state.tickedinSearch) {
          window.history.replaceState({ tickedinSearch: true, scrollY: y }, "", window.location.href);
        } else {
          window.history.pushState({ tickedinSearch: true, scrollY: y }, "", window.location.href);
        }
        detailHistoryActive = true;
      } catch (_) {
        /* history unavailable — the results still render */
      }
    }

    function renderSearchHits(query, hits) {
      lastSearchHitsCache = { query, hits };
      renderSearchHitsView(query, hits);
      pushSearchHistoryState(window.scrollY || 0);
      showScreen("detail");
    }

    function renderAnalyzeOffer(query) {
      metricsEl.style.display = "none";
      resultsEl.innerHTML = `
        <div class="empty-state">
          '${escapeHtml(query)}'에 대한 기존 분석이 없습니다. 지금 분석해드릴까요? (최대 1분)
          <br><br>
          <button type="button" class="primary" data-run-analyze-offer>지금 분석하기</button>
        </div>
      `;
      const offerButton = resultsEl.querySelector("[data-run-analyze-offer]");
      if (offerButton) offerButton.addEventListener("click", analyze);
      pushDetailHistoryState(window.scrollY || 0);
      showScreen("detail");
    }

    async function searchFirst() {
      const query = queryInput.value.trim();
      if (!query) {
        alert("검색어를 입력해주세요.");
        return;
      }
      hideError();
      hideStatus();
      v2ResetProgress();
      v2SetProgressVisible(true);
      v2UpdateProgress(30, "기존 분석 검색 중…");
      setBusy(true);
      let hits = [];
      try {
        const response = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(query)}`);
        if (response.ok) {
          const data = await response.json();
          if (data && Array.isArray(data.results)) hits = data.results;
        }
      } catch (error) {
        // Search unavailable → fall through to the offer (never auto-run
        // the paid pipeline, never dead-end).
      } finally {
        setBusy(false);
      }
      if (hits.length) {
        v2UpdateProgress(100, `기존 분석 ${hits.length}건을 찾았습니다`);
        renderSearchHits(query, hits);
      } else {
        v2UpdateProgress(100, "기존 분석 없음 — 새 분석을 제안합니다");
        renderAnalyzeOffer(query);
      }
    }

    // SHARE-IMAGE Slice 1: delegated handler for the per-card share button
    // (injected via innerHTML in the detail render, so direct binding won't
    // survive re-renders). Fail-silent inside downloadShareImage.
    resultsEl.addEventListener("click", (event) => {
      const shareButton = event.target.closest("[data-share-image]");
      if (!shareButton) return;
      const resultId = Number(shareButton.getAttribute("data-share-image"));
      const shareTitle = shareButton.getAttribute("data-share-title") || "";
      // SHARE-IMAGE-FILL A7: the render bakes the card's officialStatusLabel into
      // the button so the canvas can draw the same source-status line.
      const shareOfficial = shareButton.getAttribute("data-share-official") || "";
      downloadShareImage(shareTitle, resultId, shareOfficial);
    });

    analyzeBtn.addEventListener("click", searchFirst);
    historyBtn.addEventListener("click", loadHistory);
    copyReportBtn.addEventListener("click", copyReport);
    downloadReportBtn.addEventListener("click", downloadReport);
    downloadMarkdownBtn.addEventListener("click", downloadMarkdownReport);
    clearHistoryBtn.addEventListener("click", clearLocalHistory);
    resultsEl.addEventListener("click", (event) => {
      const saveButton = event.target.closest("[data-save-review-action]");
      const clearButton = event.target.closest("[data-clear-review-action]");
      if (!saveButton && !clearButton) return;
      const card = event.target.closest(".reviewer-action-card");
      const key = (saveButton || clearButton).dataset.saveReviewAction
        || (saveButton || clearButton).dataset.clearReviewAction
        || card?.dataset.reviewActionKey;
      if (!key) return;
      if (clearButton) {
        clearReviewerActionForKey(key);
        showStatus("검토 액션을 초기화했습니다.", true);
      } else {
        const status = card?.querySelector("[data-review-status]")?.value || "unreviewed";
        const note = card?.querySelector("[data-review-note]")?.value || "";
        saveReviewerActionForKey(key, status, note);
        showStatus("검토 액션을 저장했습니다.", true);
      }
      if (Array.isArray(currentReportContext?.results)) {
        renderResults(currentReportContext.results, selectedResultIndex);
      }
      renderHistory(safeReadLocalHistory());
    });
    reviewFilterEl.addEventListener("change", () => {
      renderReviewQueue(safeReadReviewQueue());
    });
    historyEl.addEventListener("click", (event) => {
      const deleteButton = event.target.closest("[data-delete-history-id]");
      if (deleteButton) {
        event.stopPropagation();
        deleteHistoryRecord(deleteButton.dataset.deleteHistoryId);
        return;
      }

      const row = event.target.closest("[data-history-id]");
      if (!row) {
        return;
      }
      const recordId = row.dataset.historyId;
      const record = safeReadLocalHistory().find((item) => item.id === recordId);
      if (!record) {
        showStatus("선택한 분석 기록을 찾을 수 없습니다.", true);
        renderHistory(safeReadLocalHistory());
        return;
      }
      // HISTORY-BACK: this history-list opener calls loadHistoryRecord directly
      // (not via openTopicCard), so push the back-entry here too.
      pushDetailHistoryState(window.scrollY || 0);
      loadHistoryRecord(record, `"${record.query || "분석 기록"}" 기록을 불러왔습니다.`);
    });
    reviewQueueEl.addEventListener("click", (event) => {
      const actionButton = event.target.closest("[data-review-action]");
      if (actionButton) {
        event.stopPropagation();
        updateReviewQueueStatus(
          actionButton.dataset.reviewId,
          actionButton.dataset.reviewAction
        );
        return;
      }

      const row = event.target.closest("[data-review-id]");
      if (!row) {
        return;
      }
      const itemId = row.dataset.reviewId;
      const item = safeReadReviewQueue().find((entry) => entry.id === itemId);
      if (!item) {
        showStatus("선택한 검토 큐 항목을 찾을 수 없습니다.", true);
        renderReviewQueue(safeReadReviewQueue());
        return;
      }
      // DESIGN-DETAIL-3: loadReviewQueueItem switches to the detail screen; push a
      // back-entry so BACK returns to the feed (mirrors the history-list opener).
      pushDetailHistoryState(window.scrollY || 0);
      loadReviewQueueItem(item, `"${item.query || "검토 항목"}" 검토 큐 항목을 불러왔습니다.`);
    });
    // DESIGN-C3h-2: shared tab-switch — set the active domain, reset paging, re-render.
    // renderCategoryTabs re-marks the active tab on every render, so this is all that's
    // needed. Reused by the category tabs AND the "{label} 전체 →" section links.
    function setActiveDomain(key) {
      activeDomain = key || "전체";
      currentPage = 1;  // DESIGN-C3-2: different card set → back to page 1
      // STABLE-TABS S2: a specific domain not yet cached → kick off its
      // server-scoped fetch (fire-and-forget; the feed shows a loading line until
      // ensureDomainLoaded re-renders). 전체 uses the existing recent feed.
      if (activeDomain !== "전체" && !domainResultsCache.has(activeDomain)) {
        ensureDomainLoaded(activeDomain);
      }
      renderHotTopics();
    }
    // DESIGN-DETAIL-2: screen toggle. The home view (#homeScreen — #metrics
    // through #correctionsSection) and the 검증 방법 page (#methodology) are
    // mutually-exclusive "screens"; the header (logo/search), the domain tabs, and
    // the footer (with the page-level disclaimer) sit OUTSIDE both and stay
    // always-visible. The sidebar lives inside .home-shell (inside #homeScreen), so
    // it hides with the home view → 검증 방법 is full-width. Built so a "detail"
    // case slots in trivially next step. showScreen itself stays PURE (visual
    // only — no history side effects) so it is safe to call from init, the tab/
    // logo handlers, and the popstate handler without loops; the history push/pop
    // is driven by the link/tab/logo handlers + the unified popstate handler
    // (DETAIL-2b) — mirroring how openTopicCard (not renderResults) owns
    // pushDetailHistoryState.
    // DESIGN-DETAIL-3: three mutually-exclusive screens — home / methodology /
    // detail. The card detail is now a real screen (#detailScreen wraps #metrics +
    // #selectedIssueIntro + #reportActions + #results, outside #homeScreen). Exactly
    // one of the three groups is shown; the header (logo/search), the domain-tabs
    // row, and the footer sit OUTSIDE all three and stay always-visible.
    function showScreen(name) {
      const homeEl = document.getElementById("homeScreen");
      const methodologyEl = document.getElementById("methodology");
      const detailEl = document.getElementById("detailScreen");
      // ABOUT-PAGE: #aboutScreen is a 4th mutually-exclusive screen, toggled here
      // exactly like #methodology.
      const aboutEl = document.getElementById("aboutScreen");
      // GRADE-STATUS-PAGE: #gradeStatus is a 5th mutually-exclusive screen,
      // toggled here exactly like #methodology / #aboutScreen.
      const gradeStatusEl = document.getElementById("gradeStatus");
      const isHome = name === "home";
      if (homeEl) homeEl.classList.toggle("screen-hidden", !isHome);
      if (methodologyEl) methodologyEl.classList.toggle("screen-hidden", name !== "methodology");
      if (detailEl) detailEl.classList.toggle("screen-hidden", name !== "detail");
      if (aboutEl) aboutEl.classList.toggle("screen-hidden", name !== "about");
      if (gradeStatusEl) gradeStatusEl.classList.toggle("screen-hidden", name !== "gradeStatus");
      // DESIGN-DETAIL-2c: the under-search status line (#statusLine — e.g. "저장된
      // 검증 결과를 불러왔습니다") lives in the always-visible header region, so it
      // leaked onto non-home pages. Clear it whenever we leave home; it is a
      // home-feed status only. Home status behavior is untouched — showStatus() on
      // the home feed re-shows it as before. (NOTE: the detail loaders call
      // showScreen("detail") BEFORE their own showStatus, so their confirmation
      // survives — see loadHistoryRecord / loadServerResultById.)
      if (!isHome) hideStatus();
      // DESIGN-DETAIL-3: both non-home screens land at the top (was methodology-only).
      if (!isHome) window.scrollTo(0, 0);
    }
    // DESIGN-DETAIL-2b: history sync for the screen toggle so browser BACK from the
    // 검증 방법 page returns to the home screen (was exiting the site — no entry was
    // pushed). Mirrors the HISTORY-BACK detail layer: a state flag rides in
    // history.state (.tickedinScreen) + a module variable; the URL is left UNCHANGED
    // (pass window.location.href) so the ?result_id= deep-link read + the
    // operator_tools replaceState are unaffected (same choice as pushDetailHistoryState).
    let methodologyHistoryActive = false;
    function pushMethodologyHistoryState() {
      if (!window.history || !window.history.pushState) return;
      // DESIGN-DETAIL-3b (FIX C): demote-before-push REMOVED. The old
      // clearDetailHistoryState() here replaceState(null)'d the detail entry, nulling
      // its state so a later FORWARD popped null → router showed home → detail never
      // re-shown. The symmetric popstate router routes by the POPPED state in both
      // directions, so a [base, detail, methodology] sandwich is harmless — each
      // BACK/FORWARD shows the correct screen (accepted tradeoff: detail→검증방법→BACK
      // is now a standard one-step undo back to the detail, not a jump home).
      if (window.history.state && window.history.state.tickedinScreen === "methodology") {
        methodologyHistoryActive = true;  // already a methodology entry — don't stack duplicates
        return;
      }
      try {
        window.history.pushState({ tickedinScreen: "methodology" }, "", window.location.href);
        methodologyHistoryActive = true;
      } catch (_) {
        /* history unavailable — the page still opens via the visual toggle */
      }
    }
    // DESIGN-DETAIL-3b (FIX C): now UNUSED — all callers (the demote-before-push and
    // the tab/logo cleanup) were removed because the replaceState(null) demote killed
    // browser FORWARD. Kept (not deleted) in case a future demote is wanted.
    function clearMethodologyHistoryState() {
      // Leaving 검증 방법 via in-page nav (tab/logo): demote the current methodology
      // entry to a neutral state so a later BACK can't resurface it (which would
      // otherwise hijack a card-detail BACK opened from the returned-to home view).
      // replaceState keeps the URL; the browser owns the pop when BACK is pressed.
      if (!methodologyHistoryActive) return;
      methodologyHistoryActive = false;
      if (!window.history || !window.history.replaceState) return;
      if (window.history.state && window.history.state.tickedinScreen === "methodology") {
        try { window.history.replaceState(null, "", window.location.href); } catch (_) { /* noop */ }
      }
    }
    // DESIGN-DETAIL-2: every in-page link to #methodology now OPENS the 검증 방법
    // page via the toggle instead of anchor-scrolling to a now-hidden section
    // (footer 검증 방법론 ×2, the 검증 방법론 전체 → teaser link).
    // MOBILE-POLISH 2b: the tab row's 검증 등급 안내 → link is no longer in that set —
    // it now targets #gradeLegend (handler below), as the footer's 등급 안내 does.
    // The compact 이렇게 검증합니다 teaser (#verifyHowSection) stays on home; only
    // its "전체 →" link opens the full page.
    document.querySelectorAll('a[href="#methodology"]').forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        pushMethodologyHistoryState();  // DETAIL-2b: BACK from here returns home
        showScreen("methodology");
      });
    });
    // ABOUT-PAGE: About opens as a full-page view-swap, mirroring the methodology
    // interception above line-for-line (pushAboutHistoryState → showScreen). The
    // About nav link stays href="#about"; this preventDefault swaps the anchor
    // scroll for the screen toggle. BACK returns to the prior screen via the
    // popstate router. pushAboutHistoryState mirrors pushMethodologyHistoryState.
    let aboutHistoryActive = false;
    function pushAboutHistoryState() {
      if (!window.history || !window.history.pushState) return;
      if (window.history.state && window.history.state.tickedinScreen === "about") {
        aboutHistoryActive = true;  // already an about entry — don't stack duplicates
        return;
      }
      try {
        window.history.pushState({ tickedinScreen: "about" }, "", window.location.href);
        aboutHistoryActive = true;
      } catch (_) {
        /* history unavailable — the page still opens via the visual toggle */
      }
    }
    document.querySelectorAll('a[href="#about"]').forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        pushAboutHistoryState();
        showScreen("about");
      });
    });
    // GRADE-STATUS-PAGE: 등급·상태 안내 opens as a full-page view-swap, mirroring the
    // methodology/about interception line-for-line. Links come from the home tab row,
    // the sidebar legend panel, the footer, About, and the 검증 방법 cross-link.
    let gradeStatusHistoryActive = false;
    function pushGradeStatusHistoryState() {
      if (!window.history || !window.history.pushState) return;
      if (window.history.state && window.history.state.tickedinScreen === "gradeStatus") {
        gradeStatusHistoryActive = true;  // already a gradeStatus entry — don't stack duplicates
        return;
      }
      try {
        window.history.pushState({ tickedinScreen: "gradeStatus" }, "", window.location.href);
        gradeStatusHistoryActive = true;
      } catch (_) {
        /* history unavailable — the page still opens via the visual toggle */
      }
    }
    document.querySelectorAll('a[href="#gradeStatus"]').forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        pushGradeStatusHistoryState();
        showScreen("gradeStatus");
      });
    });
    // GRADE-STATUS-PAGE: the MOBILE-POLISH G(c) handler for a[href="#gradeLegend"]
    // was REMOVED — the full legend now lives on the #gradeStatus screen, and every
    // link that used to target #gradeLegend (home tab row, footer 등급 안내) now points
    // at #gradeStatus. No anchor references #gradeLegend any more, so the handler was
    // dead code. The id itself stays on the sidebar panel as its element id.
    // DESIGN-DETAIL-2 / DESIGN-UNIFY: restore the FULL home feed and land on home.
    // Shared by the header .brand-home logo AND the footer .footer-brand logo. The
    // detail loaders narrow currentReportContext to the single opened card, so without
    // clearCurrentReportContext() currentTopicCards() returns a 1-card pool and the
    // feed stays narrowed; setActiveDomain("전체") repaints the full unfiltered feed;
    // hideStatus()/v2ResetProgress() clear stale under-search banners. (The demote
    // clear…HistoryState calls are intentionally omitted — the symmetric popstate
    // router makes any residual entry harmless, and the demotes killed FORWARD.)
    function goHome() {
      clearCurrentReportContext();
      hideStatus();
      v2ResetProgress();
      setActiveDomain("전체");
      showScreen("home");
      window.scrollTo(0, 0);
    }
    // Header logo: preventDefault swaps the href="/" full reload for the in-page toggle.
    const brandHomeEl = document.querySelector(".brand-home");
    if (brandHomeEl) {
      brandHomeEl.addEventListener("click", (event) => {
        event.preventDefault();
        goHome();
      });
    }
    // DESIGN-UNIFY: footer logo (a <div>, no href) — same go-home behavior, no
    // preventDefault needed. cursor:pointer is added in CSS so it reads as clickable.
    const footerBrandEl = document.querySelector(".footer-brand");
    if (footerBrandEl) {
      footerBrandEl.addEventListener("click", () => {
        goHome();
      });
    }
    // FOOTER-CLEANUP A3: the footer 도메인 column links carry [data-domain] (the raw
    // canonical enum key — realestate/finance/… — as in DOMAIN_LABELS_KO; the column
    // now lists all 13 TAB_ORDER domains). A click switches the home feed
    // to that domain via the SAME sequence a category-tab click uses (clear context,
    // reset progress, showScreen home, setActiveDomain) — no new nav path. They are
    // <a href="#"> so preventDefault the anchor jump. Delegated on .footer-links;
    // closest("a[data-domain]") ignores the #about/#methodology anchors in the same
    // nav (those are handled by their per-href init handlers above).
    const footerLinksEl = document.querySelector(".footer-links");
    if (footerLinksEl) {
      footerLinksEl.addEventListener("click", (event) => {
        const link = event.target.closest("a[data-domain]");
        if (!link) return;
        event.preventDefault();
        clearCurrentReportContext();
        hideStatus();
        v2ResetProgress();
        showScreen("home");
        setActiveDomain(link.dataset.domain || "전체");
        window.scrollTo(0, 0);
      });
    }
    // DESIGN-UNIFY: "맨 위로" — smooth scroll to the top of the (long) detail screen.
    const scrollTopLinkEl = document.getElementById("scrollTopLink");
    if (scrollTopLinkEl) {
      scrollTopLinkEl.addEventListener("click", () => {
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    }
    if (categoryTabsEl) {
      // DISPLAY-CATEGORY B-1: filter on the raw domain enum (data-domain), not
      // the old resultCategory() heuristic. renderCategoryTabs re-renders the
      // tab strip (incl. the .active state) on every renderHotTopics, so the
      // handler only needs to set activeDomain and re-render.
      // DESIGN-DETAIL-2: prepend showScreen("home") so a tab click from ANY screen
      // (incl. 검증 방법) returns to the home feed filtered to that domain.
      categoryTabsEl.addEventListener("click", (event) => {
        const tab = event.target.closest("[data-domain]");
        if (!tab) return;
        // DESIGN-DETAIL-3b (FIX A): restore the FULL home feed BEFORE filtering. The
        // detail loaders narrow currentReportContext to the single opened card, so
        // without this clear setActiveDomain would filter that 1-card pool. Clearing
        // it lets currentTopicCards() fall back to the full serverHotTopicResults;
        // setActiveDomain then filters that full pool to the tab's domain — identical
        // to clicking the tab from a fresh home. (FIX B) clear both under-search
        // banners. (FIX C) the demote clear…HistoryState calls are dropped — the
        // symmetric popstate router makes any residual entry harmless.
        clearCurrentReportContext();
        hideStatus();
        v2ResetProgress();
        showScreen("home");
        setActiveDomain(tab.dataset.domain || "전체");
      });
    }
    // DESIGN-DETAIL-2: land on the home screen (hides #methodology, which is no
    // longer an always-on in-page section).
    showScreen("home");
    // DESIGN-C3h-2: the "{label} 전체 →" section links switch to that domain tab via
    // the SAME setActiveDomain. They carry [data-domain] (not [data-topic-source]),
    // so the .home-main card-open delegation ignores them and these never open a card.
    if (feedDomainSectionsEl) {
      feedDomainSectionsEl.addEventListener("click", (event) => {
        const btn = event.target.closest("[data-domain]");
        if (!btn) return;
        setActiveDomain(btn.dataset.domain || "전체");
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    }
    if (hotTopicsSortEl) {
      hotTopicsSortEl.addEventListener("change", () => {
        activeSort = hotTopicsSortEl.value || "뜨는순";
        currentPage = 1;  // DESIGN-C3-2: order changed → page N is meaningless, reset
        renderHotTopics();
      });
    }
    // DESIGN-C3-2: page-number nav — ONE delegated click on #feedPagination. A number
    // button jumps to that page; prev/next step by one. Disabled buttons (page-1 prev /
    // last next) are ignored. Then re-render (re-slices the grid; hero unchanged, sort +
    // domain KEPT — only currentPage changes) and smooth-scroll the grid into view so
    // page N shows from its start. NO history entry is pushed per page — that would
    // reintroduce the DETAIL-3b history sandwiching (page preservation on a detail BACK
    // is achieved by currentPage being module state, not by history).
    if (feedPaginationEl) {
      feedPaginationEl.addEventListener("click", (event) => {
        const btn = event.target.closest("button");
        if (!btn || btn.disabled) return;
        let target = currentPage;
        if (btn.dataset.page) {
          target = Number(btn.dataset.page);
        } else if (btn.dataset.pageNav === "prev") {
          target = currentPage - 1;
        } else if (btn.dataset.pageNav === "next") {
          target = currentPage + 1;
        } else {
          return;
        }
        if (!Number.isFinite(target) || target === currentPage) return;
        currentPage = target;  // renderHotTopics self-clamps to [1, totalPages]
        renderHotTopics();
        // DESIGN-C3-2-FIX (BUG 2): scroll to the SORT row (.feed-sort-row sits above
        // #hotTopics and below the hero band #hotTopicsTop) so the sort dropdown + grid
        // land at the top of the viewport with the hero scrolled off above — was the
        // grid's first card row, which pushed the sort above the fold. No sticky/fixed
        // header anywhere → plain block:"start" needs no offset. Falls back to the grid.
        const scrollTargetEl = document.querySelector(".feed-sort-row") || hotTopicsEl;
        if (scrollTargetEl) scrollTargetEl.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
    // HISTORY-BACK: minimal browser-history layer for the in-page detail view.
    // The detail view is NOT a view-swap (the #hotTopics/#domainSections feed is
    // never hidden); opening a detail just fills #results + scrolls to it. Without
    // a history entry, the browser BACK button leaves the site. This layer pushes
    // ONE entry when a detail opens (recording the pre-open scroll position) and,
    // on popstate back, dismisses the detail (renderResults([]) -> #results empty
    // state + feed re-render) and restores that scroll — keeping the user on the
    // page. URL is left UNCHANGED (no result_id write) so the ?result_id= deep-link
    // READ and the operator_tools replaceState are unaffected. State rides in
    // history.state.tickedinDetail + a module variable (no storage, no libs).
    let detailHistoryActive = false;
    let detailReturnScrollY = 0;
    function pushDetailHistoryState(scrollY) {
      if (!window.history || !window.history.pushState) return;
      // DESIGN-DETAIL-3b (FIX C): demote-before-push REMOVED. The old
      // clearMethodologyHistoryState() here replaceState(null)'d the methodology entry,
      // nulling its state so a later FORWARD popped null → router showed home →
      // methodology never re-shown. The symmetric popstate router routes by the POPPED
      // state in both directions, so a residual methodology entry is harmless.
      const alreadyDetail = !!(window.history.state && window.history.state.tickedinDetail);
      try {
        if (alreadyDetail) {
          // Card -> card without going back: keep the ORIGINAL pre-open scroll and
          // refresh the single detail entry in place (double-push guard — never
          // stack duplicate detail entries, so ONE back always returns home).
          window.history.replaceState(
            { tickedinDetail: true, scrollY: detailReturnScrollY },
            "", window.location.href
          );
        } else {
          detailReturnScrollY = (typeof scrollY === "number" && isFinite(scrollY))
            ? scrollY : (window.scrollY || 0);
          window.history.pushState(
            { tickedinDetail: true, scrollY: detailReturnScrollY },
            "", window.location.href
          );
        }
        detailHistoryActive = true;
      } catch (_) {
        /* history unavailable — feature degrades, the card still opens */
      }
    }
    // DESIGN-DETAIL-3: mirror of clearMethodologyHistoryState. Leaving the detail
    // SCREEN via in-page nav (tab/logo) OR before pushing another non-home screen —
    // demote the current detail entry to a neutral state so a later BACK can't
    // resurface a stale detail (at-most-one-non-home rule). replaceState keeps the URL.
    // DESIGN-DETAIL-3b (FIX C): now UNUSED — see clearMethodologyHistoryState. Kept.
    function clearDetailHistoryState() {
      if (!detailHistoryActive) return;
      detailHistoryActive = false;
      if (!window.history || !window.history.replaceState) return;
      if (window.history.state && window.history.state.tickedinDetail) {
        try { window.history.replaceState(null, "", window.location.href); } catch (_) { /* noop */ }
      }
    }
    // DISPLAY-CATEGORY B-1: open a topic card's detail report. Extracted so the
    // #hotTopics feed and the per-domain sections share one click path.
    function openTopicCard(card) {
      // HISTORY-BACK: push one history entry (with the pre-open scroll) at the
      // shared card-click choke point so BACK returns here. Captured BEFORE any
      // loader scrollIntoView fires. Covers all branches (history/server/in-memory).
      pushDetailHistoryState(window.scrollY || 0);
      const source = card.dataset.topicSource;
      const index = Number(card.dataset.topicIndex || 0);
      activeTopicKey = card.dataset.topicKey || "";
      selectedResultIndex = Number.isFinite(index) ? index : null;
      if (source === "history") {
        const record = safeReadLocalHistory().find((item) => item.id === card.dataset.topicRecordId);
        if (record) {
          // DETAIL-FIX B: loadHistoryRecord is async; it scrolls to the report top
          // itself AFTER renderResults paints (scrolling here would fire pre-render).
          loadHistoryRecord(record, `"${record.query || "검증 뉴스"}" 카드를 불러왔습니다.`, selectedResultIndex);
          return;
        }
      }
      // M45: server hot-topic cards carry their result_id in
      // data-topic-record-id; open the full row via the existing M39c
      // server-result loader (same path the ?result_id= deep link uses).
      if (source === "server") {
        const resultId = Number(card.dataset.topicRecordId);
        if (Number.isInteger(resultId) && resultId > 0) {
          // DETAIL-FIX B: loadServerResultById is async; it scrolls to the report top
          // itself AFTER renderResults paints (scrolling here would fire pre-render).
          loadServerResultById(resultId);
          return;
        }
      }
      if (Array.isArray(currentReportContext?.results) && currentReportContext.results[index]) {
        renderResults(currentReportContext.results, selectedResultIndex);
        showScreen("detail");  // DESIGN-DETAIL-3: detail screen at top (was scrollIntoView); showStatus after survives
        showStatus("상세 검증 리포트로 이동했습니다.", true);
      } else {
        renderSelectedIssueIntro([]);
        resultsEl.innerHTML = '<div class="empty-state">이 카드에는 아직 상세 검증 데이터가 충분하지 않습니다. 검색을 실행하면 상세 리포트가 표시됩니다.</div>';
        showScreen("detail");  // DESIGN-DETAIL-3: detail screen at top (was scrollIntoView)
      }
    }
    // DESIGN-C3h-1d: the card-open delegation is bound to .home-main (the shared
    // ancestor of #hotTopicsTop AND #hotTopics) so it catches clicks on the hero +
    // 오늘의 검증 cards (now in #hotTopicsTop) as well as the card-row + list (in
    // #hotTopics). Same closest("[data-topic-source]") → openTopicCard pattern;
    // openTopicCard / pushDetailHistoryState (HISTORY-BACK) are unchanged.
    const homeMainEl = hotTopicsEl && hotTopicsEl.closest(".home-main");
    if (homeMainEl) {
      homeMainEl.addEventListener("click", (event) => {
        const card = event.target.closest("[data-topic-source]");
        if (!card) return;
        openTopicCard(card);
      });
    }
    // HOME-TOP5 S5b: the #rankList delegated click listener was retired with
    // the 인기 검증 랭킹 panel (the trending panel uses plain hrefs).
    // RECENT-VIEWED: the strip items carry the same data-topic-* attrs as feed/sidebar
    // rows, so this delegated listener reuses openTopicCard verbatim (re-opens detail +
    // records the click + pushDetailHistoryState). Same closest() pattern as above.
    const recentViewedEl = document.getElementById("recentViewed");
    if (recentViewedEl) {
      recentViewedEl.addEventListener("click", (event) => {
        const card = event.target.closest("[data-topic-source]");
        if (!card) return;
        openTopicCard(card);
      });
    }
    // SIDEBAR-RANK-B2: 제보 — open a mailto to the REAL contact address with the
    // typed claim/link as the body (mirrors the per-analysis error-report mailto).
    // NO backend write, NO live-analysis trigger. Empty input still opens a blank
    // draft (graceful). location.href triggers the mail client without navigating.
    if (reportClaimBtnEl) {
      reportClaimBtnEl.addEventListener("click", () => {
        const text = (reportClaimInputEl && reportClaimInputEl.value || "").trim();
        const subject = encodeURIComponent("[tickedin 제보] 사실 확인 요청");
        const body = encodeURIComponent(text);
        window.location.href = `mailto:contact@tickedin.org?subject=${subject}&body=${body}`;
      });
    }
    // DESIGN-C3h-1d: the dedicated #domainSections (tier-2) click listener was
    // REMOVED — #domainSections lives inside .home-main, so the relocated .home-main
    // delegation above now opens tier-2 cards too (same closest()/openTopicCard path).
    // Keeping both would double-fire openTopicCard (double HISTORY-BACK entry).
    // 더보기/접기 are separate buttons, wired elsewhere.
    // ---- Phase 2 M8.1: server-backed reviewer UI ---------------------------
    // Wires the FastAPI /review/* endpoints to the admin panel. Auth is the
    // signed session cookie (AUTH-2d: session-only; the legacy X-Review-Token
    // path was retired). Safety contract:
    //   * gated calls carry only the httponly session cookie (same-origin)
    //   * UI never mutates analysis_results / final_decision / policy_confidence
    //   * a 401 from any gated call means "log in first"
    //   * No publish/correction path is exposed
    // ===== C20 — Server-review (operator) client =====
    // Only statuses the M8.x backend will actually return for a reviewer
    // are labeled. `published` / `corrected` remain reserved server-side
    // but the UI does NOT carry a display label for them — there is no
    // publication path in the reviewer UI.
    const SERVER_REVIEW_STATUS_LABELS = {
      pending_review: "대기 (pending_review)",
      needs_more_evidence: "근거 보강 필요 (needs_more_evidence)",
      approved: "승인됨 (approved)",
      rejected: "반려됨 (rejected)",
    };
    const SERVER_REVIEW_NO_CURRENT_RESULT_MESSAGE =
      "등록할 분석 결과가 없습니다. 먼저 분석을 실행하거나 기록에서 결과를 선택하세요.";
    // Surfaced by every gated action (refresh / register / decision) when no
    // authenticated session is present, so the lockout state is unambiguous.
    const SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE =
      "관리자 로그인이 필요합니다. 먼저 로그인해 주세요.";
    const SERVER_REVIEW_FROM_RESULT_PATH = "/review/tasks/from-result";

    // M9.2 — internal audit packet UI (read-only, explicit click only).
    // No public path. The UI never auto-fetches the audit packet; the
    // operator must press "감사 패킷 보기" while a task is selected and
    // a token is present.
    const SERVER_REVIEW_AUDIT_PACKET_PATH_TEMPLATE =
      "/review/tasks/{task_id}/audit-packet";
    const SERVER_REVIEW_AUDIT_PACKET_NO_TASK_MESSAGE =
      "감사 패킷을 불러올 검수 작업을 먼저 선택하세요.";
    const SERVER_REVIEW_AUDIT_PACKET_NO_TOKEN_MESSAGE =
      "관리자 로그인이 필요합니다. 먼저 로그인해 주세요.";
    const SERVER_REVIEW_AUDIT_PACKET_NOT_FOUND_MESSAGE =
      "감사 패킷을 찾을 수 없습니다. 검수 작업이 삭제되었거나 더 이상 존재하지 않을 수 있습니다.";
    const SERVER_REVIEW_AUDIT_PACKET_COPY_OK_MESSAGE =
      "감사 패킷 JSON을 복사했습니다. 내부 검수 기록 확인용이며 게시물이 아닙니다.";
    const SERVER_REVIEW_AUDIT_PACKET_COPY_FAIL_MESSAGE =
      "복사에 실패했습니다. 감사 패킷 내용을 직접 선택해 복사해 주세요.";
    const SERVER_REVIEW_AUDIT_PACKET_NOT_LOADED_MESSAGE =
      "복사할 감사 패킷이 없습니다. 먼저 '감사 패킷 보기'를 눌러 주세요.";

    let serverReviewSelectedTaskId = null;
    let serverReviewLastList = [];

    function serverReviewFormatErrorMessage(status, detail) {
      if (status === 401) {
        return SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE;
      }
      if (status === 404) {
        return "요청한 검수 작업을 찾을 수 없습니다.";
      }
      if (status === 409) {
        return "현재 상태에서는 이 판정을 적용할 수 없습니다.";
      }
      if (status === 400) {
        return "요청 형식이 올바르지 않습니다.";
      }
      if (status === 0) {
        return "네트워크 오류로 서버에 연결할 수 없습니다.";
      }
      return `서버 리뷰 API 호출에 실패했습니다 (HTTP ${status || "?"})`;
    }

    function serverReviewFormatStatusLabel(status) {
      const key = String(status || "").trim();
      return SERVER_REVIEW_STATUS_LABELS[key] || (key || "(없음)");
    }

    function serverReviewSetStatusBanner(kind, message) {
      const el = document.getElementById("serverReviewStatus");
      if (!el) return;
      el.classList.remove("is-info", "is-success", "is-error");
      if (!message) {
        el.style.display = "none";
        el.textContent = "";
        return;
      }
      if (kind === "success") el.classList.add("is-success");
      else if (kind === "error") el.classList.add("is-error");
      else el.classList.add("is-info");
      el.style.display = "block";
      el.textContent = message;
    }

    async function serverReviewFetch(path, options) {
      const opts = options || {};
      // AUTH-2d: session-only. The gated calls carry just the httponly
      // session cookie (same-origin); no X-Review-Token header. A 401 from
      // the backend means the operator must log in.
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        opts.headers || {},
      );
      const init = { method: opts.method || "GET", headers, credentials: "same-origin" };
      if (opts.body != null) {
        init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
      }
      let resp;
      try {
        resp = await fetch(`${API_BASE}${path}`, init);
      } catch (err) {
        return { ok: false, status: 0, body: null, reason: "network" };
      }
      let body = null;
      try {
        body = await resp.json();
      } catch (_) {
        body = null;
      }
      return { ok: resp.ok, status: resp.status, body };
    }

    // M9.2 — currently-loaded audit packet (raw JSON object). Lives in
    // a module-scoped variable rather than the DOM so the copy button
    // can grab the *unrendered* JSON regardless of how the summary view
    // is collapsed/expanded. Cleared on task change and on token clear.
    let serverReviewLoadedAuditPacket = null;

    function serverReviewResetAuditPacketView() {
      serverReviewLoadedAuditPacket = null;
      const summary = document.getElementById("serverReviewAuditPacketSummary");
      if (summary) {
        summary.hidden = true;
        summary.innerHTML = "";
      }
      const rawWrap = document.getElementById("serverReviewAuditPacketRawWrap");
      if (rawWrap) {
        rawWrap.hidden = true;
        if (rawWrap.open) rawWrap.open = false;
      }
      const raw = document.getElementById("serverReviewAuditPacketRaw");
      // textContent (not innerHTML) — any future audit-packet content must
      // be inserted as plain text only. Defensive even though the JSON we
      // pretty-print never carries HTML.
      if (raw) raw.textContent = "";
      const copyBtn = document.getElementById("serverReviewAuditPacketCopyBtn");
      if (copyBtn) copyBtn.disabled = true;
      const status = document.getElementById("serverReviewAuditPacketStatus");
      if (status) {
        status.style.display = "none";
        status.textContent = "";
        status.classList.remove("is-info", "is-success", "is-error");
      }
    }

    function serverReviewClearDetail() {
      serverReviewSelectedTaskId = null;
      const detail = document.getElementById("serverReviewDetail");
      if (detail) detail.hidden = true;
      const body = document.getElementById("serverReviewDetailBody");
      if (body) body.innerHTML = "";
      const history = document.getElementById("serverReviewHistory");
      if (history) {
        history.innerHTML = '<div class="empty-state">선택한 작업의 판정 이력이 없습니다.</div>';
      }
      // M9.2 — clearing the detail (e.g. token clear) must also drop
      // any loaded audit packet view so a stale internal-audit JSON
      // never lingers on screen for the next operator session.
      serverReviewResetAuditPacketView();
    }

    // Full lockout reset (used when the operator panel is hidden): drop the
    // cached task list so a stale row cannot be selected, blank the register
    // banner, and re-render the empty placeholder in the list area.
    function serverReviewResetAfterTokenClear() {
      serverReviewLastList = [];
      serverReviewClearDetail();
      serverReviewRenderEmpty("로그인하면 서버 검수 큐를 불러옵니다.");
      serverReviewSetRegisterStatus(null, "");
    }

    function serverReviewRenderEmpty(message) {
      const list = document.getElementById("serverReviewList");
      if (!list) return;
      const safe = escapeHtml(message || "조건에 맞는 검수 작업이 없습니다.");
      list.innerHTML = `<div class="empty-state">${safe}</div>`;
    }

    function serverReviewRenderList(tasks) {
      const list = document.getElementById("serverReviewList");
      if (!list) return;
      serverReviewLastList = Array.isArray(tasks) ? tasks : [];
      if (!serverReviewLastList.length) {
        serverReviewRenderEmpty();
        return;
      }
      const rows = serverReviewLastList.map((task) => {
        const id = String(task.task_id || "");
        const statusKey = String(task.status || "unknown");
        const statusLabel = serverReviewFormatStatusLabel(statusKey);
        const chipClass = `server-review-status-chip status-${escapeHtml(statusKey)}`;
        const title = escapeHtml(task.title || task.query || "(제목 없음)");
        const claim = escapeHtml(task.claim_text || "");
        const verdict = escapeHtml(task.final_decision || "");
        const conf = escapeHtml(task.policy_confidence || "");
        const updated = escapeHtml(task.updated_at || task.created_at || "");
        const isActive = serverReviewSelectedTaskId === id ? " is-active" : "";
        return `
          <div class="server-review-list-row${isActive}" data-server-review-task-id="${escapeHtml(id)}" role="button" tabindex="0">
            <div>
              <div class="row-title">${title}</div>
              <div class="row-meta">주장: ${claim || "(없음)"}</div>
              <div class="row-meta">초안 판정: ${verdict || "(없음)"} · 신뢰: ${conf || "(없음)"}</div>
              <div class="row-meta">업데이트: ${updated || "(없음)"}</div>
            </div>
            <span class="${chipClass}">${escapeHtml(statusLabel)}</span>
          </div>
        `;
      }).join("");
      list.innerHTML = rows;
    }

    function serverReviewRenderDetail(task, decisions) {
      const detail = document.getElementById("serverReviewDetail");
      const body = document.getElementById("serverReviewDetailBody");
      if (!detail || !body) return;
      detail.hidden = false;
      const statusKey = String(task.status || "unknown");
      const chipClass = `server-review-status-chip status-${escapeHtml(statusKey)}`;
      const statusLabel = serverReviewFormatStatusLabel(statusKey);
      body.innerHTML = `
        <div class="server-review-detail-grid">
          <div><strong>작업 ID</strong>${escapeHtml(task.task_id || "")}</div>
          <div><strong>현재 상태</strong><span class="${chipClass}">${escapeHtml(statusLabel)}</span></div>
          <div><strong>검색어</strong>${escapeHtml(task.query || "(없음)")}</div>
          <div><strong>인덱스</strong>${escapeHtml(String(task.item_index ?? 0))}</div>
          <div style="grid-column: 1 / -1;"><strong>제목</strong>${escapeHtml(task.title || "(없음)")}</div>
          <div style="grid-column: 1 / -1;"><strong>핵심 주장</strong>${escapeHtml(task.claim_text || "(없음)")}</div>
          <div><strong>초안 판정</strong>${escapeHtml(task.final_decision || "(없음)")}</div>
          <div><strong>신뢰도 라벨</strong>${escapeHtml(task.policy_confidence || "(없음)")}</div>
          <div><strong>원문 URL</strong>${task.url ? `<a href="${escapeHtml(safeUrl(task.url))}" target="_blank" rel="noopener noreferrer">${escapeHtml(task.url)}</a>` : "(없음)"}</div>
          <div><strong>사람 검토 필요</strong>${task.human_review_required ? "예" : "아니오"}</div>
          <div><strong>생성</strong>${escapeHtml(task.created_at || "(없음)")}</div>
          <div><strong>업데이트</strong>${escapeHtml(task.updated_at || "(없음)")}</div>
        </div>
      `;
      serverReviewRenderHistory(decisions);
    }

    function serverReviewRenderHistory(decisions) {
      const el = document.getElementById("serverReviewHistory");
      if (!el) return;
      const items = Array.isArray(decisions) ? decisions : [];
      if (!items.length) {
        el.innerHTML = '<div class="empty-state">선택한 작업의 판정 이력이 없습니다.</div>';
        return;
      }
      el.innerHTML = items.map((d) => {
        const decision = escapeHtml(d.decision || "");
        const prev = escapeHtml(d.previous_status || "");
        const next = escapeHtml(d.new_status || "");
        const reviewer = escapeHtml(d.reviewer_id || "");
        const comment = escapeHtml(d.comment || "");
        const note = escapeHtml(d.public_note || "");
        const when = escapeHtml(d.created_at || "");
        // M9.0 — audit fields. ``transition`` falls back to the
        // legacy "prev → next" string when the server hasn't enriched
        // the row yet; ``decision_source`` defaults to "unknown".
        const transition = escapeHtml(
          d.transition || (
            prev && next
              ? `${prev} → ${next}`
              : (prev ? `${prev} → (없음)` : "(상태 변경 없음)")
          ),
        );
        const source = escapeHtml(d.decision_source || "unknown");
        const decisionId = escapeHtml(d.decision_id || "");
        return `
          <div class="server-review-history-row">
            <div class="row-head">
              <span>${decision} · ${transition}</span>
              <span>${when}</span>
            </div>
            <div class="row-meta server-review-history-audit">
              <span>source: ${source}</span>
              ${decisionId ? `<span>id: ${decisionId}</span>` : ""}
              ${reviewer ? `<span>리뷰어: ${reviewer}</span>` : ""}
            </div>
            <div class="row-body">${comment ? `코멘트: ${comment}\n` : ""}${note ? `공개 메모: ${note}` : ""}</div>
          </div>
        `;
      }).join("");
    }

    async function serverReviewLoadList() {
      const list = document.getElementById("serverReviewList");
      if (!list) return;
      if (!serverReviewPrivilegedReady()) {
        serverReviewSetStatusBanner("info", SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE);
        serverReviewRenderEmpty("로그인하면 서버 검수 큐를 불러옵니다.");
        serverReviewClearDetail();
        return;
      }
      const filterEl = document.getElementById("serverReviewStatusFilter");
      const status = filterEl && filterEl.value ? filterEl.value : "";
      serverReviewSetStatusBanner("info", "서버 검수 큐를 불러오는 중...");
      list.innerHTML = '<div class="empty-state">서버 검수 큐를 불러오는 중...</div>';
      const path = status
        ? `/review/tasks?status=${encodeURIComponent(status)}`
        : "/review/tasks";
      const result = await serverReviewFetch(path);
      if (!result.ok) {
        const message = serverReviewFormatErrorMessage(result.status);
        serverReviewSetStatusBanner("error", message);
        serverReviewRenderEmpty(message);
        return;
      }
      const tasks = (result.body && Array.isArray(result.body.tasks)) ? result.body.tasks : [];
      serverReviewRenderList(tasks);
      const count = (result.body && typeof result.body.count === "number") ? result.body.count : tasks.length;
      serverReviewSetStatusBanner(
        "success",
        `서버 검수 큐 ${count}건 (필터: ${status || "전체"})`,
      );
    }

    async function serverReviewLoadDetail(taskId) {
      const id = String(taskId || "").trim();
      if (!id) return;
      if (!serverReviewPrivilegedReady()) {
        serverReviewSetStatusBanner("error", SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE);
        return;
      }
      // M9.2 — selecting a different task must drop any previously loaded
      // audit packet view. The audit packet is NOT auto-fetched for the
      // new task; the operator must press the explicit button.
      if (serverReviewSelectedTaskId !== id) {
        serverReviewResetAuditPacketView();
      }
      serverReviewSelectedTaskId = id;
      // Reflect selection in the list immediately.
      serverReviewRenderList(serverReviewLastList);
      const result = await serverReviewFetch(`/review/tasks/${encodeURIComponent(id)}`);
      if (!result.ok) {
        const message = serverReviewFormatErrorMessage(result.status);
        serverReviewSetStatusBanner("error", message);
        return;
      }
      const task = (result.body && result.body.task) || {};
      const decisions = (result.body && Array.isArray(result.body.decisions))
        ? result.body.decisions
        : (Array.isArray(task.decisions) ? task.decisions : []);
      serverReviewRenderDetail(task, decisions);
      serverReviewSetStatusBanner("success", `작업 ${id} 상세를 불러왔습니다.`);
    }

    async function serverReviewSubmitDecision() {
      if (!serverReviewPrivilegedReady()) {
        serverReviewSetStatusBanner("error", SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE);
        return;
      }
      const id = serverReviewSelectedTaskId;
      if (!id) {
        serverReviewSetStatusBanner("error", "먼저 검수 작업을 선택해 주세요.");
        return;
      }
      const typeEl = document.getElementById("serverReviewDecisionType");
      const reviewerEl = document.getElementById("serverReviewReviewerId");
      const commentEl = document.getElementById("serverReviewComment");
      const publicNoteEl = document.getElementById("serverReviewPublicNote");
      const decision = (typeEl && typeEl.value) || "comment";
      const body = {
        decision,
        reviewer_id: (reviewerEl && reviewerEl.value) ? reviewerEl.value.trim() : null,
        comment: (commentEl && commentEl.value) ? commentEl.value : null,
        public_note: (publicNoteEl && publicNoteEl.value) ? publicNoteEl.value : null,
      };
      const result = await serverReviewFetch(
        `/review/tasks/${encodeURIComponent(id)}/decision`,
        { method: "POST", body },
      );
      if (!result.ok) {
        const message = serverReviewFormatErrorMessage(result.status);
        serverReviewSetStatusBanner("error", message);
        return;
      }
      if (commentEl) commentEl.value = "";
      if (publicNoteEl) publicNoteEl.value = "";
      serverReviewSetStatusBanner(
        "success",
        result.body && result.body.status_changed
          ? `판정 기록 완료: ${result.body.previous_status} → ${result.body.new_status}`
          : "판정 기록을 추가했습니다 (상태 변경 없음).",
      );
      const task = (result.body && result.body.task) || {};
      const decisions = Array.isArray(task.decisions) ? task.decisions : [];
      serverReviewRenderDetail(task, decisions);
      // Refresh list so the row's status chip reflects the new state.
      await serverReviewLoadList();
    }

    // M8.2 — Build the /review/tasks/from-result body from the current
    // analysis result snapshot without mutating the source. The caller
    // passes `currentReportContext` (or a compatible shape). Only safe,
    // already-public fields go into the payload; the server still runs
    // its own snapshot extractor for verdict-side fields.
    function buildReviewTaskFromResultPayload(context, itemIndex) {
      const safeContext = context && typeof context === "object" ? context : {};
      const sourceResults = Array.isArray(safeContext.results) ? safeContext.results : [];
      const resultsCopy = sourceResults.slice();
      const safeIndex = (
        Number.isInteger(itemIndex) && itemIndex >= 0 && itemIndex < resultsCopy.length
      ) ? itemIndex : 0;
      const focusItem = (resultsCopy[safeIndex] && typeof resultsCopy[safeIndex] === "object")
        ? resultsCopy[safeIndex]
        : {};
      const resultIdRaw = focusItem.result_id;
      const resultId = (resultIdRaw === null || resultIdRaw === undefined || resultIdRaw === "")
        ? null
        : String(resultIdRaw);
      const query = safeContext.query ? String(safeContext.query) : null;
      // Wrap in the /jobs/{id}/result-style envelope the server expects.
      // We do NOT mutate any item in `sourceResults`; the wrapper holds
      // the same references — the server snapshot extractor reads only.
      return {
        result_id: resultId,
        job_id: null,
        item_index: safeIndex,
        query,
        result_payload: {
          status: "ok",
          query,
          result: { results: resultsCopy },
        },
      };
    }

    function serverReviewSetRegisterStatus(kind, message) {
      const el = document.getElementById("serverReviewRegisterStatus");
      if (!el) return;
      el.classList.remove("is-info", "is-success", "is-error");
      if (!message) {
        el.style.display = "none";
        el.textContent = "";
        return;
      }
      if (kind === "success") el.classList.add("is-success");
      else if (kind === "error") el.classList.add("is-error");
      else el.classList.add("is-info");
      el.style.display = "block";
      el.textContent = message;
    }

    // M40b — status line for the operator-only promote panel. Mirrors
    // serverReviewSetRegisterStatus exactly, targeting its own element.
    function serverReviewSetPromoteStatus(kind, message) {
      const el = document.getElementById("serverReviewPromoteStatus");
      if (!el) return;
      el.classList.remove("is-info", "is-success", "is-error");
      if (!message) {
        el.style.display = "none";
        el.textContent = "";
        return;
      }
      if (kind === "success") el.classList.add("is-success");
      else if (kind === "error") el.classList.add("is-error");
      else el.classList.add("is-info");
      el.style.display = "block";
      el.textContent = message;
    }

    // -----------------------------------------------------------------
    // M9.2 — internal audit packet viewer / copy helper.
    //
    // Safety contract pinned by tests/review_ui.test.js:
    //   * Never auto-fetched. Only explicit button click triggers the
    //     GET /review/tasks/{id}/audit-packet request.
    //   * Authenticated via the session cookie through serverReviewFetch.
    //   * No publish/correct affordance. The copy success message
    //     explicitly says "내부 검수 기록 확인용이며 게시물이 아닙니다".
    //   * View is reset on task change + token clear.
    //   * All rendered text uses textContent (or escapeHtml when going
    //     into innerHTML) — never raw innerHTML with packet content.
    // -----------------------------------------------------------------

    function serverReviewSetAuditPacketStatus(kind, message) {
      const el = document.getElementById("serverReviewAuditPacketStatus");
      if (!el) return;
      el.classList.remove("is-info", "is-success", "is-error");
      if (!message) {
        el.style.display = "none";
        el.textContent = "";
        return;
      }
      if (kind === "success") el.classList.add("is-success");
      else if (kind === "error") el.classList.add("is-error");
      else el.classList.add("is-info");
      el.style.display = "block";
      el.textContent = message;
    }

    function serverReviewAuditPacketPath(taskId) {
      return SERVER_REVIEW_AUDIT_PACKET_PATH_TEMPLATE.replace(
        "{task_id}", encodeURIComponent(String(taskId || "")),
      );
    }

    function serverReviewBuildAuditPacketSummary(packet) {
      // Defensive: any packet field may be missing/null. The summary
      // surfaces stable fields only — no semantic labels, no raw
      // payload. ``safety_contract`` is shown only as a debug-only
      // contract (no value here ever flips to "true" for mutation /
      // publication flags by design).
      const safe = (packet && typeof packet === "object") ? packet : {};
      const task = (safe.task && typeof safe.task === "object") ? safe.task : {};
      const verdict = (safe.verdict_snapshot && typeof safe.verdict_snapshot === "object")
        ? safe.verdict_snapshot : {};
      const contract = (safe.safety_contract && typeof safe.safety_contract === "object")
        ? safe.safety_contract : {};
      const decisions = Array.isArray(safe.review_decisions) ? safe.review_decisions : [];
      const render = (v) => {
        if (v === null || v === undefined || v === "") return "(없음)";
        return String(v);
      };
      const boolText = (v) => (v === true ? "true" : (v === false ? "false" : "(없음)"));
      return [
        { label: "packet_type", value: render(safe.packet_type) },
        { label: "audit_version", value: render(safe.audit_version) },
        { label: "generated_at", value: render(safe.generated_at) },
        { label: "task_id", value: render(task.task_id) },
        { label: "task.status", value: render(task.status) },
        { label: "verdict_snapshot.final_decision", value: render(verdict.final_decision) },
        { label: "verdict_snapshot.policy_confidence", value: render(verdict.policy_confidence) },
        { label: "verdict_snapshot.verification_card_status", value: render(verdict.verification_card_status) },
        { label: "review_decision_count", value: String(decisions.length) },
        { label: "safety_contract.publication", value: boolText(contract.publication) },
        { label: "safety_contract.mutates_final_decision", value: boolText(contract.mutates_final_decision) },
        { label: "safety_contract.mutates_policy_confidence", value: boolText(contract.mutates_policy_confidence) },
        { label: "safety_contract.mutates_verification_card", value: boolText(contract.mutates_verification_card) },
        { label: "safety_contract.semantic_matching_debug_only", value: boolText(contract.semantic_matching_debug_only) },
      ];
    }

    function serverReviewRenderAuditPacket(packet) {
      const summaryEl = document.getElementById("serverReviewAuditPacketSummary");
      const rawWrap = document.getElementById("serverReviewAuditPacketRawWrap");
      const rawEl = document.getElementById("serverReviewAuditPacketRaw");
      const copyBtn = document.getElementById("serverReviewAuditPacketCopyBtn");
      if (!summaryEl || !rawEl || !rawWrap) return;

      const rows = serverReviewBuildAuditPacketSummary(packet);
      // Use escapeHtml on every label/value before inserting — the audit
      // packet is JSON we received from the server, but defensive HTML
      // escaping makes a stray "<" in a claim_text safe.
      const dl = rows.map(({ label, value }) => (
        `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`
      )).join("");
      summaryEl.hidden = false;
      summaryEl.innerHTML = (
        `<dl>${dl}</dl>`
        + `<div class="audit-section-title">사람 검토 기록 확인용 · 게시가 아님</div>`
      );

      // Raw JSON goes through textContent ONLY. No innerHTML for raw.
      try {
        rawEl.textContent = JSON.stringify(packet, null, 2);
      } catch (_) {
        rawEl.textContent = "";
      }
      rawWrap.hidden = false;
      if (copyBtn) copyBtn.disabled = !packet;
    }

    async function serverReviewLoadAuditPacket() {
      if (!serverReviewPrivilegedReady()) {
        serverReviewSetAuditPacketStatus(
          "error", SERVER_REVIEW_AUDIT_PACKET_NO_TOKEN_MESSAGE,
        );
        return;
      }
      const taskId = serverReviewSelectedTaskId;
      if (!taskId) {
        serverReviewSetAuditPacketStatus(
          "error", SERVER_REVIEW_AUDIT_PACKET_NO_TASK_MESSAGE,
        );
        return;
      }
      serverReviewSetAuditPacketStatus("info", "감사 패킷을 불러오는 중...");
      const result = await serverReviewFetch(
        serverReviewAuditPacketPath(taskId),
      );
      if (!result.ok) {
        // 404 has a packet-specific message; everything else (503/403/0/...)
        // routes through the shared formatErrorMessage so the disabled-API
        // and 403 messages stay byte-for-byte identical to M8.1/M8.7.
        const message = (result.status === 404)
          ? SERVER_REVIEW_AUDIT_PACKET_NOT_FOUND_MESSAGE
          : serverReviewFormatErrorMessage(result.status);
        serverReviewSetAuditPacketStatus("error", message);
        // Do NOT keep a stale packet visible on error.
        serverReviewLoadedAuditPacket = null;
        const summary = document.getElementById("serverReviewAuditPacketSummary");
        if (summary) { summary.hidden = true; summary.innerHTML = ""; }
        const rawWrap = document.getElementById("serverReviewAuditPacketRawWrap");
        if (rawWrap) { rawWrap.hidden = true; }
        const rawEl = document.getElementById("serverReviewAuditPacketRaw");
        if (rawEl) rawEl.textContent = "";
        const copyBtn = document.getElementById("serverReviewAuditPacketCopyBtn");
        if (copyBtn) copyBtn.disabled = true;
        return;
      }
      serverReviewLoadedAuditPacket = (result.body && typeof result.body === "object")
        ? result.body : null;
      serverReviewRenderAuditPacket(serverReviewLoadedAuditPacket);
      serverReviewSetAuditPacketStatus(
        "success",
        "감사 패킷을 불러왔습니다 (내부 검수 기록 확인용, 게시가 아님).",
      );
    }

    async function serverReviewCopyAuditPacket() {
      if (!serverReviewLoadedAuditPacket) {
        serverReviewSetAuditPacketStatus(
          "error", SERVER_REVIEW_AUDIT_PACKET_NOT_LOADED_MESSAGE,
        );
        return;
      }
      let serialized;
      try {
        serialized = JSON.stringify(serverReviewLoadedAuditPacket, null, 2);
      } catch (_) {
        serverReviewSetAuditPacketStatus(
          "error", SERVER_REVIEW_AUDIT_PACKET_COPY_FAIL_MESSAGE,
        );
        return;
      }
      // Prefer the modern clipboard API; fall back to execCommand for
      // legacy contexts. Either path must never crash on failure.
      let copied = false;
      try {
        if (navigator && navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(serialized);
          copied = true;
        }
      } catch (_) {
        copied = false;
      }
      if (!copied) {
        try {
          const ta = document.createElement("textarea");
          ta.value = serialized;
          ta.setAttribute("readonly", "");
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          copied = document.execCommand && document.execCommand("copy");
          document.body.removeChild(ta);
        } catch (_) {
          copied = false;
        }
      }
      if (copied) {
        serverReviewSetAuditPacketStatus(
          "success", SERVER_REVIEW_AUDIT_PACKET_COPY_OK_MESSAGE,
        );
      } else {
        serverReviewSetAuditPacketStatus(
          "error", SERVER_REVIEW_AUDIT_PACKET_COPY_FAIL_MESSAGE,
        );
      }
    }

    async function serverReviewRegisterCurrentResult() {
      if (!serverReviewPrivilegedReady()) {
        serverReviewSetRegisterStatus(
          "error",
          SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE,
        );
        return;
      }
      const context = currentReportContext;
      const hasResults = context
        && Array.isArray(context.results)
        && context.results.length > 0;
      if (!hasResults) {
        serverReviewSetRegisterStatus("error", SERVER_REVIEW_NO_CURRENT_RESULT_MESSAGE);
        return;
      }
      const candidateIndex = Number.isInteger(selectedResultIndex)
        ? selectedResultIndex
        : 0;
      const payload = buildReviewTaskFromResultPayload(context, candidateIndex);
      serverReviewSetRegisterStatus("info", "서버 검수 큐에 등록 중...");
      const result = await serverReviewFetch(
        SERVER_REVIEW_FROM_RESULT_PATH,
        { method: "POST", body: payload },
      );
      if (!result.ok) {
        const message = serverReviewFormatErrorMessage(result.status);
        serverReviewSetRegisterStatus("error", message);
        return;
      }
      const task = (result.body && result.body.task) || {};
      const idempotent = !!(result.body && result.body.idempotent);
      serverReviewSetRegisterStatus(
        "success",
        idempotent
          ? "이미 검수 큐에 등록된 결과입니다. 기존 검수 작업을 표시합니다 (사람 검토 필요)."
          : "검수 큐 등록 완료. 사람 검토 대기 상태로 추가되었습니다.",
      );
      // Refresh the queue so the row's status chip reflects reality, then
      // select the created/existing task if the server returned an id.
      await serverReviewLoadList();
      const taskId = task && task.task_id ? String(task.task_id) : "";
      if (taskId) {
        await serverReviewLoadDetail(taskId);
      }
    }

    // M40b — resolve which analysis_results id the promote action targets.
    // Priority: an explicit positive-integer typed into the panel input,
    // else the currently-focused result's result_id (the same id the
    // ?result_id= loader uses). Returns null when neither yields a valid id.
    function serverReviewResolvePromoteId() {
      const input = document.getElementById("serverReviewPromoteId");
      const typed = input && input.value ? String(input.value).trim() : "";
      if (typed) {
        const typedId = Number(typed);
        return (Number.isInteger(typedId) && typedId > 0) ? typedId : null;
      }
      const context = currentReportContext;
      const results = context && Array.isArray(context.results) ? context.results : [];
      if (!results.length) return null;
      const idx = Number.isInteger(selectedResultIndex) ? selectedResultIndex : 0;
      const focus = results[idx] || results[0] || {};
      const focusId = Number(focus.result_id);
      return (Number.isInteger(focusId) && focusId > 0) ? focusId : null;
    }

    // M40b — call the session-gated M40a endpoint to set/clear the M39a
    // human-review columns, then refresh the card in place (M39c) so the
    // "사람 검토됨" badge appears/disappears. Operator-only (this panel
    // lives inside #operatorTools). Reuses serverReviewFetch (session cookie)
    // and the existing error messaging.
    async function serverReviewPromoteCurrentResult(promote) {
      if (!serverReviewPrivilegedReady()) {
        serverReviewSetPromoteStatus("error", SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE);
        return;
      }
      const resultId = serverReviewResolvePromoteId();
      if (!resultId) {
        serverReviewSetPromoteStatus("error", SERVER_REVIEW_NO_CURRENT_RESULT_MESSAGE);
        return;
      }
      serverReviewSetPromoteStatus(
        "info",
        promote ? "사람 검토됨으로 승격 중..." : "사람 검토됨 표시 해제 중...",
      );
      const result = await serverReviewFetch(
        `/review/results/${encodeURIComponent(resultId)}/promote`,
        { method: "POST", body: { promote: !!promote, reviewer: "operator" } },
      );
      if (!result.ok) {
        serverReviewSetPromoteStatus("error", serverReviewFormatErrorMessage(result.status));
        return;
      }
      serverReviewSetPromoteStatus(
        "success",
        promote
          ? `결과 ${resultId}을(를) 사람 검토됨으로 승격했습니다.`
          : `결과 ${resultId}의 사람 검토됨 표시를 해제했습니다.`,
      );
      // Best-effort in-place refresh so the badge updates without a reload.
      try {
        await loadServerResultById(resultId);
      } catch (_) {
        /* status already shown; card refresh is non-critical */
      }
    }

    // ===== AUTH-2c/2d — operator account login (session-only auth) =====
    // Establishes a signed session cookie via /auth/login. Since AUTH-2d this
    // is the ONLY admin auth path (the legacy X-Review-Token was retired). The
    // submitted password is read into a local var, sent, and the input cleared;
    // it is NEVER written to the DOM, sessionStorage, localStorage, or any log.
    const AUTH_LOGIN_PATH = "/auth/login";
    const AUTH_LOGOUT_PATH = "/auth/logout";
    const AUTH_ME_PATH = "/auth/me";
    const AUTH_LOGIN_FAILED_MESSAGE =
      "로그인에 실패했습니다. 사용자 이름과 비밀번호를 확인해 주세요.";
    const AUTH_LOGIN_ERROR_MESSAGE =
      "로그인 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.";
    const AUTH_LOGGED_OUT_MESSAGE = "로그아웃되었습니다.";

    function authLoggedInLabel(role) {
      return `관리자(${String(role || "admin")})로 로그인됨`;
    }

    // Display-only mirror of the last-known /auth/me state. The real gate is
    // server-side (require_admin, session-only since AUTH-2d); this only drives
    // UI enablement / the "log in first" guard messages.
    let authSessionActive = false;

    // AUTH-2d: privilegedReady = authenticated session ONLY (token path gone).
    function serverReviewPrivilegedReady() {
      return authSessionActive;
    }

    function authSetStatus(kind, message) {
      const el = document.getElementById("serverReviewLoginStatus");
      if (!el) return;
      el.classList.remove("is-info", "is-success", "is-error");
      if (!message) {
        el.style.display = "none";
        el.textContent = "";
        return;
      }
      if (kind === "success") el.classList.add("is-success");
      else if (kind === "error") el.classList.add("is-error");
      else el.classList.add("is-info");
      el.style.display = "block";
      el.textContent = message;
    }

    function authReflectSession(authenticated, role) {
      authSessionActive = !!authenticated;
      const loginBtn = document.getElementById("serverReviewLoginBtn");
      const logoutBtn = document.getElementById("serverReviewLogoutBtn");
      if (authenticated) {
        authSetStatus("success", authLoggedInLabel(role));
        if (loginBtn) loginBtn.disabled = true;
        if (logoutBtn) logoutBtn.disabled = false;
      } else {
        if (loginBtn) loginBtn.disabled = false;
        if (logoutBtn) logoutBtn.disabled = true;
      }
    }

    async function authFetchJson(path, options) {
      const opts = options || {};
      const init = {
        method: opts.method || "GET",
        // Same-origin: the httponly session cookie is sent/stored by the
        // browser. JS never reads or writes the cookie itself.
        credentials: "same-origin",
        headers: Object.assign(
          { "Content-Type": "application/json" }, opts.headers || {},
        ),
      };
      if (opts.body != null) {
        init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
      }
      let resp;
      try {
        resp = await fetch(`${API_BASE}${path}`, init);
      } catch (err) {
        return { ok: false, status: 0, body: null };
      }
      let body = null;
      try {
        body = await resp.json();
      } catch (_) {
        body = null;
      }
      return { ok: resp.ok, status: resp.status, body };
    }

    async function authMe() {
      const res = await authFetchJson(AUTH_ME_PATH, { method: "GET" });
      const authenticated = !!(res.ok && res.body && res.body.authenticated);
      const role = (res.body && res.body.role) || null;
      authReflectSession(authenticated, role);
      return { authenticated: authenticated, role: role };
    }

    async function authLogin(username, password) {
      const res = await authFetchJson(AUTH_LOGIN_PATH, {
        method: "POST",
        body: { username: username || "", password: password || "" },
      });
      // Clear the password input immediately — never leave it on-screen, never
      // persist it anywhere.
      const passInput = document.getElementById("serverReviewLoginPass");
      if (passInput) passInput.value = "";
      if (res.ok && res.body && res.body.ok) {
        const role = res.body.role || "admin";
        authReflectSession(true, role);
        return { ok: true, role: role };
      }
      // Generic failure — never reveal username-vs-password, never echo the
      // submitted password.
      authSetStatus(
        "error",
        res.status === 0 ? AUTH_LOGIN_ERROR_MESSAGE : AUTH_LOGIN_FAILED_MESSAGE,
      );
      authReflectSession(false, null);
      return { ok: false };
    }

    async function authLogout() {
      await authFetchJson(AUTH_LOGOUT_PATH, { method: "POST" });
      authReflectSession(false, null);
      authSetStatus("info", AUTH_LOGGED_OUT_MESSAGE);
      return { ok: true };
    }

    function authBindEvents() {
      const loginBtn = document.getElementById("serverReviewLoginBtn");
      const logoutBtn = document.getElementById("serverReviewLogoutBtn");
      const userInput = document.getElementById("serverReviewLoginUser");
      const passInput = document.getElementById("serverReviewLoginPass");
      if (loginBtn && userInput && passInput) {
        loginBtn.addEventListener("click", async () => {
          const username = (userInput.value || "").trim();
          // Read the password into a local var only; do NOT persist it.
          const password = passInput.value || "";
          await authLogin(username, password);
        });
      }
      if (logoutBtn) {
        logoutBtn.addEventListener("click", async () => {
          await authLogout();
        });
      }
      // Reflect current session state, but only when the operator panel is
      // actually exposed — avoids an /auth/me ping on every public page load.
      if (loginBtn && operatorToolsFlagSet()) {
        authMe();
      }
    }

    function serverReviewBindEvents() {
      const refreshBtn = document.getElementById("serverReviewRefreshBtn");
      const filterEl = document.getElementById("serverReviewStatusFilter");
      const listEl = document.getElementById("serverReviewList");
      const submitBtn = document.getElementById("serverReviewSubmitDecisionBtn");
      const registerBtn = document.getElementById("serverReviewRegisterCurrentBtn");
      if (!refreshBtn || !filterEl || !listEl || !submitBtn) {
        return;
      }
      if (registerBtn) {
        registerBtn.addEventListener("click", () => {
          serverReviewRegisterCurrentResult();
        });
      }
      // M40b — operator-only promote / un-promote buttons. Optional (like
      // registerBtn / the audit buttons): guarded so a partial DOM doesn't
      // break binding, and explicit-click-only (no auto-fetch at bind).
      const promoteBtn = document.getElementById("serverReviewPromoteBtn");
      if (promoteBtn) {
        promoteBtn.addEventListener("click", () => {
          serverReviewPromoteCurrentResult(true);
        });
      }
      const unpromoteBtn = document.getElementById("serverReviewUnpromoteBtn");
      if (unpromoteBtn) {
        unpromoteBtn.addEventListener("click", () => {
          serverReviewPromoteCurrentResult(false);
        });
      }
      refreshBtn.addEventListener("click", () => {
        if (!serverReviewPrivilegedReady()) {
          serverReviewSetStatusBanner("error", SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE);
          return;
        }
        serverReviewLoadList();
      });
      filterEl.addEventListener("change", () => {
        if (!serverReviewPrivilegedReady()) {
          serverReviewSetStatusBanner("error", SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE);
          return;
        }
        serverReviewLoadList();
      });
      listEl.addEventListener("click", (event) => {
        const row = event.target.closest("[data-server-review-task-id]");
        if (!row) return;
        const taskId = row.getAttribute("data-server-review-task-id");
        if (!taskId) return;
        serverReviewLoadDetail(taskId);
      });
      listEl.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        const row = event.target.closest("[data-server-review-task-id]");
        if (!row) return;
        event.preventDefault();
        const taskId = row.getAttribute("data-server-review-task-id");
        if (!taskId) return;
        serverReviewLoadDetail(taskId);
      });
      submitBtn.addEventListener("click", () => {
        serverReviewSubmitDecision();
      });
      // M9.2 — audit packet buttons. Both are explicit-click only. No
      // other code path inside this binder fetches the audit packet.
      const auditLoadBtn = document.getElementById("serverReviewAuditPacketLoadBtn");
      if (auditLoadBtn) {
        auditLoadBtn.addEventListener("click", () => {
          serverReviewLoadAuditPacket();
        });
      }
      const auditCopyBtn = document.getElementById("serverReviewAuditPacketCopyBtn");
      if (auditCopyBtn) {
        auditCopyBtn.addEventListener("click", () => {
          serverReviewCopyAuditPacket();
        });
      }
      // M8.7 — page initialization must NOT auto-call /review/tasks. The
      // operator must explicitly trigger queue refresh / register / decision
      // actions. This keeps the reviewer UI a strictly manual, internal/admin
      // workflow.
      if (serverReviewPrivilegedReady()) {
        serverReviewSetStatusBanner(
          "info",
          "로그인되었습니다. '큐 새로고침'을 누르면 검수 큐를 불러옵니다.",
        );
      } else {
        serverReviewSetStatusBanner(
          "info",
          "관리자 로그인 후 서버 검수 큐를 사용할 수 있습니다.",
        );
      }
    }

    // -----------------------------------------------------------------
    // M9.4 — public/admin surface separation.
    //
    // Operator tools are hidden by default on the public page. The
    // operator opts in via ``?operator_tools=1`` (which sets a
    // sessionStorage flag and is then stripped from the URL so the
    // share-link is clean) or by re-using an existing sessionStorage
    // flag set earlier in the same browser tab. Hiding clears the
    // operator-mode flag and any loaded queue / detail / audit-packet
    // state (it does NOT end the server session — use 로그아웃 for that).
    //
    // This is UI visibility only. Real protection of the review API is the
    // require_admin session gate server-side (AUTH-2d: session-only).
    // -----------------------------------------------------------------
    // ===== C21 — Operator-tools visibility =====
    const OPERATOR_TOOLS_STORAGE_KEY = "policy_ai_operator_tools_visible";
    const OPERATOR_TOOLS_URL_FLAG = "operator_tools";

    function operatorToolsRequestedByUrl() {
      try {
        const search = (window.location && window.location.search) || "";
        if (!search) return false;
        const params = new URLSearchParams(search);
        return params.get(OPERATOR_TOOLS_URL_FLAG) === "1";
      } catch (_) {
        return false;
      }
    }

    function operatorToolsFlagSet() {
      try {
        if (!window.sessionStorage) return false;
        return sessionStorage.getItem(OPERATOR_TOOLS_STORAGE_KEY) === "true";
      } catch (_) {
        return false;
      }
    }

    function setOperatorToolsFlag(value) {
      try {
        if (!window.sessionStorage) return false;
        if (value) {
          sessionStorage.setItem(OPERATOR_TOOLS_STORAGE_KEY, "true");
        } else {
          sessionStorage.removeItem(OPERATOR_TOOLS_STORAGE_KEY);
        }
        return true;
      } catch (_) {
        return false;
      }
    }

    function operatorToolsCleanUrl() {
      // Strip ?operator_tools=1 from the visible URL so a bookmark or
      // shared link doesn't lock everyone into operator mode. The
      // sessionStorage flag keeps the panel visible for the current
      // tab session only.
      try {
        if (!window.history || !window.history.replaceState) return;
        const search = (window.location && window.location.search) || "";
        if (!search) return;
        const params = new URLSearchParams(search);
        if (!params.has(OPERATOR_TOOLS_URL_FLAG)) return;
        params.delete(OPERATOR_TOOLS_URL_FLAG);
        const remaining = params.toString();
        const newSearch = remaining ? `?${remaining}` : "";
        const pathname = (window.location && window.location.pathname) || "/";
        const hash = (window.location && window.location.hash) || "";
        window.history.replaceState({}, "", pathname + newSearch + hash);
      } catch (_) {
        /* ignore — visibility still works without URL cleanup */
      }
    }

    function showOperatorTools() {
      const el = document.getElementById("operatorTools");
      if (el) el.hidden = false;
    }

    function hideOperatorToolsElement() {
      const el = document.getElementById("operatorTools");
      if (el) el.hidden = true;
    }

    function hideOperatorToolsAndResetState() {
      // Clear visibility flag and reset every piece of in-memory review-side
      // UI state (cached queue / detail / register banner). Hiding the panel
      // is local UI only — it does NOT end the server session (use 로그아웃 for
      // that); AUTH-2d removed the token, so there is no token to clear here.
      setOperatorToolsFlag(false);
      try {
        serverReviewResetAfterTokenClear();
      } catch (_) { /* helper may not be defined in some fixtures */ }
      hideOperatorToolsElement();
    }

    function applyOperatorToolsVisibility() {
      const requestedByUrl = operatorToolsRequestedByUrl();
      if (requestedByUrl) {
        setOperatorToolsFlag(true);
        operatorToolsCleanUrl();
      }
      if (operatorToolsFlagSet()) {
        showOperatorTools();
      } else {
        hideOperatorToolsElement();
      }
    }

    function operatorToolsBindEvents() {
      const hideBtn = document.getElementById("operatorToolsHideBtn");
      if (hideBtn) {
        hideBtn.addEventListener("click", () => {
          hideOperatorToolsAndResetState();
        });
      }
    }

    // ===== C22 — Test-export & init/wiring (HUB tail) =====
    // Expose pure helpers for JS regression tests (no network, no I/O).
    if (typeof window !== "undefined") {
      window.__serverReviewHelpers = {
        formatErrorMessage: serverReviewFormatErrorMessage,
        formatStatusLabel: serverReviewFormatStatusLabel,
        buildFromResultPayload: buildReviewTaskFromResultPayload,
        loginRequiredMessage: SERVER_REVIEW_LOGIN_REQUIRED_MESSAGE,
        noCurrentResultMessage: SERVER_REVIEW_NO_CURRENT_RESULT_MESSAGE,
        fromResultPath: SERVER_REVIEW_FROM_RESULT_PATH,
        statusLabels: SERVER_REVIEW_STATUS_LABELS,
        // M9.2 audit packet — exposed for tests/review_ui.test.js.
        auditPacketPathTemplate: SERVER_REVIEW_AUDIT_PACKET_PATH_TEMPLATE,
        auditPacketPath: serverReviewAuditPacketPath,
        auditPacketNoTaskMessage: SERVER_REVIEW_AUDIT_PACKET_NO_TASK_MESSAGE,
        auditPacketNoTokenMessage: SERVER_REVIEW_AUDIT_PACKET_NO_TOKEN_MESSAGE,
        auditPacketNotFoundMessage: SERVER_REVIEW_AUDIT_PACKET_NOT_FOUND_MESSAGE,
        auditPacketCopyOkMessage: SERVER_REVIEW_AUDIT_PACKET_COPY_OK_MESSAGE,
        auditPacketCopyFailMessage: SERVER_REVIEW_AUDIT_PACKET_COPY_FAIL_MESSAGE,
        auditPacketNotLoadedMessage: SERVER_REVIEW_AUDIT_PACKET_NOT_LOADED_MESSAGE,
        buildAuditPacketSummary: serverReviewBuildAuditPacketSummary,
        // M9.4 — operator-mode visibility helpers.
        operatorToolsStorageKey: OPERATOR_TOOLS_STORAGE_KEY,
        operatorToolsUrlFlag: OPERATOR_TOOLS_URL_FLAG,
        operatorToolsRequestedByUrl: operatorToolsRequestedByUrl,
        operatorToolsFlagSet: operatorToolsFlagSet,
        setOperatorToolsFlag: setOperatorToolsFlag,
        showOperatorTools: showOperatorTools,
        hideOperatorToolsElement: hideOperatorToolsElement,
        hideOperatorToolsAndResetState: hideOperatorToolsAndResetState,
        applyOperatorToolsVisibility: applyOperatorToolsVisibility,
        // AUTH-2c — account login helpers (additive; existing members above
        // are unchanged in name, signature, and value).
        authLogin: authLogin,
        authLogout: authLogout,
        authMe: authMe,
        loginPath: AUTH_LOGIN_PATH,
        logoutPath: AUTH_LOGOUT_PATH,
        mePath: AUTH_ME_PATH,
        loginFailedMessage: AUTH_LOGIN_FAILED_MESSAGE,
        loggedInLabel: authLoggedInLabel,
        privilegedReady: serverReviewPrivilegedReady,
      };
    }

    // M39c — open a server-side history result directly by id via a
    // ?result_id= query param. Mirrors the on-load param-parse style used
    // for operator_tools (URLSearchParams off window.location.search). The
    // param is intentionally NOT stripped afterward so the link stays
    // shareable / reloadable. Reuses the existing v2FetchHistoryRow ->
    // v2InflateResults (mapHistoryRowToResult) -> renderResults path, so a
    // promoted row renders its "사람 검토됨" badge (M39b).
    function requestedResultIdFromUrl() {
      try {
        const search = (window.location && window.location.search) || "";
        if (!search) return null;
        const raw = new URLSearchParams(search).get("result_id");
        if (raw === null) return null;
        const id = Number(raw);
        if (!Number.isInteger(id) || id <= 0) return null;
        return id;
      } catch (_) {
        return null;
      }
    }

    async function loadServerResultById(resultId) {
      hideError();
      showStatus("저장된 검증 결과를 불러오는 중...");
      setBusy(true);
      try {
        // v2InflateResults swallows per-id fetch errors and returns an
        // empty results array, so a missing id / network failure lands on
        // the normal homepage state rather than crashing the page.
        const inflated = await v2InflateResults([resultId], {});
        const results = (inflated && inflated.results) || [];
        if (!results.length) {
          hideStatus();
          return;
        }
        // RECENT-VIEWED: record this successful card open (dedupe-to-front, cap 8).
        // Only fires here (after the load succeeds), never on a failed deep-link.
        pushRecentViewed(resultId);
        selectedResultIndex = null;
        activeTopicKey = "";
        setCurrentReportContext(
          "",
          results.length,
          results,
          new Date().toISOString(),
          inflated.ai_status
        );
        renderResults(results);
        // RECENT-VIEWED: repaint the strip with the OTHER opened cards (current excluded).
        renderRecentViewed(resultId);
        // DESIGN-DETAIL-3: switch to the detail SCREEN at its top (no jump-scroll).
        // BEFORE showStatus so the confirmation below survives (showScreen calls
        // hideStatus for non-home screens). Replaces the old resultsEl.scrollIntoView.
        showScreen("detail");
        showStatus("저장된 검증 결과를 불러왔습니다.", true);
      } catch (_) {
        hideStatus();
      } finally {
        setBusy(false);
      }
    }

    renderHistory(safeReadLocalHistory());
    renderReviewQueue(safeReadReviewQueue());
    renderHotTopics();
    // SIDEBAR-RANK-B2: fire-and-forget the read-only weekly-stats fetch (fills
    // the 이번 주 검증 현황 numbers; fail-quiet, never blocks init).
    renderWeeklyStats();
    // HOME-TOP5 S5a: fire-and-forget the read-only trending fetch (fills the
    // 확산 성장 Top 5 panel; fail-quiet, hidden until rows exist).
    renderTrendingTop5();
    // M45: asynchronously fill the hot-topic area from the server (GET
    // /history, cron output included). Fire-and-forget so it never blocks
    // synchronous init and never touches renderHistory()/localStorage. A
    // live session search or ?result_id= view still takes precedence inside
    // currentTopicCards; these server cards only show when neither is active.
    (async () => {
      const serverResults = await getServerRecentAnalyses();
      if (serverResults.length) {
        serverHotTopicResults = serverResults;
        renderHotTopics();
      }
    })();
    const requestedResultId = requestedResultIdFromUrl();
    if (requestedResultId) {
      // HISTORY-BACK baseline guard — behavior depends on WHERE the deep link
      // came from:
      //   * EXTERNAL / DIRECT (shared link, bookmark, new tab — referrer empty
      //     or cross-origin): PUSH a detail entry on top of the load entry, so
      //     the load entry stays below as an on-site "home base" and BACK clears
      //     the detail → shows the feed instead of exiting to the previous site.
      //     (Original behavior, preserved for on-site retention of shared links.)
      //   * SAME-SITE (arrived from within tickedin — e.g. the brain map, or any
      //     internal link): REPLACE the current /?result_id= entry with the same
      //     detail state (no synthetic home-base), so BACK returns straight to the
      //     referrer (the brain map) in ONE press — a full-document nav back, no
      //     popstate involved. Uses the SAME state object shape as
      //     pushDetailHistoryState so the popstate router still recognizes it.
      // (scrollY 0: a deep link loads at the top.)
      const referrer = (document && document.referrer) || "";
      const sameSite = referrer !== "" &&
        referrer.indexOf(location.origin + "/") === 0;
      if (sameSite && window.history && window.history.replaceState) {
        try {
          detailReturnScrollY = 0;
          window.history.replaceState(
            { tickedinDetail: true, scrollY: 0 }, "", window.location.href
          );
          detailHistoryActive = true;
        } catch (_) {
          // history unavailable — fall back to the push path so the detail still opens.
          pushDetailHistoryState(0);
        }
      } else {
        pushDetailHistoryState(0);
      }
      loadServerResultById(requestedResultId);
    }
    // HISTORY-BACK: on BACK out of a pushed detail entry, dismiss the detail and
    // restore the pre-open scroll instead of letting the browser leave the site.
    // Keyed on the tickedinDetail flag; renderResults([]) is the existing clear
    // primitive (resets #results to empty-state + re-renders the feed). Scroll is
    // restored on the next frame (mirrors the detail loaders' post-render scroll
    // timing) so it lands after the DOM settles, not pre-paint. Registered once.
    // DESIGN-DETAIL-2b: unified screen/detail router. Routes a popstate to the
    // correct screen (methodology / home / detail) by the popped state, without
    // breaking the existing card-detail BACK behavior:
    //   - methodology entry (forward/re-enter) → show the 검증 방법 page
    //   - detail entry (forward/re-enter) → keep the detail flag (unchanged)
    //   - popped OUT of methodology (state is now home/null) → return to home
    //   - popped OUT of a detail (state is now home/null) → dismiss + restore scroll
    // DESIGN-DETAIL-3b (FIX C): SYMMETRIC, state-driven router. Routes a popstate to
    // the screen indicated by the POPPED state in BOTH directions (BACK and FORWARD),
    // so re-entering a screen via FORWARD re-shows it (the old detail branch was
    // flag-only — it never called showScreen, so FORWARD into a detail didn't re-show
    // it). Detail/methodology painted content survives in their own DOM regions
    // (#detailScreen / #methodology), so re-showing is just a visibility toggle.
    window.addEventListener("popstate", (event) => {
      const navState = event.state;
      if (navState && navState.tickedinScreen === "methodology") {
        // Entered (BACK) or re-entered (FORWARD) the methodology entry → re-show it.
        methodologyHistoryActive = true;
        detailHistoryActive = false;
        aboutHistoryActive = false;
        gradeStatusHistoryActive = false;
        showScreen("methodology");  // non-home branch scrolls to top
        return;
      }
      // GRADE-STATUS-PAGE: mirror the methodology branch — entering (BACK) or
      // re-entering (FORWARD) the gradeStatus entry re-shows 등급·상태 안내.
      if (navState && navState.tickedinScreen === "gradeStatus") {
        gradeStatusHistoryActive = true;
        methodologyHistoryActive = false;
        detailHistoryActive = false;
        aboutHistoryActive = false;
        showScreen("gradeStatus");  // non-home branch scrolls to top
        return;
      }
      // ABOUT-PAGE: mirror the methodology branch — entering (BACK) or re-entering
      // (FORWARD) the about entry re-shows the About page (pure visibility toggle).
      if (navState && navState.tickedinScreen === "about") {
        aboutHistoryActive = true;
        methodologyHistoryActive = false;
        detailHistoryActive = false;
        gradeStatusHistoryActive = false;
        showScreen("about");  // non-home branch scrolls to top
        return;
      }
      // SEARCH-ANALYZE S-i (bug b): entered (BACK from a card) or re-entered
      // (FORWARD) the search-results entry → RE-RENDER the cached hits (the
      // card overwrote #results in place, so a visibility toggle isn't enough)
      // and re-show the detail screen. With the cache gone (page reload), fall
      // through — the flag check below then leaves the page alone.
      if (navState && navState.tickedinSearch) {
        if (lastSearchHitsCache && Array.isArray(lastSearchHitsCache.hits)
            && lastSearchHitsCache.hits.length) {
          detailHistoryActive = true;
          methodologyHistoryActive = false;
          aboutHistoryActive = false;
          gradeStatusHistoryActive = false;
          renderSearchHitsView(lastSearchHitsCache.query, lastSearchHitsCache.hits);
          showScreen("detail");
          const searchY = (typeof navState.scrollY === "number") ? navState.scrollY : 0;
          requestAnimationFrame(() => { window.scrollTo(0, searchY || 0); });
          return;
        }
      }
      if (navState && navState.tickedinDetail) {
        // Entered (FORWARD, or a sandwiched BACK) the detail entry → re-SHOW the
        // detail screen. Its painted content in #detailScreen survives, so this is a
        // pure visibility toggle. Restore the saved scroll for the detail entry.
        detailHistoryActive = true;
        methodologyHistoryActive = false;
        aboutHistoryActive = false;
        gradeStatusHistoryActive = false;
        showScreen("detail");
        const detailY = (typeof navState.scrollY === "number") ? navState.scrollY : 0;
        requestAnimationFrame(() => { window.scrollTo(0, detailY || 0); });
        return;
      }
      // Popped to a home/neutral (null) entry. Only act if we were tracking a non-home
      // screen; otherwise this popstate isn't ours (e.g. an operator_tools
      // replaceState navigation) and the page is left alone.
      if (!methodologyHistoryActive && !detailHistoryActive && !aboutHistoryActive
          && !gradeStatusHistoryActive) return;
      const wasDetail = detailHistoryActive;
      methodologyHistoryActive = false;
      detailHistoryActive = false;
      aboutHistoryActive = false;
      gradeStatusHistoryActive = false;
      // Restore the FULL home feed WITHOUT destroying #detailScreen content (so a
      // later FORWARD can re-show the detail). The old renderResults([]) wiped it —
      // replaced with showScreen("home") + clearCurrentReportContext() (un-narrow the
      // pool) + renderHotTopics() (repaint from serverHotTopicResults). Clear both
      // under-search banners. Detail-back restores the pre-open scroll; methodology-
      // back lands at the top.
      showScreen("home");
      clearCurrentReportContext();
      renderHotTopics();
      hideStatus();
      v2ResetProgress();
      const targetY = wasDetail ? (detailReturnScrollY || 0) : 0;
      requestAnimationFrame(() => { window.scrollTo(0, targetY || 0); });
    });
    // M9.4 — decide reviewer/admin visibility BEFORE binding the
    // server-review events, so the panel is in its final state when
    // ``serverReviewBindEvents`` runs (it doesn't auto-fetch anyway,
    // per M8.7 — ordering here is purely defensive).
    applyOperatorToolsVisibility();
    operatorToolsBindEvents();
    serverReviewBindEvents();
    // AUTH-2c — bind the account login/logout form and reflect session state
    // (authMe) when the operator panel is exposed. Additive; does not alter
    // the token path or any existing binding.
    authBindEvents();