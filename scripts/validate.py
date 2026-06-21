"""Run the full Phase 2 validation suite locally.

Mirrors the CI workflow's compile + offline test steps so contributors can
verify changes before pushing. Prints each command, stops on the first
failure, and exits with the failing command's return code.

Works on Windows PowerShell and Linux/macOS shells alike — commands are
launched via ``subprocess`` with ``shell=False`` so quoting is consistent
across platforms.

NOTE (M13.0): This script defines the canonical local validation flow.
``.github/workflows/ci.yml`` runs this script as the primary CI check.
If a check belongs in CI, add it here. Do not duplicate logic in the
workflow YAML — CI must mirror local.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parent.parent


def _npm_executable() -> str:
    # On Windows, ``npm`` is shipped as ``npm.cmd``; resolving via shutil.which
    # avoids the "[WinError 193] %1 is not a valid Win32 application" error
    # that subprocess raises when shell=False finds the bare ``npm`` shim.
    found = shutil.which("npm.cmd") if os.name == "nt" else None
    if found:
        return found
    found = shutil.which("npm")
    if found:
        return found
    return "npm.cmd" if os.name == "nt" else "npm"


def _commands() -> List[List[str]]:
    python = sys.executable or "python"
    npm = _npm_executable()
    _assert_dual_write_disabled_for_determinism()
    _normalize_log_format_for_determinism()
    _normalize_database_url_for_determinism()
    return [
        [python, "-m", "compileall", "api_server.py", "database.py", "job_manager.py",
         "source_crawler.py", "scripts/fetch_registry_source.py",
         "scripts/enable_registry_source.py",
         "artifact_extractor.py", "scripts/extract_artifact_text.py",
         "artifact_evidence_linker.py", "scripts/link_artifact_evidence.py",
         "verdict_producer_comparison.py",
         "scripts/compare_verdict_producers.py",
         "verdict_label_diagnostic.py",
         "scripts/diagnose_verdict_labels.py",
         "legacy_review_enrollment.py",
         "scripts/enroll_legacy_weak_verified.py",
         "korean_constants.py",
         # M12.0a — Postgres dual-write foundation.
         "postgres_storage.py", "scripts/check_postgres_health.py",
         # M12.1 — Postgres parity check.
         # M12.0e-6b-2: postgres_backfill.py + run_postgres_backfill.py
         # retired (migration complete; SQLite unwritten).
         "scripts/check_parity.py",
         # M13.1a — LLM Judge infrastructure (dry-run only).
         "llm_judge.py", "scripts/dry_run_llm_judge.py",
         # M13.2a — frontend build pipeline.
         "frontend/build_index.py",
         # M13.3a — shared HTTP cache infrastructure (disabled by default).
         "http_cache.py", "scripts/check_http_cache.py",
         # M14.0a — structured logging foundation (opt-in via LOG_FORMAT).
         "structured_logging.py", "scripts/check_logging.py",
         # M14.2 — JSON logging production activation tooling.
         "scripts/check_json_logging.py",
         # M14.3a — request ID propagation infrastructure.
         "request_context.py",
         # M13.3c — cache measurement + activation tooling.
         "scripts/measure_cache_impact.py",
         "scripts/check_cache_activation.py"],
        [python, "tests/test_jobs.py"],
        [python, "tests/test_postgres_dual_write.py"],
        [python, "tests/test_ai_reasoner_status.py"],
        [python, "tests/test_semantic_matching.py"],
        [python, "tests/test_semantic_activation.py"],
        [python, "tests/test_semantic_calibration.py"],
        [python, "tests/test_semantic_fact_guardrails.py"],
        [python, "tests/test_semantic_provider_comparison.py"],
        [python, "tests/test_semantic_real_claim_batch.py"],
        [python, "tests/test_historical_claim_batch_builder.py"],
        [python, "tests/test_semantic_canary_metrics.py"],
        [python, "tests/test_smoke_semantic_canary.py"],
        [python, "tests/test_operational_checks_runner.py"],
        [python, "tests/test_review_workflow.py"],
        [python, "tests/test_review_api.py"],
        [python, "tests/test_review_workflow_smoke.py"],
        [python, "tests/test_review_audit_trail.py"],
        [python, "tests/test_operator_preflight.py"],
        [python, "tests/test_review_bundle.py"],
        # AUTH-2d: the token-gate / public-exposure smoke tests were deleted
        # with the X-Review-Token gate (admin auth is session-only). Their
        # invocations are removed here so validate.py / CI enumerate only
        # existing files.
        [python, "tests/test_review_ui_local_demo.py"],
        [python, "tests/test_source_registry.py"],
        # M10.1 — URL classifier CLI smoke + assertions.
        [python, "scripts/classify_source_url.py", "--help"],
        [python, "tests/test_source_url_classifier.py"],
        # M10.2 — static crawler + operator CLI (dry-run only).
        [python, "scripts/fetch_registry_source.py", "--help"],
        [python, "tests/test_source_crawler.py"],
        # M10.3 — operator enable workflow CLI (offline, list smoke + tests).
        [python, "scripts/enable_registry_source.py", "--help"],
        [python, "scripts/enable_registry_source.py", "--list"],
        [python, "tests/test_enable_registry_source.py"],
        # M10.4 — artifact text extractor + operator CLI (offline tests).
        [python, "scripts/extract_artifact_text.py", "--help"],
        [python, "tests/test_artifact_extractor.py"],
        # M10.5 — evidence candidate linker + operator CLI (offline tests).
        [python, "scripts/link_artifact_evidence.py", "--help"],
        [python, "tests/test_artifact_evidence_linker.py"],
        # M11.0a — verdict producer comparison tool (offline tests).
        [python, "scripts/compare_verdict_producers.py", "--help"],
        [python, "tests/test_verdict_producer_comparison.py"],
        # M11.0b — verdict label branch diagnostic (offline tests).
        [python, "scripts/diagnose_verdict_labels.py", "--help"],
        [python, "scripts/diagnose_verdict_labels.py", "--branch-table"],
        [python, "tests/test_verdict_label_diagnostic.py"],
        # M11.0c — B08 conservative fix pin tests (offline, fast).
        [python, "tests/test_verdict_label_b08_fix.py"],
        # M11.1 — legacy weak-verified review-queue enrollment.
        [python, "scripts/enroll_legacy_weak_verified.py", "--help"],
        [python, "tests/test_legacy_review_enrollment.py"],
        # M11.4b — dead-duplicate removal pin. AST-level uniqueness +
        # signature + behavioral pins for verification_card.
        # _missing_context_specific. The dead L491 copy was deleted;
        # this test catches any future re-introduction.
        [python, "tests/test_verification_card_dedup.py"],
        # M11.5 — audit-identified dead-code removal pins. Items 1-3
        # were deleted (evidence_comparator dup branch,
        # extract_evidence_snippets claim_evidence_map double-build,
        # frontend renderResultsLegacy + buildReportTextLegacy). Item 4
        # (OFFICIAL_DOMAIN_QUERY_HINTS) was deferred; the test pins
        # current behavior so a future cleanup can confirm scope.
        [python, "tests/test_dead_code_removal.py"],
        # M11.6 — mojibake sentinel removal in official_crawler. Two
        # FSS error-page detection checks held byte-corrupted Korean
        # strings that could never match real titles. Default-to-delete
        # cleanup; pins assert the byte sequences do not reappear and
        # that the surrounding fetch path still produces its documented
        # public shape on representative Korean input.
        [python, "tests/test_mojibake_cleanup.py"],
        # M11.5c — dead-function removal in official_crawler.
        # `fetch_official_page` had zero callers anywhere in the repo
        # (surfaced as Site 5a in the M11.7 exception-handling audit)
        # and was deleted as a follow-up dead-code pass. Pins assert
        # the definition is gone, no repo file references it, the
        # module still imports cleanly, and the actually-used public
        # surface is intact.
        [python, "tests/test_m11_5c_fetch_official_page_removed.py"],
        # M11.7a — Category 2 logging sweep (HIGH-priority sites only).
        # Adds structured `log.warning` calls to two broad-Exception
        # boundaries previously silent: memory_store.load_policy_memory
        # (corrupt JSON → empty memory) and
        # official_crawler.fetch_best_official_document outer wrapper
        # (broad pipeline-resilience swallow). Return values are
        # byte-identical; pins assert the warning fires on the failure
        # path AND stays silent on legitimate first-run / happy paths.
        [python, "tests/test_m11_7a_category2_logging.py"],
        # M11.7b — Category 4 Playwright exception narrowing in
        # official_browser_crawler.fetch_rendered_page. The broad
        # `except Exception` was replaced with a three-tier chain
        # (PlaywrightTimeoutError / PlaywrightError / Exception)
        # with distinct structured-warning event names per tier.
        # Sentinel return shape byte-identical; KeyboardInterrupt /
        # SystemExit propagate correctly. Tests mock Playwright; no
        # real headless browser is launched.
        [python, "tests/test_m11_7b_playwright_narrowing.py"],
        # M11.7a-2 — Category 2 logging sweep for the 5 remaining
        # audit sites after M11.7a / M11.7b / M11.5c. Adds structured
        # `log.warning` calls at article_extractor.fetch_article_body
        # (Site 2), official_crawler._extract_candidate_links (Site 5b),
        # the per-attempt retry loop (Site 5c), and per-candidate
        # evaluation (Site 5d); structurally upgrades the existing
        # Korean log.error at news_collector.resolve_google_news_url
        # (Site 3a) with extra={} fields while preserving the message
        # verbatim. Return shapes byte-identical; all 9 audit cites
        # now resolved.
        [python, "tests/test_m11_7a_2_exception_logging.py"],
        # M11.7c — exception-narrowing review of the same 5 sites.
        # Reviewed each broad `except Exception` for narrowing
        # feasibility and concluded all 5 should remain broad with
        # documented inline rationale. Decisive Site 3a finding:
        # googlenewsdecoder/decoderv2.py raises bare `Exception("...")`
        # — narrowing would silently leak library errors. Static AST
        # pins assert each handler still catches `Exception` (not
        # narrowed) AND carries an M11.7c marker comment; runtime
        # pins assert each site still catches its primary expected
        # exception class (RequestException family / library bare
        # Exception / BS4 AttributeError). Guards against future
        # "cleanup" PRs that narrow without operator approval.
        [python, "tests/test_m11_7c_exception_narrowing.py"],
        # audit §1.5 #3 re-audit (2026-05-26) — keyword consolidation
        # follow-up to M11.2. Lifts LOW_RISK_KEYWORDS (policy_confidence)
        # and LOW_IMPACT_KEYWORDS (policy_impact) — which M11.2 missed
        # because they were in single-source files — to
        # korean_constants.py as two separately named tuples preserving
        # each consumer's order verbatim. The two are SET-EQUAL but
        # differ in trailing-item order (설명 ↔ 전망), pinned by the
        # set-equivalence + order-preservation tests in the file
        # below. AST pins guard against re-inlining; identity pins
        # guard against accidental rebinding.
        [python, "tests/test_keyword_consolidation.py"],
        # audit §1.5 #2 re-audit (2026-05-26) — generalises M11.4b's
        # single-function uniqueness pin into a codebase-wide AST-walk
        # pin (no intra-file duplicate module-level OR class-level
        # defs) plus a cross-file name-collision allowlist of 27
        # known-good per-module helpers. Adds defense-in-depth pins
        # for CASE A (_missing_context_specific still unique) and
        # CASE B (the two _official_adjusted_* functions retain
        # different signatures + their byte-identical body sections
        # produce equivalent output). Zero production-code change;
        # confirms M11.4 + M11.4b resolutions still hold.
        [python, "tests/test_no_duplicate_definitions.py"],
        # audit §1.5 #5 (2026-05-26) — magic-thresholds documentation
        # pins. Asserts docs/MAGIC_THRESHOLDS.md exists and covers the
        # verdict-pipeline file subset; that targeted module-level
        # threshold constants carry inline calibration-source comments;
        # and that the most verdict-critical values (official_crawler
        # document gates + policy_scoring alert cutoffs) match the
        # catalog. Drift detector for the most behaviour-critical
        # numeric literals in the codebase.
        [python, "tests/test_magic_thresholds_documented.py"],
        # audit §1.5 #6 + #7 re-audit (2026-05-26) — generalises
        # tests/test_mojibake_cleanup.py's official_crawler.py-only
        # scan into a codebase-wide `?<Hangul>` fingerprint detector
        # (with raw-string and regex-syntax false-positive filtering).
        # Also adds three stale-audit-cite confirmation pins:
        # evidence_comparator._make_summary excluded_non_policy_page
        # branch unique, evidence_extraction_agent claim_evidence_map
        # built once, source_retrieval_agent.OFFICIAL_DOMAIN_QUERY_HINTS
        # has use-sites. Zero production-code change.
        [python, "tests/test_dead_code_removal_phase2.py"],
        # M13.1b-obs (2026-05-26) — LLM judge + ai_reasoner
        # operational observability. Pins the cost-estimation formula,
        # aggregator math (accumulate / caller-separation / p95 /
        # ring-buffer cap / reset), ai_reasoner.completed log fields,
        # ai_reasoner.failed log on the broad-except path (M11.7c
        # contract preserved), stub-mode aggregator skip, and the
        # llm_judge aggregator hook inside _emit_cost_log. 11 tests.
        # llm_observability.py and ai_reasoner.py are NOT in
        # MIGRATED_FILES so EXPECTED_TOTAL_LOG_CALLS stays at 270.
        [python, "tests/test_m13_1b_obs.py"],
        # M14.0-print-a (2026-05-26) — pipeline print() → structured
        # logging migration for 26 print calls across 9 pipeline
        # production files (official_source_search.py, memory_store.py,
        # source_reliability_agent.py, worker.py, source_retrieval_agent.py,
        # claim_extractor.py, official_evidence_resolution.py,
        # claim_normalizer.py, pipeline_debug.py). Pins zero remaining
        # print() calls, get_logger import + module-level init present,
        # MIGRATED_FILES contains all 9, and EXPECTED_TOTAL_LOG_CALLS = 298.
        # Resolves audit §1.5 #10 for pipeline scope. timeline.py +
        # scheduler.py deferred to M14.0-print-b.
        [python, "tests/test_m14_0_print_migration.py"],
        # M14.0-print-b (2026-05-26) — operational scripts print() →
        # structured logging migration for 25 print calls across 2
        # files (timeline.py runs inside analyze_pipeline at
        # main.py:1260 on every Render request; scheduler.py is
        # operator-run CLI). All 25 categorized CATEGORY A in Phase 1
        # diagnosis: 24 log.info + 1 log.error (scheduler.run_once's
        # per-query failure path inside `except Exception as error:`).
        # Pins zero remaining print() calls, both files have get_logger
        # import + module-level init, MIGRATED_FILES contains both,
        # EXPECTED_TOTAL_LOG_CALLS = 323, EXPECTED_TOTAL_LOG_ERRORS = 14.
        # Includes a combined-scope membership pin asserting all 11
        # files (M14.0-print-a's 9 + M14.0-print-b's 2) appear in
        # MIGRATED_FILES — the "audit §1.5 #10 closed" invariant.
        [python, "tests/test_m14_0_print_b_migration.py"],
        # M13.1c (2026-05-27) — AnthropicProvider + multi-provider
        # abstraction for the LLM judge. Adds Claude Sonnet 4.6 as
        # primary provider with OpenAI gpt-4o-mini as fallback,
        # gated by LLM_PROVIDER / LLM_FALLBACK_PROVIDER env vars.
        # 10 tests: happy path (2 — response shape + JSON fence
        # stripping), failure (3 — SDK missing / no key / call raise),
        # cost formula for Sonnet 4.6, routing (4 — primary success /
        # primary fail with fallback log + primary_provider_failed
        # flag / LLM_PROVIDER=openai skips Anthropic / LLM_PROVIDER=
        # disabled returns safe-confirm).
        [python, "tests/test_m13_1c_anthropic_provider.py"],
        # M11.0d-1 — verdict producer disagreement diagnostic
        # (DIAGNOSIS ONLY, no production code changed). Pins the
        # current per-producer output snapshot for 42 synthetic-matrix
        # rows + 3 named regression fixtures, and pins the
        # disagreement-count summary. Any future producer change
        # without an explicit M11.0d-3 re-baselining fails this test.
        [python, "tests/test_verdict_producer_disagreement_diagnostic.py"],
        # M11.0d-3a — Strategy C: capture all three producer labels
        # into debug_summary["disagreement_signal"] + a structured
        # log.info("verdict.disagreement_signal", ...) emission.
        # Zero behaviour change — final_decision.policy_alert_level
        # and verification_card.verdict_label are byte-identical.
        # Pins the helper, the P3 mapping table, the agreement
        # semantics, and the main.py wiring points.
        [python, "tests/test_m11_0d_3a_disagreement_signal.py"],
        # M11.0d-3b — NARROW Strategy A: codification of P2 authority
        # + invariant pins for Constraints #11 (operator_review_required
        # ALWAYS True) and #12 (LLM cannot raise verdict). Docstring +
        # comment additions only — no logic change. Prose alignment
        # is shipped in M11.0d-3b-2.
        [python, "tests/test_m11_0d_3b_p2_authority.py"],
        # M11.0d-3b-2 — Strategy A FULL: Korean prose alignment to P2's
        # authoritative policy_alert_level. main.analyze_pipeline now
        # realigns decision_summary + action_recommendation to P2's
        # label after calibrate_final_decision returns. Pins prose
        # behavior + byte-identity invariants + immutable fixture
        # hashes for the 6 M11.0d-1 snapshot files.
        [python, "tests/test_m11_0d_3b_2_prose_alignment.py"],
        # M15.0a — job queue infrastructure (RQ + Redis). Tests run
        # fully offline using fakeredis; --help / default invocations
        # of check_job_queue.py confirm the CLI surface. /analyze
        # remains synchronous; M15.0b wires it to RQ via /v2/*.
        [python, "scripts/check_job_queue.py", "--help"],
        [python, "tests/test_job_queue.py"],
        # M15.0b — RQ-callable wrapper + SSE-backed /v2/* endpoints.
        # Existing /analyze stays byte-identical. Tests use fakeredis
        # + a mocked analyze_pipeline (the real 174s pipeline is
        # never executed) and TestClient.stream for SSE assertions.
        [python, "tests/test_pipeline_worker.py"],
        [python, "tests/test_v2_endpoints.py"],
        # M15.0c — Frontend async integration (SSE + progress UI).
        # Two pins: a Node-based static test that extracts the V2
        # client section from the built web/index.html and asserts
        # the SSE event types + fallback chain + Korean labels are
        # all wired; a Python e2e test that uses RQ SimpleWorker in
        # burst mode to run a mocked pipeline through the full
        # enqueue → execute → /history inflation chain. Both fully
        # offline.
        ["node", str(ROOT / "tests" / "test_frontend_v2_client.test.js")],
        [python, "tests/test_v2_endpoints_e2e.py"],
        # M15.0d — parallel per-news-item processing
        # (concurrent.futures.ThreadPoolExecutor). Pins: order
        # preservation, parallel-thread overlap detection, error
        # isolation, MAX_PARALLEL_NEWS_ITEMS=1 sequential rollback
        # path, progress_callback wiring, M11.0d invariant
        # reachability. All tests fully offline using mocked
        # Phase A / Phase B helpers — real analyze_pipeline never
        # invoked.
        [python, "tests/test_parallel_news_processing.py"],
        # M11.3 — read-only audit of the M11.1 candidate list. Compile
        # + --help smoke confirms the script loads; the test suite
        # pins idempotency, atomic-write, Korean round-trip, and the
        # read-only contract (no review_tasks ever created by the
        # audit script).
        [python, "-m", "compileall", "scripts/audit_legacy_enrollment.py"],
        [python, "scripts/audit_legacy_enrollment.py", "--help"],
        [python, "tests/test_audit_legacy_enrollment.py"],
        # M11.2 — centralized Korean keyword constants (read-move refactor).
        [python, "tests/test_korean_constants.py"],
        # M12.0a — Postgres dual-write foundation.
        [python, "scripts/check_postgres_health.py", "--help"],
        [python, "tests/test_postgres_storage.py"],
        # M12.0e-2 — PG-schema-creation invariant pin (TEST ONLY).
        # Locks that get_engine() alone creates the mirror schema,
        # independently of database.init_db(), before a later sub-stage
        # removes init_db.
        [python, "tests/test_m12_0e_pg_schema_startup_invariant.py"],
        # M12.0e-6b-2: postgres_backfill retired — its CLI + tests
        # (run_postgres_backfill --help/--status, test_postgres_backfill)
        # removed. The migration is complete; SQLite is an unwritten
        # source, so the row-mover has nothing to do.
        # M12.1 — Postgres parity check CLI + tests. Default-env runs
        # report "dual-write disabled" cleanly (no-op pass) — the tests
        # exercise the parity logic, drift detection, exit-code policy,
        # and the read-only contract using patched status payloads. No
        # real Postgres is required.
        [python, "scripts/check_parity.py", "--help"],
        [python, "scripts/check_parity.py"],
        [python, "tests/test_check_parity.py"],
        # M12.2 — atomic policy_memory.json write + reports rotation.
        # The atomic-write tests pin the tmp+rename contract on
        # memory_store.save_policy_memory. The rotation tests exercise
        # scripts/rotate_reports.py against a temp-dir sandbox so the
        # real reports/ directory is never touched. The --dry-run
        # smoke confirms the CLI runs cleanly against the project's
        # real reports/ directory (no files are moved). Neither file
        # is in tests/test_log_level_reclassification.py's
        # MIGRATED_FILES, so EXPECTED_TOTAL_LOG_CALLS is unaffected.
        [python, "tests/test_atomic_memory_store.py"],
        [python, "tests/test_rotate_reports.py"],
        [python, "scripts/rotate_reports.py", "--help"],
        [python, "scripts/rotate_reports.py", "--dry-run", "--quiet"],
        # M13.1a — LLM Judge dry-run CLI + tests.
        [python, "scripts/dry_run_llm_judge.py", "--help"],
        [python, "scripts/dry_run_llm_judge.py", "--status"],
        [python, "tests/test_llm_judge.py"],
        # M13.1b — Real OpenAI provider + pipeline activation. Tests
        # mock the openai SDK; no real API call is ever issued.
        # The --provider stub default keeps the dry-run CLI offline.
        [python, "scripts/dry_run_llm_judge.py", "--provider", "stub", "--status"],
        [python, "tests/test_m13_1b_openai_provider.py"],
        # M13.2a / M13.2b — frontend build pipeline. --check enforces
        # the byte-identical guarantee between frontend/ source (now
        # template.html + styles/main.css + scripts/main.js) and the
        # committed web/index.html artifact. Drift here fails CI.
        [python, "frontend/build_index.py", "--check"],
        [python, "tests/test_frontend_build.py"],
        # M13.3a — shared HTTP cache infrastructure. --help and
        # --status are read-only smokes; the tests pin the cache's
        # safety contract (never raises, never integrated with the
        # pipeline, no real HTTP traffic).
        [python, "scripts/check_http_cache.py", "--help"],
        [python, "scripts/check_http_cache.py", "--status"],
        [python, "tests/test_http_cache.py"],
        # M13.3b — HTTP cache integration into official_crawler
        # (feature-flagged, default off). The byte-identicality
        # regression pin lives in CacheOffByteIdentityTests.
        [python, "tests/test_official_crawler_cache.py"],
        # M13.3d — cache expansion to official_source_body +
        # news_collector. Each has its own feature flag and its own
        # CacheOffByteIdentityTests regression pin.
        [python, "-m", "compileall", "official_source_body.py", "news_collector.py"],
        [python, "tests/test_official_source_body_cache.py"],
        [python, "tests/test_news_collector_cache.py"],
        # M13.3c — cache measurement + activation tooling.
        # --help smokes confirm the scripts load and parse args;
        # the unit tests exercise the parser, verdict thresholds,
        # and simulate paths without hitting Render.
        [python, "scripts/measure_cache_impact.py", "--help"],
        [python, "scripts/check_cache_activation.py", "--help"],
        [python, "tests/test_measure_cache_impact.py"],
        [python, "tests/test_check_cache_activation.py"],
        # M14.0b — print() -> structured logging migration on the top
        # 5 files. AST + token pin that prints are gone in the M14.0b
        # set + in the M14.0c set (formerly 'deferred', now migrated).
        [python, "tests/test_print_migration.py"],
        # M14.0c — completes the migration on the remaining 8 files.
        # Subprocess-invokes verdict test suites to prove invariance.
        [python, "tests/test_print_migration_m14_0c.py"],
        # M14.4 — log level reclassification: AST pins that no log.error
        # call is a field-name reporter, that known false-positives are
        # log.info, and that known real errors are still log.error.
        [python, "tests/test_log_level_reclassification.py"],
        # M14.2 — JSON logging production activation tooling.
        [python, "scripts/check_json_logging.py", "--help"],
        [python, "tests/test_check_json_logging.py"],
        # M14.3a — request ID propagation (contextvars + FastAPI
        # middleware). Backward-compatible: no request_id field in
        # JSON output when the ContextVar is unset.
        [python, "tests/test_request_context.py"],
        [python, "tests/test_api_request_id_middleware.py"],
        # M14.3b — worker context propagation. Compile + the two
        # propagation/end-to-end suites. The compileall ensures
        # job_manager still imports cleanly after the helpers were
        # added; the propagation suite pins concurrent isolation;
        # the end-to-end suite pins the middleware → worker JSON
        # log line path.
        [python, "-m", "compileall", "job_manager.py"],
        [python, "tests/test_job_request_id_propagation.py"],
        [python, "tests/test_end_to_end_request_id_through_job.py"],
        # M14.0a — structured logging foundation. --help and --status
        # are read-only smokes; the test suite pins module-adoption
        # for the 10 M13.x modules, legacy-isolation for 18 untouched
        # files, and JSON shape / Korean-text preservation.
        [python, "scripts/check_logging.py", "--help"],
        [python, "scripts/check_logging.py", "--status"],
        [python, "tests/test_structured_logging.py"],
        [npm, "test"],
    ]


def _assert_dual_write_disabled_for_determinism() -> None:
    """M12.0a — validate.py must run with dual-write disabled so the
    test suite is byte-deterministic regardless of operator env. If
    ``USE_POSTGRES_WRITE`` is set to anything other than an explicit
    "false"/empty value, refuse to start; the operator has likely left
    a local DATABASE_URL pointed at a real Postgres and a deterministic
    validation run is no longer guaranteed.
    """
    raw = os.environ.get("USE_POSTGRES_WRITE", "").strip().lower()
    if raw not in ("", "false", "0", "no", "off"):
        raise SystemExit(
            "[validate] refusing to run with USE_POSTGRES_WRITE="
            f"{raw!r}. Set USE_POSTGRES_WRITE=false (or unset it) to "
            "keep validation runs deterministic; the dual-write tests "
            "exercise the toggle internally."
        )


def _normalize_log_format_for_determinism() -> None:
    """M14.0a — clear ``LOG_FORMAT`` from the environment before
    running subprocesses so JSON-mode output cannot pollute the
    text-parsing output parsers used by smoke checks. The structured
    logging tests exercise the JSON toggle internally via env-scope
    helpers; CI and validate.py runs always behave as text-mode.
    """
    raw = os.environ.get("LOG_FORMAT")
    if raw and raw.strip(" \t").lower() == "json":
        print(
            "[validate] LOG_FORMAT=json detected -- clearing for "
            "deterministic validation run. The structured-logging tests "
            "exercise the JSON toggle via env-scope helpers internally."
        )
    # Unconditionally clear so every child subprocess sees the same
    # baseline.
    os.environ.pop("LOG_FORMAT", None)


def _normalize_database_url_for_determinism() -> None:
    """VALIDATE-SPEEDUP — make local runs fast without weakening any check.

    The DB-dependent tests (e.g. ``tests/test_postgres_storage.py``) reach
    Postgres via ``postgres_storage.get_engine()`` → ``ensure_schema`` with no
    ``connect_timeout``. When ``DATABASE_URL`` points at an unreachable host
    (e.g. Render's *internal* URL on a contributor laptop) those connects block
    for minutes each, so the suite stalls 10-20 min. CI is fast because
    ``ci.yml`` sets ``DATABASE_URL=""`` → ``get_engine`` takes the fast
    "DATABASE_URL not set" path.

    This reproduces CI's offline mode LOCALLY *only when no DB is reachable*:
    it clears ``DATABASE_URL`` for this process (inherited by every child
    subprocess) so the DB tests still RUN — just in their offline mode — rather
    than hanging. No check is removed; exit-code logic is unchanged.

    Behaviour is conditional so DB-reachable runs and CI are untouched:
      * ``VALIDATE_REQUIRE_DB`` truthy → never clear (the run MUST validate
        against the real DB; if unreachable the DB tests fail/stall by intent).
      * ``VALIDATE_SKIP_DB`` truthy → force offline without probing.
      * empty ``DATABASE_URL`` → no-op (already CI behaviour).
      * otherwise → a fast 3s socket probe: reachable → keep; unreachable (or
        any parse/probe error) → clear for this run.

    Separately, ``PGCONNECT_TIMEOUT`` is defaulted to 3s. Several DB tests set
    their OWN deliberately-invalid URL (e.g. ``127.0.0.1:1``) expecting an
    instant connection-refused; on Linux/CI that refuses immediately, but on
    Windows the closed/filtered port makes libpq block on its (unbounded)
    default connect timeout for ~2 min EACH — the real dominant cause of the
    local stall. ``get_engine`` sets no ``connect_timeout``, so bounding it via
    the libpq env var (honoured by psycopg) is the validate.py-only lever that
    caps every connect at 3s. A reachable DB connects in well under 3s, so this
    never weakens a real-DB run; ``setdefault`` respects an operator override.
    """
    # The real fix for the local stall: cap every psycopg connect at 3s so the
    # invalid-URL tests fail fast on Windows too. Applies regardless of the
    # DATABASE_URL branch below (those tests set their own URL).
    os.environ.setdefault("PGCONNECT_TIMEOUT", "3")

    url = os.environ.get("DATABASE_URL", "").strip()

    if os.environ.get("VALIDATE_REQUIRE_DB", "").strip():
        if not url:
            print(
                "[validate] VALIDATE_REQUIRE_DB set but DATABASE_URL is empty "
                "-- DB tests will run offline anyway (no URL to validate against)."
            )
        else:
            print(
                "[validate] VALIDATE_REQUIRE_DB set -- DATABASE_URL left intact; "
                "DB tests run against the real DB."
            )
        return

    if os.environ.get("VALIDATE_SKIP_DB", "").strip():
        os.environ["DATABASE_URL"] = ""
        print("[validate] VALIDATE_SKIP_DB set -- DB tests run offline (same as CI).")
        return

    if not url:
        # Already CI behaviour — nothing to probe or clear.
        return

    # Fast reachability probe. The whole thing is guarded so a malformed URL or
    # any socket error is treated as "unreachable" and never raises out of here.
    reachable = False
    try:
        import socket
        from urllib.parse import urlsplit

        # Strip a SQLAlchemy driver suffix (e.g. ``postgresql+psycopg://``) so
        # urlsplit can parse host/port; the scheme value itself is unused.
        probe_url = url
        scheme_sep = probe_url.find("://")
        if scheme_sep != -1:
            scheme = probe_url[:scheme_sep]
            if "+" in scheme:
                probe_url = scheme.split("+", 1)[0] + probe_url[scheme_sep:]
        parts = urlsplit(probe_url)
        host = parts.hostname
        port = parts.port or 5432
        if host:
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
            reachable = True
    except Exception:  # noqa: BLE001 — any failure means "treat as unreachable".
        reachable = False

    if reachable:
        print("[validate] DATABASE_URL reachable -- DB tests run against the real DB.")
        return

    os.environ["DATABASE_URL"] = ""
    print(
        "[validate] DATABASE_URL unreachable (3s probe) -- cleared for this run; "
        "DB tests run offline (same as CI). Set VALIDATE_REQUIRE_DB=1 to force a "
        "real-DB run."
    )


def _format_command(cmd: List[str]) -> str:
    return " ".join(cmd)


def main() -> int:
    print(f"[validate] working directory: {ROOT}")
    for cmd in _commands():
        print(f"\n[validate] $ {_format_command(cmd)}")
        try:
            completed = subprocess.run(cmd, cwd=ROOT, check=False)
        except FileNotFoundError as error:
            print(f"[validate] FAILED — command not found: {error}", file=sys.stderr)
            return 127
        if completed.returncode != 0:
            print(
                f"[validate] FAILED — command exited with {completed.returncode}: "
                f"{_format_command(cmd)}",
                file=sys.stderr,
            )
            return completed.returncode
    print("\n[validate] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
