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
# Per-tier per-file caps for the source context embedded in generation
# prompts. Definition files (the target function's body lives there) get the
# most room; include-closure and same-basename headers get less; build files
# and caller/reference files the least.
MAX_TARGET_FILE_BYTES = 64 * 1024
MAX_DEPENDENCY_FILE_BYTES = 32 * 1024
MAX_BUILD_FILE_BYTES = 16 * 1024
MAX_REFERENCE_FILE_BYTES = 16 * 1024
# Fallback total context budget when the caller does not pass one; the
# controller always derives it from FuzzRunRequest.max_context_kb.
MAX_CONTEXT_TOTAL_BYTES = 256 * 1024
# Quoted-include parser used to build the dependency closure of target files.
_INCLUDE_QUOTED_RE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.MULTILINE)
# A definition is `symbol(` ... `)` followed (possibly across lines) by a
# body opening brace; a mere call site or declaration does not qualify.
_DEFINITION_RE_TEMPLATE = r"(?<![\w:]){symbol}\s*\([^;{{}}]*\)\s*\{{"
_SOURCE_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def preprocess_request(
    workspace_root: Path,
    run_id: str,
    request: FuzzRunRequest,
    profile: ValidationProfile,
    *,
    check_runtime: bool = True,
    api_key_env: str = "DEEPSEEK_API_KEY",
    max_context_bytes: int | None = None,
) -> PreprocessResult:
    workspace_root = workspace_root.resolve()
    repo_root, source_scope = _resolve_request_paths(workspace_root, request)
    project_name = repo_root.name if repo_root.name else "unknown"
    repos_root = (workspace_root / "repos").resolve()
    inside_repos = repo_root.is_relative_to(repos_root)
    # Resolve the CMake build directory up front (does not require it to
    # exist yet; existence is checked below for the ready path).
    resolved_build_dir: Path | None = None
    if request.build_dir is not None:
        resolved_build_dir = (
            request.build_dir if request.build_dir.is_absolute() else workspace_root / request.build_dir
        ).resolve()

    basic_caps: list[Capability] = []
    if not repo_root.is_dir():
        basic_caps.append(Capability(name="repo", available=False, detail="repository directory missing"))
        return _not_ready(
            run_id,
            project_name,
            repo_root,
            request,
            basic_caps,
            TerminalStatus.NEEDS_INPUT,
            f"repository directory does not exist: {repo_root}",
            source_scope=source_scope,
        )

    if not source_scope.is_relative_to(repo_root):
        basic_caps.append(Capability(name="source_scope", available=False, detail="outside repository"))
        return _not_ready(
            run_id,
            project_name,
            repo_root,
            request,
            basic_caps,
            TerminalStatus.NEEDS_INPUT,
            f"source path must be inside repository {repo_root}: {source_scope}",
            source_scope=source_scope,
        )

    if not source_scope.is_dir() and not source_scope.is_file():
        basic_caps.append(Capability(name="source_scope", available=False, detail="source path missing"))
        return _not_ready(
            run_id,
            project_name,
            repo_root,
            request,
            basic_caps,
            TerminalStatus.NEEDS_INPUT,
            f"source path does not exist: {source_scope}",
            source_scope=source_scope,
        )

    if request.build_dir is not None:
        build_dir = (
            request.build_dir
            if request.build_dir.is_absolute()
            else workspace_root / request.build_dir
        ).resolve()
        if not build_dir.is_dir():
            basic_caps.append(Capability(name="build_dir", available=False, detail="build directory missing"))
            return _not_ready(
                run_id,
                project_name,
                repo_root,
                request,
                basic_caps,
                TerminalStatus.NEEDS_INPUT,
                f"build directory does not exist: {build_dir}",
                source_scope=source_scope,
            )
        if not (build_dir / "CMakeLists.txt").is_file():
            basic_caps.append(Capability(name="build_dir", available=False, detail="CMakeLists.txt missing"))
            return _not_ready(
                run_id,
                project_name,
                repo_root,
                request,
                basic_caps,
                TerminalStatus.NEEDS_INPUT,
                f"build directory has no CMakeLists.txt: {build_dir}",
                source_scope=source_scope,
            )
        if profile.sandbox.required:
            basic_caps.append(Capability(name="build_dir", available=False, detail="cmake build requires no sandbox"))
            return _not_ready(
                run_id,
                project_name,
                repo_root,
                request,
                basic_caps,
                TerminalStatus.BLOCKED,
                "build-directory mode requires sandbox.required = false",
                source_scope=source_scope,
            )

    escape = _find_symlink_escape(repo_root)
    if escape is not None:
        basic_caps.append(Capability(name="source_scope", available=False, detail=str(escape)))
        return _not_ready(
            run_id,
            project_name,
            repo_root,
            request,
            basic_caps,
            TerminalStatus.NEEDS_INPUT,
            f"symlink escapes repository tree: {escape}",
            source_scope=source_scope,
        )

    files = _source_files(repo_root)
    scoped_files = _source_files(source_scope)
    matching = _files_containing_symbol(scoped_files, request.function)
    if not matching:
        basic_caps.append(Capability(name="target_function", available=False, detail="symbol not found"))
        return _not_ready(
            run_id,
            project_name,
            repo_root,
            request,
            basic_caps,
            TerminalStatus.NEEDS_INPUT,
            f"target function {request.function!r} was not found under source path {source_scope}",
            source_scope=source_scope,
        )

    language = _detect_language(request.language, matching, files)
    # The source-context budget comes from the request (CLI --max-context-kb);
    # an explicit override wins when provided (used by tests).
    if max_context_bytes is None:
        max_context_bytes = request.max_context_kb * 1024
    contexts = _collect_context(
        repo_root,
        matching,
        files,
        max_context_bytes,
        target_symbol=request.function,
        # In --build-dir mode the controller builds the project itself, so
        # build-file contents are redundant (the model cannot read files and
        # must not guess build parameters); the resolved path is enough.
        include_build_files=request.build_dir is None,
    )
    signatures = _candidate_signatures(matching, request.function)
    scope_kind = "file" if source_scope.is_file() else "directory"
    scope_path = source_scope.relative_to(repo_root).as_posix() or "."
    capabilities = [
        Capability(
            name="repository_scope",
            available=True,
            detail="repository under repos/" if inside_repos else "custom repository directory (outside repos/)",
        ),
        Capability(name="source_scope", available=True, detail=f"target {scope_kind}: {scope_path}"),
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
            source_root=repo_root,
            source_scope=source_scope,
            language=language,
            target_function=request.function,
            contexts=contexts,
            candidate_signatures=signatures,
            capability_report=report,
            terminal_status=TerminalStatus.BLOCKED,
            reason=f"required runtime capabilities unavailable: {missing}",
            build_dir=resolved_build_dir,
        )
    return PreprocessResult(
        run_id=run_id,
        ready=True,
        project_name=project_name,
        source_root=repo_root,
        source_scope=source_scope,
        language=language,
        target_function=request.function,
        contexts=contexts,
        candidate_signatures=signatures,
        capability_report=report,
        build_dir=resolved_build_dir,
    )


