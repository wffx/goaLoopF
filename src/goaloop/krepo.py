"""Read-only adapter for kRepo reports and generation-stage symbol queries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

from .redaction import redact

KREPO_TIMEOUT_SECONDS = 300
KREPO_QUERY_TIMEOUT_SECONDS = 120
KREPO_QUERY_MAX_CHARS = 16 * 1024
KREPO_TOOL_BINDING_SCHEMA_VERSION = "1.0"
KREPO_QUERY_KINDS = frozenset(
    {"struct", "union", "enum", "enumerator", "typedef", "variable", "macro", "macro_define"}
)
_SYMBOL_RE = re.compile(r"^[A-Za-z_]\w*$")


class KRepoError(RuntimeError):
    """kRepo is unavailable or returned an unusable report."""


@dataclass(frozen=True)
class KRepoReport:
    source: str
    incoming_tree: list[str]
    outgoing_tree: list[str]
    param_constraints: list[dict[str, object]]
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True)
class KRepoSymbolQuery:
    """One controller-approved, read-only non-function symbol lookup."""

    symbol: str
    kind: str | None = None


@dataclass(frozen=True)
class KRepoToolBinding:
    """Controller-owned target binding consumed by the native DSH tool."""

    run_id: str
    session_id: str
    workspace_root: Path
    repo_root: Path
    audit_root: Path
    target_function: str
    target_file: str
    python_executable: Path


class KRepoQueryService:
    """Execute kRepo symbol lookups with durable cache and audit records."""

    def __init__(
        self,
        workspace_root: Path,
        repo_root: Path,
        audit_root: Path,
        target_function: str,
        target_file: str,
        *,
        session_id: str | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.repo_root = repo_root.resolve()
        self.audit_root = audit_root.resolve()
        self.target_function = target_function
        self.target_file = _validate_file_filter(target_file)
        self.session_id = session_id
        self.cache_dir = self.audit_root / "cache"
        self.audit_path = self.audit_root / "queries.jsonl"
        self.last_audit_record: dict[str, object] | None = None

    def query(
        self,
        query: KRepoSymbolQuery,
        *,
        on_command: Callable[[list[str]], None] | None = None,
    ) -> dict[str, object]:
        normalized = _validate_symbol_query(query)
        cache_key = _query_cache_key(normalized, self.target_function, self.target_file)
        cache_path = self.cache_dir / f"{cache_key}.json"
        cached = _read_cached_query(cache_path)
        if cached is not None:
            self.last_audit_record = self._append_audit(
                normalized,
                cached,
                cache_hit=True,
                command=None,
            )
            return cached
        executed_command: list[str] | None = None

        def capture_command(command: list[str]) -> None:
            nonlocal executed_command
            executed_command = command.copy()
            if on_command is not None:
                on_command(command.copy())

        try:
            output = query_krepo_symbol(
                self.workspace_root,
                self.repo_root,
                normalized.symbol,
                function=self.target_function,
                file_filter=self.target_file,
                kind=normalized.kind,
                on_command=capture_command,
            )
            result: dict[str, object] = {"ok": True, "output": output}
        except KRepoError as exc:
            result = {"ok": False, "error": str(exc)}
        if result.get("ok") is True:
            self._write_cache(cache_path, result)
        self.last_audit_record = self._append_audit(
            normalized,
            result,
            cache_hit=False,
            command=executed_command,
        )
        return result

    def _write_cache(self, path: Path, result: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)

    def _append_audit(
        self,
        query: KRepoSymbolQuery,
        result: dict[str, object],
        *,
        cache_hit: bool,
        command: list[str] | None,
    ) -> dict[str, object]:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "query": {
                "symbol": query.symbol,
                "function": self.target_function,
                "kind": query.kind,
                "file": self.target_file,
            },
            "cache_hit": cache_hit,
            "command_executed": command is not None,
            "command": shlex.join(command) if command is not None else None,
            "argv": command,
            "cwd": str(self.repo_root),
            "result": result,
        }
        if self.session_id is not None:
            record["session_id"] = self.session_id
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record


def krepo_tool_binding_path(binding_root: Path, session_id: str) -> Path:
    """Return the traversal-safe binding path for one DSH session."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return binding_root.resolve() / f"{digest}.json"


