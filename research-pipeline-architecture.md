# Personal Creative Research Pipeline — Architecture Summary

**Status:** Stages 1–4 locked.
**Purpose:** Design reference to review before continuing implementation planning.

---

## Core Principle
"Automate everything that does not require thinking."
The system collects, stores, structures, and retrieves. It never analyzes, judges, or draws conclusions. All intellectual conclusions are made by the human.

---

## Stage 1 — System Boundaries (LOCKED)

**In scope:**
- Ingestion of raw content from external sources (Telegram first; others added later without touching the core)
- Mechanical acquisition: download video/images, save text, capture metadata
- Storage of Research Items and raw artifacts
- Structuring/normalization of metadata
- Search & retrieval (full-text, semantic/embedding-based)
- Deduplication / "already seen" tracking
- Capture of the human's own notes and tags (stored, never generated)

**Out of scope:**
- Any AI-generated judgment, scoring, or quality assessment
- Hypothesis generation or "insight extraction" by AI
- Automated summarization that substitutes for the human reading/watching
- Ad performance/spend analytics
- Publishing or content-creation features

**Access model:** Local machine only — single computer, single folder + SQLite file. No server, no sync, no multi-device access, no auth. (Not a permanent restriction — storage will be built behind a clear interface so remote/sync access could be added later without a rebuild, but it is not built now.)

**MVP interaction mode:** Pure CLI scripts + direct SQLite queries. No UI. (A UI can be added later as a separate layer on top of the same database, if ever needed.)

---

## Stage 2 — Domain Model (LOCKED)

**Entities:**

1. **Source** — a trackable origin (Telegram channel, YouTube channel, website, etc.)
   Fields: source type, name/handle, platform-specific ID, date first tracked

2. **Research Item** — the central object; one per source post/message, regardless of how many content types (text/video/link) it contains
   Fields: source reference, original URL, ingestion timestamp, raw text, status, source-specific extras (flexible field for source-unique data)

3. **Media Asset** — video/image/file attached to a Research Item (a post can have several)
   Fields: type, local file path, original URL, technical metadata (duration, size)

4. **Structured Field** — explicitly confirmed factual data only (see extraction rule below)
   Fields: field name, value, origin (system-extracted vs human-entered)

5. **Note** — free-text human analysis/conclusions; always human-authored, never system-generated

**Relationships:**
```
Source (1) ──< (many) Research Item (1) ──< (many) Media Asset
                                      │
                                      ├──< (many) Structured Field
                                      │
                                      └──< (many) Note
```

**Design rationale:**
- One Research Item = one source post. Links inside a post are resolved and folded into that same item. No cross-post merging (kept simple; can be layered on later as a separate "collection" concept if ever needed — not needed now).
- Structured Field and Note are deliberately separate tables — this structurally enforces the boundary between mechanical fact and human judgment, not just as a coding convention.
- Source is its own entity (not just a text field) so posting frequency and per-channel activity are simple queries, not built features.
- Extensibility: adding a new source type (e.g., Facebook Ad Library) means a new `Source` row + a new ingestion adapter — no core schema changes.

---

## Stage 3 — Mandatory vs Optional Data (LOCKED)

**Mandatory** for a Research Item to be created:
- Source reference
- At least one piece of original content (text, video, image, or other supported media)

**Optional** (added anytime after creation):
- Structured Fields (extracted or manual)
- Notes
- Additional Media Assets
- Timestamp/URL, where a source may lack them (e.g., manually-added items)

**Structured Field extraction rule (whitelist):**
Only information explicitly present in the source, requiring no interpretation, qualifies for auto-extraction:
GEO, brand, offer, vertical, traffic source, language, KPIs (CPA, CPL, CPI, FTD, ROI, CR, Spend, Revenue — only if explicitly stated), dates, monetary amounts, percentages, other objective facts explicitly present.
If a value isn't explicitly stated, it is **not** extracted — it stays absent until the human adds it manually.

---

## Stage 4 — Lifecycle of a Research Item (LOCKED)

Proposed stages:
1. **Discovered** — system detects new content from a tracked Source
2. **Ingested** — Research Item created with mandatory fields; raw content + media stored locally
3. **Processed** — mechanical extraction runs *(scope under discussion — see open question below)*
4. **Reviewed** — human reads/watches, optionally adds Notes and manual Structured Fields
5. **Archived** — no further action expected; never deleted; stays searchable forever

