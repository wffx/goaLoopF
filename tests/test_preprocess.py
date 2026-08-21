"""Preprocess tests: scoping, symbol lookup, language detection, path escape."""

from __future__ import annotations

from pathlib import Path

import pytest

from goaloop.models import FuzzRunRequest, Language, TerminalStatus
from goaloop.preprocess import preprocess_request


def _request(workspace: Path, **overrides: object) -> FuzzRunRequest:
    values: dict[str, object] = {
        "source": "repos/safe",
        "function": "safe_parse",
        "language": Language.AUTO,
        "profile": "default",
        "model_profile": "default",
        "max_generation_loops": 3,
        "fuzz_seconds": 1,
    }
    values.update(overrides)
    return FuzzRunRequest.model_validate(values)


def test_ready_preprocess(workspace_root: Path, default_profile: object) -> None:
    result = preprocess_request(
        workspace_root,
        "run-1",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert result.ready
    assert result.project_name == "safe"
    assert result.language is Language.C
    assert result.target_function == "safe_parse"
    assert any(ctx.path == "src/safe.c" for ctx in result.contexts)
    assert result.candidate_signatures


def test_missing_source_directory(workspace_root: Path, default_profile: object) -> None:
    result = preprocess_request(
        workspace_root,
        "run-2",
        _request(workspace_root, source="repos/nope"),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert not result.ready
    assert result.terminal_status is TerminalStatus.NEEDS_INPUT


def test_custom_source_directory(workspace_root: Path, default_profile: object, tmp_path: Path) -> None:
    # Targets no longer have to live under repos/: any explicit directory works.
    custom = tmp_path / "custom-proj"
    (custom / "src").mkdir(parents=True)
    (custom / "src" / "impl.c").write_text(
        "#include <stddef.h>\n#include <stdint.h>\nint custom_parse(const uint8_t *d, size_t s) { return d[s-1]; }\n",
        encoding="utf-8",
    )
    result = preprocess_request(
        workspace_root,
        "run-custom",
        _request(workspace_root, source=str(custom), function="custom_parse"),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert result.ready
    assert result.project_name == "custom-proj"
    scope = next(item for item in result.capability_report.capabilities if item.name == "source_scope")
    assert "custom" in scope.detail


def test_symlink_escape_detected(workspace_root: Path, default_profile: object) -> None:
    target = workspace_root.parent / "outside-target"
    target.mkdir(exist_ok=True)
    (workspace_root / "repos" / "safe" / "src" / "escape.c").symlink_to(target / "file.c")
    result = preprocess_request(
        workspace_root,
        "run-4",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert not result.ready
    assert "symlink" in (result.reason or "")


def test_target_symbol_not_found(workspace_root: Path, default_profile: object) -> None:
    result = preprocess_request(
        workspace_root,
        "run-5",
        _request(workspace_root, function="missing_symbol"),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert not result.ready
    assert result.terminal_status is TerminalStatus.NEEDS_INPUT
    assert "missing_symbol" in (result.reason or "")


def test_runtime_capabilities_gate_blocked(
    workspace_root: Path, default_profile: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = preprocess_request(
        workspace_root,
        "run-6",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=True,
    )
    assert not result.ready
    assert result.terminal_status is TerminalStatus.BLOCKED
    assert "deepseek_api_key" in (result.reason or "")


def test_cpp_language_detection(workspace_root: Path, default_profile: object) -> None:
    (workspace_root / "repos" / "safe" / "src" / "extra.cpp").write_text("int safe_parse();\n", encoding="utf-8")
    result = preprocess_request(
        workspace_root,
        "run-7",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert result.language is Language.CPP


def test_context_truncation_budget(workspace_root: Path, default_profile: object) -> None:
    (workspace_root / "repos" / "safe" / "src" / "huge.c").write_text(
        "// safe_parse\n" + "x" * 512 * 1024, encoding="utf-8"
    )
    result = preprocess_request(
        workspace_root,
        "run-8",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    total = sum(len(ctx.content) for ctx in result.contexts)
    assert total <= 256 * 1024
    assert any(ctx.truncated for ctx in result.contexts)
