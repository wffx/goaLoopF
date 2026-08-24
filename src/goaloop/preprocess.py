"""Deterministic intake, source scoping, context collection and capability checks."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import re
from pathlib import Path

from .backend import toolchain_capabilities
from .models import (
    Capability,
    CapabilityReport,
    FuzzRunRequest,
    Language,
    PreprocessResult,
    SourceContext,
    TerminalStatus,
    ValidationProfile,
)

SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
BUILD_NAMES = {"CMakeLists.txt", "Makefile", "meson.build", "BUILD", "BUILD.bazel"}
MAX_CONTEXT_FILE_BYTES = 64 * 1024
MAX_CONTEXT_TOTAL_BYTES = 256 * 1024


def preprocess_request(
    workspace_root: Path,
    run_id: str,
    request: FuzzRunRequest,
    profile: ValidationProfile,
    *,
    check_runtime: bool = True,
    api_key_env: str = "DEEPSEEK_API_KEY",
) -> PreprocessResult:
    workspace_root = workspace_root.resolve()
    source_root = _resolve_source(workspace_root, request.source)
    project_name = source_root.name if source_root.name else "unknown"
    repos_root = (workspace_root / "repos").resolve()
    inside_repos = source_root.is_relative_to(repos_root)

    basic_caps: list[Capability] = []
    if not source_root.is_dir():
        basic_caps.append(Capability(name="source", available=False, detail="source directory missing"))
        return _not_ready(
            run_id,
            project_name,
            source_root,
            request,
            basic_caps,
            TerminalStatus.NEEDS_INPUT,
            f"source directory does not exist: {source_root}",
        )

    escape = _find_symlink_escape(source_root)
    if escape is not None:
        basic_caps.append(Capability(name="source_scope", available=False, detail=str(escape)))
        return _not_ready(
            run_id,
            project_name,
            source_root,
            request,
            basic_caps,
            TerminalStatus.NEEDS_INPUT,
            f"symlink escapes source tree: {escape}",
        )

    files = _source_files(source_root)
    matching = _files_containing_symbol(files, request.function)
    if not matching:
        basic_caps.append(Capability(name="target_function", available=False, detail="symbol not found"))
        return _not_ready(
            run_id,
            project_name,
            source_root,
            request,
            basic_caps,
            TerminalStatus.NEEDS_INPUT,
            f"target function {request.function!r} was not found in source files",
        )

    language = _detect_language(request.language, matching, files)
    contexts = _collect_context(source_root, matching, files)
    signatures = _candidate_signatures(matching, request.function)
    capabilities = [
        Capability(
            name="source_scope",
            available=True,
            detail="source under repos/" if inside_repos else "custom source directory (outside repos/)",
        ),
        Capability(name="target_function", available=True, detail=f"found in {len(matching)} file(s)"),
    ]
    if check_runtime:
        capabilities.extend(_runtime_capabilities(profile, api_key_env=api_key_env))
    report = CapabilityReport(platform=platform.platform(), capabilities=capabilities)
    if not report.ready:
        missing = ", ".join(item.name for item in capabilities if not item.available)
        return PreprocessResult(
            run_id=run_id,
            ready=False,
            project_name=project_name,
            source_root=source_root,
            language=language,
            target_function=request.function,
            contexts=contexts,
            candidate_signatures=signatures,
            capability_report=report,
            terminal_status=TerminalStatus.BLOCKED,
            reason=f"required runtime capabilities unavailable: {missing}",
        )
    return PreprocessResult(
        run_id=run_id,
        ready=True,
        project_name=project_name,
        source_root=source_root,
        language=language,
        target_function=request.function,
        contexts=contexts,
        candidate_signatures=signatures,
        capability_report=report,
    )


def _resolve_source(workspace_root: Path, source: Path) -> Path:
    path = source if source.is_absolute() else workspace_root / source
    return path.resolve(strict=False)


def _find_symlink_escape(root: Path) -> Path | None:
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=False).is_relative_to(resolved_root):
            return path
    return None


def _source_files(root: Path) -> list[Path]:
    selected: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", "build", "out", "node_modules", "vendor"} for part in path.parts):
            continue
        if path.suffix.lower() in SOURCE_SUFFIXES or path.name in BUILD_NAMES:
            selected.append(path)
    return sorted(selected)


def _files_containing_symbol(files: list[Path], symbol: str) -> list[Path]:
    pattern = re.compile(rf"(?<![\w:]){re.escape(symbol)}(?![\w:])")
    matches: list[Path] = []
    for path in files:
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            matches.append(path)
    return matches


def _detect_language(requested: Language, matches: list[Path], files: list[Path]) -> Language:
    if requested is not Language.AUTO:
        return requested
    cpp_suffixes = {".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"}
    if any(path.suffix.lower() in cpp_suffixes for path in matches):
        return Language.CPP
    if any(path.suffix.lower() == ".c" for path in matches):
        return Language.C
    return Language.CPP if any(path.suffix.lower() in cpp_suffixes for path in files) else Language.C


def _collect_context(root: Path, matches: list[Path], files: list[Path]) -> list[SourceContext]:
    ordered: list[Path] = []
    for path in [*matches, *(item for item in files if item.name in BUILD_NAMES), *files]:
        if path not in ordered:
            ordered.append(path)
    result: list[SourceContext] = []
    total = 0
    for path in ordered:
        if total >= MAX_CONTEXT_TOTAL_BYTES:
            break
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        available = min(MAX_CONTEXT_FILE_BYTES, MAX_CONTEXT_TOTAL_BYTES - total)
        content_bytes = raw[:available]
        total += len(content_bytes)
        result.append(
            SourceContext(
                path=path.relative_to(root).as_posix(),
                sha256=hashlib.sha256(raw).hexdigest(),
                content=content_bytes.decode("utf-8", errors="replace"),
                truncated=len(raw) > len(content_bytes),
            )
        )
    return result


def _candidate_signatures(matches: list[Path], symbol: str) -> list[str]:
    signatures: list[str] = []
    pattern = re.compile(rf"[^\n;{{}}]{{0,300}}\b{re.escape(symbol)}\s*\([^;{{}}]*\)")
    for path in matches:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in pattern.finditer(text):
            signature = " ".join(match.group(0).split())
            if signature and signature not in signatures:
                signatures.append(signature)
            if len(signatures) >= 10:
                return signatures
    return signatures


def _runtime_capabilities(profile: ValidationProfile, *, api_key_env: str = "DEEPSEEK_API_KEY") -> list[Capability]:
    capabilities = toolchain_capabilities(profile)
    sdk_available = importlib.util.find_spec("deepseek_harness") is not None
    capabilities.append(
        Capability(
            name="deepseek_harness_sdk",
            available=sdk_available,
            detail="importable" if sdk_available else "not installed",
        )
    )
    has_key = bool(os.environ.get(api_key_env))
    capabilities.append(
        Capability(
            name="model_api_key",
            available=has_key,
            detail=f"{api_key_env} set" if has_key else f"{api_key_env} missing",
        )
    )
    return capabilities


def _not_ready(
    run_id: str,
    project_name: str,
    source_root: Path,
    request: FuzzRunRequest,
    capabilities: list[Capability],
    status: TerminalStatus,
    reason: str,
) -> PreprocessResult:
    return PreprocessResult(
        run_id=run_id,
        ready=False,
        project_name=project_name,
        source_root=source_root,
        language=request.language,
        target_function=request.function,
        capability_report=CapabilityReport(platform=platform.platform(), capabilities=capabilities),
        terminal_status=status,
        reason=reason,
    )
