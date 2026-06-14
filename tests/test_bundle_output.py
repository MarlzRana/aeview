from __future__ import annotations

from pathlib import Path

from aeview.bundle import build_bundle
from aeview.prompt import compose_prompt
from aeview.resolve import Reviewer
from aeview.runstore import RunStore, new_run_id
from aeview.schema import ScopeSpec
from aeview.scope import ResolvedScope

_BIG_DIFF = (
    "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -0,0 +1 @@\n"
    + "+padding line to push the diff over the inline threshold\n" * 8000
)


def _self_collect_bundle():
    resolved = ResolvedScope(
        spec=ScopeSpec(type="branch", base="main"),
        diff=_BIG_DIFF,
        summary="1 file(s) changed:\n  big.py  +8000 -0",
        inspect=["git diff main..HEAD"],
        commits="",
    )
    bundle = build_bundle(resolved)
    assert bundle.mode == "self-collect"  # precondition for this test
    return bundle


def _reviewer(*, source: Path = Path("."), body: str = "REVIEW BODY") -> Reviewer:
    return Reviewer(name="default", description="d", body=body, source=source, harnesses=[])


def test_write_bundle_self_collect_writes_artifacts_and_returns_path(aeview_home):
    store = RunStore.create(new_run_id())
    bundle = _self_collect_bundle()

    full = store.write_bundle(bundle)

    assert full is not None
    assert full.name == "self_collect.diff"
    assert full.read_text() == bundle.diff  # full diff frozen on disk
    md = (store.bundle_dir / "self_collect_bundle.md").read_text()
    assert "big.py" in md  # summary present
    assert "git diff main..HEAD" in md  # inspect hint present
    assert not (store.bundle_dir / "inline_bundle.diff").exists()  # not inline mode


def test_compose_prompt_self_collect_embeds_path_and_inspect(tmp_path):
    bundle = _self_collect_bundle()
    full = tmp_path / "self_collect.diff"
    prompt = compose_prompt(_reviewer(), bundle, full)

    assert "too large to inline" in prompt
    assert str(full) in prompt  # tells the harness where the full diff lives
    assert "git diff main..HEAD" in prompt  # inspect command
    assert "big.py" in prompt  # summary
    assert bundle.diff not in prompt  # the huge diff is NOT embedded


def test_compose_prompt_self_collect_path_fallback():
    bundle = _self_collect_bundle()
    prompt = compose_prompt(_reviewer(), bundle, None)
    assert "see the run's bundle/ directory" in prompt


def _resource_lead(src: Path) -> str:
    # The exact block compose_prompt must lead with: pins the base path to the reviewer DIRECTORY
    # (not the REVIEWER.md file or a subpath) and its termination before the body.
    return f"All relative paths in this reviewer's instructions are relative to:\n  {src}\n\n"


def _inline_bundle():
    resolved = ResolvedScope(
        spec=ScopeSpec(type="working-tree", base="HEAD"),
        diff="diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
        summary="1 file(s) changed:\n  x.py  +1 -1",
        inspect=["git diff HEAD"],
        commits="",
    )
    bundle = build_bundle(resolved)
    assert bundle.is_inline  # precondition
    return bundle


def test_compose_prompt_leads_with_reviewer_resource_base(tmp_path):
    # N4: the reviewer's own dir is injected at the TOP so relative links in the body resolve there,
    # ahead of the body and the read-only guard.
    src = tmp_path / ".aeview" / "reviewers" / "r"
    prompt = compose_prompt(_reviewer(source=src), _inline_bundle())

    assert prompt.startswith(_resource_lead(src))  # exact dir, not a substring/file path
    assert prompt.index(str(src)) < prompt.index("REVIEW BODY")
    assert prompt.index(str(src)) < prompt.index("Operating rules (read-only)")


def test_compose_prompt_resource_base_present_in_self_collect(tmp_path):
    # The base path leads regardless of bundle mode.
    src = tmp_path / "rev"
    prompt = compose_prompt(_reviewer(source=src), _self_collect_bundle(), tmp_path / "x")
    assert prompt.startswith(_resource_lead(src))
