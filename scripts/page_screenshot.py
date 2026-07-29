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
    args = ap.parse_args()

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
                opened, failures = expand_all(page)
                page.wait_for_timeout(250)
                path = os.path.join(args.out, "%s_%dx%d.png"
                                    % (label, width, height))
                page.screenshot(path=path, full_page=True)
                size = page.evaluate(
                    """() => [document.documentElement.scrollWidth,
                              document.documentElement.scrollHeight]""")
                written.append(path)
                print("CAPTURED %s | viewport %dx%d @100%% zoom, dsf=1 | "
                      "page %dx%d css px | waited on %s | expanded %d "
                      "<details>%s"
                      % (path, width, height, size[0], size[1],
                         waited or "NOTHING", opened,
                         (" | FAILED TO OPEN: %s" % failures) if failures
                         else " | none failed"))

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
