from __future__ import annotations

from typing import cast

import pytest

from aeview import doctor
from aeview.config import HarnessInstance, Settings
from aeview.process import ProcResult
from conftest import make_reviewer


@pytest.fixture(autouse=True)
def _isolate_home(aeview_home):
    # discover_reviewers climbs to ~/.aeview, so isolate HOME to a temp dir — otherwise these
    # tests pick up the developer's real global reviewers (and their harnesses).
    return aeview_home


def _settings(dedup_harness: str = "claude-code"):
    return Settings(
        fallback_reviewer_harnesses=[HarnessInstance(harness="claude-code", model="sonnet")],
        deduplication_harness=HarnessInstance(harness=dedup_harness, model="sonnet"),
    )


def _run_sync_rc(rc: int):
    return lambda args, cwd=None, timeout=None: ProcResult(rc, "", "")


def _mock_seams(monkeypatch, *, authed=True):
    """Mock the SDK-resolving adapters' preflight seams: claude_code.run_sync (claude's bundled-
    binary auth probe), codex.which/run_sync (codex's override/bundled resolution + probe), and
    doctor.which/run_sync (gh). claude, codex AND copilot all resolve their bundled binaries (so a
    missing-binary case must use a bad override — see test_doctor_bad_override_fails); copilot has
    no auth probe, so its real bundle → warn."""
    from aeview.harness import claude_code, codex

    def which_fn(binary):
        return f"/usr/bin/{binary}"

    rs = _run_sync_rc(0 if authed else 1)
    monkeypatch.setattr(claude_code, "run_sync", rs)
    monkeypatch.setattr(codex, "which", which_fn)
    monkeypatch.setattr(codex, "run_sync", rs)
    monkeypatch.setattr(doctor, "which", which_fn)
    monkeypatch.setattr(doctor, "run_sync", rs)


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_doctor_all_ok(tmp_path, monkeypatch):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    _mock_seams(monkeypatch)
    report = doctor.run_doctor(tmp_path, _settings())
    assert report.ok
    assert _check(report, "harness:claude-code").status == "ok"
    assert _check(report, "reviewer:good").status == "ok"


