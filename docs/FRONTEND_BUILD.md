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
