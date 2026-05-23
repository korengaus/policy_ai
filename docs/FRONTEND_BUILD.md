# Frontend build pipeline (M13.2a → M13.2c)

## Why

The pre-M13.2a served HTML (`web/index.html`) was a single 392KB file
mixing markup, CSS, and JavaScript. The Phase 1 audit identified this
as frontend debt. M13.2 introduces a minimal, no-bundler build
pipeline that grows incrementally without disrupting UI/UX.

## Sub-phases

| Phase   | Scope                                                       | Status     |
|---------|-------------------------------------------------------------|------------|
| M13.2a  | CSS extraction + build script; byte-identical output        | this PR    |
| M13.2b  | JavaScript module extraction                                | future     |
| M13.2c  | Reusable component fragments                                | future     |

## Invariants

- The served `web/index.html` MUST be byte-identical to what
  `frontend/build_index.py` produces.
  `scripts/validate.py` enforces this via the `--check` step.
- Building is a manual local step before commit. Render does NOT run
  the build — it serves the committed artifact.
- No bundler, no transpilation, no minification.
- No new runtime dependencies. The build script is stdlib-only.
- All file I/O is bytes-mode so Windows newline translation cannot
  silently corrupt the artifact.

## How to make a frontend change

1. Edit files under `frontend/`.
2. Run `python frontend/build_index.py`.
3. Verify with `python frontend/build_index.py --check`.
4. Commit the changed `frontend/` files AND the rebuilt
   `web/index.html` AND the refreshed `frontend/dist_checksum.txt`.
5. Push.

If you forget step 2, `validate.py` / CI will fail on the `--check`
step and the PR will not merge.

## Rollback

To revert M13.2a:

1. Delete the `frontend/` directory.
2. Remove the `--check` + `compileall` additions in
   `scripts/validate.py`.
3. Remove the `frontend-build` profile in
   `scripts/run_operational_checks.py`.
4. Delete `tests/test_frontend_build.py`.
5. The committed `web/index.html` continues to serve unchanged
   (M13.2a's byte-identical guarantee means the served artifact never
   diverged from the pre-M13.2a state).

## CI

`scripts/validate.py` runs `python frontend/build_index.py --check` on
every CI run. Drift between `frontend/` source and the served
`web/index.html` fails CI and blocks merge.

## What M13.2a does NOT do

- Does not change UI/UX in any user-observable way.
- Does not modify `api_server.py`, `render.yaml`, or
  `tests/regression.test.js`.
- Does not extract JavaScript (M13.2b) or reusable components
  (M13.2c).
- Does not add an npm dependency or a JS bundler.
- Does not run on Render — the build is a local pre-commit step.

## M13.2b — JS Extraction

M13.2a extracted CSS into `frontend/styles/main.css`. M13.2b extracts
the inline `<script>` block into `frontend/scripts/main.js` using the
same template + marker pattern.

### File layout after M13.2b

```
frontend/
├── build_index.py        # builds web/index.html
├── template.html         # has <!-- CSS_INJECT --> and <!-- JS_INJECT --> markers
├── styles/main.css       # ~1,744 lines, M13.2a
├── scripts/main.js       # ~4,745 lines / 314,498 bytes, M13.2b
└── dist_checksum.txt     # SHA256 of built web/index.html

web/
└── index.html            # built artifact (383,176 bytes)
```

### Build process

```
template.html
+ frontend/styles/main.css   (injected at <!-- CSS_INJECT --> as <style>...</style>)
+ frontend/scripts/main.js   (injected at <!-- JS_INJECT --> as raw JS)
→ web/index.html
```

The CSS marker is wrapped at build time with `<style>...</style>` tags.
The JS marker is **not** wrapped — `<script>` and `</script>` live in
the template itself, so the file on disk contains pure JS bytes.

### Byte-identical invariant

After M13.2b, the built `web/index.html` SHA256 is exactly
`59061267f671f7b57b2d31d32e1b1c5e11870253ecccf579e1f4b1af1fa4d386` —
identical to the pre-M13.2b production build. Browser behavior is
unchanged. `tests/regression.test.js` (which loads the JS via `vm` and
exercises functions) continues to pass byte-identical.

### Pins in `tests/test_frontend_build.py`

| Class | What it pins |
|---|---|
| `JsExtractionFileShapeTests` | main.js exists, non-empty, LF-only, no BOM, no embedded `<script>` tags |
| `JsTemplateMarkerPlacementTests` | exactly one `JS_INJECT` marker, sits inside a `<script>...</script>` pair, exactly one `<script>` pair in template |
| `JsBuildInjectionBehaviourTests` | missing JS file raises, missing JS marker raises, duplicate JS markers raises, build is idempotent, content appears in output, no markers leak through |
| `RepoLevelJsSignatureTests` | known function names (`const API_BASE`, `serverReviewBindEvents`) appear in built HTML; committed `dist_checksum.txt` matches a fresh build |

### Future extraction (M13.2c, M13.2d)

M13.2b keeps all JS in a single file. Future phases will split it into
focused modules, each preserving byte-identical built output via the
same template + build approach:

- Rendering functions (`render*`, `build*`)
- Utility helpers (`escape*`, `format*`, `normalize*`)
- Event handlers
- Storage abstractions (localStorage wrappers)
- Reviewer dashboard logic
- Methodology section logic

Each split will need its own marker (`<!-- JS_INJECT_RENDER -->`,
`<!-- JS_INJECT_UTILS -->`, etc.) or a single marker that the build
script replaces with multiple concatenated files in deterministic order.

### What M13.2b does NOT do

- Does not split main.js into multiple files (M13.2c territory).
- Does not introduce a bundler (no webpack, rollup, vite, esbuild).
- Does not add a JS framework (no React, no Vue).
- Does not modify any backend file.
- Does not change any JS content — every byte preserved.
- Does not modify CSS, `api_server.py`, `render.yaml`,
  `tests/regression.test.js`, or `.gitattributes`.

### Verification

```
python frontend/build_index.py              # rewrite web/index.html
python frontend/build_index.py --check      # verify byte-identical
python tests/test_frontend_build.py         # 38 pins
python scripts/validate.py                  # full suite
npm test                                    # regression — must pass byte-identical
```
