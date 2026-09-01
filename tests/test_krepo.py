"""Read-only kRepo subprocess adapter tests."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from goaloop.krepo import (
    KRepoError,
    KRepoQueryService,
    KRepoSymbolQuery,
    krepo_cli_path,
    query_krepo_symbol,
    read_krepo_report,
)


def _write_cli(path: Path, payload: object, *, exit_code: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import json, os, sys\n"
        "assert 'DEEPSEEK_API_KEY' not in os.environ\n"
        f"payload = {payload!r}\n"
        "print(json.dumps(payload))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )


def _payload() -> dict[str, object]:
    return {
        "source": "int target_fn(void) { return 1; }",
        "incoming_tree": ["Incoming call tree:", "target_fn"],
        "outgoing_tree": ["Outgoing call tree:", "target_fn"],
        "param_constraints": [{"name": "enabled", "type": "int", "constraints": ["0 or 1"]}],
    }


def test_resolves_current_directory_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    krepo_root = tmp_path / "kRepo"
    current_cli = krepo_root / "main.py"
    current_cli.parent.mkdir(parents=True)
    current_cli.touch()
    monkeypatch.setenv("GOALOOP_KREPO", str(krepo_root))

    assert krepo_cli_path(tmp_path) == current_cli.resolve()


def test_reads_report_json_without_writing_krepo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repos" / "sample"
    source = repo / "src" / "target.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target_fn(void) { return 1; }\n", encoding="utf-8")
    cli = tmp_path / "fake-krepo" / "main.py"
    _write_cli(cli, _payload())
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))
    commands: list[list[str]] = []

    report = read_krepo_report(workspace, repo, source, "target_fn", on_command=commands.append)

    assert report.source.startswith("int target_fn")
    assert report.start_line == 1
    assert report.incoming_tree[0] == "Incoming call tree:"
    assert report.outgoing_tree[0] == "Outgoing call tree:"
    assert report.param_constraints[0]["name"] == "enabled"
    assert commands == [[
        sys.executable,
        str(cli.resolve()),
        "report",
        "target_fn",
        "--repo",
        str(repo),
        "--file",
        "src/target.c",
        "--format",
        "json",
    ]]
    assert not list(cli.parent.rglob("__pycache__"))


def test_rejects_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    source = repo / "target.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target_fn(void) { return 1; }\n", encoding="utf-8")
    cli = tmp_path / "bad-krepo.py"
    cli.write_text("print('not json')\n", encoding="utf-8")
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))

    with pytest.raises(KRepoError, match="not valid JSON"):
        read_krepo_report(workspace, repo, source, "target_fn")


def test_ignores_report_schema_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    source = repo / "target.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target_fn(void) { return 1; }\n", encoding="utf-8")
    payload = _payload()
    payload["schema_version"] = 999
    cli = tmp_path / "future-krepo.py"
    _write_cli(cli, payload)
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))

    report = read_krepo_report(workspace, repo, source, "target_fn")

    assert report.source.startswith("int target_fn")


def test_accepts_camel_case_tree_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    source = repo / "target.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target_fn(void) { return 1; }\n", encoding="utf-8")
    payload = _payload()
    payload["incomingTree"] = payload.pop("incoming_tree")
    payload["outgoingTree"] = payload.pop("outgoing_tree")
    cli = tmp_path / "camel-krepo.py"
    _write_cli(cli, payload)
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))

    report = read_krepo_report(workspace, repo, source, "target_fn")

    assert report.incoming_tree[0] == "Incoming call tree:"


def test_requires_param_constraints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    source = repo / "target.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target_fn(void) { return 1; }\n", encoding="utf-8")
    payload = _payload()
    payload.pop("param_constraints")
    cli = tmp_path / "missing-params.py"
    _write_cli(cli, payload)
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))

    with pytest.raises(KRepoError, match="param_constraints"):
        read_krepo_report(workspace, repo, source, "target_fn")


def test_queries_symbol_with_bounded_read_only_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    cli = tmp_path / "symbol-krepo.py"
    _write_cli(cli, {"symbol": "packet_t", "candidates": [{"snippet": "typedef int packet_t;"}]})
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))
    commands: list[list[str]] = []

    output = query_krepo_symbol(
        workspace,
        repo,
        "packet_t",
        function="parse_packet",
        kind="typedef",
        file_filter="include/packet.h",
        on_command=commands.append,
    )

    assert "typedef int packet_t" in output
    assert commands[0][:4] == [sys.executable, str(cli.resolve()), "symbol", "packet_t"]
    assert commands[0][4:8] == ["--repo", str(repo.resolve()), "--function", "parse_packet"]
    assert "--max-candidates" not in commands[0]
    assert "--max-snippet-lines" not in commands[0]
    assert commands[0][-4:] == ["--kind", "typedef", "--file", "include/packet.h"]


@pytest.mark.parametrize("file_filter", ["../secret.h", "/etc/passwd", "a/../../secret.h"])
def test_symbol_query_rejects_unsafe_file_filter(
    tmp_path: Path, file_filter: str
) -> None:
    with pytest.raises(KRepoError, match="unsafe kRepo file filter"):
        query_krepo_symbol(
            tmp_path,
            tmp_path,
            "packet_t",
            function="parse_packet",
            file_filter=file_filter,
        )


def test_query_service_caches_and_audits_identical_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    cli = tmp_path / "cached-krepo.py"
    _write_cli(cli, {"symbol": "LIMIT", "candidates": [{"snippet": "#define LIMIT 8"}]})
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))
    commands: list[list[str]] = []
    service = KRepoQueryService(
        workspace,
        repo,
        tmp_path / "run" / "krepo-queries",
        "parse_config",
    )
    query = KRepoSymbolQuery(symbol="LIMIT", kind="macro")

    first = service.query(query, on_command=commands.append)
    second = service.query(query, on_command=commands.append)

    assert first == second
    assert len(commands) == 1
    audit = [json.loads(line) for line in service.audit_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["cache_hit"] for entry in audit] == [False, True]
    assert audit[0]["command_executed"] is True
    assert audit[0]["argv"] == commands[0]
    assert audit[0]["command"] == shlex.join(commands[0])
    assert audit[0]["cwd"] == str(repo.resolve())
    assert audit[0]["query"]["function"] == "parse_config"
    assert audit[1]["command_executed"] is False
    assert audit[1]["command"] is None
    assert audit[1]["argv"] is None


def test_query_service_audits_failed_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    cli = tmp_path / "failing-krepo.py"
    _write_cli(cli, {"error": "usage mismatch"}, exit_code=2)
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))
    service = KRepoQueryService(
        workspace,
        repo,
        tmp_path / "run" / "krepo-queries",
        "parse_packet",
    )

    result = service.query(KRepoSymbolQuery(symbol="packet_t", kind="typedef"))

    assert result["ok"] is False
    audit = json.loads(service.audit_path.read_text(encoding="utf-8").strip())
    assert audit["command_executed"] is True
    assert audit["argv"][:4] == [sys.executable, str(cli.resolve()), "symbol", "packet_t"]
    assert audit["argv"][4:8] == ["--repo", str(repo.resolve()), "--function", "parse_packet"]
    assert "--max-candidates" not in audit["argv"]
    assert "--max-snippet-lines" not in audit["argv"]
    assert audit["command"] == shlex.join(audit["argv"])
    assert audit["cwd"] == str(repo.resolve())
    assert "exit code 2" in audit["result"]["error"]
