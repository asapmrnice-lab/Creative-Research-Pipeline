"""Write every keyword-matched post to a readable Markdown file.

Read-only against the archive. Safe to run while a backup is in flight.

    python scripts/export_matches.py
    python scripts/export_matches.py --out out/matched-posts.md
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from research_pipeline.env import load_project_env  # noqa: E402
from research_pipeline.filtering import KeywordFilter, KeywordFilterConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="out/matched-posts.md")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    load_project_env(root)

    kf = KeywordFilter(KeywordFilterConfig.from_env())
    db_path = Path(os.environ["TRIAL_DB_PATH"]).resolve()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    chats = {cid: title for cid, title in conn.execute("SELECT id, title FROM chats")}
    rows = conn.execute(
        "SELECT id, chat_id, date, text FROM messages "
        "WHERE text IS NOT NULL AND text != '' ORDER BY chat_id, date"
    ).fetchall()

    out_path = root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    kept = 0
    with out_path.open("w", encoding="utf-8") as f:
        f.write("# Keyword-matched posts\n\n")
        f.write(f"Keywords: `{', '.join(kf.config.keywords)}`  \n")
        f.write(f"Match: `{kf.config.match}`, inflections: `{kf.config.allow_russian_inflections}`\n\n")
        f.write("---\n\n")

        for msg_id, chat_id, date, text in rows:
            hits = kf.find(text)
            if not hits:
                continue
            kept += 1
            found = ", ".join(sorted({h.matched_text for h in hits}))
            channel = chats.get(chat_id, str(chat_id))
            f.write(f"## {kept}. {channel} — {str(date)[:16]}\n\n")
            f.write(f"**matched:** {found}  \n")
            f.write(f"**msg id:** {msg_id}\n\n")
            f.write("```\n" + text.strip() + "\n```\n\n")

    print(f"wrote {kept} matched posts -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