def _resolve_request_paths(workspace_root: Path, request: FuzzRunRequest) -> tuple[Path, Path]:
    if request.repo is None:
        repo = request.source if request.source.is_absolute() else workspace_root / request.source
        repo_root = repo.resolve(strict=False)
        return repo_root, repo_root
    repo = request.repo if request.repo.is_absolute() else workspace_root / request.repo
    repo_root = repo.resolve(strict=False)
    source = request.source if request.source.is_absolute() else repo_root / request.source
    return repo_root, source.resolve(strict=False)


def _find_symlink_escape(root: Path) -> Path | None:
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=False).is_relative_to(resolved_root):
            return path
    return None


def _source_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in SOURCE_SUFFIXES or root.name in BUILD_NAMES else []
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


def _collect_context(
    root: Path,
    matches: list[Path],
    files: list[Path],
    max_context_bytes: int | None = None,
    target_symbol: str | None = None,
    include_build_files: bool = True,
) -> list[SourceContext]:
    """Collect source context by relevance, not by directory sweep.

    Files are selected in tiers, each with its own per-file cap:

    1. definition files (the target function's body) — 64 KiB each, with a
       symbol-window truncation for oversized files;
    2. their quoted-include closure (transitively) and same-basename
       headers — 32 KiB each;
    3. build files (CMakeLists.txt / Makefile / ...) — 16 KiB each, unless
       --build-dir mode is in use (the controller builds the project itself,
       so build-file contents are redundant);
    4. caller/reference files (mention the symbol without defining it,
       e.g. tests) — 16 KiB each, last.

    Unrelated files are never included: in large projects a blanket sweep
    consumed the whole budget on code the model does not need, blowing up
    preprocess.json past the model's input-token limit.
    """
    budget = MAX_CONTEXT_TOTAL_BYTES if max_context_bytes is None else max_context_bytes
    definitions, references = _definition_files(matches, target_symbol)
    windowed = {path.resolve() for path in definitions}
    selected = _select_context_files(
        root, definitions, references, files, target_symbol, include_build_files=include_build_files
    )
    result: list[SourceContext] = []
    total = 0
    for path, per_file_cap in selected:
        if total >= budget:
            break
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        available = min(per_file_cap, budget - total)
        content_bytes = raw
        if len(content_bytes) > available:
            if target_symbol is not None and path in windowed:
                content_bytes = _symbol_window(raw, target_symbol, cap=available)
            else:
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


