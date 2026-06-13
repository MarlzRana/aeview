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


def _settings():
    return Settings(
        fallback_reviewer_harnesses=[HarnessInstance(harness="claude-code", model="sonnet")],
        deduplication_harness=HarnessInstance(harness="claude-code", model="sonnet"),
    )


def _run_sync_rc(rc: int):
    return lambda args, cwd=None, timeout=None: ProcResult(rc, "", "")


def _all_present(monkeypatch, *, authed=True):
    monkeypatch.setattr(doctor, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0 if authed else 1))


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_doctor_all_ok(tmp_path, monkeypatch):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    _all_present(monkeypatch)
    report = doctor.run_doctor(tmp_path, _settings())
    assert report.ok
    assert _check(report, "harness:claude-code").status == "ok"
    assert _check(report, "reviewer:good").status == "ok"


def test_doctor_missing_binary_fails(tmp_path, monkeypatch):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    monkeypatch.setattr(doctor, "which", lambda binary: None)
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))
    report = doctor.run_doctor(tmp_path, _settings())
    assert not report.ok
    assert _check(report, "harness:claude-code").status == "fail"


def test_doctor_invalid_reviewer_config_fails(tmp_path, monkeypatch):
    # A harness entry missing its required `model` fails frontmatter validation → reviewer fail.
    make_reviewer(tmp_path, "bad", harnesses=[{"harness": "claude-code"}])
    _all_present(monkeypatch)
    report = doctor.run_doctor(tmp_path, _settings())
    assert not report.ok
    assert _check(report, "reviewer:bad").status == "fail"


def test_doctor_unverified_auth_is_warn_not_fail(tmp_path, monkeypatch):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    _all_present(monkeypatch, authed=False)
    report = doctor.run_doctor(tmp_path, _settings())
    assert report.ok  # auth-unverified is a warning, not a failure
    assert _check(report, "harness:claude-code").status == "warn"


def test_doctor_missing_gh_is_warn(tmp_path, monkeypatch):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    monkeypatch.setattr(doctor, "which", lambda binary: None if binary == "gh" else f"/b/{binary}")
    monkeypatch.setattr(doctor, "run_sync", _run_sync_rc(0))
    report = doctor.run_doctor(tmp_path, _settings())
    assert report.ok  # gh only needed for --scope pr
    assert _check(report, "gh").status == "warn"


def test_doctor_auth_probes_are_bounded_by_a_timeout(tmp_path, monkeypatch):
    # Guards the boundary: doctor must pass a timeout so a wedged auth CLI can't hang it.
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    monkeypatch.setattr(doctor, "which", lambda binary: f"/b/{binary}")
    seen_timeouts = []

    def record(args, cwd=None, timeout=None):
        seen_timeouts.append(timeout)
        return ProcResult(0, "", "")

    monkeypatch.setattr(doctor, "run_sync", record)
    doctor.run_doctor(tmp_path, _settings())
    assert seen_timeouts  # probes ran
    assert all(t is not None and t > 0 for t in seen_timeouts)  # every probe bounded


def test_doctor_only_checks_referenced_harnesses(tmp_path, monkeypatch):
    # A reviewer using only claude -> codex is never checked (its absence isn't a problem).
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "sonnet"}])
    _all_present(monkeypatch)
    report = doctor.run_doctor(tmp_path, _settings())
    assert not any(c.name == "harness:codex" for c in report.checks)


def test_doctor_copilot_warns_without_a_billed_auth_call(tmp_path, monkeypatch):
    # copilot has no no-cost auth-status command (empty auth_status_args). doctor must warn it's
    # present-but-unverifiable WITHOUT invoking run_sync (which would be a billed/erroring call).
    make_reviewer(tmp_path, "cop", harnesses=[{"harness": "copilot", "model": "gpt-5.4"}])
    monkeypatch.setattr(doctor, "which", lambda binary: f"/usr/bin/{binary}")
    run_sync_calls = []

    def record(args, cwd=None, timeout=None):
        run_sync_calls.append(args)
        return ProcResult(0, "", "")

    monkeypatch.setattr(doctor, "run_sync", record)

    report = doctor.run_doctor(tmp_path, _settings())

    check = _check(report, "harness:copilot")
    assert check.status == "warn"
    assert "auth not verifiable" in check.detail
    assert all(args != [] for args in run_sync_calls)  # never probed copilot's empty auth args
