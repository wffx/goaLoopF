"""Source-based coverage measurement and attribution for one candidate loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .models import CoverageMetrics, ProcessRequest, ProcessResult

LLVM_PROFILE_FILENAME = "loop.profraw"


class _Executor(Protocol):
    def execute(self, request: ProcessRequest) -> ProcessResult: ...


class CoverageMeasurementError(RuntimeError):
    """Coverage artifacts are missing, corrupt, or cannot be attributed."""


def measure_coverage(
    *,
    backend: _Executor,
    binary: Path,
    profraw: Path,
    profdata: Path,
    coverage_json: Path,
    source_root: Path,
    target_function: str,
    llvm_profdata: str,
    llvm_cov: str,
    timeout_seconds: int,
) -> CoverageMetrics:
    """Merge the loop's .profraw and attribute hits to target source."""
    if not profraw.is_file():
        raise CoverageMeasurementError(f"profile data file is missing: {profraw.name}")
    merge = backend.execute(
        ProcessRequest(
            argv=[llvm_profdata, "merge", "-sparse", str(profraw), "-o", str(profdata)],
            cwd=profraw.parent,
            timeout_seconds=timeout_seconds,
        )
    )
    if merge.exit_code != 0:
        raise CoverageMeasurementError(f"llvm-profdata merge failed: {merge.stderr[-2000:]}")
    export = backend.execute(
        ProcessRequest(
            argv=[
                llvm_cov,
                "export",
                str(binary),
                f"-instr-profile={profdata}",
                "--format=text",  # LLVM 14+ "text" export is JSON; modern LLVM dropped -format=json
            ],
            cwd=profraw.parent,
            timeout_seconds=timeout_seconds,
            stdout_path=coverage_json,
        )
    )
    if export.exit_code != 0:
        raise CoverageMeasurementError(f"llvm-cov export failed: {export.stderr[-2000:]}")
    if not coverage_json.is_file():
        raise CoverageMeasurementError("llvm-cov export produced no output file")
    try:
        payload = json.loads(coverage_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CoverageMeasurementError(f"llvm-cov export is not valid JSON: {exc}") from exc
    return _parse_export(payload, source_root=source_root, target_function=target_function)


def _parse_export(payload: dict[str, Any], *, source_root: Path, target_function: str) -> CoverageMetrics:
    source_root = source_root.resolve()
    files: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    for data in payload.get("data", []):
        files.extend(data.get("files", []) or [])
        functions.extend(data.get("functions", []) or [])
    if not files and not functions:
        raise CoverageMeasurementError("coverage export contains no files")

    target_hit = False
    for function in functions:
        count = function.get("count") or 0
        if count <= 0:
            continue
        name = function.get("name") or ""
        demangled = function.get("demangled_name") or ""
        if target_function in name or target_function in demangled:
            target_hit = True
            break

    total_lines = 0
    covered_lines = 0
    attributed_files = 0
    for entry in files:
        filename = entry.get("filename") or ""
        if not Path(filename).resolve().is_relative_to(source_root):
            continue
        attributed_files += 1
        lines_with_counts: set[int] = set()
        lines_covered: set[int] = set()
        for segment in entry.get("segments", []):
            if len(segment) < 4:
                continue
            line, _col, count, has_count = segment[:4]
            if has_count:
                lines_with_counts.add(line)
                if count > 0:
                    lines_covered.add(line)
        total_lines += len(lines_with_counts)
        covered_lines += len(lines_covered)

    if attributed_files == 0:
        raise CoverageMeasurementError("coverage export cannot be attributed to target source")
    coverage = (covered_lines / total_lines * 100.0) if total_lines else 0.0
    return CoverageMetrics(
        target_function_hit=target_hit,
        target_line_coverage=round(coverage, 2),
        target_line_delta=round(coverage, 2),
    )
