"""CLI command tests via typer.testing.CliRunner (no model backend required)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from goaloop.cli import app
from goaloop.models import FuzzRunRequest, GenerationGoal, Phase, RunState, TerminalStatus
from goaloop.storage import ArtifactStore

runner = CliRunner()


def _make_run(
    workspace_root: Path,
    run_id: str,
    *,
    project: str = "safe",
    terminal: TerminalStatus | None = None,
    with_report: bool = False,
) -> Path:
    store = ArtifactStore(workspace_root, project, run_id)
    store.initialize()
    state = RunState(
        run_id=run_id,
        project_name=project,
        request=FuzzRunRequest(source=f"repos/{project}", function="safe_parse"),
        phase=Phase.CRASH_ANALYSIS_REPORT,
        terminal_status=terminal,
        goal=GenerationGoal(
            run_id=run_id,
            objective="o",
            target_function="safe_parse",
            acceptance_criteria=["a"],
            max_generation_loops=3,
            current_loop=1,
        ),
    )
    store.save_state(state)
    if with_report:
        (store.run_dir / "report.md").write_text("# Fuzz Harness Validation Report\n\nstatus: ok\n", encoding="utf-8")
        (store.run_dir / "validation.json").write_text(
            json.dumps({"run_id": run_id, "status": "harness_verified"}), encoding="utf-8"
        )
    return store.run_dir


class TestDoctor:
    def test_ready_with_key(self, workspace_root: Path) -> None:
        result = runner.invoke(app, ["doctor", "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        assert "environment is ready" in result.output

    def test_missing_profile(self, workspace_root: Path) -> None:
        result = runner.invoke(app, ["doctor", "--profile", "nope", "--workspace", str(workspace_root)])
        assert result.exit_code == 1
        assert "invalid" in result.output

    def test_sandboxed_profile_requires_bwrap(self, workspace_root: Path, monkeypatch) -> None:
        # Pretend bwrap is missing so the outcome is deterministic everywhere.
        import shutil

        original_which = shutil.which
        monkeypatch.setattr(shutil, "which", lambda name: None if name == "bwrap" else original_which(name))
        result = runner.invoke(app, ["doctor", "--profile", "sandboxed", "--workspace", str(workspace_root)])
        assert result.exit_code == 1
        assert "bubblewrap" in result.output


class TestStatus:
    def test_missing_run(self, workspace_root: Path) -> None:
        result = runner.invoke(app, ["status", "--run-id", "no-such-run", "--workspace", str(workspace_root)])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_valid_run_summary(self, workspace_root: Path) -> None:
        run_id = "run-cli-status"
        _make_run(workspace_root, run_id, terminal=TerminalStatus.HARNESS_VERIFIED)
        result = runner.invoke(app, ["status", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        assert "run-cli-status" in result.output
        assert "harness_verified" in result.output

    def test_valid_run_json(self, workspace_root: Path) -> None:
        run_id = "run-cli-json"
        _make_run(workspace_root, run_id, terminal=TerminalStatus.HARNESS_VERIFIED)
        result = runner.invoke(app, ["status", "--run-id", run_id, "--json", "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["run_id"] == run_id
        assert data["terminal_status"] == "harness_verified"


class TestReport:
    def test_missing_run(self, workspace_root: Path) -> None:
        result = runner.invoke(app, ["report", "--run-id", "no-such-run", "--workspace", str(workspace_root)])
        assert result.exit_code != 0

    def test_markdown_report(self, workspace_root: Path) -> None:
        run_id = "run-cli-report"
        _make_run(workspace_root, run_id, terminal=TerminalStatus.HARNESS_VERIFIED, with_report=True)
        result = runner.invoke(app, ["report", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        assert "Fuzz Harness Validation Report" in result.output

    def test_json_report(self, workspace_root: Path) -> None:
        run_id = "run-cli-report-json"
        _make_run(workspace_root, run_id, terminal=TerminalStatus.HARNESS_VERIFIED, with_report=True)
        result = runner.invoke(
            app, ["report", "--run-id", run_id, "--format", "json", "--workspace", str(workspace_root)]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["run_id"] == run_id

    def test_report_not_produced(self, workspace_root: Path) -> None:
        run_id = "run-cli-noreport"
        _make_run(workspace_root, run_id, terminal=TerminalStatus.BLOCKED, with_report=False)
        result = runner.invoke(app, ["report", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert result.exit_code == 1
        assert "not produced" in result.output


class TestRun:
    def test_missing_source_is_needs_input(self, workspace_root: Path) -> None:
        result = runner.invoke(
            app,
            [
                "run",
                "--source",
                "repos/does-not-exist",
                "--function",
                "f",
                "--workspace",
                str(workspace_root),
                "--max-generation-loops",
                "1",
                "--fuzz-seconds",
                "1",
            ],
        )
        assert result.exit_code == 0
        assert "needs_input" in result.output

    def test_invalid_language(self, workspace_root: Path) -> None:
        result = runner.invoke(
            app,
            [
                "run",
                "--source",
                "repos/safe",
                "--function",
                "safe_parse",
                "--language",
                "pascal",
                "--workspace",
                str(workspace_root),
            ],
        )
        assert result.exit_code != 0


class TestResume:
    def test_missing_run(self, workspace_root: Path) -> None:
        result = runner.invoke(app, ["resume", "--run-id", "no-such-run", "--workspace", str(workspace_root)])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestEvaluate:
    def test_missing_manifest(self, workspace_root: Path) -> None:
        result = runner.invoke(
            app, ["evaluate", str(workspace_root / "no-suite.json"), "--workspace", str(workspace_root)]
        )
        assert result.exit_code != 0

    def test_empty_manifest(self, workspace_root: Path) -> None:
        manifest = workspace_root / "empty-suite.json"
        manifest.write_text('{"entries": []}', encoding="utf-8")
        result = runner.invoke(app, ["evaluate", str(manifest), "--workspace", str(workspace_root)])
        assert result.exit_code == 1
        assert "no entries" in result.output

    def test_malformed_manifest(self, workspace_root: Path) -> None:
        manifest = workspace_root / "bad-suite.json"
        manifest.write_text("not json", encoding="utf-8")
        result = runner.invoke(app, ["evaluate", str(manifest), "--workspace", str(workspace_root)])
        assert result.exit_code != 0


class TestModelOverrides:
    def test_override_helper_merges(self) -> None:
        from goaloop.cli import _apply_model_overrides
        from goaloop.models import ModelProfile

        base = ModelProfile(name="default")
        merged = _apply_model_overrides(base, "gpt-4o", "https://proxy.example/v1", None)
        assert merged.model == "gpt-4o"
        assert merged.base_url == "https://proxy.example/v1"
        assert merged.provider == "deepseek-official"  # unchanged

    def test_override_helper_injects_api_key(self, monkeypatch) -> None:
        import os

        from goaloop.cli import _apply_model_overrides
        from goaloop.models import ModelProfile

        monkeypatch.delenv("CUSTOM_MODEL_KEY", raising=False)
        base = ModelProfile(name="custom", api_key_env="CUSTOM_MODEL_KEY")
        _apply_model_overrides(base, None, None, "secret-token")
        assert os.environ.get("CUSTOM_MODEL_KEY") == "secret-token"

    def test_run_accepts_model_override_flags(self, workspace_root: Path) -> None:
        result = runner.invoke(
            app,
            [
                "run",
                "--source",
                "repos/does-not-exist",
                "--function",
                "f",
                "--model-profile",
                "default",
                "--model-name",
                "gpt-4o",
                "--base-url",
                "https://proxy.example/v1",
                "--workspace",
                str(workspace_root),
                "--max-generation-loops",
                "1",
                "--fuzz-seconds",
                "1",
            ],
        )
        assert result.exit_code == 0
        assert "needs_input" in result.output

    def test_resume_accepts_api_key_flag(self, workspace_root: Path, monkeypatch) -> None:
        import os

        run_id = "run-cli-override"
        _make_run(workspace_root, run_id, terminal=TerminalStatus.BLOCKED)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        result = runner.invoke(
            app,
            [
                "resume",
                "--run-id",
                run_id,
                "--api-key",
                "cli-supplied-key",
                "--workspace",
                str(workspace_root),
            ],
        )
        # resume of a terminal blocked run re-renders the report without calling
        # the model, but the CLI must parse the flags and inject the key.
        assert result.exit_code == 0
        assert os.environ.get("DEEPSEEK_API_KEY") == "cli-supplied-key"
