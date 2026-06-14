from __future__ import annotations

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


def _mock_seams(monkeypatch, *, present=True, authed=True):
    """Mock all three preflight seams: base.which/run_sync (codex/copilot default_preflight),
    claude_code.run_sync (claude's bundled-binary auth probe), and doctor.which/run_sync (gh). The
    claude adapter resolves its bundled binary regardless of `present`, so a missing-binary case
    must use codex/copilot (which gate on PATH)."""
    from aeview.harness import base, claude_code

    def which_fn(binary):
        return f"/usr/bin/{binary}" if present else None

    rs = _run_sync_rc(0 if authed else 1)
    monkeypatch.setattr(base, "which", which_fn)
    monkeypatch.setattr(base, "run_sync", rs)
    monkeypatch.setattr(claude_code, "run_sync", rs)
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
    # codex uses the shared PATH+auth default_preflight: present binary + rc0 auth -> ok.
    make_reviewer(tmp_path, "cx", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    _mock_seams(monkeypatch)
    report = doctor.run_doctor(tmp_path, _settings(dedup_harness="codex"))
    assert _check(report, "harness:codex").status == "ok"


def test_doctor_missing_binary_fails(tmp_path, monkeypatch):
    # codex (PATH-gated) is the one that can fail on a missing binary; claude resolves its bundle.
    make_reviewer(tmp_path, "cx", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    _mock_seams(monkeypatch, present=False)
    report = doctor.run_doctor(tmp_path, _settings(dedup_harness="codex"))
    assert not report.ok
    assert _check(report, "harness:codex").status == "fail"


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


def test_doctor_copilot_warns_without_a_billed_auth_call(tmp_path, monkeypatch):
    # copilot has no no-cost auth-status command (empty auth_status_args). default_preflight must
    # warn it's present-but-unverifiable WITHOUT invoking run_sync (which would be a billed call).
    make_reviewer(tmp_path, "cop", harnesses=[{"harness": "copilot", "model": "gpt-5.4"}])
    from aeview.harness import base

    monkeypatch.setattr(base, "which", lambda binary: f"/usr/bin/{binary}")
    base_run_sync_calls = []

    def record(args, cwd=None, timeout=None):
        base_run_sync_calls.append(args)
        return ProcResult(0, "", "")

    monkeypatch.setattr(base, "run_sync", record)
    monkeypatch.setattr(doctor, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))

    report = doctor.run_doctor(tmp_path, _settings(dedup_harness="copilot"))

    check = _check(report, "harness:copilot")
    assert check.status == "warn"
    assert "auth not verifiable" in check.detail
    assert base_run_sync_calls == []  # copilot's empty auth args -> never probed


def test_doctor_passes_binary_override_to_the_adapter(tmp_path, monkeypatch):
    # settings.harnessBinaries reaches the harness check: codex's default_preflight gates on the
    # OVERRIDE path, not the default "codex".
    from aeview.harness import base

    make_reviewer(tmp_path, "cx", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    checked: list[str] = []

    def which_fn(binary):
        checked.append(binary)
        return f"/usr/bin/{binary}"

    monkeypatch.setattr(base, "which", which_fn)
    monkeypatch.setattr(base, "run_sync", _run_sync_rc(0))
    monkeypatch.setattr(doctor, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))
    settings = Settings(
        deduplication_harness=HarnessInstance(harness="codex", model="sonnet"),
        harness_binaries={"codex": "/opt/codex"},
    )
    doctor.run_doctor(tmp_path, settings)
    assert "/opt/codex" in checked  # the override path is what was probed on PATH


def test_doctor_default_preflight_auth_probe_is_bounded(tmp_path, monkeypatch):
    # codex/copilot default_preflight must bound the auth probe so a wedged CLI can't hang doctor.
    from aeview.harness import base

    make_reviewer(tmp_path, "cx", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    calls: list = []

    def record(args, cwd=None, timeout=None):
        calls.append((args, timeout))
        return ProcResult(0, "", "")

    monkeypatch.setattr(base, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(base, "run_sync", record)
    monkeypatch.setattr(doctor, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))
    doctor.run_doctor(tmp_path, _settings(dedup_harness="codex"))
    assert calls and all(t is not None and t > 0 for _, t in calls)  # every probe bounded
    # the probe is the binary (PATH-resolved at spawn) + its auth subcommand, not a bare subcommand
    assert any(args == ["codex", "login", "status"] for args, _ in calls)
