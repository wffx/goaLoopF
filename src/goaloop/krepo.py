"""Read-only adapter for kRepo function and call-tree reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import redact

KREPO_TIMEOUT_SECONDS = 300


class KRepoError(RuntimeError):
    """kRepo is unavailable or returned an unusable report."""


@dataclass(frozen=True)
class KRepoReport:
    source: str
    incoming_tree: dict[str, Any]
    outgoing_tree: dict[str, Any]
    selected_file: str
    start_line: int | None
    end_line: int | None


def krepo_cli_path(workspace_root: Path) -> Path:
    override = os.environ.get("GOALOOP_KREPO")
    candidate = Path(override).expanduser() if override else workspace_root / "tools" / "kRepo"
    if candidate.is_dir():
        candidate = candidate / "src" / "cpp_meta_query.py"
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
    incoming_tree = payload.get("incoming_tree")
    outgoing_tree = payload.get("outgoing_tree")
    selected = payload.get("selected")
    if not isinstance(source, str) or not source.strip():
        raise KRepoError("kRepo report has no target function source")
    if not isinstance(incoming_tree, dict) or not isinstance(outgoing_tree, dict):
        raise KRepoError("kRepo report is missing incoming_tree or outgoing_tree")
    if not isinstance(selected, dict):
        raise KRepoError("kRepo report is missing selected function metadata")
    selected_file = str(selected.get("file", file_filter))
    normalized_selected = selected_file.replace("\\", "/").lower()
    if not normalized_selected.endswith(file_filter.lower()):
        raise KRepoError(
            f"kRepo selected an unexpected same-name function: {selected_file}; expected file suffix {file_filter}"
        )
    return KRepoReport(
        source=source,
        incoming_tree=incoming_tree,
        outgoing_tree=outgoing_tree,
        selected_file=selected_file,
        start_line=_optional_int(selected.get("start_line")),
        end_line=_optional_int(selected.get("end_line")),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 1 else None
