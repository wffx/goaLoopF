"""Read-only adapter for kRepo function and call-tree reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from .redaction import redact

KREPO_TIMEOUT_SECONDS = 300


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
    environment = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "PYTHONPATH")
        if key in os.environ
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
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