def _definition_files(matches: list[Path], symbol: str | None) -> tuple[list[Path], list[Path]]:
    """Split symbol-mentioning files into definition vs reference files.

    A definition file is one where the symbol appears with a body brace
    (``symbol(...) {``). Files that merely declare or call the function are
    references: they are much less useful to the harness model and must not
    crowd out the definition. Without a symbol, every match is treated as a
    definition (head truncation only).
    """
    if not symbol:
        return list(matches), []
    pattern = re.compile(_DEFINITION_RE_TEMPLATE.format(symbol=re.escape(symbol)))
    definitions: list[Path] = []
    references: list[Path] = []
    for path in matches:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            references.append(path)
            continue
        (definitions if pattern.search(text) else references).append(path)
    return definitions, references


def _select_context_files(
    root: Path,
    definitions: list[Path],
    references: list[Path],
    files: list[Path],
    symbol: str | None,
    include_build_files: bool = True,
) -> list[tuple[Path, int]]:
    """Order candidate files by relevance, returning (path, per-file cap)."""
    selected: list[tuple[Path, int]] = []
    seen: set[Path] = set()

    def add(path: Path, cap: int) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        selected.append((resolved, cap))

    for path in definitions:
        add(path, MAX_TARGET_FILE_BYTES)
    for path in _include_closure(root, definitions, files):
        add(path, MAX_DEPENDENCY_FILE_BYTES)
    for path in _same_basename_headers(definitions, files):
        add(path, MAX_DEPENDENCY_FILE_BYTES)
    if include_build_files:
        for path in sorted(item for item in files if item.name in BUILD_NAMES):
            add(path, MAX_BUILD_FILE_BYTES)
    for path in references:
        add(path, MAX_REFERENCE_FILE_BYTES)
    return selected


def _include_closure(root: Path, seeds: list[Path], files: list[Path]) -> list[Path]:
    """Transitive closure of quoted #include targets of the seed files.

    Only files inside the source root count; system/angle-bracket includes
    are ignored. A name lookup over the collected source files is the last
    resort for includes that resolve via include dirs rather than relative
    paths.
    """
    root = root.resolve()
    by_name: dict[str, Path] = {}
    for path in files:
        if path.suffix.lower() in SOURCE_SUFFIXES:
            by_name.setdefault(path.name, path)
    found: set[Path] = set()
    visited: set[Path] = set()
    queue = [path.resolve() for path in seeds if path.suffix.lower() in SOURCE_SUFFIXES]
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        try:
            text = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for include in _INCLUDE_QUOTED_RE.findall(text):
            target = _resolve_include(root, current.parent, include, by_name)
            if target is None or target in visited:
                continue
            queue.append(target)
            found.add(target)
    return sorted(found, key=lambda path: path.as_posix())


