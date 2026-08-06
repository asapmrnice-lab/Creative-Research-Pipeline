"""Duplicate detection. Flags candidates, never deletes anything."""

from .hashing import (
    DedupConfig,
    NearDuplicate,
    NearDuplicateIndex,
    hamming,
    simhash,
    simhash_hex,
)

__all__ = [
    "DedupConfig",
    "NearDuplicate",
    "NearDuplicateIndex",
    "hamming",
    "simhash",
    "simhash_hex",
]
