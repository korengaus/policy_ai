# PAGE-SCREENSHOT-TOOL — READ-ONLY visual capture (pin-OUT, no DB access).
#
# Our text instruments (card_render_audit.js, the reviewer probes) read what a
# reader READS. Nothing sees what a page LOOKS like: layout collapse,
# overlapping badges, clipped text, broken spacing. Checking that by eye from
# a zoomed-out screenshot is unreliable in BOTH directions — a real defect can
# be missed, and a healthy element can look broken at 67% zoom. This captures
# at a FIXED 100% zoom (deviceScaleFactor is the only magnification, and it is
# stated per file) so "is this element broken" can be settled by looking.
#
# WHAT IT DOES
#   * loads a URL in headless Chromium (Playwright — ALREADY a project
#     dependency, see official_browser_crawler.py; nothing new is installed)
#   * waits for the card content to EXIST rather than sleeping a fixed time
#     (see WAIT_SELECTORS), then for fonts + a paint settle
#   * expands every <details> on the page (card sections nest, so it loops
#     until no newly-opened disclosure appears) and REPORTS the count; any
#     disclosure that refuses to open is named in the output — a screenshot
#     that quietly omits content is worse than no screenshot
#   * captures a full-page PNG per viewport
#   * optionally captures a NAMED REGION at higher deviceScaleFactor, located
#     by CSS selector or by visible text, with padding so the element is seen
#     in its surroundings — the capture that settles "broken or just small"
#
# DISCLOSURE STATES (the second question this tool answers). Expand-all is
# right for INSPECTION — the text reviewer reads the fully expanded render and
# that is how defects get found — and it stays the DEFAULT. But it is the wrong
# measurement for "what does a reader see on open, before clicking anything",
# which is what 100% of visitors (including a cold-email click) actually get.
#   --collapsed        capture the default state, expanding NOTHING
#   --expand-path A>B  open only that ordered path of disclosures, by the
#                      visible summary text of each — the N-deliberate-clicks
#                      view. A step that matches nothing is named and the run
#                      stops rather than capturing a misleading state.
#   --section TEXT     measure/crop ONE section: from the heading carrying TEXT
#                      down to the next sibling section (not the heading box —
#                      that is what made an earlier capture 643x64 and useless
#                      for judging list length)
#   --find TEXT        for every occurrence of TEXT: is it visible in the
#                      DEFAULT collapsed state, and if not, how many
#                      disclosures must be opened to reach it (click depth)
#
# USAGE (no credentials needed; the card page is public):
#   python scripts/page_screenshot.py --url https://tickedin.org/?result_id=13592
#   python scripts/page_screenshot.py --url ... --region-text "제외/불일치" --scale 3
#   python scripts/page_screenshot.py --url ... --region-selector ".vrf-cand-count"
#   options: --viewports 1440x900,390x844  --out web/_screenshots  --label 13592
#
# SAFETY: loads pages only. No DB access, no writes outside --out, no stored
# field touched, no credentials. pin-OUT scripts/* — zero log.* call sites.

import argparse
import os
import re
import sys
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# Mirrors official_browser_crawler.py's context options (same UA/locale posture).
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# Content-presence waits, in order of preference: the card detail container,
# then any rendered result section. Waiting on a SELECTOR (not a sleep) is what
# makes the capture deterministic on a slow cold start.
WAIT_SELECTORS = ("#results .verification-card", "#results .result-card",
                  "#results section", "#results")
DEFAULT_VIEWPORTS = "1440x900,390x844"


def parse_viewports(spec):
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", chunk)
        if not match:
            print("BAD VIEWPORT %r — expected WIDTHxHEIGHT (e.g. 1440x900)"
                  % chunk)
            raise SystemExit(2)
        out.append((int(match.group(1)), int(match.group(2))))
    return out


def label_from_url(url, explicit):
    if explicit:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", explicit)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("result_id", "id", "week"):
        if query.get(key):
            return re.sub(r"[^A-Za-z0-9_.-]+", "_", query[key][0])
    return "page"


def wait_for_content(page, timeout_ms):
    """Wait on CONTENT PRESENCE, never a fixed sleep. Returns the selector
    that satisfied the wait (reported in the output) or None."""
    for selector in WAIT_SELECTORS:
        try:
            page.wait_for_selector(selector, state="attached",
                                   timeout=timeout_ms)
            return selector
        except Exception:
            continue
    return None


