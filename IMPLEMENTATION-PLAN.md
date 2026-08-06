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

### 3a. As actually executed (recorded 2026-08-05)

Built and running; `scripts/enrich.py` is the entry point. **The default configuration calls no model** — with `ANTHROPIC_API_KEY` empty, three of the four Allowed rows run anyway, and the fourth reports itself skipped. That is the fallback above, shipped as the default rather than as an escape hatch.

| §3 row | How it was built | Needs a model? |
|---|---|---|
| Noise cleaning | `cleaning/steps.py` (5 deterministic steps) then `cleaning/llm_cleaner.py` | Only the second pass |
| Whitelist extraction | `extraction/whitelist.json` compiled to a JSON Schema, `extraction/engine.py` | Yes |
| Dedup signal | `dedup/hashing.py` — SimHash, flag-only | No |
| Language/format detection | `detect/signals.py` — character counting | No — see below |

Four things came out differently from the plan as written:

1. **Detection never needed a model.** Which script a post is in, and whether it holds a link or a table, are countable properties of the characters. The row's real value turned out to be the line beside it in the Forbidden column: a label says what a post *is*, so the human can find it; a summary says what it *means*, so the human can skip it. `Signals` has no `topic`, `quality` or `category` field, and a test asserts it never grows one.

2. **"Do not rephrase" was made checkable rather than instructed.** Model-cleaned text is accepted only if its whitespace-delimited tokens are a subsequence of the input's — every surviving word present, unaltered, in order. Output that fails is discarded whole in favour of the deterministic text. The check must be token-level, not character-level: character-wise, turning `ROI 140%` into `ROI 14%` *is* a deletion, and it is precisely the failure worth catching.

3. **Both guardrails moved into the schema.** `raw_text` is protected by a trigger (SQLite and Postgres both), and a system-origin field without `model` and `prompt_version` is rejected by the database — the same mechanism as the `note.author = 'human'` CHECK, so no future caller can forget. The gate's own keyword fields were retro-fitted with provenance to satisfy it; `--backfill-provenance` attributes pre-existing rows.

4. **Prompt caching does not fire on Haiku, and §5 should not assume it does.** The minimum cacheable prefix on `claude-haiku-4-5` is 4096 tokens; the whitelist instructions are far shorter, so caching silently no-ops (`cache_creation_input_tokens: 0`, no error). Padding the prompt to reach the minimum would mean paying for tokens in order to pretend to save tokens. The client therefore *reports* cache tokens on every result rather than assuming the saving, and the Batch API's 50% discount is the real cost lever.

`DEDUP_SIMHASH_DISTANCE` was set from measurement, not intuition: across the 65 real posts in `tests/fixtures`, four kinds of repost edit never exceeded 9 bits of difference and no unrelated pair came closer than 17. The default is 12, and a test re-derives it from the corpus so it cannot quietly rot.

---

## 3b. The Collection Gate (added after the fact — this plan originally missed it)

Everything above assumes the pipeline collects *every* post from a tracked Source and then decides what to do with it. That is not what we want, and the omission was an oversight in the first draft of this plan. A tracked channel posts a great deal that has nothing to do with creatives; storing all of it and filtering at read time means downloading every video to keep a fraction of them.

So there is a **gate in front of storage**: a post is collected only when its text carries one of the human's configured keywords. A post that matches nothing is never written, and its media is therefore never fetched.

**Why this is not the "judgment" the Core Principle forbids.** The table in §3 forbids *"deciding a post is low quality and dropping it."* The gate does drop posts, so the distinction has to be exact:

- The **human** writes the keyword list. The machine never adds, infers, widens or "improves" a keyword.
- The rule is **literal and deterministic** — string matching plus declared morphology, no model, no scoring, no threshold. The same post and the same list always produce the same verdict, and a human can verify it by reading.
- It is a **scope decision, not a quality decision**. The gate says "this is not the subject I am researching," never "this is a bad post."

That is the same category as choosing which channels to track in the first place — which the architecture already treats as the human's call. The gate is that choice expressed one level finer.

**The cost, stated plainly:** filtering at collection time is lossy and not retroactive. Adding a keyword later does **not** backfill posts already rejected — they were never stored, so there is nothing to re-scan. This is the deliberate trade for not hoarding gigabytes of irrelevant video. It is why the gate reports its rejections by reason (`no-text` vs `no-keyword`) rather than silently discarding, so the human can see what a list is costing them and widen it before it matters.

**Where it sits relative to "never deletes":** the architecture's guarantee that items are never deleted applies to *collected* items. A post the gate rejected was never a Research Item, so nothing was deleted. No tombstone is written.

---

## 4. SOLID mapping (the "solid principles" ask)

The doc's modules already lean this way; here's the explicit mapping and where I'd tighten:

