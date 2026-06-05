"""Build the review bundle from a resolved scope.

Increment 1 always uses inline mode: the diff is frozen into `bundle/inline_bundle.diff`
and embedded directly in the prompt, so every review of the same change sees identical
input. Adaptive measure -> self-collect for large diffs arrives in I2.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .schema import ScopeSpec
from .scope import collect_diff


@dataclass(slots=True)
class Bundle:
    mode: str  # "inline" (I1) | "self-collect" (I2)
    scope: ScopeSpec
    diff: str

    @property
    def is_empty(self) -> bool:
        return not self.diff.strip()

    def manifest(self) -> dict:
        return {
            "mode": self.mode,
            "scope": self.scope.model_dump(),
            "diff_bytes": len(self.diff.encode("utf-8")),
        }


def build_bundle(scope_type: str, cwd: Path) -> Bundle:
    scope, diff = collect_diff(scope_type, cwd)
    return Bundle(mode="inline", scope=scope, diff=diff)