def expand_all(page):
    """Open every <details> on the page. Card sections NEST (a candidate row
    lives inside 출처와 공식 근거, and the overflow list inside that), so this
    loops until a pass opens nothing new. Returns (opened, failures)."""
    opened, failures, rounds = 0, [], 0
    while rounds < 8:
        rounds += 1
        result = page.evaluate(
            """() => {
              const shut = Array.from(document.querySelectorAll('details:not([open])'));
              let n = 0; const bad = [];
              for (const d of shut) {
                d.open = true;
                if (d.open) n += 1;
                else {
                  const s = d.querySelector('summary');
                  bad.push(((s && s.textContent) || d.className || 'details').trim().slice(0, 60));
                }
              }
              return { n, bad };
            }"""
        )
        failures.extend(result["bad"])
        opened += result["n"]
        if not result["n"]:
            break
        page.wait_for_timeout(150)  # let the newly-revealed subtree lay out
    # Anything still closed after the loop is a genuine failure to open.
    still_shut = page.evaluate(
        """() => Array.from(document.querySelectorAll('details:not([open])'))
              .map((d) => { const s = d.querySelector('summary');
                return ((s && s.textContent) || d.className || 'details').trim().slice(0, 60); })"""
    )
    for name in still_shut:
        if name not in failures:
            failures.append(name)
    return opened, failures


def expand_path(page, steps):
    """Open ONLY the named disclosures, in order, matching each step against
    the visible <summary> text (normalized, substring). Each step is searched
    among disclosures reachable AFTER the previous step opened, so a nested
    path behaves like real clicks. Returns (opened_labels, missing_step)."""
    opened = []
    for step in steps:
        found = page.evaluate(
            """(needle) => {
              const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
              const want = norm(needle);
              for (const d of Array.from(document.querySelectorAll('details'))) {
                const s = d.querySelector('summary');
                if (!s) continue;
                if (!norm(s.textContent).includes(want)) continue;
                // only reachable ones: every ancestor <details> already open
                let p = d.parentElement, reachable = true;
                while (p) {
                  if (p.tagName === 'DETAILS' && !p.open) { reachable = false; break; }
                  p = p.parentElement;
                }
                if (!reachable) continue;
                d.open = true;
                return norm(s.textContent).slice(0, 60);
              }
              return null;
            }""", step)
        if not found:
            return opened, step
        opened.append(found)
        page.wait_for_timeout(150)
    return opened, None


def measure_section(page, heading_text):
    """Height of ONE section: from the element carrying heading_text down to
    the next sibling section. Returns a dict with the box, or None."""
    return page.evaluate(
        """(needle) => {
          const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
          const want = norm(needle);
          const heads = Array.from(document.querySelectorAll(
            'h1,h2,h3,h4,summary,.collapsible-section>summary,legend'))
            .filter((el) => norm(el.textContent).includes(want));
          if (!heads.length) return null;
          const head = heads.find((el) => el.getBoundingClientRect().width > 0)
            || heads[0];
          // the block to measure = the heading's own section container
          let block = head.closest('details,section,.collapsible-section') || head;
          const r = block.getBoundingClientRect();
          const top = r.top + window.scrollY;
          const next = block.nextElementSibling;
          const nr = next ? next.getBoundingClientRect() : null;
          const bottom = nr && nr.height > 0 ? nr.top + window.scrollY : top + r.height;
          return {
            x: r.left + window.scrollX, y: top,
            width: r.width, height: Math.max(r.height, bottom - top),
            headingHeight: head.getBoundingClientRect().height,
            open: block.tagName === 'DETAILS' ? !!block.open : null,
            tag: block.tagName.toLowerCase(),
          };
        }""", heading_text)


def find_occurrences(page, needle):
    """For each occurrence of `needle`: is it visible right now, and how many
    CLOSED ancestor <details> stand between it and the reader (click depth)."""
    return page.evaluate(
        """(needle) => {
          const out = [];
          const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const seen = new Set();
          while (walk.nextNode()) {
            const node = walk.currentNode;
            if (!node.nodeValue || !node.nodeValue.includes(needle)) continue;
            const el = node.parentElement;
            if (!el || seen.has(el)) continue;
            seen.add(el);
            let depth = 0, p = el, labels = [];
            while (p) {
              if (p.tagName === 'DETAILS' && !p.open) {
                depth += 1;
                const s = p.querySelector('summary');
                labels.unshift(String((s && s.textContent) || '')
                  .replace(/\\s+/g, ' ').trim().slice(0, 40));
              }
              p = p.parentElement;
            }
            const r = el.getBoundingClientRect();
            const styled = window.getComputedStyle(el);
            const painted = r.width > 0 && r.height > 0
              && styled.visibility !== 'hidden' && styled.display !== 'none';
            out.push({
              depth, painted, path: labels,
              context: String(el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
            });
          }
          return out;
        }""", needle)


