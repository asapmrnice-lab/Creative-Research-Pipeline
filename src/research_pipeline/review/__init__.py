"""The Review interface -- the human-facing half of the system.

Two front-ends over the same read-only store view: the CLI in
scripts/review.py, and the local page rendered here for scripts/serve_review.py.
Neither writes directly; notes and manual fields go through SqliteStore.
"""

from .html import highlight, render_page

__all__ = ["highlight", "render_page"]
