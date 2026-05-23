## Summary

Brief description of what this PR does.

## Milestone

Which milestone does this PR implement? (e.g., M12.0c, M13.1)

## Safety checklist

- [ ] No verdict logic changed (or change is explicit and reviewed)
- [ ] No semantic matching logic changed
- [ ] `truth_claim` never set to True
- [ ] `operator_review_required` never set to False
- [ ] No network calls added (or added intentionally and documented)
- [ ] No OpenAI calls added (or added intentionally with cost guard)
- [ ] `tests/regression.test.js` NOT modified (or change is explicit)
- [ ] No `reports/` outputs committed
- [ ] No `.claude/settings.local.json` committed
- [ ] `render.yaml` NOT modified (or change is explicit and tested)
- [ ] Render env vars NOT changed via this PR (operator handles manually)

## Test results

- [ ] `python scripts/validate.py`: PASS
- [ ] `npm test`: PASS
- [ ] `python scripts/run_operational_checks.py --profile quick`: PASS

## Render impact (if applicable)

If this PR modifies `database.py`, `api_server.py`, `main.py`, or `render.yaml`:

- [ ] Render baseline checked after deploy
- [ ] `smoke_async_job`: PASS
- [ ] Semantic canary status: pass / runtime-only warn / NEEDS ROLLBACK

## Notes

Anything reviewers should know.
