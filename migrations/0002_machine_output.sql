-- Creative Research Pipeline -- machine output (plan §3).
--
-- Adds the columns the model stages write to, and the trigger that stops
-- anything writing over what they were given. Run after 0001:
--
--   psql "$SUPABASE_DB_URL" -f migrations/0002_machine_output.sql
--
-- Idempotent: safe to re-run.

-- ---------------------------------------------------------------------------
-- Derived columns on Research Item
-- ---------------------------------------------------------------------------
-- All nullable. An item is complete without any of them, and stays complete
-- with the model stages switched off -- which is the configuration plan §3
-- offers as the fallback, so it has to be a first-class state and not a
-- half-populated row.
alter table research_item
    add column if not exists cleaned_text           text,
    add column if not exists cleaned_by_model       text,
    add column if not exists cleaned_prompt_version text,
    add column if not exists simhash                text;

-- Near-duplicate lookup (plan §7 tier 2). Stored as text, not a bigint: a
-- 64-bit fingerprint uses the full unsigned range and Postgres bigint is
-- signed, so half of all fingerprints would need a sign flip to round-trip.
create index if not exists idx_item_simhash on research_item (simhash);

-- ---------------------------------------------------------------------------
-- Guardrail 1: raw is immutable
-- ---------------------------------------------------------------------------
-- Cleaning and extraction are only safe because the human can compare their
-- output against the text the model was actually given. That is a promise the
-- database keeps, not one each caller remembers -- the same way the note
-- table's author CHECK keeps the fact/opinion split.
--
-- `is distinct from` rather than `<>`: a null-to-null update must not raise,
-- and `<>` on nulls is null, which would let it through unnoticed.
create or replace function research_item_raw_text_is_immutable()
returns trigger as $$
begin
    if new.raw_text is distinct from old.raw_text then
        raise exception 'raw_text is immutable: write cleaned_text instead';
    end if;
    return new;
end;
$$ language plpgsql;

drop trigger if exists research_item_raw_text_guard on research_item;
create trigger research_item_raw_text_guard
    before update on research_item
    for each row
    execute function research_item_raw_text_is_immutable();

-- ---------------------------------------------------------------------------
-- Guardrail 2: machine output is traceable
-- ---------------------------------------------------------------------------
-- 0001 gave structured_field its model/prompt_version columns but left them
-- optional for every row. They are only optional for the human's rows: a
-- field the human observed needs no model, and a field a machine produced is
-- worthless without one, because it cannot be re-run or disproved.
--
-- Written as NOT VALID so an existing table with system rows from before this
-- migration is not rejected outright; validate separately once those rows
-- carry provenance:
--
--   update structured_field set model = 'gate', prompt_version = 'keyword-1'
--    where origin = 'system' and model is null;
--   alter table structured_field validate constraint system_fields_are_traceable;
alter table structured_field
    drop constraint if exists system_fields_are_traceable;

alter table structured_field
    add constraint system_fields_are_traceable
    check (
        origin <> 'system'
        or (model is not null and prompt_version is not null)
    ) not valid;
