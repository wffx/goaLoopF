"""CLI command tests via typer.testing.CliRunner (no model backend required)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goaloop.cli import _dsh_trace_printer, _evaluate_optimization, _event_printer, app
from goaloop.models import FuzzRunRequest, GenerationGoal, Phase, RunEvent, RunState, TerminalStatus
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
        assert "krepo" in result.output
        assert "environment is ready" in result.output

    def test_missing_krepo_is_not_ready(self, workspace_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOALOOP_KREPO", str(workspace_root / "missing-krepo"))
        result = runner.invoke(app, ["doctor", "--workspace", str(workspace_root)])
        assert result.exit_code == 1
        assert "krepo" in result.output

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
        run_dir = _make_run(workspace_root, run_id, terminal=TerminalStatus.HARNESS_VERIFIED)
        (run_dir / "optimization-suggestions.json").write_text(
            json.dumps(
                {
                    "suggestions": [
                        {
                            "priority": "medium",
                            "title": "减少候选重生成轮次",
                            "recommendation": "固化稳定的构建知识。",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["status", "--run-id", run_id, "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        assert "run-cli-status" in result.output
        assert "harness_verified" in result.output
        assert "optimization [medium] 减少候选重生成轮次" in result.output

    def test_valid_run_json(self, workspace_root: Path) -> None:
        run_id = "run-cli-json"
        _make_run(workspace_root, run_id, terminal=TerminalStatus.HARNESS_VERIFIED)
        result = runner.invoke(app, ["status", "--run-id", run_id, "--json", "--workspace", str(workspace_root)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["run_id"] == run_id
        assert data["terminal_status"] == "harness_verified"

    def test_status_reports_optimization_generation_failure(self, workspace_root: Path) -> None:
        run_id = "run-cli-optimization-failed"
        run_dir = _make_run(workspace_root, run_id, terminal=TerminalStatus.HARNESS_VERIFIED)
        (run_dir / "optimization-suggestions.json").write_text(
            json.dumps(
                {
                    "generation_status": "failed",
                    "failure_reason": "model response remained invalid",
                    "suggestions": [],
                }
            ),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["status", "--run-id", run_id, "--workspace", str(workspace_root)])

        assert result.exit_code == 0
        assert "optimization suggestions failed: model response remained invalid" in result.output


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
    def test_help_includes_debug(self) -> None:
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "--debug" in result.output

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
        assert "phase=preprocess step=started" in result.output
        assert "phase=preprocess step=completed" in result.output
        assert "step=optimization_completed" in result.output
        assert "step=optimization_failed" in result.output
        run_dirs = list((workspace_root / "work" / "does-not-exist" / "runs").glob("*"))
        assert len(run_dirs) == 1
        assert (run_dirs[0] / "optimization-suggestions.json").is_file()
        assert (run_dirs[0] / "optimization-suggestions.md").is_file()

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


class TestProgressOutput:
    def test_debug_printer_streams_redacted_dsh_trace(self, capsys, tmp_path: Path) -> None:
        printer = _dsh_trace_printer(tmp_path)
        printer(
            "session.event",
            {
                "sessionId": "run-1",
                "event": {
                    "type": "assistant/message",
                    "data": {
                        "turn": 1,
                        "step": 1,
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"read {tmp_path}/src/a.c with sk-123456789",
                                }
                            ]
                        },
                    },
                },
            },
        )

        output = capsys.readouterr().out
        assert output.startswith("[goaloop][debug][dsh] ")
        assert "assistant response turn=1 step=1" in output
        assert "sk-123456789" not in output
        assert str(tmp_path) not in output
        assert "<redacted>" in output
        assert "<workspace>" in output

    def test_debug_printer_hides_session_title(self, capsys, tmp_path: Path) -> None:
        printer = _dsh_trace_printer(tmp_path)
        printer(
            "session.event",
            {
                "sessionId": "run-1",
                "event": {"type": "session/title", "data": {"title": "large generated title"}},
            },
        )

        assert capsys.readouterr().out == ""

    def test_default_printer_shows_phase_and_step(self, capsys) -> None:
        printer = _event_printer(False)
        printer(
            RunEvent(
                sequence=1,
                phase=Phase.HARNESS_GENERATION,
                kind="generation:model_started",
                payload={"loop": 2, "max_loops": 5},
            )
        )

        output = capsys.readouterr().out
        assert "phase=harness_generation" in output
        assert "step=model_generation_started" in output
        assert "loop=2" in output
        assert "details=" not in output

    def test_verbose_printer_includes_payload(self, capsys) -> None:
        printer = _event_printer(True)
        printer(
            RunEvent(
                sequence=1,
                phase=Phase.HARNESS_EXECUTION,
                kind="execution:fuzz_started",
                payload={"loop": 1, "seconds": 600},
            )
        )

        output = capsys.readouterr().out
        assert "phase=harness_execution" in output
        assert "step=fuzz_started" in output
        assert 'details={"loop": 1, "seconds": 600}' in output

    def test_default_printer_shows_krepo_command(self, capsys) -> None:
        printer = _event_printer(False)
        printer(
            RunEvent(
                sequence=1,
                phase=Phase.PREPROCESS,
                kind="preprocess:krepo_command",
                payload={
                    "argv": [
                        "/usr/bin/python3",
                        "/tmp/tools/kRepo/main.py",
                        "report",
                        "target_fn",
                        "--repo",
                        "/tmp/repo with space",
                        "--format",
                        "json",
                    ]
                },
            )
        )

        output = capsys.readouterr().out
        assert "phase=preprocess" in output
        assert "step=krepo_command" in output
        assert "/tmp/tools/kRepo/main.py report target_fn" in output
        assert "'/tmp/repo with space'" in output

    def test_default_printer_shows_optimization_summary(self, capsys) -> None:
        printer = _event_printer(False)
        printer(
            RunEvent(
                sequence=1,
                phase=Phase.CRASH_ANALYSIS_REPORT,
                kind="optimization:completed",
                payload={
                    "suggestions": 2,
                    "highest_priority": "high",
                    "top_suggestion": "提高模型结构化输出稳定性",
                },
            )
        )

        output = capsys.readouterr().out
        assert "step=optimization_completed" in output
        assert "suggestions=2" in output
        assert "提高模型结构化输出稳定性" in output


class TestResume:
    def test_help_includes_debug(self) -> None:
        result = runner.invoke(app, ["resume", "--help"])
        assert result.exit_code == 0
        assert "--debug" in result.output

    def test_missing_run(self, workspace_root: Path) -> None:
        result = runner.invoke(app, ["resume", "--run-id", "no-such-run", "--workspace", str(workspace_root)])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_rejects_concurrent_resume(self, workspace_root: Path) -> None:
        run_id = "run-cli-locked"
        _make_run(workspace_root, run_id)
        owner = ArtifactStore(workspace_root, "safe", run_id)
        owner.acquire_lock()
        try:
            result = runner.invoke(app, ["resume", "--run-id", run_id, "--workspace", str(workspace_root)])
        finally:
            owner.release_lock()

        assert result.exit_code == 1
        assert "resume rejected" in result.output
        assert "already active" in result.output


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
    def test_observability_aggregation(self) -> None:
        from goaloop.cli import _evaluate_observability

        summary = _evaluate_observability(
            [
                {
                    "function": "parse",
                    "dsh_trace_events": 10,
                    "model_calls": 2,
                    "model_call_seconds": 1.5,
                    "estimated_input_tokens": 300,
                    "model_response_chars": 500,
                    "tool_calls": 1,
                    "format_retries": 0,
                },
                {
                    "function": "parse",
                    "dsh_trace_events": 14,
                    "model_calls": 3,
                    "model_call_seconds": 2.5,
                    "estimated_input_tokens": 500,
                    "model_response_chars": 700,
                    "tool_calls": 2,
                    "format_retries": 1,
                },
            ]
        )

        assert summary["parse"]["trace_events"] == 24
        assert summary["parse"]["model_calls"] == 5
        assert summary["parse"]["average_model_call_seconds"] == 2.0
        assert summary["parse"]["average_estimated_input_tokens"] == 400.0

    def test_optimization_aggregation(self) -> None:
        summary = _evaluate_optimization(
            [
                {
                    "function": "parse",
                    "optimization_suggestions": [
                        {"id": "reduce-context", "title": "压缩上下文", "priority": "medium"}
                    ],
                },
                {
                    "function": "parse",
                    "optimization_suggestions": [
                        {"id": "reduce-context", "title": "压缩上下文", "priority": "medium"},
                        {"id": "fix-build", "title": "修复构建", "priority": "high"},
                    ],
                },
            ]
        )

        assert summary["parse"][0]["id"] == "reduce-context"
        assert summary["parse"][0]["runs"] == 2

    def test_help_includes_debug(self) -> None:
        result = runner.invoke(app, ["evaluate", "--help"])
        assert result.exit_code == 0
        assert "--debug" in result.output

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
