"""The Review interface: read the store, and write your own analysis into it.

This is Workflow 2 and 3 from the architecture -- the human-driven half of the
system. Everything else in scripts/ is machinery that runs unattended; this is
the part you sit in front of.

    python scripts/review.py stats              # what is in the store
    python scripts/review.py list               # newest first
    python scripts/review.py list --unreviewed  # only items with no notes yet
    python scripts/review.py view 12            # one item in full
    python scripts/review.py search крео        # full-text search
    python scripts/review.py note 12 "strong hook, weak CTA"
    python scripts/review.py field 12 geo UA
    python scripts/review.py export             # CSV snapshot for Excel

Reads go through a read-only store; the two writing commands go through the
annotation store, which records everything it writes as origin='human'. Which
backend answers -- SQLite or Supabase -- is decided by config.py, not here.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from research_pipeline.config import (  # noqa: E402
    ConfigError,
    StorageConfig,
    open_annotation_store,
    open_read_store,
)
from research_pipeline.env import load_project_env  # noqa: E402
from research_pipeline.protocols import ReadStore  # noqa: E402
from research_pipeline.storage.views import ItemDetail  # noqa: E402

RULE = "-" * 72


def _fmt_date(value: str | None, width: int = 16) -> str:
    return (value or "").replace("T", " ")[:width].ljust(width)


def cmd_stats(reader: ReadStore, args) -> int:
    s = reader.stats()
    print(f"items        : {s.items}")
    print(f"sources      : {s.sources}")
    print(f"date range   : {_fmt_date(s.first_post).strip()} .. {_fmt_date(s.last_post).strip()}")
    print(f"media assets : {s.media}")
    print(f"notes        : {s.notes}")
    print(f"un-reviewed  : {s.unreviewed}  (no note yet)")
    if s.keywords:
        print("matched keywords:")
        for word, count in s.keywords.items():
            print(f"  {word:<14} {count}")
    return 0


def cmd_list(reader: ReadStore, args) -> int:
    items = reader.list_items(limit=args.limit, unreviewed_only=args.unreviewed)
    if not items:
        print("nothing to show" + (" -- every item has a note" if args.unreviewed else ""))
        return 0

    print(f"{'id':>4}  {'posted':16}  {'channel':22}  {'matched':22}  text")
    print(RULE)
    for it in items:
        mark = " " if it.reviewed else "*"
        print(
            f"{it.id:>4}{mark} {_fmt_date(it.posted_at)}  "
            f"{it.channel[:22]:22}  {', '.join(it.keywords)[:22]:22}  {it.preview}"
        )
    print(RULE)
    shown = f"{len(items)} item(s)"
    print(f"{shown}   * = no note yet   'view <id>' for the full post")
    return 0


def _print_item(item: ItemDetail) -> None:
    print(RULE)
    print(f"#{item.id}  {item.channel}  {_fmt_date(item.posted_at).strip()}")
    if item.url:
        print(f"link      : {item.url}")
    print(f"source id : {item.external_id}   ingested {_fmt_date(item.ingested_at).strip()}")

    system = [f for f in item.fields if f.origin == "system"]
    human = [f for f in item.fields if f.origin == "human"]
    if system:
        print("matched   : " + ", ".join(f"{f.value}" for f in system if f.name == "keyword"))
    if human:
        print("your fields:")
        for f in human:
            print(f"  {f.name} = {f.value}")
    if item.media:
        print(f"media     : {len(item.media)} file(s)")

    print(RULE)
    print(item.text.strip())
    print(RULE)
    if item.notes:
        print("notes:")
        for n in item.notes:
            print(f"  [{_fmt_date(n.created_at).strip()}] {n.body}")
    else:
        print("notes: none yet  --  add one with: review.py note "
              f"{item.id} \"...\"")


def cmd_view(reader: ReadStore, args) -> int:
    item = reader.get_item(args.id)
    if item is None:
        print(f"no item with id {args.id}", file=sys.stderr)
        return 1
    _print_item(item)
    return 0


def cmd_search(reader: ReadStore, args) -> int:
    hits = reader.search(" ".join(args.query), limit=args.limit)
    if not hits:
        print("no matches")
        return 0
    for h in hits:
        print(f"{h.id:>4}  {_fmt_date(h.posted_at)}  {h.channel[:20]:20}  {h.snippet}")
    print(RULE)
    print(f"{len(hits)} match(es)   'view <id>' for the full post")
    return 0


def cmd_export(storage: StorageConfig, args) -> int:
    with open_read_store(storage) as reader:
        rows = reader.export_rows()
    if not rows:
        print("store is empty -- nothing to export")
        return 0

    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parent.parent / out
    out.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: Excel on Windows reads Cyrillic as mojibake without the BOM.
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} items -> {out}")
    return 0


def cmd_note(storage: StorageConfig, args) -> int:
    with open_annotation_store(storage) as store:
        store.add_note(args.id, args.body)
    print(f"note added to item {args.id}")
    return 0


def cmd_field(storage: StorageConfig, args) -> int:
    with open_annotation_store(storage) as store:
        store.add_field(args.id, args.name, args.value)
    print(f"{args.name} = {args.value} added to item {args.id} (origin: human)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="override STORE_DB_PATH")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="counts and keyword tally")

    p_list = sub.add_parser("list", help="list items, newest first")
    p_list.add_argument("--limit", type=int, help="show at most N items")
    p_list.add_argument(
        "--unreviewed", action="store_true", help="only items with no note yet"
    )

    p_view = sub.add_parser("view", help="show one item in full")
    p_view.add_argument("id", type=int)

    p_search = sub.add_parser("search", help="full-text search (bare words match prefixes)")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("--limit", type=int, default=20)

    p_note = sub.add_parser("note", help="add your analysis to an item")
    p_note.add_argument("id", type=int)
    p_note.add_argument("body")

    p_field = sub.add_parser("field", help="add a manual structured field")
    p_field.add_argument("id", type=int)
    p_field.add_argument("name")
    p_field.add_argument("value")

    p_export = sub.add_parser("export", help="CSV snapshot for Excel")
    p_export.add_argument("--out", default="out/research-items.csv")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args()
    root = Path(__file__).resolve().parent.parent
    load_project_env(root)

    try:
        storage = StorageConfig.from_env(root, args.db)

        if args.command == "note":
            return cmd_note(storage, args)
        if args.command == "field":
            return cmd_field(storage, args)
        if args.command == "export":
            return cmd_export(storage, args)

        with open_read_store(storage) as reader:
            return {
                "stats": cmd_stats,
                "list": cmd_list,
                "view": cmd_view,
                "search": cmd_search,
            }[args.command](reader, args)
    except KeyError as e:
        # KeyError stringifies as "'no such item'" -- strip the quotes it adds,
        # and only for KeyError, or a message that legitimately ends in a quote
        # ("got 'nope'") gets silently truncated.
        print(str(e).strip("'"), file=sys.stderr)
        return 1
    except (ConfigError, FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
