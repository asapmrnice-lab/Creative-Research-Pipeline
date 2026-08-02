"""Dry-run the collection-time keyword filter against the trial archive.

Shows what the filter WOULD have kept, using posts already downloaded by the
trial tool. Read-only: it never writes to the archive.

    python scripts/keyword_report.py [--samples N]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from research_pipeline.env import load_project_env  # noqa: E402
from research_pipeline.filtering import KeywordFilter, KeywordFilterConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()

    # Cyrillic output on a cp1252 Windows console would otherwise raise.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(__file__).resolve().parent.parent
    load_project_env(root)

    db_path = os.environ.get("TRIAL_DB_PATH")
    if not db_path:
        print("TRIAL_DB_PATH is not set (see .env.example)", file=sys.stderr)
        return 1

    kf = KeywordFilter(KeywordFilterConfig.from_env())
    print(f"keywords    : {', '.join(kf.config.keywords)}")
    print(f"match       : {kf.config.match}")
    print(f"inflections : {kf.config.allow_russian_inflections}")
    print()

    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    # Empty text is not text: media-only posts have text = '' and can never
    # match a keyword, so counting them would understate the keep rate.
    rows = conn.execute(
        "SELECT id, text FROM messages WHERE text IS NOT NULL AND text != ''"
    ).fetchall()

    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    with_text = len(rows)
    kept: list[tuple[int, str]] = []
    per_keyword: Counter[str] = Counter()
    per_form: Counter[str] = Counter()

    for msg_id, text in rows:
        hits = kf.find(text)
        if not hits:
            continue
        kept.append((msg_id, text))
        for keyword in {h.keyword for h in hits}:
            per_keyword[keyword] += 1
        for h in hits:
            per_form[h.matched_text] += 1

    pct = (len(kept) / with_text * 100) if with_text else 0.0
    print(f"messages in archive : {total}")
    print(f"  with text         : {with_text}")
    print(f"  media-only        : {total - with_text}  (no text to match on)")
    print(f"  KEPT by filter    : {len(kept)}  ({pct:.1f}% of texted posts)")
    print(f"  dropped           : {with_text - len(kept)}")
    print()

    if per_keyword:
        print("hits per keyword (posts):")
        for keyword, count in per_keyword.most_common():
            print(f"  {keyword:<10} {count}")
        print()
        print("word forms actually seen:")
        for form, count in per_form.most_common(15):
            print(f"  {form:<16} {count}")
        print()

    for msg_id, text in kept[: args.samples]:
        snippet = " ".join(text.split())[:220]
        print(f"--- msg {msg_id}\n{snippet}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
