"""Show backup progress as a percentage bar.

Polls the archive DB and compares against the expected message total from a
previous full run. Read-only -- safe to run while a backup is in flight.

    python scripts/backup_progress.py            # one snapshot
    python scripts/backup_progress.py --watch    # live bar until complete
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from research_pipeline.env import load_project_env  # noqa: E402

BAR_WIDTH = 40


def snapshot(db_path: Path) -> tuple[int, int, float]:
    """Return (messages, media_files, media_MB). Zeros if the DB isn't created yet."""
    if not db_path.exists():
        return 0, 0, 0.0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        media = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        size = conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM media").fetchone()[0]
        return messages, media, size / 1e6
    except sqlite3.OperationalError:
        return 0, 0, 0.0  # tables not created yet
    finally:
        conn.close()


def render(done: int, total: int, media: int, mb: float) -> str:
    pct = min(done / total, 1.0) if total else 0.0
    filled = int(pct * BAR_WIDTH)
    bar = "█" * filled + "░" * (BAR_WIDTH - filled)
    return f"[{bar}] {pct * 100:5.1f}%  {done}/{total} msgs  {media} media  {mb:.0f} MB"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="refresh until complete")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument(
        "--expected",
        type=int,
        default=int(os.environ.get("EXPECTED_MESSAGE_TOTAL", "0")) or None,
        help="expected message count (defaults to EXPECTED_MESSAGE_TOTAL in .env)",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(__file__).resolve().parent.parent
    load_project_env(root)

    db_path = Path(os.environ["TRIAL_DB_PATH"]).resolve()
    total = args.expected or int(os.environ.get("EXPECTED_MESSAGE_TOTAL", "0"))
    if not total:
        print("Set EXPECTED_MESSAGE_TOTAL in .env or pass --expected", file=sys.stderr)
        return 1

    if not args.watch:
        done, media, mb = snapshot(db_path)
        print(render(done, total, media, mb))
        return 0

    started = time.monotonic()
    last_done = 0
    while True:
        done, media, mb = snapshot(db_path)
        elapsed = time.monotonic() - started
        rate = (done - last_done) / args.interval if elapsed > args.interval else 0
        eta = f"  ~{(total - done) / rate / 60:.0f} min left" if rate > 0 else ""
        print("\r" + render(done, total, media, mb) + eta + "   ", end="", flush=True)
        if done >= total:
            print("\ncomplete")
            return 0
        last_done = done
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
