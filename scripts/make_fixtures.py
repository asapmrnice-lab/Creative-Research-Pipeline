"""Freeze real archive posts into a test fixture.

The unit tests must run locally with no database and no network, but they
should be judged against text the filter will actually meet in production --
not text invented to make the filter look good. This script reads the trial
archive (read-only) and writes the posts it picks to a JSON fixture that gets
committed alongside the tests.

Posts are stored as excerpts, and every expectation is computed from the
stored excerpt, so the fixture is self-consistent even though it is a window
onto a longer post.

    python scripts/make_fixtures.py
    python scripts/make_fixtures.py --merge   # keep posts already frozen
    python scripts/make_fixtures.py --out tests/fixtures/real_posts.json

--merge exists because the archive is a moving window, not an accumulating
one: a trial that re-collects a different date range deletes the posts the
current fixture was cut from. Without merging, regenerating after such a run
silently narrows the test corpus to whatever is in the archive today. The
corpus counts always describe the live archive either way -- only the frozen
excerpts accumulate.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from research_pipeline.env import load_project_env  # noqa: E402
from research_pipeline.filtering import (  # noqa: E402
    KeywordFilter,
    KeywordFilterConfig,
    normalize,
)

# How much of a post to keep. Long enough to read as a real post, short enough
# that the fixture stays reviewable in a diff.
EXCERPT = 420
NON_MATCHING_SAMPLES = 20


def excerpt_around(text: str, start: int, end: int) -> str:
    """A readable window that is guaranteed to still contain the hit.

    Offsets come from the normalized text, but NFKC can change length, so the
    window is widened rather than trusted to line up exactly.
    """
    pad = max(0, (EXCERPT - (end - start)) // 2)
    return text[max(0, start - pad) : end + pad].strip()


def merge_records(previous: list[dict], fresh: list[dict]) -> list[dict]:
    """Fresh records win; previously frozen ones are kept if still unique.

    Identity is (channel, msg_id, text): the same message id can be reused by
    a later archive of a different date range, and an excerpt may be re-cut
    around a different hit, so the text has to take part in the key.
    """
    def key(r: dict) -> tuple:
        return (r.get("channel"), r.get("msg_id"), r.get("text", "")[:80])

    seen = {key(r) for r in fresh}
    return fresh + [r for r in previous if key(r) not in seen]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tests/fixtures/real_posts.json")
    parser.add_argument(
        "--merge",
        action="store_true",
        help="keep posts already in the fixture (the archive may have moved on)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    load_project_env(root)

    config = KeywordFilterConfig.from_env()
    strict = KeywordFilter(config)
    loose = KeywordFilter(
        KeywordFilterConfig(keywords=config.keywords, match="substring")
    )

    db_path = Path(os.environ["TRIAL_DB_PATH"]).resolve()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    chats = {cid: title for cid, title in conn.execute("SELECT id, title FROM chats")}
    rows = conn.execute(
        "SELECT id, chat_id, date, text FROM messages "
        "WHERE text IS NOT NULL AND text != '' ORDER BY chat_id, id"
    ).fetchall()

    matching: list[dict] = []
    non_matching: list[dict] = []
    near_miss: list[dict] = []
    forms_covered: set[str] = set()

    for msg_id, chat_id, date, text in rows:
        record = {
            "msg_id": msg_id,
            "channel": chats.get(chat_id, str(chat_id)),
            "date": str(date)[:16],
        }
        hits = strict.find(text)
        loose_hits = loose.find(text)

        if hits:
            # Every distinct surface form the archive contains gets pinned by a
            # test. The window is centred on the hit that introduces the form,
            # and coverage is credited from the stored excerpt only -- crediting
            # it from the full post would "cover" forms that fell outside the
            # window and never actually get asserted.
            for hit in hits:
                if hit.matched_text in forms_covered:
                    continue
                body = excerpt_around(text, hit.start, hit.end)
                body_forms = {h.matched_text for h in strict.find(body)}
                if hit.matched_text not in body_forms:
                    continue
                forms_covered |= body_forms
                matching.append(
                    {
                        **record,
                        "text": body,
                        "expect_keywords": strict.matched_keywords(body),
                        "expect_forms": sorted(body_forms),
                    }
                )
        elif loose_hits:
            # Contains a keyword inside a longer word ("антикейс"). Whole-word
            # mode rejects these; the fixture records that as a decision.
            hit = loose_hits[0]
            body = excerpt_around(text, hit.start, hit.end)
            if strict.find(body):
                continue
            record["text"] = body
            record["substring_keyword"] = hit.keyword
            record["context"] = normalize(text)[
                max(0, hit.start - 20) : hit.end + 20
            ].strip()
            near_miss.append(record)
        else:
            record["text"] = text.strip()[:EXCERPT]
            non_matching.append(record)

    # Spread the negatives across the whole corpus rather than taking the
    # first N, which would all come from one channel and one time period.
    if len(non_matching) > NON_MATCHING_SAMPLES:
        step = len(non_matching) / NON_MATCHING_SAMPLES
        non_matching = [
            non_matching[int(i * step)] for i in range(NON_MATCHING_SAMPLES)
        ]

    total_with_text = len(rows)
    total_matched = sum(1 for _, _, _, t in rows if strict.matches(t))

    out_path = root / args.out
    if args.merge and out_path.exists():
        previous = json.loads(out_path.read_text(encoding="utf-8"))
        matching = merge_records(previous.get("matching", []), matching)
        non_matching = merge_records(previous.get("non_matching", []), non_matching)
        near_miss = merge_records(previous.get("near_miss", []), near_miss)

    fixture = {
        "_comment": (
            "Generated by scripts/make_fixtures.py from the trial Telegram "
            "archive. Real post text, excerpted. Do not hand-edit -- "
            "regenerate instead."
        ),
        "config": {
            "keywords": list(config.keywords),
            "match": config.match,
            "allow_russian_inflections": config.allow_russian_inflections,
        },
        "corpus": {
            "posts_with_text": total_with_text,
            "posts_matching": total_matched,
            "posts_not_matching": total_with_text - total_matched,
        },
        "matching": matching,
        "non_matching": non_matching,
        "near_miss": near_miss,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {len(matching)} matching / {len(non_matching)} non-matching / "
        f"{len(near_miss)} near-miss -> {out_path}"
    )
    print(
        f"corpus: {total_matched}/{total_with_text} posts carry a keyword "
        f"({total_with_text - total_matched} were parsed without one)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
