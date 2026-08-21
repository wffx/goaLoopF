"""Crash analysis: sanitizer stack ownership, input minimization, reproduction."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from .models import (
    CrashAnalysisResult,
    CrashOwnership,
    ProcessRequest,
    ProcessResult,
    ValidationProfile,
)
from .validation import detect_sanitizer


class _Executor(Protocol):
    def execute(self, request: ProcessRequest) -> ProcessResult: ...


def classify_stack(
    output: str,
    *,
    source_root: Path,
    candidate_dir: Path,
    target_function: str | None = None,
) -> CrashOwnership:
    """Attribute sanitizer frames to product source, the harness, or neither.

    Frames are product evidence when they name source under ``source_root`` OR
    the frame's function is the target function. The latter covers models that
    inline a copy of the product function into the harness translation unit,
    which would otherwise make every product crash look like a harness bug.
    """
    source_root = source_root.resolve()
    candidate_dir = candidate_dir.resolve()
    product_lines: list[str] = []
    harness_lines: list[str] = []
    other_lines: list[str] = []
    for line in output.splitlines():
        lowered = line.lower()
        if source_root.as_posix().lower() in lowered or (
            target_function is not None
            and f" in {target_function.lower()} " in lowered
            and not _is_harness_function(lowered)
        ):
            product_lines.append(line)
        elif candidate_dir.as_posix().lower() in lowered:
            harness_lines.append(line)
        elif "in " in lowered or lowered.startswith("#"):
            other_lines.append(line)
    if product_lines:
        return CrashOwnership.PRODUCT
    if harness_lines and not product_lines:
        return CrashOwnership.HARNESS
    return CrashOwnership.UNKNOWN


def _is_harness_function(frame_line: str) -> bool:
    # LLVMFuzzerTestOneInput/libFuzzer frames that happen to mention the target
    # name would be false positives; only exact "in <target>" frames count.
    return "llvmfuzzer" in frame_line or "libfuzzer" in frame_line


def analyze_crash(
    *,
    source_root: Path,
    candidate_dir: Path,
    run_dir: Path,
    fuzzer_binary: Path,
    crash_files: list[Path],
    output: str,
    profile: ValidationProfile,
    backend: _Executor,
    target_function: str | None = None,
    max_reproductions: int = 3,
) -> CrashAnalysisResult:
    """Run the complete crash investigation for one candidate.

    ``backend`` only needs ``execute(ProcessRequest) -> ProcessResult``.
    """
    sanitizer_kind = detect_sanitizer(output)
    ownership = classify_stack(
        output,
        source_root=source_root,
        candidate_dir=candidate_dir,
        target_function=target_function,
    )

    if ownership is CrashOwnership.HARNESS or not crash_files:
        return CrashAnalysisResult(
            ownership=ownership,
            sanitizer_kind=sanitizer_kind,
            reproductions=0,
            minimized_artifact=None,
            stack_excerpt=output[-4000:] or None,
            reason=(
                "crash attributed to harness code" if ownership is CrashOwnership.HARNESS else "no crash artifact found"
            ),
        )

    minimized = _minimize(
        fuzzer_binary=fuzzer_binary,
        crash_files=crash_files,
        run_dir=run_dir,
        profile=profile,
        backend=backend,
    )
    reproductions = _reproduce(
        fuzzer_binary=fuzzer_binary,
        crash_file=minimized or crash_files[0],
        expected_kind=sanitizer_kind,
        profile=profile,
        backend=backend,
        count=max_reproductions,
    )
    reason = (
        f"product crash reproduced {reproductions}/{max_reproductions} times"
        if reproductions >= max_reproductions
        else f"crash did not reproduce consistently ({reproductions}/{max_reproductions})"
    )
    if reproductions < max_reproductions:
        ownership = CrashOwnership.UNKNOWN
    return CrashAnalysisResult(
        ownership=ownership,
        sanitizer_kind=sanitizer_kind,
        reproductions=reproductions,
        minimized_artifact=minimized.relative_to(run_dir).as_posix() if minimized is not None else None,
        stack_excerpt=output[-4000:] or None,
        reason=reason,
    )


def _minimize(
    *,
    fuzzer_binary: Path,
    crash_files: list[Path],
    run_dir: Path,
    profile: ValidationProfile,
    backend: _Executor,
) -> Path | None:
    source = crash_files[0]
    if source.name.startswith("timeout-"):
        return source
    minimized_dir = run_dir / "crashes"
    minimized_dir.mkdir(parents=True, exist_ok=True)
    minimized = minimized_dir / "minimized.bin"
    shutil.copy2(source, minimized)
    request = ProcessRequest(
        argv=[
            str(fuzzer_binary),
            "-minimize_crash=1",
            "-runs=100000",
            str(minimized),
        ],
        cwd=run_dir,
        timeout_seconds=profile.resources.timeout_seconds,
    )
    result = backend.execute(request)
    if result.exit_code in (0, None) or minimized.stat().st_size == 0:
        return None
    return minimized


def _reproduce(
    *,
    fuzzer_binary: Path,
    crash_file: Path,
    expected_kind: str | None,
    profile: ValidationProfile,
    backend: _Executor,
    count: int,
) -> int:
    reproduced = 0
    for _ in range(count):
        request = ProcessRequest(
            argv=[str(fuzzer_binary), str(crash_file)],
            cwd=crash_file.parent,
            timeout_seconds=profile.resources.timeout_seconds,
        )
        result = backend.execute(request)
        output = f"{result.stdout}\n{result.stderr}"
        kind = detect_sanitizer(output)
        if result.exit_code not in (0, None) and kind is not None and (expected_kind is None or kind == expected_kind):
            reproduced += 1
    return reproduced