def test_doctor_codex_ok(tmp_path, monkeypatch):
    # codex resolves its bundled binary (need not be on PATH) and probes auth: rc0 -> ok.
    make_reviewer(tmp_path, "cx", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    _mock_seams(monkeypatch)
    report = doctor.run_doctor(tmp_path, _settings(dedup_harness="codex"))
    assert _check(report, "harness:codex").status == "ok"


def test_doctor_bad_override_fails(tmp_path, monkeypatch):
    # Post-N5c no adapter is PATH-gated (claude/codex/copilot all resolve bundled binaries), so a
    # missing binary now means a bad OVERRIDE that doesn't resolve: copilot's preflight resolves the
    # override via which → None → fail.
    from aeview.harness import copilot

    make_reviewer(tmp_path, "cop", harnesses=[{"harness": "copilot", "model": "gpt-5.4"}])
    monkeypatch.setattr(copilot, "which", lambda b: None)  # the override doesn't resolve
    monkeypatch.setattr(doctor, "which", lambda b: f"/b/{b}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))
    settings = Settings(
        deduplication_harness=HarnessInstance(harness="copilot", model="gpt-5.4"),
        override_harness_binaries={"copilot": "/nonexistent/copilot"},
    )
    report = doctor.run_doctor(tmp_path, settings)
    assert not report.ok
    assert _check(report, "harness:copilot").status == "fail"


def test_doctor_invalid_reviewer_config_fails(tmp_path, monkeypatch):
    # A harness entry missing its required `model` fails frontmatter validation → reviewer fail.
    make_reviewer(tmp_path, "bad", harnesses=[{"harness": "claude-code"}])
    _mock_seams(monkeypatch)
    report = doctor.run_doctor(tmp_path, _settings())
    assert not report.ok
    assert _check(report, "reviewer:bad").status == "fail"


def test_doctor_unverified_auth_is_warn_not_fail(tmp_path, monkeypatch):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    _mock_seams(monkeypatch, authed=False)
    report = doctor.run_doctor(tmp_path, _settings())
    assert report.ok  # auth-unverified is a warning, not a failure
    assert _check(report, "harness:claude-code").status == "warn"


def test_doctor_missing_gh_is_warn(tmp_path, monkeypatch):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    _mock_seams(monkeypatch)
    monkeypatch.setattr(doctor, "which", lambda binary: None if binary == "gh" else f"/b/{binary}")
    report = doctor.run_doctor(tmp_path, _settings())
    assert report.ok  # gh only needed for --scope pr
    assert _check(report, "gh").status == "warn"


def test_doctor_claude_auth_probe_is_bounded_by_a_timeout(tmp_path, monkeypatch):
    # Guards the boundary: claude's preflight must bound its auth probe so a wedged CLI can't hang.
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    _mock_seams(monkeypatch)
    seen_timeouts = []

    def record(args, cwd=None, timeout=None):
        seen_timeouts.append(timeout)
        return ProcResult(0, "", "")

    from aeview.harness import claude_code

    monkeypatch.setattr(claude_code, "run_sync", record)
    doctor.run_doctor(tmp_path, _settings())
    assert seen_timeouts  # the probe ran
    assert all(t is not None and t > 0 for t in seen_timeouts)  # bounded


def test_doctor_only_checks_referenced_harnesses(tmp_path, monkeypatch):
    # A reviewer using only claude -> codex is never checked (its absence isn't a problem).
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    _mock_seams(monkeypatch)
    report = doctor.run_doctor(tmp_path, _settings())
    assert not any(c.name == "harness:codex" for c in report.checks)


def test_doctor_copilot_warns_present_but_unverifiable(tmp_path, monkeypatch):
    # copilot has no no-cost auth-status command, so its SDK-aware preflight resolves the bundled
    # binary and warns (present, auth unverifiable) — there is no run_sync in copilot's path at all,
    # so the "no billed call" property is structural, not asserted on a spy.
    make_reviewer(tmp_path, "cop", harnesses=[{"harness": "copilot", "model": "gpt-5.4"}])
    monkeypatch.setattr(doctor, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))

    report = doctor.run_doctor(tmp_path, _settings(dedup_harness="copilot"))

    check = _check(report, "harness:copilot")
    assert check.status == "warn"
    assert "auth not verifiable" in check.detail


def test_doctor_passes_binary_override_to_the_adapter(tmp_path, monkeypatch):
    # settings.overrideHarnessBinaries reaches the harness check: codex's preflight resolves
    # the OVERRIDE path (via which), not the bundled binary.
    from aeview.harness import codex

    make_reviewer(tmp_path, "cx", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    checked: list[str] = []

    def which_fn(binary):
        checked.append(binary)
        return f"/usr/bin{binary}"

    monkeypatch.setattr(codex, "which", which_fn)
    monkeypatch.setattr(codex, "run_sync", _run_sync_rc(0))
    monkeypatch.setattr(doctor, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))
    settings = Settings(
        deduplication_harness=HarnessInstance(harness="codex", model="sonnet"),
        override_harness_binaries={"codex": "/opt/codex"},
    )
    doctor.run_doctor(tmp_path, settings)
    assert "/opt/codex" in checked  # the override path is what was resolved


def test_doctor_codex_auth_probe_is_bounded(tmp_path, monkeypatch):
    # codex's preflight must bound its auth probe so a wedged CLI can't hang doctor.
    from aeview.harness import codex

    make_reviewer(tmp_path, "cx", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    calls: list = []

    def record(args, cwd=None, timeout=None):
        calls.append((args, timeout))
        return ProcResult(0, "", "")

    monkeypatch.setattr(codex, "run_sync", record)
    monkeypatch.setattr(doctor, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))
    doctor.run_doctor(tmp_path, _settings(dedup_harness="codex"))
    assert calls and all(t is not None and t > 0 for _, t in calls)  # every probe bounded
    # the probe is the resolved binary + its auth subcommand, not a bare subcommand
    assert all(args[-2:] == ["login", "status"] for args, _ in calls)


# --- default_preflight (generic PATH-gated check; no SDK adapter calls it now) ---


class _PathGatedAdapter:
    """A PATH-gated adapter shape: a named binary that must be on PATH + an optional no-cost auth
    probe. default_preflight reads only these two."""

    def __init__(self, binary: str = "cli", auth_status_args: list[str] | None = None) -> None:
        self.binary = binary
        self.auth_status_args = auth_status_args or []


def _preflight(adapter: _PathGatedAdapter):
    from aeview.harness import base

    return base.default_preflight(cast("base.Adapter", adapter))


def test_default_preflight_present_and_authed_is_ok(monkeypatch):
    from aeview.harness import base

    monkeypatch.setattr(base, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(base, "run_sync", _run_sync_rc(0))
    assert _preflight(_PathGatedAdapter(auth_status_args=["auth", "status"])).status == "ok"


def test_default_preflight_present_without_probe_is_warn(monkeypatch):
    from aeview.harness import base

    monkeypatch.setattr(base, "which", lambda b: f"/usr/bin/{b}")
    # No auth_status_args → present but unverifiable → warn, with no billed call.
    assert _preflight(_PathGatedAdapter(auth_status_args=[])).status == "warn"


def test_default_preflight_auth_probe_fails_is_warn(monkeypatch):
    from aeview.harness import base

    monkeypatch.setattr(base, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(base, "run_sync", _run_sync_rc(1))  # probe says not authed
    assert _preflight(_PathGatedAdapter(auth_status_args=["auth", "status"])).status == "warn"


def test_default_preflight_missing_binary_is_fail(monkeypatch):
    from aeview.harness import base

    monkeypatch.setattr(base, "which", lambda b: None)  # not on PATH
    assert _preflight(_PathGatedAdapter()).status == "fail"
