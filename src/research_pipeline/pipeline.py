"""Source -> Gate -> Store.

The whole point of this module is the order of those three steps: the gate is
consulted *before* the store is touched, so a post without a keyword is never
written and its media is never fetched. Nothing downstream of the gate ever
sees a rejected post.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .filtering.gate import Verdict
from .protocols import Gate, Source, Store


@dataclass
class IngestResult:
    """What a run did, in numbers that add up."""

    seen: int = 0
    collected: int = 0
    duplicates: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    @property
    def stored(self) -> int:
        return self.collected - self.duplicates

    def _reject(self, verdict: Verdict) -> None:
        self.rejected[verdict.value] = self.rejected.get(verdict.value, 0) + 1

    def check(self) -> None:
        """Every post must be accounted for exactly once."""
        assert self.seen == self.collected + sum(self.rejected.values())


def ingest(source: Source, gate: Gate, store: Store) -> IngestResult:
    result = IngestResult()
    for post in source.fetch():
        result.seen += 1

        decision = gate.evaluate(post)
        if not decision.collect:
            result._reject(decision.verdict)
            continue

        result.collected += 1
        if not store.save(post, decision.keywords):
            result.duplicates += 1

    result.check()
    return result
