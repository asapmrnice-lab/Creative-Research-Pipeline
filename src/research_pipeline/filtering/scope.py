"""Which channels the gate is allowed to collect from.

The keyword list decides *what* is worth keeping. This decides *where* we are
looking at all. They are two rules, not one, because they fail for different
reasons and the human edits them on different occasions: swapping a channel
should not require touching the keyword list, and widening the keyword list
should not silently widen the set of channels being read.

Scope is matched on the platform's own id, never on a handle or a title. A
channel can be renamed and re-@'d at any time, but -1001107320646 is the same
channel forever -- and it is the same unit the collector is configured in, so
both lists are written the same way.

Empty means unrestricted, deliberately. An unset INGEST_CHANNELS has to keep
reading whatever the source yields, because that is what every existing run
does; a scope that failed closed on an absent value would turn an upgrade into
a silent collection stoppage. The run prints its scope either way, so
"unrestricted" is a visible state rather than an assumed one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from ..domain import RawSource


@dataclass(frozen=True)
class ChannelScope:
    """The platform ids the gate will collect from. Empty means every channel."""

    channels: tuple[str, ...] = ()

    @property
    def restricted(self) -> bool:
        return bool(self.channels)

    @property
    def label(self) -> str:
        """What to show a human, including the fact that there is no limit."""
        if not self.restricted:
            return "every channel in the source (INGEST_CHANNELS unset)"
        return ", ".join(self.channels)

    def allows(self, source: RawSource) -> bool:
        if not self.restricted:
            return True
        return str(source.platform_id).strip() in self.channels

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ChannelScope":
        env = os.environ if env is None else env
        raw = env.get("INGEST_CHANNELS", "")
        return cls(channels=tuple(c.strip() for c in raw.split(",") if c.strip()))