### Video processing scope (RESOLVED — Option A)
No processing of video at all. Video is extracted and saved locally as a file, period. No transcription, no computer vision, nothing automated touches its content. "Processed" therefore applies only to text-based mechanical extraction (Structured Fields from explicit text) — video sits as an opaque file for the human to watch.

### Status tracking (RESOLVED — No)
No explicit status field. Lifecycle is an implicit concept, not a queryable/filterable attribute. No "unreviewed" filter is built.

---

## Stage 5 — Primary User Workflows (LOCKED)

**Workflow 1 — Passive collection (unattended)**
New post appears on a tracked Source → system detects it → Research Item created → text stored, media downloaded → explicit Structured Fields extracted if present. No human involved.

**Workflow 2 — Review session (human-driven)**
Run a CLI script → get a list of un-reviewed items (defined as: items with no Notes yet, since there is no status field) → read/watch each → add Notes and/or manual Structured Fields.

**Workflow 3 — Search & retrieval (human-driven, ad hoc)**
Run a search script (full-text and/or semantic) → get matching Research Items → open the relevant one to re-read/re-watch.

No manual-entry workflow — ingestion is always automatic via tracked Sources only.

---

## Stage 6–7 — Architectural Modules & Responsibility Boundaries (LOCKED)

1. **Source Registry** — knows which Sources are tracked and their connection details. All raw content access goes through here first.

2. **Ingestion Adapter(s)** — one per source type (e.g., `telegram_adapter`). Detects new content from a Source, pulls raw data (text, media, link), hands it off in a common format. New source type = new adapter, nothing else changes. Kept separate from Extraction Engine.

3. **Extraction Engine** — takes raw content, applies the Structured Field whitelist rule, produces only explicitly-confirmed facts. No inference. Runs after ingestion, before storage is finalized. Deliberately not merged into the Ingestion Adapter — kept as its own module.

4. **Storage Layer** — owns the SQLite database (sole source of truth — no shift to Excel or any other storage) and the local file/folder structure for media. Only this module writes to the DB.

5. **Search Module** — reads from Storage, builds/queries full-text and semantic indexes. Read-only against Storage.

6. **Review/CLI Interface** — the scripts you run directly: list items, view an item, add a Note or manual Structured Field, run a search, and export a table snapshot (e.g., to Excel/CSV) on demand for quick visual browsing. The export is a disposable view, not a second source of truth — SQLite remains authoritative.

**Data flow:**
`Source Registry → Ingestion Adapter → Extraction Engine → Storage Layer` (automatic pipeline)
`Review/CLI Interface ↔ Storage Layer` and `Search Module ↔ Storage Layer` (on-demand, human-triggered)

---

## Stage 8 — MVP Scope (LOCKED)

**In MVP:**
- Source Registry — Telegram sources only
- Ingestion Adapter — Telegram only, no second source at launch
- Extraction Engine — whitelist-based Structured Field extraction from text only (no video processing, per Stage 4 decision)
- Storage Layer — SQLite + local media folder
- Review/CLI — list items, view one item, add a Note, add a manual Structured Field
- Basic Search — full-text search only
- Excel/CSV export — simple table dump on demand (disposable view, not a second source of truth)

## Stage 9 — Intentionally Postponed (LOCKED)

- Semantic/embedding-based search
- Any second source type (YouTube, Facebook Ad Library, websites, etc.)
- Any UI beyond CLI
- Multi-device/sync access

---

## Stage 10 — Long-Term Scalability Decisions (LOCKED)

**Already built in, from earlier decisions:**
- New sources are cheap — a new Ingestion Adapter, no core changes
- New fact types are cheap — the whitelist in the Extraction Engine can grow without touching the schema
- Storage isolation — only the Storage Layer touches SQLite directly, so local-only could become synced later without rewriting other modules
- Fact/opinion split enforced by schema, not convention — Structured Field and Note stay separate tables forever

**Named risks, not concerns for now:**
- SQLite has a practical scale ceiling (fine for years of personal use; migration to something like Postgres would be a contained project later if ever needed, thanks to Storage Layer isolation)
- Media storage will grow over years on one local disk — archival strategy is a future decision, out of scope now
- Semantic/embedding search is anticipated, not rejected — already postponed to Stage 9, and Search Module isolation means adding it later is additive, not disruptive

---

## Architecture Design: Complete

All 10 stages are locked. This document is ready to hand off to implementation planning.


---

*This document reflects the complete, locked architecture from the design conversation. All open questions have been resolved. Ready to take into implementation planning (e.g., Claude Code).*
