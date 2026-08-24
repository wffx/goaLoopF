"""Execution backend: LocalLinuxBackend with an optional bubblewrap sandbox.

The backend protocol is stable so a different executor (remote VM, container)
can be swapped in without touching the workflow. Commands always run from an
argv array; ``shell=True`` is never used.
"""

from __future__ import annotations

import contextlib
import os
import platform
import resource
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .models import (
    Capability,
    CapabilityReport,
    CollectedArtifacts,
    ExecutionLease,
    ProcessRequest,
    ProcessResult,
    RunContext,
    ValidationProfile,
)


class ExecutionBackend(Protocol):
    def probe(self, profile: ValidationProfile) -> CapabilityReport: ...
    def prepare(self, context: RunContext) -> ExecutionLease: ...
    def execute(self, request: ProcessRequest) -> ProcessResult: ...
    def collect(self, context: RunContext) -> CollectedArtifacts: ...
    def close(self, context: RunContext) -> None: ...


def toolchain_capabilities(profile: ValidationProfile) -> list[Capability]:
    """Probe platform and toolchain availability shared by preprocess and doctor."""
    capabilities = [
        Capability(
            name="linux",
            available=platform.system() == "Linux",
            detail=platform.system(),
        )
    ]
    tool_names = {
        "clang": profile.tools.clang,
        "clangxx": profile.tools.clangxx,
        "llvm_profdata": profile.tools.llvm_profdata,
        "llvm_cov": profile.tools.llvm_cov,
    }
    if profile.sandbox.required:
        tool_names["bubblewrap"] = profile.tools.bubblewrap
    tool_names["cmake"] = profile.tools.cmake
    for name, executable in tool_names.items():
        found = shutil.which(executable)
        capabilities.append(
            Capability(name=name, available=found is not None, detail=found or f"missing: {executable}")
        )
    return capabilities


