"""Serve the research store as a local page.

The same store the CLI reads, in a browser: full post text with the matched
keywords highlighted, search, an un-reviewed filter, and a box to write a note
straight into the store.

    python scripts/serve_review.py            # then open the printed URL
    python scripts/serve_review.py --port 8123
    python scripts/serve_review.py --no-open  # don't launch a browser

Standard library only -- this project declares no runtime dependencies, and a
local viewer is not a good reason to take on a web framework.

Binds to loopback by default. The page has no authentication, so serving it on
a routable address would publish your collected research to the network.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from research_pipeline.env import load_project_env  # noqa: E402
from research_pipeline.filtering import KeywordFilter, KeywordFilterConfig  # noqa: E402
from research_pipeline.review import render_page  # noqa: E402
from research_pipeline.storage.reader import ResearchStoreReader  # noqa: E402
from research_pipeline.storage.sqlite_store import SqliteStore  # noqa: E402

# A single note is a sentence or two; anything vastly larger is a mistake or an
# abuse, and reading it into memory unbounded would be the bug.
MAX_NOTE_BYTES = 64 * 1024


def build_handler(store_path: Path, kf: KeywordFilter | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ResearchStore/1.0"

        def log_message(self, fmt, *args):  # quieter than the default one-line-per-asset
            sys.stderr.write(f"  {self.command} {self.path} -> {args[1]}\n")

        # -- responses ----------------------------------------------------

        def _send(self, body: str, status: int = 200) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _redirect(self, params: dict[str, str], anchor: str = "") -> None:
            query = urlencode({k: v for k, v in params.items() if v})
            self.send_response(303)
            self.send_header("Location", "/" + (f"?{query}" if query else "") + anchor)
            self.end_headers()

        # -- read ---------------------------------------------------------

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path not in ("/", "/index.html"):
                self._send("<h1>404</h1>", 404)
                return

            params = parse_qs(url.query)
            query = (params.get("q", [""])[0]).strip()
            unreviewed = bool(params.get("unreviewed", [""])[0])

            with ResearchStoreReader(store_path) as reader:
                stats = reader.stats()
                if query:
                    # Search decides the order (by relevance); the un-reviewed
                    # toggle then narrows it, so the two compose.
                    ids = [h.id for h in reader.search(query, limit=200)]
                    if unreviewed:
                        allowed = {i.id for i in reader.list_items(unreviewed_only=True)}
                        ids = [i for i in ids if i in allowed]
                else:
                    ids = [i.id for i in reader.list_items(unreviewed_only=unreviewed)]
                items = [item for item in (reader.get_item(i) for i in ids) if item]

            self._send(
                render_page(
                    stats,
                    items,
                    kf=kf,
                    query=query,
                    unreviewed=unreviewed,
                    db_label=store_path.name,
                )
            )

        # -- write --------------------------------------------------------

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/note":
                self._send("<h1>404</h1>", 404)
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_NOTE_BYTES:
                self._send("<h1>413 note too large</h1>", 413)
                return

            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            keep = {
                "q": form.get("q", [""])[0],
                "unreviewed": form.get("unreviewed", [""])[0],
            }
            try:
                item_id = int(form.get("id", ["0"])[0])
                body = form.get("body", [""])[0]
                with SqliteStore(store_path) as store:
                    store.add_note(item_id, body)
            except (ValueError, KeyError) as e:
                self._send(f"<h1>400</h1><p>{e}</p>", 400)
                return

            self._redirect(keep, anchor=f"#i{item_id}")

    return Handler


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(__file__).resolve().parent.parent
    load_project_env(root)

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="override STORE_DB_PATH")
    parser.add_argument("--host", default=os.environ.get("REVIEW_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("REVIEW_PORT", "8001")))
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    store_path = Path(args.db or os.environ.get("STORE_DB_PATH", "./data/research.db"))
    if not store_path.is_absolute():
        store_path = root / store_path

    try:
        with ResearchStoreReader(store_path) as reader:
            stats = reader.stats()
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    try:
        kf = KeywordFilter(KeywordFilterConfig.from_env())
    except ValueError:
        kf = None  # highlighting is optional; the page works without it

    httpd = ThreadingHTTPServer((args.host, args.port), build_handler(store_path, kf))
    url = f"http://{args.host}:{args.port}/"
    print(f"store : {store_path}")
    print(f"items : {stats.items} ({stats.unreviewed} un-reviewed)")
    print(f"serving {url}   (Ctrl-C to stop)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: not on loopback -- this page has no authentication.")

    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
