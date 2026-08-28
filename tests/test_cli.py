"""CLI command tests via typer.testing.CliRunner (no model backend required)."""

from __future__ import annotations

import json
import os
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
    output_root: Path | None = None,
) -> Path:
    store = ArtifactStore(workspace_root, project, run_id, output_root=output_root)
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
    def test_missing_repo_is_needs_input(self, workspace_root: Path) -> None:
        result = runner.invoke(
            app,
            [
                "run",
                "--repo",
                "repos/does-not-exist",
                "--source",
                ".",
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

    def test_missing_source_scope_is_needs_input(self, workspace_root: Path) -> None:
        result = runner.invoke(
            app,
            [
                "run",
                "--repo",
                "repos/safe",
                "--source",
                "src/does-not-exist.c",
                "--function",
                "safe_parse",
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
        assert "source path does not exist" in result.output

    def test_invalid_language(self, workspace_root: Path) -> None:
        result = runner.invoke(
            app,
            [
                "run",
                "--repo",
                "repos/safe",
                "--source",
                ".",
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


class TestOutputDir:
    def test_run_with_output_relocates_products(self, workspace_root: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = runner.invoke(
            app,
            [
                "run",
                "--repo",
                "repos/does-not-exist",
                "--source",
                ".",
                "--function",
                "f",
                "--workspace",
                str(workspace_root),
                "--output",
                str(out),
                "--max-generation-loops",
                "1",
                "--fuzz-seconds",
                "1",
            ],
        )
        assert result.exit_code == 0
        assert "needs_input" in result.output
        runs = list((out / "does-not-exist" / "runs").glob("*"))
        assert len(runs) == 1
        # the default work/ layout must not receive this run
        assert not list((workspace_root / "work" / "does-not-exist" / "runs").glob("*"))

    def test_status_requires_same_output(self, workspace_root: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        run_id = "run-out-cli"
        _make_run(workspace_root, run_id, terminal=TerminalStatus.HARNESS_VERIFIED, output_root=out)
        # not found without --output
        missing = runner.invoke(app, ["status", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert missing.exit_code != 0
        assert "not found" in missing.output
        # found with --output
        found = runner.invoke(
            app, ["status", "--run-id", run_id, "--output", str(out), "--workspace", str(workspace_root)]
        )
        assert found.exit_code == 0
        assert run_id in found.output

    def test_missing_run_hints_at_output(self, workspace_root: Path) -> None:
        result = runner.invoke(app, ["status", "--run-id", "no-such-run", "--workspace", str(workspace_root)])
        assert result.exit_code != 0
        assert "--output" in result.output


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
                "--repo",
                "repos/does-not-exist",
                "--source",
                ".",
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
        # A terminal run that must NOT be recovered (otherwise resume would try
        # a real model call with the injected test key).
        _make_run(workspace_root, run_id, terminal=TerminalStatus.NEEDS_INPUT)
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



class TestProfileApiKey:
    def test_load_api_key_from_toml(self, tmp_path: Path) -> None:
        from goaloop.config import load_model_profile

        (tmp_path / "model-profiles").mkdir()
        (tmp_path / "model-profiles" / "keyed.toml").write_text(
            'name = "keyed"\nprovider = "openai"\nmodel = "gpt-4o"\napi_key = "sk-toml-key"\n',
            encoding="utf-8",
        )
        profile = load_model_profile("keyed", tmp_path)
        assert profile.api_key == "sk-toml-key"

    def test_cli_overrides_profile_api_key(self, monkeypatch) -> None:
        from goaloop.cli import _apply_model_overrides
        from goaloop.models import ModelProfile

        monkeypatch.delenv("CUSTOM_KEY", raising=False)
        base = ModelProfile(name="k", api_key="sk-toml", api_key_env="CUSTOM_KEY")
        _apply_model_overrides(base, None, None, "sk-cli")
        assert os.environ.get("CUSTOM_KEY") == "sk-cli"

    def test_profile_api_key_used_when_no_cli(self, monkeypatch) -> None:
        from goaloop.cli import _apply_model_overrides
        from goaloop.models import ModelProfile

        monkeypatch.delenv("CUSTOM_KEY", raising=False)
        base = ModelProfile(name="k", api_key="sk-toml", api_key_env="CUSTOM_KEY")
        _apply_model_overrides(base, None, None, None)
        assert os.environ.get("CUSTOM_KEY") == "sk-toml"

    def test_doctor_reports_profile_api_key(self, workspace_root: Path, monkeypatch) -> None:
        (workspace_root / "model-profiles" / "keyed.toml").write_text(
            'name = "keyed"\nprovider = "openai"\nmodel = "gpt-4o"\napi_key = "sk-profile-key"\n',
            encoding="utf-8",
        )
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        result = runner.invoke(
            app, ["doctor", "--model-profile", "keyed", "--workspace", str(workspace_root)]
        )
        assert result.exit_code == 0
        assert "profile api_key" in result.output


class TestProviderBaseUrl:
    def test_provider_base_url_env_convention(self) -> None:
        from goaloop.cli import _provider_base_url_env

        assert _provider_base_url_env("custom-gateway") == "CUSTOM_GATEWAY_BASE_URL"
        assert _provider_base_url_env("openai") == "OPENAI_BASE_URL"
        assert _provider_base_url_env("deepseek-official") == "DEEPSEEK_OFFICIAL_BASE_URL"

    def test_cli_base_url_injects_provider_env(self, monkeypatch) -> None:
        from goaloop.cli import _apply_model_overrides
        from goaloop.models import ModelProfile

        monkeypatch.delenv("CUSTOM_GATEWAY_BASE_URL", raising=False)
        base = ModelProfile(name="pi-ai-custom", provider="custom-gateway", model="local-model")
        _apply_model_overrides(base, None, "http://my-gateway:9000/v1", None)
        assert os.environ.get("CUSTOM_GATEWAY_BASE_URL") == "http://my-gateway:9000/v1"

    def test_profile_base_url_injects_provider_env(self, monkeypatch) -> None:
        from goaloop.cli import _apply_model_overrides
        from goaloop.models import ModelProfile

        monkeypatch.delenv("CUSTOM_GATEWAY_BASE_URL", raising=False)
        base = ModelProfile(
            name="pi-ai-custom",
            provider="custom-gateway",
            model="local-model",
            base_url="http://profile-host:7000/v1",
        )
        _apply_model_overrides(base, None, None, None)
        assert os.environ.get("CUSTOM_GATEWAY_BASE_URL") == "http://profile-host:7000/v1"

    def test_pi_ai_custom_profile_has_base_url(self) -> None:
        import tomllib
        from pathlib import Path

        data = tomllib.loads(
            Path("model-profiles/pi-ai-custom.toml").read_text(encoding="utf-8")
        )
        assert data["base_url"] == "http://localhost:8000/v1"


class TestStatusReason:
    def test_status_shows_terminal_reason(self, workspace_root: Path) -> None:
        run_id = "run-cli-reason"
        run_dir = _make_run(workspace_root, run_id, terminal=TerminalStatus.FAILED)
        (run_dir / "validation.json").write_text(
            json.dumps(
                {"run_id": run_id, "status": "failed", "reason": "generation loop budget exhausted after 3 loop(s)"}
            ),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["status", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        assert "reason: generation loop budget exhausted" in result.output
        assert "goaloop report --run-id" in result.output
        assert "events.jsonl" in result.output

    def test_status_falls_back_to_events(self, workspace_root: Path) -> None:
        run_id = "run-cli-events-reason"
        run_dir = _make_run(workspace_root, run_id, terminal=TerminalStatus.BLOCKED)
        # no validation.json; reason only in events.jsonl
        (run_dir / "events.jsonl").write_text(
            json.dumps({"kind": "run:terminal", "payload": {"status": "blocked", "reason": "cmake configure failed"}})
            + "\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["status", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        assert "reason: cmake configure failed" in result.output


class TestNoCandidateWarning:
    def test_status_warns_when_no_harness(self, workspace_root: Path) -> None:
        run_id = "run-no-candidate"
        run_dir = _make_run(workspace_root, run_id, terminal=TerminalStatus.BLOCKED)
        (run_dir / "validation.json").write_text(
            json.dumps({"run_id": run_id, "status": "blocked", "reason": "model_api_key missing"}),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["status", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        assert "未生成任何 harness 候选" in result.output

    def test_status_no_warning_when_harness_exists(self, workspace_root: Path) -> None:
        run_id = "run-with-candidate"
        run_dir = _make_run(workspace_root, run_id, terminal=TerminalStatus.BLOCKED)
        candidate = run_dir / "iterations" / "loop-01" / "candidate"
        candidate.mkdir(parents=True)
        (candidate / "harness.c").write_text("int main(){}", encoding="utf-8")
        result = runner.invoke(app, ["status", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        assert "未生成任何 harness 候选" not in result.output