def write_krepo_tool_binding(
    binding_root: Path,
    *,
    run_id: str,
    session_id: str,
    workspace_root: Path,
    repo_root: Path,
    audit_root: Path,
    target_function: str,
    target_file: str,
    python_executable: Path | None = None,
) -> Path:
    """Persist the controller-owned target facts used by ``query_krepo_symbol``."""
    binding = KRepoToolBinding(
        run_id=run_id,
        session_id=session_id,
        workspace_root=workspace_root.resolve(),
        repo_root=repo_root.resolve(),
        audit_root=audit_root.resolve(),
        target_function=_validate_target_function(target_function),
        target_file=_validate_file_filter(target_file),
        python_executable=(python_executable or Path(sys.executable)).resolve(),
    )
    path = krepo_tool_binding_path(binding_root, session_id)
    _write_json_atomic(
        path,
        {
            "schema_version": KREPO_TOOL_BINDING_SCHEMA_VERSION,
            "run_id": binding.run_id,
            "session_id": binding.session_id,
            "workspace_root": str(binding.workspace_root),
            "repo_root": str(binding.repo_root),
            "audit_root": str(binding.audit_root),
            "target_function": binding.target_function,
            "target_file": binding.target_file,
            "python_executable": str(binding.python_executable),
        },
    )
    return path


def execute_krepo_tool_query(
    binding_path: Path,
    *,
    symbol: str,
    kind: str | None,
    repo_root: Path,
    target_function: str,
    target_file: str,
) -> dict[str, object]:
    """Execute one native-tool query after verifying controller-bound arguments."""
    query = _validate_symbol_query(KRepoSymbolQuery(symbol=symbol, kind=kind))
    binding = load_krepo_tool_binding(binding_path)
    normalized_repo = repo_root.resolve()
    normalized_function = _validate_target_function(target_function)
    normalized_file = _validate_file_filter(target_file)
    if normalized_repo != binding.repo_root:
        raise KRepoError("kRepo tool repo does not match the active session binding")
    if normalized_function != binding.target_function:
        raise KRepoError("kRepo tool function does not match the active session binding")
    if normalized_file != binding.target_file:
        raise KRepoError("kRepo tool file does not match the active session binding")
    service = KRepoQueryService(
        binding.workspace_root,
        binding.repo_root,
        binding.audit_root,
        binding.target_function,
        binding.target_file,
        session_id=binding.session_id,
    )
    result = service.query(query)
    audit = service.last_audit_record or {}
    return {
        **result,
        "symbol": query.symbol,
        "kind": query.kind,
        "repo": str(normalized_repo),
        "function": normalized_function,
        "file": normalized_file,
        "cache_hit": audit.get("cache_hit") is True,
        "command": audit.get("command"),
    }