def _resolve_include(root: Path, from_dir: Path, include: str, by_name: dict[str, Path]) -> Path | None:
    for candidate in (from_dir / include, root / include):
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    named = by_name.get(Path(include).name)
    if named is not None:
        resolved = named.resolve()
        if resolved.is_relative_to(root):
            return resolved
    return None


def _same_basename_headers(matches: list[Path], files: list[Path]) -> list[Path]:
    """Headers sharing the basename of a target file (e.g. cJSON.c -> cJSON.h).

    Covers implementations that declare their API in a header they do not
    include themselves; also the reason the file contains the symbol may be a
    definition while the header only carries the declaration without it.
    """
    by_stem: dict[str, Path] = {}
    for path in files:
        if path.suffix.lower() in SOURCE_SUFFIXES and path.suffix.lower().startswith(".h"):
            by_stem.setdefault(path.stem, path)
    headers: set[Path] = set()
    for match in matches:
        header = by_stem.get(match.stem)
        if header is not None and header.resolve() != match.resolve():
            headers.add(header.resolve())
    return sorted(headers, key=lambda path: path.as_posix())


def _symbol_window(raw: bytes, symbol: str, *, cap: int) -> bytes:
    """Truncate an oversized target file around the symbol instead of the head.

    Keeps a small head (includes/typedefs, up to 4 KiB) plus a window centered
    on the last ``symbol(`` occurrence, so the model sees the function
    definition even when it sits deep inside a huge file. Falls back to the
    file head when the symbol never appears with an opening paren.
    """
    if len(raw) <= cap:
        return raw
    text = raw.decode("utf-8", errors="replace")
    pattern = re.compile(rf"(?<![\w:]){re.escape(symbol)}\s*\(")
    positions = [match.start() for match in pattern.finditer(text)]
    if not positions:
        return raw[:cap]
    head = min(4096, cap // 4)
    window_cap = cap - head
    pos = positions[-1]
    if pos < head:  # symbol already inside the head: keep the head
        return raw[:cap]
    start = max(0, pos - window_cap // 2)
    end = min(len(text), start + window_cap)
    window = text[start:end].encode("utf-8", errors="replace")[:window_cap]
    return raw[:head] + b"\n/* ... goaloop truncated to symbol window ... */\n" + window


def _candidate_signatures(matches: list[Path], symbol: str) -> list[str]:
    signatures: list[str] = []
    pattern = re.compile(rf"[^\n;{{}}]{{0,300}}\b{re.escape(symbol)}\s*\([^;{{}}]*\)")
    for path in matches:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = _SOURCE_COMMENT_RE.sub(lambda match: "\n" * match.group(0).count("\n"), text)
        for match in pattern.finditer(text):
            signature = " ".join(match.group(0).split())
            symbol_match = re.search(rf"\b{re.escape(symbol)}\s*\(", signature)
            prefix = signature[: symbol_match.start()].strip() if symbol_match is not None else ""
            if _looks_like_signature_prefix(prefix) and signature not in signatures:
                signatures.append(signature)
            if len(signatures) >= 10:
                return signatures
    return signatures


def _looks_like_signature_prefix(prefix: str) -> bool:
    """Reject obvious call expressions while retaining C/C++ declarations."""
    if not prefix or "=" in prefix or "#" in prefix:
        return False
    if re.search(r"\b(?:return|if|while|for|switch|sizeof|assert)\s*$", prefix):
        return False
    if prefix.count("(") != prefix.count(")") or prefix.count("[") != prefix.count("]"):
        return False
    return re.fullmatch(r"\([^()]+\)", prefix) is None


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
    *,
    source_scope: Path | None = None,
) -> PreprocessResult:
    return PreprocessResult(
        run_id=run_id,
        ready=False,
        project_name=project_name,
        source_root=source_root,
        source_scope=source_scope,
        language=request.language,
        target_function=request.function,
        capability_report=CapabilityReport(platform=platform.platform(), capabilities=capabilities),
        terminal_status=status,
        reason=reason,
    )