- **S — Single Responsibility.** Each module owns one job: `SourceRegistry`, `IngestionAdapter`, `CollectionGate`, `NoiseCleaner`, `Deduplicator`, `ExtractionEngine`, `StorageLayer`, `SearchModule`, `CLI`. Cleaning and dedup are *split out* of the Extraction Engine (the doc lumped them loosely) so each is independently testable and disableable. The gate (§3b) is likewise its own module, not a branch inside the adapter — an adapter that knew about keywords could not be reused unfiltered.
- **O — Open/Closed.** New source = new `IngestionAdapter` implementation. New fact type = new entry in the extraction **whitelist config**, not code. New cleaning rule = new `CleaningStep`. New collection rule = new `Gate` implementation. No edits to the core.
- **L — Liskov.** Every adapter satisfies the same `Adapter` protocol (`fetch_new() -> Iterable[RawPost]`); the pipeline treats Telegram exactly like a future YouTube adapter.
- **I — Interface Segregation.** Small protocols: `Adapter`, `Gate`, `Cleaner`, `Deduplicator`, `Extractor`, `Store`, `LLMClient`. The Search module depends only on a read-only `Store` view; it can't write.
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
    filtering/
      keywords.py      # the literal matcher: normalisation + morphology
      gate.py          # Gate impl: RawPost → Decision(verdict, keywords)
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
    pipeline.py          # Registry → Adapter → Gate → Clean → Dedup → Extract → Store
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
4. **Collection gate (§3b)** in front of the store, with rejection counters that reconcile against posts seen.
5. Deterministic cleaning + exact/simhash dedup (still no model).
6. Haiku extraction (structured output, whitelist, Batch API + caching).
7. Haiku noise-cleaning model step.
8. CLI: list / view / add-note / add-field / full-text search / CSV export.
9. (Deferred, Stage 9) pgvector semantic search + semantic dedup tier.

**Build order as actually executed (recorded 2026-08-03).** The gate was built before storage, not after, because the trial archive had already downloaded everything unfiltered and the gate was the fix for that. Steps 3, 4 and 8 are done against a **local SQLite** store; steps 1–2 (Supabase) are the outstanding work, not the finished foundation this list assumes. See §12.

---

## 12. What the Supabase move actually costs (audit, 2026-08-03)

§4 claims swapping storage "touches one wiring file." That is the *design intent* and it is nearly true on the write side, but it is not true of the code as it stands. Before `SupabaseStore` is written, four things are in the way:

1. **There is no composition root.** `config.py` from §9 was never built. `scripts/ingest.py`, `scripts/review.py` and `scripts/serve_review.py` each import and construct `SqliteStore` / `ResearchStoreReader` **directly**. Every one of those is an edit site. The `Store` protocol exists but nothing depends on it — `ingest.py` even does `isinstance(store, SqliteStore)` to decide whether to print a count.
2. **The read side has no protocol.** `protocols.py` defines `Source`, `Gate` and `Store` — write-side only. `ResearchStoreReader` is a concrete class with no abstraction above it, so the CLI and the review page are welded to SQLite. This is the larger half of the work, and §4's "the Search module depends only on a read-only `Store` view" is currently aspiration, not fact.
3. **Search is SQLite-specific in its implementation, not just its dialect.** `reader.search()` uses an FTS5 virtual table, `MATCH`, and the `snippet()` function, kept in sync by a SQL trigger. Postgres does the same job with `tsvector`, a GIN index and `ts_headline`. The *behaviour* to preserve is the deliberate one: a bare word becomes a prefix query so Russian inflections are found. That intent has to be re-implemented, not translated.
4. **Media has no implementation on either side.** The store records `storage_path` and nothing ever writes a file. Moving to a Supabase Storage bucket is therefore not a migration — it is the original build, still outstanding.

**What is genuinely cheap:** the domain layer. `RawPost`/`RawSource`/`RawMedia` know nothing about storage, `content_hash()` is pure, the gate never touches a database, and `pipeline.ingest()` depends only on protocols. None of that changes. The schema in `sqlite_store.py` already maps 1:1 to §6's Postgres tables, including the `origin` CHECK and the `author='human'` constraint — those become Postgres `ENUM`/`CHECK` almost verbatim.

**Order of work, so nothing is done twice:**

1. Commit the review layer as it stands (currently uncommitted — refactoring on top of uncommitted work loses the ability to bisect).
2. Extract a read-side protocol (`ReadStore`) from `ResearchStoreReader`'s existing public surface — `list_items`, `get_item`, `search`, `stats`, `export_rows`. Pure refactor, no behaviour change, 186 tests must stay green.
3. Add `config.py` as the composition root; make the three scripts request a store from it rather than construct one. Delete the `isinstance` check.
4. Only then write `SupabaseStore` + `SupabaseReadStore` against those protocols, and the Postgres migration.
5. Backfill: re-run ingest against the archive to populate Supabase. Nothing needs migrating out of SQLite — the archive is the source of truth for collected posts and re-ingest is idempotent on `(source, external_id)`.

Steps 2–3 are worth doing regardless of which backend wins, which is why they come first.

---

## Open questions for you

1. ~~**AI scope (§3):**~~ — built (see §3a). Runs model-free by default; setting `ANTHROPIC_API_KEY` is the only thing that turns extraction and model-cleaning on.
2. **Telegram access:** Bot API (bots must be in the channel) or a user client (Telethon/MTProto, needs your account + API credentials)? This changes the adapter. Still open — collection currently reads the trial archive, not Telegram.
3. **Confidence handling:** auto-escalate low-confidence Haiku extractions to Sonnet, or just flag them for human review?
4. **SQLite's fate after Supabase** — see §12. Kept as the offline/test implementation behind the protocol, or removed once Supabase works?