def load_krepo_tool_binding(path: Path) -> KRepoToolBinding:
    """Load and validate a controller-owned native-tool binding."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise KRepoError(f"kRepo tool binding is unavailable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise KRepoError(f"kRepo tool binding is not valid JSON: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != KREPO_TOOL_BINDING_SCHEMA_VERSION:
        raise KRepoError("unsupported kRepo tool binding schema")
    required = {
        "run_id",
        "session_id",
        "workspace_root",
        "repo_root",
        "audit_root",
        "target_function",
        "target_file",
        "python_executable",
    }
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
        raise KRepoError("kRepo tool binding is missing required string fields")
    binding = KRepoToolBinding(
        run_id=payload["run_id"],
        session_id=payload["session_id"],
        workspace_root=Path(payload["workspace_root"]).resolve(),
        repo_root=Path(payload["repo_root"]).resolve(),
        audit_root=Path(payload["audit_root"]).resolve(),
        target_function=_validate_target_function(payload["target_function"]),
        target_file=_validate_file_filter(payload["target_file"]),
        python_executable=Path(payload["python_executable"]).resolve(),
    )
    _validate_tool_binding(binding)
    expected = krepo_tool_binding_path(path.parent, binding.session_id)
    if path.resolve() != expected:
        raise KRepoError("kRepo tool binding path does not match its session")
    return binding


def krepo_cli_path(workspace_root: Path) -> Path:
    override = os.environ.get("GOALOOP_KREPO")
    candidate = Path(override).expanduser() if override else workspace_root / "tools" / "kRepo"
    if candidate.is_dir():
        candidate = candidate / "main.py"
    return candidate.resolve(strict=False)


def read_krepo_report(
    workspace_root: Path,
    repo_root: Path,
    source_file: Path,
    function: str,
    *,
    timeout_seconds: int = KREPO_TIMEOUT_SECONDS,
    on_command: Callable[[list[str]], None] | None = None,
) -> KRepoReport:
    cli = krepo_cli_path(workspace_root)
    if not cli.is_file():
        raise KRepoError(
            f"kRepo CLI is missing: {cli}; initialize it with "
            "git submodule update --init --recursive or set GOALOOP_KREPO"
        )
    try:
        file_filter = source_file.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise KRepoError(f"target source file is outside the repository: {source_file}") from exc

    command = [
        sys.executable,
        str(cli),
        "report",
        function,
        "--repo",
        str(repo_root),
        "--file",
        file_filter,
        "--format",
        "json",
    ]
    environment = _subprocess_environment()
    if on_command is not None:
        on_command(command.copy())
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise KRepoError(f"kRepo report timed out after {timeout_seconds}s for {function!r}") from exc
    except OSError as exc:
        raise KRepoError(f"failed to start kRepo: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        detail = redact(" ".join(detail.split()), workspace_root)[:500]
        raise KRepoError(f"kRepo report failed with exit code {completed.returncode}: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        preview = redact(" ".join(completed.stdout.split()), workspace_root)[:240] or "<empty>"
        raise KRepoError(f"kRepo report was not valid JSON: {exc}; preview={preview!r}") from exc
    if not isinstance(payload, dict):
        raise KRepoError("kRepo report root is not a JSON object")

    source = payload.get("source")
    incoming_tree = _tree_value(payload, "incoming_tree", "incomingTree")
    outgoing_tree = _tree_value(payload, "outgoing_tree", "outgoingTree")
    param_constraints = payload.get("param_constraints")
    if not isinstance(source, str) or not source.strip():
        raise KRepoError("kRepo report has no target function source")
    if not _is_string_list(incoming_tree) or not _is_string_list(outgoing_tree):
        raise KRepoError("kRepo report incoming_tree and outgoing_tree must be non-empty string arrays")
    if not _is_dict_list(param_constraints):
        raise KRepoError("kRepo report param_constraints must be an array of objects")
    start_line, end_line = _source_line_span(source_file, source)
    return KRepoReport(
        source=source,
        incoming_tree=incoming_tree,
        outgoing_tree=outgoing_tree,
        param_constraints=param_constraints,
        start_line=start_line,
        end_line=end_line,
    )


def query_krepo_symbol(
    workspace_root: Path,
    repo_root: Path,
    symbol: str,
    *,
    function: str,
    file_filter: str,
    kind: str | None = None,
    timeout_seconds: int = KREPO_QUERY_TIMEOUT_SECONDS,
    max_chars: int = KREPO_QUERY_MAX_CHARS,
    on_command: Callable[[list[str]], None] | None = None,
) -> str:
    """Run kRepo's read-only ``symbol`` command with controller-owned limits."""
    query = _validate_symbol_query(KRepoSymbolQuery(symbol=symbol, kind=kind))
    normalized_file_filter = _validate_file_filter(file_filter)
    cli = krepo_cli_path(workspace_root)
    if not cli.is_file():
        raise KRepoError(
            f"kRepo CLI is missing: {cli}; initialize it with "
            "git submodule update --init --recursive or set GOALOOP_KREPO"
        )
    command = [
        sys.executable,
        str(cli),
        "symbol",
        query.symbol,
        "--repo",
        str(repo_root.resolve()),
        "--function",
        function,
        "--file",
        normalized_file_filter,
    ]
    if query.kind is not None:
        command.extend(["--kind", query.kind])
    environment = _subprocess_environment()
    if on_command is not None:
        on_command(command.copy())
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise KRepoError(f"kRepo symbol query timed out after {timeout_seconds}s for {symbol!r}") from exc
    except OSError as exc:
        raise KRepoError(f"failed to start kRepo symbol query: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        detail = redact(" ".join(detail.split()), workspace_root)[:500]
        raise KRepoError(f"kRepo symbol query failed with exit code {completed.returncode}: {detail}")
    output = completed.stdout.strip()
    if not output:
        raise KRepoError(f"kRepo symbol query returned empty output for {symbol!r}")
    if len(output) > max_chars:
        output = output[:max_chars] + "\n...<truncated by goaloop>"
    return output


def _validate_symbol_query(query: KRepoSymbolQuery) -> KRepoSymbolQuery:
    symbol = query.symbol.strip()
    if len(symbol) > 128 or not _SYMBOL_RE.fullmatch(symbol):
        raise KRepoError(f"invalid kRepo symbol name: {query.symbol!r}")
    kind = query.kind.strip().lower() if query.kind is not None else None
    if kind is not None and kind not in KREPO_QUERY_KINDS:
        raise KRepoError(f"unsupported kRepo symbol kind: {query.kind!r}")
    return KRepoSymbolQuery(symbol=symbol, kind=kind)


def _validate_target_function(function: str) -> str:
    normalized = function.strip()
    if len(normalized) > 128 or not _SYMBOL_RE.fullmatch(normalized):
        raise KRepoError(f"invalid kRepo target function: {function!r}")
    return normalized


def _validate_file_filter(file_filter: str) -> str:
    normalized = file_filter.strip().replace("\\", "/")
    path = Path(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or "\x00" in normalized:
        raise KRepoError(f"unsafe kRepo file filter: {file_filter!r}")
    if len(normalized) > 512:
        raise KRepoError("kRepo file filter is too long")
    return normalized


def _validate_tool_binding(binding: KRepoToolBinding) -> None:
    if not binding.run_id or len(binding.run_id) > 256:
        raise KRepoError("invalid kRepo tool run id")
    if not binding.session_id or len(binding.session_id) > 512:
        raise KRepoError("invalid kRepo tool session id")
    if not binding.repo_root.is_dir():
        raise KRepoError(f"kRepo tool repository is unavailable: {binding.repo_root}")
    target_path = (binding.repo_root / binding.target_file).resolve()
    if not target_path.is_relative_to(binding.repo_root) or not target_path.is_file():
        raise KRepoError(f"kRepo tool target file is unavailable: {binding.target_file}")
    if not binding.python_executable.is_file():
        raise KRepoError(f"kRepo tool Python executable is unavailable: {binding.python_executable}")

def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _query_cache_key(query: KRepoSymbolQuery, target_function: str, target_file: str) -> str:
    payload = json.dumps(
        {
            "symbol": query.symbol,
            "function": target_function,
            "kind": query.kind,
            "file": target_file,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_cached_query(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _subprocess_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "PYTHONPATH")
        if key in os.environ
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) for item in value)


def _is_dict_list(value: object) -> TypeGuard[list[dict[str, object]]]:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _tree_value(payload: dict[str, object], snake_case: str, camel_case: str) -> object:
    return payload[snake_case] if snake_case in payload else payload.get(camel_case)


def _source_line_span(source_file: Path, source: str) -> tuple[int | None, int | None]:
    try:
        file_content = source_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    offset = file_content.find(source)
    if offset < 0:
        return None, None
    start_line = file_content.count("\n", 0, offset) + 1
    return start_line, start_line + source.rstrip("\n").count("\n")
