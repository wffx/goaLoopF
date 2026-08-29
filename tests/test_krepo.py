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


def _payload(source_file: Path) -> dict[str, object]:
    location = f"{source_file}:7-9"
    return {
        "schema_version": 2,
        "source": "int target_fn(void) {\n    return 1;\n}",
        "target": {"name": "target_fn", "location": location},
        "incoming_tree": [f"Target: target_fn ({location})", "Incoming call tree:", "target_fn"],
        "outgoing_tree": [f"Target: target_fn ({location})", "Outgoing call tree:", "target_fn"],
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
    _write_cli(cli, _payload(source))
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))

    report = read_krepo_report(workspace, repo, source, "target_fn")

    assert report.source.startswith("int target_fn")
    assert report.start_line == 7
    assert report.incoming_tree[1] == "Incoming call tree:"
    assert report.outgoing_tree[1] == "Outgoing call tree:"
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


def test_rejects_legacy_report_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    source = repo / "target.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target_fn(void) { return 1; }\n", encoding="utf-8")
    payload = _payload(source)
    payload.pop("schema_version")
    cli = tmp_path / "legacy-krepo.py"
    _write_cli(cli, payload)
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))

    with pytest.raises(KRepoError, match="schema_version must be 2"):
        read_krepo_report(workspace, repo, source, "target_fn")


def test_rejects_unexpected_selected_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    source = repo / "target.c"
    other = repo / "other.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target_fn(void) { return 1; }\n", encoding="utf-8")
    cli = tmp_path / "wrong-krepo.py"
    _write_cli(cli, _payload(other))
    monkeypatch.setenv("GOALOOP_KREPO", str(cli))

    with pytest.raises(KRepoError, match="unexpected same-name function"):
        read_krepo_report(workspace, repo, source, "target_fn")
