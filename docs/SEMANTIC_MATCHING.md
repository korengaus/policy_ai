# Semantic Evidence Matching (Phase 2 M5)

A lightweight, optional layer that ranks claims against retrieved official
text using embedding similarity. It produces a `semantic_evidence_summary`
that the pipeline attaches to `debug_summary` as metadata only.

## What this layer does

- Chunks official body text (and falls back to evidence snippets) into
  Korean-aware passages.
- Embeds the claim and each chunk through the configured provider.
- Ranks chunks by cosine similarity and emits a per-claim list of
  `top_matches` with score / score_percent / chunk text / source URL.
- Classifies the best score as `strong`, `contextual`, or `weak`
  **semantic-match strength**. (The labels live in their own namespace —
  they are not verification strength.)
- Caches embeddings in SQLite (`embedding_cache` table) so repeat runs
  on the same text are free.

## What this layer does NOT do

- It does **not** decide a verdict. `policy_decision`, `policy_scoring`,
  and `verification_card` ignore the new field entirely.
- It does **not** upgrade weak official evidence. A "strong" semantic match
  with no official body confirmation still surfaces as
  "사람 검토 필요" / "공식 출처 확인 필요" / "의미 매칭 근거 부족" via the
  existing rule-based labels.
- It does **not** fabricate quotes. `top_matches[].text` is always a
  verbatim slice of the retrieved chunk.
- It does **not** require an OpenAI key or any external service unless
  explicitly enabled.
- It does **not** introduce pgvector, Qdrant, Redis, or a vector database.
  Vectors live in a plain SQLite table — see "Future migration path".

## Why semantic matching cannot independently verify a claim

The pipeline's conservative wording rules (in `policy_scoring`,
`verification_card`, and the frontend's status labels) are the source of
truth for "이 주장이 검증되었는가?". Embedding similarity captures
*paraphrase / topical overlap*, not *truth*. A high cosine score between a
claim and an official passage means "they're talking about the same thing,"
not "the official passage confirms the claim's specific numbers, dates, or
target groups." Treating semantic match as verification would silently
weaken every conservative guardrail that took the team months to encode.

The agent therefore returns the score as metadata and bakes a disclaimer
into every summary's `limitations` list:

> semantic match strength is metadata only; rule-based verification and
> official body matching remain authoritative

## Config flags

All flags are read at runtime, so changing the environment immediately
takes effect (no app restart needed for tests). Defaults are safe for CI:

| flag | default | meaning |
| --- | --- | --- |
| `SEMANTIC_MATCHING_ENABLED` | `false` | Master switch. False → no embedding calls, agent returns `available=false`. |
| `EMBEDDING_PROVIDER` | `disabled` | One of `disabled`, `deterministic`, `openai`. |
| `EMBEDDING_MODEL` | empty | OpenAI model name (e.g. `text-embedding-3-small`); ignored by other providers. **Required** when `EMBEDDING_PROVIDER=openai` — empty value fails closed with `available=false` (M5.5). |
| `EMBEDDING_CACHE_ENABLED` | `true` | When false, all calls bypass the SQLite cache. |
| `EMBEDDING_TIMEOUT_SECONDS` | `10` | Per-call timeout passed to the OpenAI client. |
| `EMBEDDING_MAX_TEXT_CHARS` | `4000` | Hard cap on text length sent to the embedding API. |
| `SEMANTIC_MAX_CHUNKS_PER_SOURCE` | `20` | Per-source chunk cap to control embedding bills. |
| `SEMANTIC_MIN_SCORE_FOR_SUPPORT` | `0.72` | Cosine threshold for `strong` semantic-match label. |
| `SEMANTIC_MIN_SCORE_FOR_CONTEXT` | `0.55` | Cosine threshold for `contextual` semantic-match label. |

## Providers

- **`disabled`** — returns `None` for every embedding call; `agent.available`
  is `false`. The default everywhere (local dev, CI, Render).
- **`deterministic`** — hash-based pseudo-embeddings (`DeterministicHashEmbeddingProvider`).
  No network, no secrets, stable across runs. Used by the test suite and
  recommended for local exploration.
- **`openai`** — wraps the existing OpenAI client. Requires `OPENAI_API_KEY`.
  If the key or SDK is missing, the provider initializes with
  `available=false` and the agent falls back to a disabled-style summary
  (rather than crashing).

