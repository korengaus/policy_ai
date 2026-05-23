# frontend/ — Source files for the served HTML

This directory contains the source files for the page served at `/`
(via `api_server.py`'s `FileResponse("web/index.html")` handler). The
build script concatenates them into the final HTML.

## Why this exists (M13.2a)

The original `web/index.html` was a single 392KB file (8991 lines)
mixing HTML, CSS, and JavaScript. The Phase 1 audit identified this as
significant frontend debt. M13.2a is the first step: extract the
single `<style>` block (lines 7–2261 of the pre-extraction file) into
its own file while keeping the served output **byte-identical**.

Future sub-phases (M13.2b, M13.2c) will extract JavaScript modules and
reusable components.

## Layout

```
frontend/
  styles/main.css       — extracted CSS (47,403 bytes)
  scripts/              — (empty in M13.2a; populated in M13.2b)
  fragments/            — (empty in M13.2a; populated in M13.2c)
  template.html         — HTML with <!-- CSS_INJECT --> marker
  build_index.py        — Python build script (stdlib only)
  dist_checksum.txt     — SHA256 of last built output (committed)
  README.md             — this file
```

## Build

```
python frontend/build_index.py           # rewrite web/index.html
python frontend/build_index.py --check   # verify (no writes; used by validate.py)
python frontend/build_index.py --status  # show paths and checksums (no writes)
```

## When to rebuild

After editing any file under `frontend/`. `scripts/validate.py` runs
`--check` and will fail if drift is detected, so you cannot accidentally
ship a modified template without rebuilding.

## Byte-identical guarantee (M13.2a)

The build output is byte-identical to the pre-M13.2a `web/index.html`.
The repo-level integration test
(`tests/test_frontend_build.py::RepoLevelIntegrationTest`) and the
`--check` mode both enforce this.

If you discover a byte mismatch:

1. DO NOT commit.
2. Run `python frontend/build_index.py --status` to see hashes and sizes.
3. Run `python frontend/build_index.py --check` for a diff at the
   first differing byte.
4. Adjust `template.html` or `styles/main.css` to restore parity, then
   rebuild.

### Why bytes-mode I/O

`build_index.py` uses `read_bytes` / `write_bytes` exclusively. On
Windows, `open(..., encoding="utf-8")` defaults to universal newline
translation (`\n` → `\r\n` on write), which would silently violate the
byte-identical guarantee. Operating on bytes sidesteps that entirely.
The contract is pinned by
`tests/test_frontend_build.py::ModuleLevelStaticChecks.test_uses_bytes_io`.

## What this is NOT

- Not a JavaScript bundler. Pure concatenation.
- Not minification. No transforms.
- Not a development server. Use `api_server.py` for serving.
- Not Render-side build. Render serves the committed
  `web/index.html` directly; this build runs locally before commit.
- Not a Node tool. No npm dependency added by M13.2a.

## Multiple `<style>` blocks

Only one `<style>` block existed in the pre-M13.2a HTML (lines 7–2261).
The entire block was extracted into `styles/main.css`. No remaining
unextracted blocks.

If a future change adds a second `<style>` block, `build_index.py`'s
multi-marker check will refuse to build until the operator routes it
through the template explicitly.
