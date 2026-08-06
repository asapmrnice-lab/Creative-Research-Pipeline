"""Run the machine stages over what is already collected (plan §3).

Collection decides what to keep; this decides nothing. It cleans, fingerprints,
labels and -- only if a model is configured -- extracts whitelisted facts. It
never adds, removes or re-judges an item.

    python scripts/enrich.py                # every stage the config allows
    python scripts/enrich.py --dry-run      # report, write nothing
    python scripts/enrich.py --no-extract   # deterministic stages only
    python scripts/enrich.py --limit 50     # a sample first

With no ANTHROPIC_API_KEY set this still does useful work: cleaning, exact and
near-duplicate detection, and language/format labelling all run with no model
and no network. Extraction reports itself skipped. That is plan §3's fallback,
and it is the default.

Re-running is safe. Cleaning is keyed by prompt version so an item already
cleaned by this version is skipped, and extraction is keyed by
(item, chunk, prompt version) so a re-run repeats work rather than doubling it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from research_pipeline.cleaning import CleaningConfig, DeterministicCleaner  # noqa: E402
from research_pipeline.cleaning.llm_cleaner import LlmCleaner  # noqa: E402
from research_pipeline.config import (  # noqa: E402
    ConfigError,
    LLMConfig,
    StorageConfig,
    open_llm_client,
    open_machine_store,
)
from research_pipeline.dedup import DedupConfig, NearDuplicateIndex, simhash_hex  # noqa: E402
from research_pipeline.detect import detect  # noqa: E402
from research_pipeline.detect.signals import DETECT_VERSION  # noqa: E402
from research_pipeline.env import load_project_env  # noqa: E402
from research_pipeline.extraction import ExtractionEngine  # noqa: E402
from research_pipeline.llm.protocol import Provenance  # noqa: E402

DETECT_PRODUCER = "detect"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--no-extract", action="store_true", help="skip the model stage")
    parser.add_argument("--limit", type=int, help="process at most N items")
    parser.add_argument("--db", help="override STORE_DB_PATH")
    parser.add_argument(
        "--backfill-provenance",
        action="store_true",
        help="attribute pre-existing keyword fields to the gate, then exit",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(__file__).resolve().parent.parent
    load_project_env(root)

    try:
        storage = StorageConfig.from_env(root, args.db)
        cleaning = CleaningConfig.from_env()
        dedup = DedupConfig.from_env()
    except (ConfigError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1

    llm_config = LLMConfig(api_key=None) if args.no_extract else LLMConfig.from_env()
    client = open_llm_client(llm_config)
    cleaner = LlmCleaner(client, deterministic=DeterministicCleaner(cleaning))
    engine = ExtractionEngine(client)

    print(f"store    : {storage.label}")
    print(f"model    : {llm_config.label}")
    print(f"cleaning : {len(cleaning.drop_patterns)} configured drop pattern(s)")
    print(f"dedup    : flag at <= {dedup.distance} bits")
    if args.dry_run:
        print("mode     : dry run -- nothing will be written")
    print()

    store = open_machine_store(storage)
    with store:
        if args.backfill_provenance:
            updated = store.backfill_keyword_provenance()
            print(f"keyword fields attributed to the gate: {updated}")
            print(f"machine fields still untraceable    : {store.untraceable_system_fields()}")
            return 0

        legacy = store.untraceable_system_fields()
        if legacy:
            print(
                f"note: {legacy} machine field(s) predate the provenance rule.\n"
                "      run with --backfill-provenance to attribute them.\n"
            )

        # `items_needing` is the idempotency key as a query: an item already
        # cleaned by this exact version is not paid for twice.
        pending = store.items_needing(cleaner.version, limit=args.limit)
        if not pending:
            print("nothing to do -- every item is current for this prompt version")
            return 0
        print(f"items to process: {len(pending)}")

        # -- clean ----------------------------------------------------------
        outcomes = cleaner.clean(pending)
        by_model = sum(1 for o in outcomes if o.used_model)
        rejected = [o for o in outcomes if o.rejected]
        changed = sum(1 for o in outcomes if o.changed)

        # -- label and fingerprint (deterministic, no model) -----------------
        index = NearDuplicateIndex(dedup)
        flags = []
        labels: list[tuple[int, tuple[tuple[str, str], ...]]] = []
        for outcome in outcomes:
            labels.append((outcome.item_id, detect(outcome.text).as_fields()))
            if (flag := index.add(outcome.item_id, outcome.text)) is not None:
                flags.append(flag)

        if not args.dry_run:
            detect_provenance = Provenance(DETECT_PRODUCER, DETECT_VERSION)
            for outcome in outcomes:
                if outcome.text:
                    store.set_cleaned_text(
                        outcome.item_id,
                        outcome.text,
                        cleaner.provenance(used_model=outcome.used_model),
                    )
                    store.set_simhash(outcome.item_id, simhash_hex(outcome.text))
            for item_id, fields in labels:
                for name, value in fields:
                    store.add_machine_field(item_id, name, value, detect_provenance)

        print()
        print("cleaning")
        print(f"  changed        : {changed}")
        print(f"  model accepted : {by_model}")
        print(f"  model rejected : {len(rejected)}")
        for outcome in rejected[:5]:
            print(f"    item {outcome.item_id}: {outcome.rejected}")

        print("near-duplicates")
        print(f"  flagged        : {len(flags)}  (flagged only -- nothing merged)")
        for flag in flags[:5]:
            print(f"    item {flag.item_id} ~ item {flag.duplicate_of} ({flag.distance} bits)")

        # -- extract --------------------------------------------------------
        print("extraction")
        if not llm_config.enabled:
            reason = "--no-extract" if args.no_extract else "no ANTHROPIC_API_KEY"
            print(f"  skipped        : {reason}")
        elif args.dry_run:
            print("  skipped        : dry run")
        else:
            texts = [(o.item_id, o.text) for o in outcomes if o.text]
            report = engine.run(texts, store)
            print(f"  extracted      : {report.extracted}")
            print(f"  skipped        : {report.skipped}")
            print(f"  failed         : {report.failed}")
            print(f"  fields written : {report.fields_written}")

    if args.dry_run:
        print()
        print("dry run -- nothing was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
