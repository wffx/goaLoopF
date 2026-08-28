"""Preprocess tests: scoping, symbol lookup, language detection, path escape."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    assert [context.path for context in result.contexts] == ["second/target.c"]
    assert "return 2" in result.contexts[0].content


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
    assert [context.path for context in result.contexts] == ["src/second.c"]


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
    (workspace_root / "repos" / "safe" / "src" / "huge.c").write_text(
        "// safe_parse\n" + "x" * 512 * 1024, encoding="utf-8"
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
    (workspace_root / "repos" / "safe" / "src" / "huge.c").write_text(
        "// safe_parse\n" + "x" * 512 * 1024, encoding="utf-8"
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
    # A large project: the target file, its include closure, and many
    # unrelated files. Only relevant files may enter preprocess.json.
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
    assert "src/target.c" in paths
    assert "src/dep.h" in paths  # include closure
    assert "src/dep.c" not in paths  # neither a match nor included
    assert "src/unrelated.c" not in paths
    assert "tests/test_other.c" not in paths
    assert "docs/notes.c" not in paths


def test_include_closure_is_transitive(workspace_root: Path, default_profile: object) -> None:
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
    assert "src/a.h" in paths
    assert "src/b.h" in paths


def test_same_basename_header_included_without_symbol(workspace_root: Path, default_profile: object) -> None:
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
    assert "src/target.h" in paths


def test_large_target_file_uses_symbol_window(workspace_root: Path, default_profile: object) -> None:
    # The target symbol sits ~300 KiB into a huge file: the head-only
    # truncation would miss it entirely; the symbol window must not.
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
    assert ctx.truncated
    assert "deep_fn" in ctx.content
    assert len(ctx.content) <= 64 * 1024


def test_callers_deprioritized_over_definition(workspace_root: Path, default_profile: object) -> None:
    # A huge caller (test) file merely invokes the function: it must be
    # capped at the reference tier and never crowd out the definition.
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
    assert paths.index("src/target.c") < paths.index("tests/huge_caller.c")
    caller = next(item for item in result.contexts if item.path == "tests/huge_caller.c")
    definition = next(item for item in result.contexts if item.path == "src/target.c")
    assert caller.truncated
    assert len(caller.content) <= 16 * 1024
    assert not definition.truncated


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
    assert "include/target.h" in paths
    # Build-file contents are redundant when the controller builds the
    # project itself: only the resolved path is exposed.
    assert "CMakeLists.txt" not in paths
    assert not any(ctx.path in {"CMakeLists.txt", "Makefile"} for ctx in result.contexts)


def test_no_build_dir_keeps_build_files(workspace_root: Path, default_profile: object) -> None:
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
    assert "Makefile" in paths  # the model must infer build params on its own
