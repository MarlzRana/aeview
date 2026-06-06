"""Adaptive bundling: measure the diff, then inline or self-collect.

- diff <= 256 KB        -> inline: the frozen diff is embedded in the prompt, so every
                           review of the same change sees byte-identical input.
- diff >  256 KB        -> self-collect: the prompt carries a stat summary + the path to
                           the frozen diff (which the harness reads selectively) and the
                           read-only git command to inspect the live tree — keeping the
                           prompt small on huge changes.
- patch scope           -> always inline (there is no git to self-collect from).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schema import ScopeSpec
from .scope import ResolvedScope

INLINE_MAX_BYTES = 256 * 1024


@dataclass(slots=True)
class Bundle:
    mode: str  # "inline" | "self-collect"
    scope: ScopeSpec
    diff: str
    summary: str
    inspect: list[str] = field(default_factory=list)
    commits: str = ""
    diff_bytes: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.diff.strip()

    @property
    def is_inline(self) -> bool:
        return self.mode == "inline"

    def manifest(self) -> dict:
        return {
            "mode": self.mode,
            "scope": self.scope.model_dump(),
            "diff_bytes": self.diff_bytes,
            "summary": self.summary,
            "inspect": self.inspect,
        }


def build_bundle(resolved: ResolvedScope) -> Bundle:
    diff_bytes = len(resolved.diff.encode("utf-8"))
    inline = resolved.inline_only or diff_bytes <= INLINE_MAX_BYTES
    return Bundle(
        mode="inline" if inline else "self-collect",
        scope=resolved.spec,
        diff=resolved.diff,
        summary=resolved.summary,
        inspect=resolved.inspect,
        commits=resolved.commits,
        diff_bytes=diff_bytes,
    )
