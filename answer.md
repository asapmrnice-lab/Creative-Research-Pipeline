Verification — the doc is sound ✅

research-pipeline-architecture.md is internally consistent and implementation-ready. Its two best decisions carry
the whole design: Structured Field vs Note as separate tables (structurally enforces fact/opinion split) and
Storage isolation (only one module writes) — the latter is exactly what makes your SQLite→Supabase swap a
contained change instead of a rewrite. The doc even anticipated it.

What the PM wants

A mechanical collect-store-structure-retrieve system, not an analyst. It watches tracked Telegram channels, pulls
every post + media, files it, extracts only explicitly-stated facts, and keeps everything searchable forever. The
human does all the thinking.

⚠️ The one thing you must decide — an actual contradiction

The doc's Core Principle says the system "never analyzes, judges, or draws conclusions" and puts "any AI-generated
judgment" out of scope. You now want Claude/Haiku in the pipeline. These reconcile only if Claude is scoped to
mechanical, verifiable, reversible work — noise-cleaning, whitelist extraction (emits null when a field is absent,
never infers), and dedup signals. Never summarizing-so-you-don't-read, never scoring quality, never
auto-deleting. Guardrails: raw text stays immutable (model writes to new columns) and every machine output records
model/prompt_version/confidence so it's auditable and disprovable.

What I built you

- skills/skill-creator/ — cloned Anthropic's canonical Agent-Skill authoring skill (the best-practice reference
  for packaging your extraction/cleaning prompts as versioned skills). Full folder, not just the SKILL.md.
- IMPLEMENTATION-PLAN.md — the full proposal: SOLID mapping, Anthropic best-practices (this is a workflow, not an
  agent — single stateless Haiku calls with structured outputs so the model can't invent a field, prompt caching on
  the whitelist, Batch API for 50%-cheaper unattended collection), Supabase schema, 3-tier dedup (sha256 → simhash →
  optional pgvector, always flag-don't-delete), a cleaning-step pipeline (deterministic first, Haiku last), Python
  package layout, and MVP build order.

Design keeps every LLM step behind an interface, so it can be disabled per-stage (regex-only fallback) if you want
the model kept out.

Three questions that change what I build (in the plan)
for packaging your extraction/cleaning prompts as versioned skills). Full folder, not just the SKILL.md.

- IMPLEMENTATION-PLAN.md — the full proposal: SOLID mapping, Anthropic best-practices (this is a workflow, not an
  agent — single stateless Haiku calls with structured outputs so the model can't invent a field, prompt caching on
  the whitelist, Batch API for 50%-cheaper unattended collection), Supabase schema, 3-tier dedup (sha256 → simhash →
  optional pgvector, always flag-don't-delete), a cleaning-step pipeline (deterministic first, Haiku last), Python
  package layout, and MVP build order.

Design keeps every LLM step behind an interface, so it can be disabled per-stage (regex-only fallback) if you want
the model kept out.

Three questions that change what I build (in the plan)

1. AI scope: Claude doing mechanical cleaning + whitelist extraction + dedup signals — or model kept out of
   extraction entirely (regex-only) for v1?
2. Telegram access: Bot API (bot must be in channel) or user client (Telethon/MTProto, your account creds)?
   Changes the adapter.
3. Low-confidence extractions: auto-escalate to Sonnet, or just flag for human review?

Want me to answer these with a recommendation and move to scaffolding the package, or refine the plan first?

Sources: anthropics/skills · skill-creator/SKILL.md