class LocalLinuxBackend:
    """Run compiler/fuzzer/coverage commands on the local Linux host.

    ``sandbox.required`` in the profile selects bubblewrap isolation. When the
    profile does not require a sandbox (local debug), the backend still applies
    wall-clock timeout plus RLIMIT_AS/RLIMIT_CPU/RLIMIT_NPROC via ``preexec_fn``.
    """

    def __init__(self, profile: ValidationProfile) -> None:
        self.profile = profile
        self._context: RunContext | None = None
        self._lease: ExecutionLease | None = None

    def probe(self, profile: ValidationProfile) -> CapabilityReport:
        return CapabilityReport(platform=platform.platform(), capabilities=toolchain_capabilities(profile))

    def prepare(self, context: RunContext) -> ExecutionLease:
        self._context = context
        allowed = [
            self.profile.tools.clang,
            self.profile.tools.clangxx,
            self.profile.tools.llvm_profdata,
            self.profile.tools.llvm_cov,
            self.profile.tools.cmake,
        ]
        if self.profile.sandbox.required:
            allowed.append(self.profile.tools.bubblewrap)
        lease = ExecutionLease(
            allowed_executables=[_resolve_tool(item) for item in allowed],
            allowed_dirs=[str(context.run_dir.resolve()), str(context.candidate_dir.resolve())]
            if context.candidate_dir is not None
            else [str(context.run_dir.resolve())],
            timeout_seconds=self.profile.resources.timeout_seconds,
            max_commands=self.profile.resources.max_commands,
        )
        self._lease = lease
        return lease

    def execute(self, request: ProcessRequest) -> ProcessResult:
        if self._lease is None or self._context is None:
            raise RuntimeError("backend.execute called before prepare()")
        lease = self._lease
        if not lease.authorize(request.argv):
            raise PermissionError(f"command is not authorized by the execution lease: {request.argv[0]}")

        cwd = request.cwd.resolve()
        env = self._sandbox_env(request.env)
        limit = self.profile.resources

        argv = request.argv
        if self._sandboxed():
            argv = self._bubblewrap_argv(argv, cwd, env, request.timeout_seconds)

        stdout_handle = None
        if request.stdout_path is not None:
            stdout_path = request.stdout_path.resolve()
            if not stdout_path.is_relative_to(self._context.run_dir.resolve()):
                raise PermissionError(f"stdout_path escapes the run directory: {request.stdout_path}")
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = stdout_path.open("wb")

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                start_new_session=True,
                preexec_fn=self._preexec if not self._sandboxed() else None,
            )
        except OSError as exc:
            lease.commands_used += 1
            if stdout_handle is not None:
                stdout_handle.close()
            return ProcessResult(
                argv=list(request.argv),
                exit_code=None,
                timed_out=False,
                duration_seconds=0.0,
                stdout="",
                stderr=f"failed to start process: {exc}",
            )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=request.timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_process_group(proc.pid)
            stdout_bytes, stderr_bytes = proc.communicate()
            exit_code = proc.returncode
        finally:
            lease.commands_used += 1
            if stdout_handle is not None:
                stdout_handle.close()

        duration = time.monotonic() - started
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if stdout_handle is not None:
            stdout = ""
            stdout_truncated = False
        else:
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stdout, stdout_truncated = _truncate(stdout, limit.max_output_bytes)
        stderr, stderr_truncated = _truncate(stderr, limit.max_output_bytes)
        return ProcessResult(
            argv=list(request.argv),
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=round(duration, 3),
            stdout=stdout,
            stderr=stderr,
            output_truncated=stdout_truncated or stderr_truncated,
        )

    def collect(self, context: RunContext) -> CollectedArtifacts:
        run_dir = context.run_dir.resolve()
        crashes_dir = run_dir / "crashes"
        coverage_dir = run_dir / "coverage"
        artifacts = CollectedArtifacts()
        if context.candidate_dir is not None:
            binary = context.candidate_dir / context.binary_name
            if binary.is_file():
                artifacts.fuzzer_binary = binary
        corpus = run_dir / "corpus"
        if corpus.is_dir():
            artifacts.corpus_dir = corpus
        if crashes_dir.is_dir():
            artifacts.crash_files = sorted(
                item
                for item in crashes_dir.rglob("*")
                if item.is_file() and (item.name.startswith("crash-") or item.name.startswith("timeout-"))
            )
        if coverage_dir.is_dir():
            artifacts.profraw_files = sorted(coverage_dir.glob("*.profraw"))
            profdata = coverage_dir / "merged.profdata"
            if profdata.is_file():
                artifacts.profdata_path = profdata
            coverage_json = coverage_dir / "coverage.json"
            if coverage_json.is_file():
                artifacts.coverage_json = coverage_json
        return artifacts

    def close(self, context: RunContext) -> None:
        self._context = None
        self._lease = None

    # -- internals ---------------------------------------------------------

    def _sandboxed(self) -> bool:
        if not self.profile.sandbox.required:
            return False
        return shutil.which(self.profile.tools.bubblewrap) is not None

    def _sandbox_env(self, extra: dict[str, str]) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(Path.home()),
            "TMPDIR": "/tmp",
        }
        env.update(extra)
        return env

    def _preexec(self) -> None:
        limits = self.profile.resources
        # RLIMIT_AS is deliberately NOT applied here: AddressSanitizer needs to
        # reserve ~20 TB of virtual address space for its shadow, so a virtual
        # memory cap aborts every ASan binary. Resident memory is bounded by
        # libFuzzer's own -rss_limit_mb and, inside the sandbox, by bwrap
        # --rlimit-rss. The CPU cap and the wall-clock timeout still apply.
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))

    def _bubblewrap_argv(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> list[str]:
        if self._context is None:
            raise RuntimeError("sandbox needs a prepared run context")
        limits = self.profile.resources
        source_root = self._context.source_root.resolve()
        run_dir = self._context.run_dir.resolve()
        args: list[str] = [
            self.profile.tools.bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
        ]
        if self.profile.sandbox.disable_network:
            args.append("--unshare-net")
        args += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--ro-bind",
            "/etc",
            "/etc",
            "--ro-bind",
            str(source_root),
            str(source_root),
            "--bind",
            str(run_dir),
            str(run_dir),
            "--tmpfs",
            "/tmp",
            "--chdir",
            str(cwd),
            "--rlimit-rss",
            str(limits.memory_mb * 1024 * 1024),
            "--rlimit-nproc",
            str(limits.process_count),
            "--rlimit-cpu",
            str(limits.cpu_seconds),
        ]
        for key, value in sorted(env.items()):
            args += ["--setenv", key, value]
        args += ["--", *argv]
        return args

    def _kill_process_group(self, pid: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[output truncated at {limit} bytes]", True


def _resolve_tool(name: str) -> str:
    path = Path(name)
    if path.is_absolute():
        return str(path.resolve())
    found = shutil.which(name)
    return str(Path(found).resolve()) if found is not None else str(path.resolve())
