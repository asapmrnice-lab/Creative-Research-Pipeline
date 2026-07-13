# Implementation Plan — Personal Creative Research Pipeline

**Status:** Proposal for review (derived from `research-pipeline-architecture.md`, Stages 1–10 locked)
**Stack decisions (new):** Supabase (Postgres) as storage · Anthropic Claude only, **Haiku by default** · data dedup + noise cleaning as first-class concerns
**Reference cloned:** `skills/skill-creator/` (Anthropic's canonical Agent-Skill authoring guide)

---

## 1. Verification of the locked architecture

The design doc is internally consistent and implementation-ready. The load-bearing decisions:

- **One Research Item = one source post.** Links folded in, no cross-post merging. ✅ Clean aggregate boundary.
- **Structured Field vs Note as separate tables** to *structurally* enforce fact/opinion split. ✅ This is a real invariant, not a convention — keep it.
- **Storage isolation** — only the Storage Layer writes. ✅ This is what makes the SQLite→Supabase swap you're now asking for a *contained* change rather than a rewrite. The doc anticipated exactly this ("local-only could become synced later").
- **Adapters are the only per-source variation point.** ✅ Open/Closed by construction.

**One contradiction to resolve up front** (see §3): the doc's Core Principle says the system *"never analyzes, judges, or draws conclusions"* and puts *"any AI-generated judgment"* out of scope — yet you now want Claude/Haiku in the pipeline. These are reconcilable, but only if we constrain precisely what the model is allowed to do.

---

## 2. What the PM wants to implement (plain reading)

A **mechanical collection-and-retrieval system**, not an analyst. It watches tracked Telegram channels, pulls every new post, downloads its media, stores the raw content, extracts *only explicitly-stated* facts into structured fields, and makes everything searchable forever. The human does 100% of the thinking; the machine does 100% of the fetching, filing, and finding. The new asks (Supabase, Claude/Haiku, dedup, noise-cleaning) are about making that mechanical layer *robust and cheap at scale* — not about adding intelligence.

---

## 3. The critical reconciliation — where Claude is allowed to operate

The Core Principle is a hard constraint, so Claude is scoped to **mechanical, verifiable, reversible** work only. Every LLM call is a *transform on text the human could have done by hand*, never a judgment:

| Allowed (mechanical) | Forbidden (judgment) |
|---|---|
| **Noise cleaning**: strip boilerplate, ads-for-the-channel, emoji spam, "subscribe" footers, zero-width chars, broken markdown — producing a cleaned copy **alongside** the untouched raw text | Deciding a post is "low quality" and dropping it |
| **Whitelist extraction**: pull GEO, brand, offer, KPIs, dates, amounts **only when explicitly present** (Stage 3 rule), emitting `null` when absent | Inferring a missing field or "estimating" a value |
| **Semantic dedup signal**: produce an embedding / near-duplicate score to *flag* "already seen" candidates for the dedup rule | Auto-merging or auto-deleting items |
| **Language/format detection**: label language, detect that a post contains a link vs a table | Summarizing so the human doesn't have to read it |

Two guardrails make this safe and auditable:
1. **Raw is immutable.** Cleaning and extraction write to *new* rows/columns; the original `raw_text` and downloaded media are never mutated. The human can always see what the model saw.
2. **Provenance is recorded.** Every Structured Field already carries `origin` (system vs human) per Stage 2 — we extend it with `model`, `prompt_version`, and `confidence`, so any machine output is traceable and disprovable.

If any of the "Allowed" column still feels like too much AI for the PM's taste, the fallback is regex/heuristic noise-cleaning + exact-hash dedup with **no** model at all — the architecture below keeps the LLM behind an interface so it can be disabled per-stage.

---

## 4. SOLID mapping (the "solid principles" ask)

The doc's modules already lean this way; here's the explicit mapping and where I'd tighten:

- **S — Single Responsibility.** Each module owns one job: `SourceRegistry`, `IngestionAdapter`, `NoiseCleaner`, `Deduplicator`, `ExtractionEngine`, `StorageLayer`, `SearchModule`, `CLI`. Cleaning and dedup are *split out* of the Extraction Engine (the doc lumped them loosely) so each is independently testable and disableable.
- **O — Open/Closed.** New source = new `IngestionAdapter` implementation. New fact type = new entry in the extraction **whitelist config**, not code. New cleaning rule = new `CleaningStep`. No edits to the core.
- **L — Liskov.** Every adapter satisfies the same `Adapter` protocol (`fetch_new() -> Iterable[RawPost]`); the pipeline treats Telegram exactly like a future YouTube adapter.
- **I — Interface Segregation.** Small protocols: `Adapter`, `Cleaner`, `Deduplicator`, `Extractor`, `Store`, `LLMClient`. The Search module depends only on a read-only `Store` view; it can't write.
- **D — Dependency Inversion.** The pipeline depends on **abstractions** (`LLMClient`, `Store`), with concrete `AnthropicClient` (Haiku) and `SupabaseStore` injected at the composition root. Swapping Haiku↔Sonnet, or Supabase↔SQLite, touches one wiring file.

---

## 5. Anthropic agent best-practices applied

Per the Claude API guidance (and deliberately staying at the *simplest tier that works*):

- **This is a workflow, not an agent.** Ingestion is a fixed, code-controlled pipeline — no open-ended tool-use loop needed. Each Claude call is a **single, structured, stateless request**. Don't reach for Managed Agents or a tool-runner here; they'd add cost and non-determinism for zero benefit.
- **Model: `claude-haiku-4-5` by default.** Cheapest capable model, right for classification/extraction/cleaning. Keep an injectable escape hatch to `claude-sonnet-5` for posts a low-confidence extraction flags for reprocessing.
- **Structured Outputs, not prose parsing.** Every extraction/cleaning call uses `output_config.format` with a JSON schema (the whitelist *is* the schema, `additionalProperties: false`, every field nullable). Guarantees parseable, whitelist-bounded output — the model literally cannot invent a field.
- **Prompt caching.** The whitelist + cleaning instructions are a large stable prefix; cache them (`cache_control`) so passive-collection runs pay ~0.1× on the instructions. Keep the per-post text after the cache breakpoint.
- **Batch API for passive collection.** Workflow 1 is unattended and latency-insensitive → route it through the Message Batches API for **50% cost reduction**. Interactive reprocessing stays on the sync API.
- **`thinking` off** for these mechanical tasks (Haiku doesn't need it); keep responses tight with low `max_tokens`.
- **Idempotency + no silent truncation.** Never trim a post to fit; if a post is huge, chunk it. Each Claude call keyed by `(research_item_id, prompt_version)` so re-runs are safe.

---

## 6. Supabase schema (maps 1:1 to Stage 2 domain model)

Postgres tables (RLS on, single-user for now; the structure already supports multi-device later, which is why you're moving off SQLite):

```
source(id, type, handle, platform_id, first_tracked_at)
research_item(id, source_id → source, original_url, ingested_at,
              raw_text, cleaned_text, content_hash, simhash, extras jsonb)
media_asset(id, research_item_id → research_item, kind, storage_path,
            original_url, duration, size_bytes)          -- files in Supabase Storage bucket
structured_field(id, research_item_id, name, value,
                 origin ENUM('system','human'), model, prompt_version, confidence)
note(id, research_item_id, body, author='human', created_at)  -- always human
```

- **Media** → Supabase Storage bucket, DB holds the path (mirrors the doc's "local file path", now object storage).
- **Full-text search** → Postgres `tsvector` GIN index on `raw_text`/`cleaned_text` (covers MVP; the doc's Stage-9 semantic search becomes a `pgvector` column later — additive, no rebuild).
- `content_hash` + `simhash` columns exist from day one to serve dedup (§7).

---

## 7. Data dedup design ("already seen" tracking)

Three tiers, cheapest first, **flag-don't-delete** (respects "never deletes, stays searchable forever"):

1. **Exact dedup (free):** `content_hash = sha256(normalized_raw_text + source_id)`. Unique-ish check on ingest; identical repost → link to existing item, skip re-download. No model.
2. **Near-duplicate (cheap):** **SimHash/MinHash** over cleaned text → Hamming-distance threshold flags forwards/reposts with minor edits. Pure Python, no model.
3. **Semantic (optional, Haiku/embeddings):** only for items that pass 1–2 but are suspected reposts across channels → embedding cosine similarity, stored in `pgvector`. Produces a **candidate flag**, surfaced in the review CLI; the human confirms. Never auto-merges.

Dedup lives behind a `Deduplicator` protocol so tier 3 can be toggled off entirely.

## 8. Noise cleaning design

A **pipeline of `CleaningStep`s**, deterministic steps first, model last, output always written to `cleaned_text` (raw untouched):

1. Deterministic (no model): unicode normalize, strip zero-width/control chars, drop known channel footers/CTAs via per-source regex config, collapse whitespace, unwrap tracking-redirect URLs.
2. Model step (Haiku, structured output): given raw + a strict instruction ("remove promotional boilerplate and subscribe prompts; **do not** rephrase, summarize, or remove factual content"), return cleaned text + a list of removed spans for auditability.

If the model step is disabled, deterministic cleaning still runs. Cleaning is idempotent and versioned by `prompt_version`.

---

## 9. Proposed Python package layout

```
telegram_agent/
  pyproject.toml
  src/research_pipeline/
    domain/            # dataclasses: Source, ResearchItem, MediaAsset, StructuredField, Note
    protocols.py       # Adapter, Cleaner, Deduplicator, Extractor, Store, LLMClient
    adapters/
      telegram.py      # Telethon/Bot API → RawPost
    cleaning/
      steps.py         # deterministic CleaningSteps
      llm_cleaner.py   # Haiku structured-output cleaner
    dedup/
      hashing.py       # sha256 + simhash
      semantic.py      # optional embedding dedup
    extraction/
      whitelist.yaml   # the Stage-3 field whitelist == JSON schema source
      engine.py        # Haiku structured-output extractor
    storage/
      supabase_store.py  # the ONLY writer
      search.py          # read-only tsvector queries
    llm/
      anthropic_client.py  # Haiku default, Batch + caching, injectable
    cli/                 # list / view / add-note / add-field / search / export-csv
    pipeline.py          # Registry → Adapter → Clean → Dedup → Extract → Store
    config.py            # composition root: wires concretes into protocols
  skills/skill-creator/  # cloned Anthropic reference (agent-skill authoring)
```

## 10. How the cloned skill fits

`skills/skill-creator/` is the reference for **how to package the pipeline's Claude-facing instructions as a versioned Agent Skill** — the extraction whitelist prompt and the noise-cleaning prompt should each become a small `SKILL.md` (name + description + strict instructions + the JSON schema in `references/`), following the progressive-disclosure and "keep SKILL.md under ~500 lines" conventions it documents. That gives prompt-versioning, reuse, and a clean audit trail for exactly the machine steps §3 constrains.

---

## 11. Suggested build order (MVP-first, per Stage 8)

1. Domain models + protocols + Supabase schema/migrations.
2. `SupabaseStore` (writer) + read-only `search.py`.
3. Telegram adapter → raw ingest (no cleaning/extraction yet) → prove end-to-end store.
4. Deterministic cleaning + exact/simhash dedup (still no model).
5. Haiku extraction (structured output, whitelist, Batch API + caching).
6. Haiku noise-cleaning model step.
7. CLI: list / view / add-note / add-field / full-text search / CSV export.
8. (Deferred, Stage 9) pgvector semantic search + semantic dedup tier.

---

## Open questions for you

1. **AI scope (§3):** comfortable with Claude doing mechanical cleaning + whitelist extraction + dedup *signals*, or do you want the model kept out of extraction entirely (regex-only) for a first cut?
2. **Telegram access:** Bot API (bots must be in the channel) or a user client (Telethon/MTProto, needs your account + API credentials)? This changes the adapter.
3. **Confidence handling:** auto-escalate low-confidence Haiku extractions to Sonnet, or just flag them for human review?
