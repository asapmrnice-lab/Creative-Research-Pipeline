-- Creative Research Pipeline -- initial Supabase/Postgres schema.
--
-- Mirrors the locked domain model (architecture Stage 2) and the SQLite
-- schema it replaces, table for table. Run once against a fresh project:
--
--   psql "$SUPABASE_DB_URL" -f migrations/0001_init.sql
--
-- or paste it into the Supabase dashboard SQL editor.
--
-- Idempotent: safe to re-run.

-- ---------------------------------------------------------------------------
-- Source
-- ---------------------------------------------------------------------------
create table if not exists source (
    id               bigint generated always as identity primary key,
    platform         text        not null,
    platform_id      text        not null,
    handle           text,
    title            text,
    first_tracked_at timestamptz not null default now(),
    unique (platform, platform_id)
);

-- ---------------------------------------------------------------------------
-- Research Item
-- ---------------------------------------------------------------------------
create table if not exists research_item (
    id           bigint generated always as identity primary key,
    source_id    bigint      not null references source (id),
    external_id  text        not null,
    original_url text,
    posted_at    timestamptz,
    ingested_at  timestamptz not null default now(),
    raw_text     text        not null,
    content_hash text        not null,
    -- Re-running an ingest must not duplicate items.
    unique (source_id, external_id)
);

-- Exact-duplicate detection (plan §7 tier 1). Deliberately NOT unique: a
-- genuine repost is still its own observation, so duplicates are detected and
-- reported, never rejected by the database.
create index if not exists idx_item_hash on research_item (content_hash);

-- Full-text search. A generated column replaces the SQLite trigger that kept
-- an FTS5 shadow table in step -- Postgres recomputes it on write, so the
-- index cannot drift out of sync with the text no matter who writes.
--
-- 'russian' rather than 'simple': the snowball stemmer folds the inflections
-- the collection filter already understands, so searching "креатив" finds
-- "креативы" without the caller doing anything.
alter table research_item
    add column if not exists fts tsvector
    generated always as (to_tsvector('russian', raw_text)) stored;

create index if not exists idx_item_fts on research_item using gin (fts);

-- ---------------------------------------------------------------------------
-- Media Asset
-- ---------------------------------------------------------------------------
create table if not exists media_asset (
    id               bigint generated always as identity primary key,
    research_item_id bigint not null references research_item (id),
    kind             text   not null,
    storage_path     text,
    original_url     text,
    file_name        text,
    size_bytes       bigint,
    duration         integer
);

create index if not exists idx_media_item on media_asset (research_item_id);

-- ---------------------------------------------------------------------------
-- Structured Field -- mechanical fact, with provenance
-- ---------------------------------------------------------------------------
create table if not exists structured_field (
    id               bigint generated always as identity primary key,
    research_item_id bigint not null references research_item (id),
    name             text   not null,
    value            text   not null,
    origin           text   not null check (origin in ('system', 'human')),
    model            text,
    prompt_version   text,
    confidence       real
);

create index if not exists idx_field_item on structured_field (research_item_id);

-- ---------------------------------------------------------------------------
-- Note -- human analysis only
-- ---------------------------------------------------------------------------
-- The CHECK is the schema-level half of the fact/opinion split. Nothing
-- automated may file its output here, and that is enforced by the database
-- rather than by everyone remembering.
create table if not exists note (
    id               bigint generated always as identity primary key,
    research_item_id bigint      not null references research_item (id),
    body             text        not null,
    author           text        not null default 'human' check (author = 'human'),
    created_at       timestamptz not null default now()
);

create index if not exists idx_note_item on note (research_item_id);

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
-- RLS on with NO policies means: deny everything to the anon and authenticated
-- roles. The pipeline connects directly as the database owner (or with the
-- service role), which bypasses RLS by design.
--
-- This matters even though the system is single-user: a Supabase project
-- exposes a PostgREST endpoint on the public internet from the moment it is
-- created. Without this, anyone holding the project's anon key -- which is
-- meant to be publishable -- could read every post you have collected.
alter table source          enable row level security;
alter table research_item   enable row level security;
alter table media_asset     enable row level security;
alter table structured_field enable row level security;
alter table note            enable row level security;