def capture_region(page, out_path, selector=None, text=None, pad=24):
    """Close-up of ONE element at the context's deviceScaleFactor. Located by
    CSS selector, else by visible text (first VISIBLE match, so a hidden
    template copy is never targeted). Returns (how_located, box) or None."""
    if selector:
        handle = page.query_selector(selector)
        how = "CSS selector %r" % selector
    else:
        handle = page.evaluate_handle(
            """(needle) => {
              const walk = document.evaluate(
                `//*[not(self::script or self::style)][contains(normalize-space(text()), ${JSON.stringify(needle)})]`,
                document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
              for (let i = 0; i < walk.snapshotLength; i++) {
                const el = walk.snapshotItem(i);
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) return el;
              }
              return null;
            }""", text)
        handle = handle.as_element() if handle else None
        how = "visible text %r (first non-empty box)" % text
    if not handle:
        return None
    box = handle.bounding_box()
    if not box:
        return None
    handle.scroll_into_view_if_needed()
    page.wait_for_timeout(120)
    box = handle.bounding_box() or box
    clip = {
        "x": max(0.0, box["x"] - pad), "y": max(0.0, box["y"] - pad),
        "width": box["width"] + pad * 2, "height": box["height"] + pad * 2,
    }
    page.screenshot(path=out_path, clip=clip)
    return how, clip


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only page screenshot tool.")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default=os.path.join("web", "_screenshots"))
    ap.add_argument("--viewports", default=DEFAULT_VIEWPORTS)
    ap.add_argument("--label", default="")
    ap.add_argument("--region-selector", default="")
    ap.add_argument("--region-text", default="")
    ap.add_argument("--scale", type=float, default=3.0,
                    help="deviceScaleFactor for the region close-up (zoom "
                         "stays 100%%; this raises pixel density only)")
    ap.add_argument("--timeout", type=int, default=45000)
    ap.add_argument("--collapsed", action="store_true",
                    help="capture the DEFAULT state, expanding nothing — what "
                         "a reader sees before clicking anything")
    ap.add_argument("--expand-path", default="",
                    help="open ONLY this ordered path of disclosures, by "
                         "visible summary text, e.g. 'A > B > C'")
    ap.add_argument("--section", default="",
                    help="measure and crop ONE section by its heading text "
                         "(heading -> next sibling section)")
    ap.add_argument("--find", default="",
                    help="report every occurrence of this string: visible in "
                         "the current state, else the click depth to reach it")
    args = ap.parse_args()
    steps = [s.strip() for s in args.expand_path.split(">") if s.strip()]
    if args.collapsed and steps:
        print("--collapsed and --expand-path are mutually exclusive: the "
              "first means expand NOTHING, the second means expand exactly "
              "those. Pick one.")
        return 2
    state = ("collapsed (default reader view)" if args.collapsed
             else ("path: " + " > ".join(steps)) if steps else "expand-all")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not importable — it is an existing project "
              "dependency (requirements.txt); run `python -m playwright "
              "install chromium` once if the browser binary is missing.")
        return 2

    viewports = parse_viewports(args.viewports)
    label = label_from_url(args.url, args.label)
    os.makedirs(args.out, exist_ok=True)
    written = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height in viewports:
                # FIXED 100% zoom: no page zoom is applied anywhere; the only
                # magnification is deviceScaleFactor on the region capture.
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                    user_agent=USER_AGENT, locale="ko-KR",
                    extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"},
                )
                page = context.new_page()
                page.goto(args.url, wait_until="domcontentloaded",
                          timeout=args.timeout)
                waited = wait_for_content(page, args.timeout)
                if not waited:
                    print("WAIT FAILED at %dx%d: none of %s appeared within "
                          "%dms — capturing anyway, treat as incomplete"
                          % (width, height, list(WAIT_SELECTORS), args.timeout))
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                try:
                    page.evaluate("() => document.fonts && document.fonts.ready")
                except Exception:
                    pass
                # --find is measured BEFORE any expansion in collapsed mode,
                # so "visible without clicking" means exactly that.
                if args.find:
                    for occ in find_occurrences(page, args.find) or []:
                        if occ["painted"] and occ["depth"] == 0:
                            verdict = "VISIBLE in this state (0 clicks)"
                        else:
                            verdict = ("hidden — %d disclosure(s) to open: %s"
                                       % (occ["depth"], " > ".join(occ["path"])
                                          or "(unnamed)"))
                        print("  FIND %r @%dpx: %s | ctx: %s"
                              % (args.find, width, verdict, occ["context"]))
                    if not find_occurrences(page, args.find):
                        print("  FIND %r @%dpx: NOT PRESENT on the page at all"
                              % (args.find, width))

                if args.collapsed:
                    opened, failures, note = 0, [], "expanded nothing"
                elif steps:
                    got, missing = expand_path(page, steps)
                    if missing:
                        print("EXPAND-PATH STEP NOT FOUND: %r (opened %s "
                              "first) — nothing captured for this viewport, "
                              "not a silent partial state"
                              % (missing, got or "nothing"))
                        context.close()
                        continue
                    opened, failures = len(got), []
                    note = "opened path %s" % " > ".join(got)
                else:
                    opened, failures = expand_all(page)
                    note = "expanded %d <details>" % opened
                page.wait_for_timeout(250)
                suffix = ("collapsed" if args.collapsed
                          else "path" if steps else "expanded")
                path = os.path.join(args.out, "%s_%dx%d_%s.png"
                                    % (label, width, height, suffix))
                page.screenshot(path=path, full_page=True)
                size = page.evaluate(
                    """() => [document.documentElement.scrollWidth,
                              document.documentElement.scrollHeight]""")
                written.append(path)
                print("CAPTURED %s | viewport %dx%d @100%% zoom, dsf=1 | "
                      "page %dx%d css px | state=%s | waited on %s | %s%s"
                      % (path, width, height, size[0], size[1], state,
                         waited or "NOTHING", note,
                         (" | FAILED TO OPEN: %s" % failures) if failures
                         else ""))

                if args.section:
                    box = measure_section(page, args.section)
                    if not box:
                        print("  SECTION %r NOT FOUND at %dpx — nothing "
                              "measured or cropped" % (args.section, width))
                    else:
                        print("  SECTION %r @%dpx: %dx%d css px (heading alone "
                              "%dpx, <%s> open=%s)"
                              % (args.section, width, round(box["width"]),
                                 round(box["height"]), round(box["headingHeight"]),
                                 box["tag"], box["open"]))
                        spath = os.path.join(
                            args.out, "%s_%dx%d_%s_section.png"
                            % (label, width, height, suffix))
                        if box["width"] < 1 or box["height"] < 1:
                            print("  SECTION PNG SKIPPED: box is %gx%g — the "
                                  "section is collapsed to nothing in this "
                                  "state (not a silent skip)"
                                  % (box["width"], box["height"]))
                        else:
                            # full_page=True so the clip may sit below the fold
                            page.screenshot(path=spath, full_page=True, clip={
                                "x": box["x"], "y": box["y"],
                                "width": box["width"],
                                "height": min(box["height"], 30000)})
                            written.append(spath)
                            print("  SECTION PNG %s" % spath)

                if (args.region_selector or args.region_text) and width == viewports[0][0]:
                    rcontext = browser.new_context(
                        viewport={"width": width, "height": height},
                        device_scale_factor=args.scale,
                        user_agent=USER_AGENT, locale="ko-KR",
                        extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"},
                    )
                    rpage = rcontext.new_page()
                    rpage.goto(args.url, wait_until="domcontentloaded",
                               timeout=args.timeout)
                    wait_for_content(rpage, args.timeout)
                    try:
                        rpage.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    expand_all(rpage)
                    rpage.wait_for_timeout(250)
                    rpath = os.path.join(args.out, "%s_region.png" % label)
                    got = capture_region(rpage, rpath,
                                         selector=args.region_selector or None,
                                         text=args.region_text or None)
                    if got:
                        how, clip = got
                        print("REGION %s | located by %s | clip %dx%d css px "
                              "@ dsf=%g -> %dx%d device px"
                              % (rpath, how, round(clip["width"]),
                                 round(clip["height"]), args.scale,
                                 round(clip["width"] * args.scale),
                                 round(clip["height"] * args.scale)))
                        written.append(rpath)
                    else:
                        print("REGION NOT FOUND: selector=%r text=%r — nothing "
                              "captured (not a silent skip)"
                              % (args.region_selector, args.region_text))
                    rcontext.close()
                context.close()
        finally:
            browser.close()

    print("WROTE %d file(s):" % len(written))
    for path in written:
        print("  %s" % os.path.abspath(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
