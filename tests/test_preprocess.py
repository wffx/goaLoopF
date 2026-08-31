"""Preprocess tests: scoping, symbol lookup, language detection, path escape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from goaloop import preprocess as preprocess_module
from goaloop.krepo import KRepoError
from goaloop.models import FuzzRunRequest, Language, TerminalStatus
from goaloop.preprocess import preprocess_request


def _request(workspace: Path, **overrides: object) -> FuzzRunRequest:
    values: dict[str, object] = {
        "repo": "repos/safe",
        "source": ".",
        "function": "safe_parse",
        "language": Language.AUTO,
        "profile": "default",
        "model_profile": "default",
        "max_generation_loops": 3,
        "fuzz_seconds": 1,
    }
    values.update(overrides)
    return FuzzRunRequest.model_validate(values)


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_ready_preprocess(workspace_root: Path, default_profile: object) -> None:
    progress: list[tuple[str, dict[str, object]]] = []
    result = preprocess_request(
        workspace_root,
        "run-1",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
        on_progress=lambda kind, payload: progress.append((kind, payload)),
    )
    assert result.ready
    assert result.project_name == "safe"
    assert result.language is Language.C
    assert result.target_function == "safe_parse"
    assert any(ctx.path == "src/safe.c" for ctx in result.contexts)
    assert [context.kind for context in result.contexts[:4]] == [
        "target_function",
        "incoming_tree",
        "outgoing_tree",
        "param_constraints",
    ]
    target = result.contexts[0]
    assert target.content.startswith("int safe_parse")
    assert "#include" not in target.content
    assert json.loads(result.contexts[1].content)[1] == "Incoming call tree:"
    assert json.loads(result.contexts[2].content)[1] == "Outgoing call tree:"
    assert json.loads(result.contexts[3].content)[0]["name"] == "data"
    assert result.candidate_signatures
    command_payload = next(payload for kind, payload in progress if kind == "preprocess:krepo_command")
    argv = command_payload["argv"]
    assert isinstance(argv, list)
    assert argv[2:4] == ["report", "safe_parse"]


def test_krepo_failure_blocks_preprocess(
    workspace_root: Path, default_profile: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*args: object, **kwargs: object) -> object:
        raise KRepoError("BROWSE.VC.DB missing")

    monkeypatch.setattr(preprocess_module, "read_krepo_report", _fail)
    result = preprocess_request(
        workspace_root,
        "run-krepo-missing",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )

    assert not result.ready
    assert result.terminal_status is TerminalStatus.BLOCKED
    assert "BROWSE.VC.DB missing" in (result.reason or "")


def test_missing_repository_directory(workspace_root: Path, default_profile: object) -> None:
    result = preprocess_request(
        workspace_root,
        "run-2",
        _request(workspace_root, repo="repos/nope"),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert not result.ready
    assert result.terminal_status is TerminalStatus.NEEDS_INPUT


def test_custom_repository_directory(workspace_root: Path, default_profile: object, tmp_path: Path) -> None:
    custom = tmp_path / "custom-proj"
    (custom / "src").mkdir(parents=True)
    (custom / "src" / "impl.c").write_text(
        "#include <stddef.h>\n#include <stdint.h>\nint custom_parse(const uint8_t *d, size_t s) { return d[s-1]; }\n",
        encoding="utf-8",
    )
    result = preprocess_request(
        workspace_root,
        "run-custom",
        _request(workspace_root, repo=str(custom), function="custom_parse"),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert result.ready
    assert result.project_name == "custom-proj"
    scope = next(item for item in result.capability_report.capabilities if item.name == "repository_scope")
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


def test_source_directory_disambiguates_duplicate_functions(
    workspace_root: Path, default_profile: object
) -> None:
    repo = workspace_root / "repos" / "duplicates"
    _write(repo, "first/target.c", "int duplicate_fn(void) { return 1; }\n")
    _write(repo, "second/target.c", "int duplicate_fn(void) { return 2; }\n")

    result = preprocess_request(
        workspace_root,
        "run-duplicate-dir",
        _request(workspace_root, repo="repos/duplicates", source="second", function="duplicate_fn"),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )

    assert result.ready
    assert result.source_root == repo.resolve()
    assert result.source_scope == (repo / "second").resolve()
    assert result.contexts[0].path == "second/target.c"
    assert result.contexts[0].kind == "target_function"
    assert "return 2" in result.contexts[0].content
    assert {context.kind for context in result.contexts[1:]} == {
        "incoming_tree",
        "outgoing_tree",
        "param_constraints",
    }


def test_source_file_disambiguates_duplicate_functions(workspace_root: Path, default_profile: object) -> None:
    repo = workspace_root / "repos" / "duplicate-files"
    _write(repo, "src/first.c", "int duplicate_fn(void) { return 1; }\n")
    selected = _write(repo, "src/second.c", "int duplicate_fn(void) { return 2; }\n")

    result = preprocess_request(
        workspace_root,
        "run-duplicate-file",
        _request(
            workspace_root,
            repo="repos/duplicate-files",
            source="src/second.c",
            function="duplicate_fn",
        ),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )

    assert result.ready
    assert result.source_scope == selected.resolve()
    assert result.contexts[0].path == "src/second.c"
    assert result.contexts[0].kind == "target_function"
    assert {context.kind for context in result.contexts[1:]} == {
        "incoming_tree",
        "outgoing_tree",
        "param_constraints",
    }


def test_candidate_signatures_exclude_comments_and_call_sites(
    workspace_root: Path, default_profile: object
) -> None:
    repo = workspace_root / "repos" / "signature-filter"
    _write(repo, "include/target.h", "API int target_fn(const char *value);\n")
    _write(
        repo,
        "src/target.c",
        "/* target_fn() returns a parsed value. */\n"
        "int target_fn(const char *value) { return value != 0; }\n"
        "int caller(const char *value) { return target_fn(value); }\n"
        "void assign(const char *value) { int result = target_fn(value); (void)result; }\n",
    )

    result = preprocess_request(
        workspace_root,
        "run-signature-filter",
        _request(workspace_root, repo="repos/signature-filter", function="target_fn"),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )

    assert result.ready
    assert set(result.candidate_signatures) == {
        "API int target_fn(const char *value)",
        "int target_fn(const char *value)",
    }


def test_source_path_must_stay_inside_repository(workspace_root: Path, default_profile: object) -> None:
    result = preprocess_request(
        workspace_root,
        "run-source-escape",
        _request(workspace_root, repo="repos/safe", source="../fragile", function="fragile_parse"),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )

    assert not result.ready
    assert result.terminal_status is TerminalStatus.NEEDS_INPUT
    assert "inside repository" in (result.reason or "")


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
    assert "model_api_key" in (result.reason or "")


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
    (workspace_root / "repos" / "safe" / "src" / "safe.c").write_text(
        "int safe_parse(const unsigned char *data, unsigned long size) {\n/*"
        + "x" * 64 * 1024
        + "*/\nreturn size ? data[0] : 0;\n}\n",
        encoding="utf-8",
    )
    result = preprocess_request(
        workspace_root,
        "run-8",
        _request(workspace_root, max_context_kb=16),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    total = sum(len(ctx.content) for ctx in result.contexts)
    assert total <= 16 * 1024
    assert any(ctx.truncated for ctx in result.contexts)


def test_context_budget_default_applies(workspace_root: Path, default_profile: object) -> None:
    (workspace_root / "repos" / "safe" / "src" / "safe.c").write_text(
        "int safe_parse(const unsigned char *data, unsigned long size) {\n/*"
        + "x" * 128 * 1024
        + "*/\nreturn size ? data[0] : 0;\n}\n",
        encoding="utf-8",
    )
    result = preprocess_request(
        workspace_root,
        "run-9",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    total = sum(len(ctx.content) for ctx in result.contexts)
    assert total <= 96 * 1024
    assert any(ctx.truncated for ctx in result.contexts)


def test_complex_project_skips_unrelated_files(workspace_root: Path, default_profile: object) -> None:
    # A large project with dependencies and unrelated files. Only the four
    # baseline kRepo contexts may enter preprocess.json.
    src = workspace_root / "repos" / "complex"
    _write(
        src,
        "src/target.c",
        '#include "dep.h"\nint target_fn(const uint8_t *d, size_t s) { return dep_convert(d[0]); }\n',
    )
    _write(src, "src/dep.h", "int dep_convert(unsigned char c);\n")
    _write(src, "src/dep.c", '#include "dep.h"\nint dep_convert(unsigned char c) { return (int)c; }\n')
    _write(src, "src/unrelated.c", "int unrelated_big_parser(const char *p) { return p ? 0 : 1; }\n")
    _write(src, "tests/test_other.c", "int main(void) { return 0; }\n")
    _write(src, "docs/notes.c", "/* notes */\n")
    result = preprocess_request(
        workspace_root,
        "run-complex",
        _request(workspace_root, repo=str(src), function="target_fn", max_context_kb=64),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    paths = [ctx.path for ctx in result.contexts]
    assert paths == [
        "src/target.c",
        "analysis/incomingTree.json",
        "analysis/outgoingTree.json",
        "analysis/param_constraints.json",
    ]
    assert "src/dep.h" not in paths
    assert "src/dep.c" not in paths  # neither a match nor included
    assert "src/unrelated.c" not in paths
    assert "tests/test_other.c" not in paths
    assert "docs/notes.c" not in paths


def test_transitive_includes_are_not_embedded(workspace_root: Path, default_profile: object) -> None:
    src = workspace_root / "repos" / "chain"
    _write(src, "src/target.c", '#include "a.h"\nint tgt(const uint8_t *d, size_t s) { return a_get(d[0]); }\n')
    _write(src, "src/a.h", '#include "b.h"\nint a_get(unsigned char c);\n')
    _write(src, "src/b.h", "int b_helper(int);\n")
    result = preprocess_request(
        workspace_root,
        "run-chain",
        _request(workspace_root, repo=str(src), function="tgt", max_context_kb=32),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    paths = [ctx.path for ctx in result.contexts]
    assert "src/a.h" not in paths
    assert "src/b.h" not in paths
    assert {ctx.kind for ctx in result.contexts} == {
        "target_function",
        "incoming_tree",
        "outgoing_tree",
        "param_constraints",
    }


def test_same_basename_header_is_not_embedded(workspace_root: Path, default_profile: object) -> None:
    src = workspace_root / "repos" / "noinc"
    _write(
        src,
        "src/target.c",
        "static int helper(const uint8_t *d) { return 0; }\n"
        "int target_fn(const uint8_t *d, size_t s) { return helper(d); }\n",
    )
    _write(src, "src/target.h", "#ifndef TARGET_H\n#define TARGET_H\n#endif\n")
    result = preprocess_request(
        workspace_root,
        "run-noinc",
        _request(workspace_root, repo=str(src), function="target_fn", max_context_kb=32),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    paths = [ctx.path for ctx in result.contexts]
    assert "src/target.c" in paths
    assert "src/target.h" not in paths


def test_large_target_file_keeps_only_raw_function_fragment(workspace_root: Path, default_profile: object) -> None:
    src = workspace_root / "repos" / "deep"
    body = "int deep_fn(const uint8_t *d, size_t s) { return d[s - 1]; }\n"
    _write(src, "src/deep.c", "/* " + "x" * (300 * 1024) + " */\n" + body)
    result = preprocess_request(
        workspace_root,
        "run-deep",
        _request(workspace_root, repo=str(src), function="deep_fn", max_context_kb=96),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    ctx = next(item for item in result.contexts if item.path == "src/deep.c")
    assert ctx.kind == "target_function"
    assert not ctx.truncated
    assert "deep_fn" in ctx.content
    assert "x" * 100 not in ctx.content
    assert ctx.start_line == 2


def test_callers_are_replaced_by_krepo_call_trees(workspace_root: Path, default_profile: object) -> None:
    src = workspace_root / "repos" / "callers"
    _write(
        src,
        "src/target.c",
        "int target_fn(const uint8_t *d, size_t s) { return d[s - 1]; }\n",
    )
    _write(
        src,
        "tests/huge_caller.c",
        "int target_fn(const uint8_t *d, size_t s);\n"
        + "/* pad */\n"
        + "int call_it(const uint8_t *d) { return target_fn(d, 1); }\n"
        + "x" * (300 * 1024),
    )
    result = preprocess_request(
        workspace_root,
        "run-callers",
        _request(workspace_root, repo=str(src), function="target_fn", max_context_kb=64),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    paths = [ctx.path for ctx in result.contexts]
    assert "src/target.c" in paths
    assert "tests/huge_caller.c" not in paths
    assert "analysis/incomingTree.json" in paths
    assert "analysis/outgoingTree.json" in paths
    assert "analysis/param_constraints.json" in paths
    assert next(item for item in result.contexts if item.kind == "incoming_tree").content
    assert next(item for item in result.contexts if item.kind == "outgoing_tree").content


def test_angle_bracket_and_missing_includes_ignored(workspace_root: Path, default_profile: object) -> None:
    src = workspace_root / "repos" / "sysinc"
    _write(
        src,
        "src/target.c",
        '#include <stdint.h>\n#include "missing.h"\nint target_fn(const uint8_t *d, size_t s) { return d[s - 1]; }\n',
    )
    _write(src, "src/other.h", "int other(void);\n")
    result = preprocess_request(
        workspace_root,
        "run-sysinc",
        _request(workspace_root, repo=str(src), function="target_fn", max_context_kb=32),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    paths = [ctx.path for ctx in result.contexts]
    assert "src/target.c" in paths
    # missing.h does not exist and other.h is not referenced: both must stay out.
    assert "src/other.h" not in paths


def test_build_dir_mode_excludes_build_files(workspace_root: Path, default_profile: object) -> None:
    src = workspace_root / "repos" / "cmake-proj"
    _write(
        src,
        "src/target.c",
        '#include <stdint.h>\n#include "target.h"\nint cmake_parse(const uint8_t *d, size_t s) { return d[s - 1]; }\n',
    )
    _write(src, "include/target.h", "int cmake_parse(const uint8_t *, size_t);\n")
    _write(src, "CMakeLists.txt", "add_library(cmake_target STATIC src/target.c)\n")
    _write(src, "build.sh", "#!/bin/sh\nexit 0\n")
    result = preprocess_request(
        workspace_root,
        "run-build-dir",
        _request(workspace_root, repo=str(src), function="cmake_parse", build_dir=str(src)),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert result.ready
    assert result.build_dir == src.resolve()
    paths = [ctx.path for ctx in result.contexts]
    assert "src/target.c" in paths
    assert "include/target.h" not in paths
    assert "CMakeLists.txt" not in paths
    assert not any(ctx.path in {"CMakeLists.txt", "Makefile", "build.sh"} for ctx in result.contexts)


def test_no_build_dir_still_excludes_build_files(workspace_root: Path, default_profile: object) -> None:
    (workspace_root / "repos" / "safe" / "Makefile").write_text(
        "all:\n\tclang -o fuzzer src/safe.c\n", encoding="utf-8"
    )
    result = preprocess_request(
        workspace_root,
        "run-no-build-dir",
        _request(workspace_root),
        default_profile,  # type: ignore[arg-type]
        check_runtime=False,
    )
    assert result.build_dir is None
    paths = [ctx.path for ctx in result.contexts]
    assert "src/safe.c" in paths
    assert "Makefile" not in paths
