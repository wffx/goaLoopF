"""LocalLinuxBackend tests: execution, timeout, lease enforcement, sandbox argv."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from goaloop.backend import LocalLinuxBackend, toolchain_capabilities
from goaloop.models import (
    ProcessRequest,
    ResourceLimits,
    RunContext,
    SandboxSettings,
    ValidationProfile,
)


def _profile(*, sandbox: bool = False, timeout: int = 10) -> ValidationProfile:
    return ValidationProfile(
        name="test",
        sandbox=SandboxSettings(required=sandbox),
        resources=ResourceLimits(timeout_seconds=timeout, max_commands=16),
    )


def _context(tmp_path: Path) -> RunContext:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source = tmp_path / "repos" / "safe"
    source.mkdir(parents=True)
    return RunContext(
        run_id="r",
        project_name="safe",
        run_dir=run_dir,
        source_root=source,
        candidate_dir=run_dir / "candidate",
    )


def _prepared(
    tmp_path: Path, *, sandbox: bool = False, timeout: int = 10, extra: list[str] | None = None
) -> LocalLinuxBackend:
    backend = LocalLinuxBackend(_profile(sandbox=sandbox, timeout=timeout))
    backend.prepare(_context(tmp_path))
    if extra:
        assert backend._lease is not None
        backend._lease.allowed_executables.extend(extra)
    return backend


def test_probe_reports_toolchain(tmp_path: Path) -> None:
    backend = LocalLinuxBackend(_profile())
    report = backend.probe(_profile())
    names = {item.name for item in report.capabilities}
    assert {"clang", "clangxx", "llvm_profdata", "llvm_cov"} <= names
    assert "bubblewrap" not in names  # not required


def test_toolchain_capabilities_require_bwrap() -> None:
    caps = toolchain_capabilities(_profile(sandbox=True))
    by_name = {item.name: item for item in caps}
    assert by_name["bubblewrap"].available == (shutil.which("bwrap") is not None)


def test_execute_echo(tmp_path: Path) -> None:
    backend = _prepared(tmp_path, extra=["/bin/sh"])
    result = backend.execute(
        ProcessRequest(
            argv=["/bin/sh", "-c", "echo hello"],
            cwd=tmp_path,
            timeout_seconds=10,
        )
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_execute_unauthorized_raises(tmp_path: Path) -> None:
    backend = _prepared(tmp_path)
    with pytest.raises(PermissionError):
        backend.execute(
            ProcessRequest(
                argv=["/usr/bin/rm", "-rf", "/"],
                cwd=tmp_path,
                timeout_seconds=10,
            )
        )


def test_execute_timeout_kills_process(tmp_path: Path) -> None:
    backend = _prepared(tmp_path, timeout=1, extra=["/bin/sleep"])
    result = backend.execute(
        ProcessRequest(
            argv=["/bin/sleep", "30"],
            cwd=tmp_path,
            timeout_seconds=1,
        )
    )
    assert result.timed_out
    assert result.exit_code is not None


def test_output_truncation(tmp_path: Path) -> None:
    profile = _profile()
    profile.resources.max_output_bytes = 4096  # minimum allowed by the model
    backend = LocalLinuxBackend(profile)
    backend.prepare(_context(tmp_path))
    assert backend._lease is not None
    backend._lease.allowed_executables.append("/bin/sh")
    result = backend.execute(
        ProcessRequest(
            argv=["/bin/sh", "-c", "awk 'BEGIN { while (i++ < 20000) printf \"x\" }'"],
            cwd=tmp_path,
            timeout_seconds=10,
        )
    )
    assert result.output_truncated
    assert len(result.stdout) <= 4096 + 128  # marker text included


def test_stdout_path_bypasses_truncation(tmp_path: Path) -> None:
    profile = _profile()
    profile.resources.max_output_bytes = 4096  # tiny cap; file output must not be truncated
    backend = LocalLinuxBackend(profile)
    context = _context(tmp_path)
    backend.prepare(context)
    assert backend._lease is not None
    backend._lease.allowed_executables.append("/bin/sh")
    out_path = context.run_dir / "big.out"
    result = backend.execute(
        ProcessRequest(
            argv=["/bin/sh", "-c", "awk 'BEGIN { while (i++ < 20000) printf \"x\" }'"],
            cwd=tmp_path,
            timeout_seconds=10,
            stdout_path=out_path,
        )
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert not result.output_truncated
    assert out_path.stat().st_size > 4096  # full output reached the file


def test_stdout_path_rejects_escape(tmp_path: Path) -> None:
    backend = LocalLinuxBackend(_profile())
    context = _context(tmp_path)
    backend.prepare(context)
    assert backend._lease is not None
    backend._lease.allowed_executables.append("/bin/true")
    with pytest.raises(PermissionError):
        backend.execute(
            ProcessRequest(
                argv=["/bin/true"],
                cwd=tmp_path,
                timeout_seconds=10,
                stdout_path=tmp_path / "outside.bin",
            )
        )


def test_bubblewrap_argv_construction(tmp_path: Path) -> None:
    profile = _profile(sandbox=True)
    backend = LocalLinuxBackend(profile)
    context = _context(tmp_path)
    context.candidate_dir.mkdir()
    backend.prepare(context)
    argv = backend._bubblewrap_argv(
        ["/tmp/run/fuzzer", "corpus"],
        context.candidate_dir,
        {"LLVM_PROFILE_FILE": "/tmp/run/loop.profraw"},
        10,
    )
    assert argv[0] == "bwrap"
    assert "--unshare-net" in argv
    assert "--die-with-parent" in argv
    assert "--ro-bind" in argv
    assert str(context.source_root) in argv
    assert "--bind" in argv
    assert str(context.run_dir) in argv
    assert "--setenv" in argv
    assert argv[argv.index("--") + 1] == "/tmp/run/fuzzer"
