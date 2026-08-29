"""Read-only kRepo subprocess adapter tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from goaloop.krepo import KRepoError, krepo_cli_path, read_krepo_report


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

    report = read_krepo_report(workspace, repo, source, "target_fn")

    assert report.source.startswith("int target_fn")
    assert report.start_line == 1
    assert report.incoming_tree[0] == "Incoming call tree:"
    assert report.outgoing_tree[0] == "Outgoing call tree:"
    assert report.param_constraints[0]["name"] == "enabled"
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
