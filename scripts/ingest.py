"""Run the pipeline: read a source, apply the gate, store what passes.

This is the filter actually connected. Unlike keyword_report.py, which
describes posts something else already stored, nothing here is written until
the gate says yes.

    python scripts/ingest.py              # ingest from the trial archive
    python scripts/ingest.py --dry-run    # decide everything, write nothing
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from research_pipeline.adapters.archive import ArchiveSource  # noqa: E402
from research_pipeline.config import (  # noqa: E402
    ConfigError,
    StorageConfig,
    open_collection_store,
)
from research_pipeline.domain import RawPost  # noqa: E402
from research_pipeline.env import load_project_env  # noqa: E402
from research_pipeline.filtering import (  # noqa: E402
    KeywordFilter,
    KeywordFilterConfig,
    KeywordGate,
)
from research_pipeline.pipeline import ingest  # noqa: E402


class DryRunStore:
    """Satisfies the Store protocol, writes nothing.

    Lets a run report exactly what would be collected before any file is
    created -- the same code path, minus the side effect.
    """

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def save(self, post: RawPost, keywords: tuple[str, ...]) -> bool:
        if post.external_id in self.seen:
            return False
        self.seen.add(post.external_id)
        return True

    def count_items(self) -> int:
        return len(self.seen)

    def close(self) -> None:
        pass

    def __enter__(self) -> "DryRunStore":
        return self

    def __exit__(self, *exc) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", help="override STORE_DB_PATH")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(__file__).resolve().parent.parent
    load_project_env(root)

    archive = os.environ.get("TRIAL_DB_PATH")
    if not archive:
        print("TRIAL_DB_PATH is not set (see .env.example)", file=sys.stderr)
        return 1

    gate = KeywordGate(KeywordFilter(KeywordFilterConfig.from_env()))
    source = ArchiveSource(root / archive)

    try:
        storage = StorageConfig.from_env(root, args.db)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 1

    store = DryRunStore() if args.dry_run else open_collection_store(storage)

    print(f"keywords : {', '.join(gate.keywords)}")
    print(f"source   : {archive}")
    print(f"target   : {'(dry run -- nothing written)' if args.dry_run else storage.label}")
    print()

    with store:
        result = ingest(source, gate, store)

        print(f"posts seen      : {result.seen}")
        print(f"  collected     : {result.collected}")
        print(f"    newly stored: {result.stored}")
        print(f"    already had : {result.duplicates}")
        print("  rejected      :")
        for reason, count in sorted(result.rejected.items()):
            print(f"    {reason:<12}: {count}")

        if not args.dry_run:
            print()
            print(f"items in store  : {store.count_items()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
