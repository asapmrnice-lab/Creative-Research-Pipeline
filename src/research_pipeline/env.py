"""Minimal .env loading.

Deliberately dependency-free: the scripts and tests need to read a handful of
KEY=value lines, not a full dotenv implementation. Existing environment
variables always win, so an exported value can override the file.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path) -> None:
    """Load KEY=value lines from `path` into os.environ, without overwriting."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def load_project_env(root: Path) -> None:
    """Load the project's .env, falling back to .env.example for defaults.

    .env is read first so its values take precedence; .env.example then fills
    in anything the user has not overridden.
    """
    load_env(root / ".env")
    load_env(root / ".env.example")