## Local deterministic testing

```
SEMANTIC_MATCHING_ENABLED=true EMBEDDING_PROVIDER=deterministic python tests/test_semantic_matching.py
```

For manual observation/calibration runs, see `docs/SEMANTIC_ACTIVATION.md`
and use:

```
python scripts/probe_semantic_matching.py --provider deterministic --show-matches
```

The test suite uses the deterministic provider exclusively for the
"enabled" paths, so it runs without network or API keys. CI runs it with
`SEMANTIC_MATCHING_ENABLED=false` to confirm the off-by-default behavior;
the individual tests enable it locally via environment overrides.

## Local real-embedding setup (opt-in)

```
export SEMANTIC_MATCHING_ENABLED=true
export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=text-embedding-3-small
export OPENAI_API_KEY=sk-...
python -m uvicorn api_server:app --reload --port 8000
```

The first run on a given query pays for the embedding; subsequent runs hit
the SQLite cache for free. Inspect cache stats via:

```python
from database import embedding_cache_stats
print(embedding_cache_stats())
```

## Cache behavior

- Stored in the `embedding_cache` table (created idempotently in
  `init_db()`).
- Lookup key: `(text_hash, provider, model)`. Different providers/models
  for the same text are stored separately.
- A malformed `vector_json` is silently ignored and the call falls back to
  recomputing.
- Writes are best-effort. A locked DB does not break the pipeline.
- Only the first 200 chars of the input are stored as `text_preview` for
  debugging.

## Pipeline integration

`main.py:analyze_pipeline` calls
`compute_semantic_evidence_summary(...)` after evidence extraction and
*before* `make_final_decision`. The result is stored in
`debug_summary["semantic_evidence_summary"]` *after*
`build_pipeline_debug_summary` and after the existing
`official_mismatch`/`calibrate_final_decision` branches.

```
build_verification_card
  └─ debug_summary = build_pipeline_debug_summary(...)
  └─ debug_summary.update(official_body_debug)
  └─ debug_summary.update(official_resolution_debug)
  └─ debug_summary["semantic_evidence_summary"] = <M5>   ← attached HERE
  └─ official_mismatch shaping
  └─ calibrate_final_decision(debug_summary=...)         ← does NOT read M5
  └─ verification_card["debug_summary"] = debug_summary  ← persisted
```

No verdict-side module reads the new key. We verified by grepping
`semantic_evidence_summary` across `policy_scoring.py`, `policy_decision.py`,
and `verification_card.py` — no matches.

## Limitations

- The deterministic provider is a test surrogate, not a real embedding
  model. Its scores capture lexical overlap, not semantic similarity, so
  paraphrased-but-different-vocabulary text will under-score it. Treat
  deterministic scores as "structurally plausible," not as production
  signal.
- The chunker uses regex sentence boundaries; very dense Korean legal text
  may yield long sentence-chunks that exceed `max_chars_per_chunk` and get
  windowed.
- Cache hits are scoped to `(text_hash, provider, model)` so changing the
  embedding model invalidates the cache for that text — by design.
- SQLite is the cache store. For high-volume production embedding workloads
  this will eventually become a bottleneck; see "Future migration path".
- This layer never gates verdicts. Even with `SEMANTIC_MATCHING_ENABLED=true`
  and high scores, the conservative labels remain in charge.

## Future path to pgvector / Qdrant

The provider abstraction (`semantic_embeddings.EmbeddingProvider`) and the
cache helpers (`database.get_cached_embedding` / `save_cached_embedding`)
are the only embedding-aware surfaces in the codebase. A future
pgvector / Qdrant migration would:

1. Replace `database.get_cached_embedding` + `database.save_cached_embedding`
   with a Postgres-backed implementation that writes `pgvector` columns or
   posts to Qdrant.
2. Update `semantic_similarity.rank_semantic_matches` to delegate ranking
   to the vector store (server-side `ORDER BY embedding <-> claim_vec`
   instead of in-process cosine).
3. Keep the provider abstraction unchanged — only the storage / retrieval
   layer moves.

This phase intentionally does none of that. The point of M5 is to land the
embedding-aware *interface* and prove out the verdict-isolation guarantee
before the storage decision is made.
