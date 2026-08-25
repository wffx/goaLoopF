"""Generation driver tests: JSON extraction, staleness checks, scripted driver."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from goaloop.driver import (
    DeepSeekHarnessDriver,
    DriverUnavailable,
    GenerationFailure,
    ScriptedGenerationDriver,
    build_format_retry_prompt,
    build_generation_prompt,
    extract_json,
)
from goaloop.models import (
    SCHEMA_VERSION,
    CapabilityReport,
    GenerationGoal,
    PreprocessResult,
)

from .helpers import make_artifact_payload


def _preprocess(run_id: str = "run-d") -> PreprocessResult:
    return PreprocessResult(
        run_id=run_id,
        ready=True,
        project_name="safe",
        source_root="/tmp/ws/repos/safe",
        language="c",
        target_function="safe_parse",
        capability_report=CapabilityReport(platform="Linux", capabilities=[]),
    )


def _goal(run_id: str = "run-d", loop: int = 0) -> GenerationGoal:
    return GenerationGoal(
        run_id=run_id,
        objective="generate a harness",
        target_function="safe_parse",
        acceptance_criteria=["compiles"],
        max_generation_loops=3,
        current_loop=loop,
    )


class TestExtractJson:
    def test_plain_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self) -> None:
        text = 'Here is the output:\n```json\n{"a": 2}\n```\nThat is all.'
        assert extract_json(text) == {"a": 2}

    def test_prose_around_object(self) -> None:
        text = 'Sure.\n{"a": 3}\nDone.'
        assert extract_json(text) == {"a": 3}

    def test_invalid_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            extract_json("no json at all")


class TestDriverCoercion:
    def test_staleness_rejects_wrong_run(self) -> None:
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload["run_id"] = "other-run"
        with pytest.raises(ValueError, match="different run"):
            driver._coerce(json.dumps(payload), _goal(), 1, format_retry=0)

    def test_staleness_rejects_wrong_loop(self) -> None:
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload["generation_loop"] = 7
        with pytest.raises(ValueError, match="generation_loop"):
            driver._coerce(json.dumps(payload), _goal(), 2, format_retry=0)

    def test_valid_payload_coerces(self) -> None:
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload["generation_loop"] = 3
        artifacts = driver._coerce(json.dumps(payload), _goal(), 3, format_retry=0)
        assert artifacts.run_id == "run-d"
        assert artifacts.generation_loop == 3


def _full_payload() -> dict:
    payload = make_artifact_payload("safe", "safe_parse")
    payload.update(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": "run-d",
            "phase": "harness_generation",
        }
    )
    return payload


class TestScriptedDriver:
    def test_patches_run_metadata(self) -> None:
        payloads = [make_artifact_payload("safe", "safe_parse")]
        driver = ScriptedGenerationDriver(payloads)
        artifacts = driver.generate_artifacts(goal=_goal("run-s"), preprocess=_preprocess("run-s"), feedback=None)
        assert artifacts.run_id == "run-s"
        assert artifacts.generation_loop == 1
        assert artifacts.schema_version == SCHEMA_VERSION
        assert driver.calls == 1

    def test_exhaustion_raises(self) -> None:
        driver = ScriptedGenerationDriver([])
        with pytest.raises(GenerationFailure):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_unavailable_raises(self) -> None:
        driver = ScriptedGenerationDriver([make_artifact_payload("safe", "safe_parse")], unavailable=True)
        with pytest.raises(DriverUnavailable):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_complete_goal_records(self) -> None:
        driver = ScriptedGenerationDriver([])
        driver.complete_goal(goal=_goal(), summary="done")
        assert driver.completed_summaries == ["done"]

    def test_invalid_payload_raises_validation_error(self) -> None:
        payload = make_artifact_payload("safe", "safe_parse")
        payload["summary"] = 123  # wrong type
        driver = ScriptedGenerationDriver([payload])
        with pytest.raises(ValidationError):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)


class TestPrompt:
    def test_prompt_contains_schema_and_loop(self) -> None:
        prompt = build_generation_prompt(
            goal=_goal(),
            preprocess=_preprocess(),
            feedback=None,
            expected_loop=1,
        )
        assert "GeneratedArtifactSet" in prompt
        assert '"run_id"' in prompt
        assert "generation loop 1" in prompt

    def test_retry_prompt_carries_error(self) -> None:
        prompt = build_format_retry_prompt("base", ValueError("bad json"), expected_loop=2)
        assert "bad json" in prompt
        assert "format_retry = 1" in prompt


class FakeHarness:
    """Minimal stand-in for the real DeepSeekHarness SDK object."""

    def __init__(self, responses: list[str], *, finish_reason: str = "completed") -> None:
        self.responses = list(responses)
        self.finish_reason = finish_reason
        self.calls: list[tuple[str, str | None]] = []
        self.closed = False

    def run(self, prompt: str, session_id: str | None = None) -> object:
        from types import SimpleNamespace

        self.calls.append((prompt, session_id))
        if not self.responses:
            raise RuntimeError("fake harness has no more responses")
        return SimpleNamespace(
            final_response=self.responses.pop(0),
            finish_reason=self.finish_reason,
            events=getattr(self, "events", []),
            notifications=getattr(self, "notifications", []),
        )

    def close(self) -> None:
        self.closed = True


def _real_driver(run_id: str = "run-live") -> DeepSeekHarnessDriver:
    return DeepSeekHarnessDriver(
        provider="deepseek-official",
        model="deepseek-v4-pro",
        max_tokens=None,
        cordis=None,
        workspace_root="/tmp/ws",
        session_root="/tmp/ws/.private-sessions/run-live",
        run_id=run_id,
    )


def _live_payload() -> dict:
    payload = make_artifact_payload("safe", "safe_parse")
    payload.update(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": "run-live",
            "phase": "harness_generation",
            "generation_loop": 1,
        }
    )
    return payload


class TestDeepSeekHarnessDriver:
    def test_successful_generation(self) -> None:
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(_live_payload())])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.generation_loop == 1
        assert driver.format_retries == 0

    def test_markdown_fenced_response(self) -> None:
        payload = _live_payload()
        text = f"Here is the output:\n```json\n{json.dumps(payload)}\n```"
        driver = _real_driver()
        driver._harness = FakeHarness([text])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.summary == payload["summary"]

    def test_format_retry_recovers(self) -> None:
        first = _live_payload()
        first["generation_loop"] = 99  # stale loop → ValueError → triggers one retry
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(first), json.dumps(_live_payload())])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.generation_loop == 1
        assert driver.format_retries == 1
        assert len(driver._harness.calls) == 2  # original + retry

    def test_format_retry_exhausted_raises(self) -> None:
        bad = _live_payload()
        bad["generation_loop"] = 99
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(bad), json.dumps(bad)])
        with pytest.raises(GenerationFailure, match="remained invalid"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert driver.format_retries == 1

    def test_stale_run_id_rejected_without_retry(self) -> None:
        payload = _live_payload()
        payload["run_id"] = "some-other-run"  # cross-run contamination: no format retry
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(payload)])
        with pytest.raises(GenerationFailure):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert driver.format_retries == 0

    def test_empty_response_with_error_reason_is_unavailable(self) -> None:
        # An endpoint error with no response is a retryable environment
        # condition (DriverUnavailable -> BLOCKED), not invalid model output.
        driver = _real_driver()
        driver._harness = FakeHarness([""], finish_reason="error")
        with pytest.raises(DriverUnavailable, match="error"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_empty_response_with_max_tokens_is_failure(self) -> None:
        driver = _real_driver()
        driver._harness = FakeHarness([""], finish_reason="max-tokens")
        with pytest.raises(GenerationFailure, match="max-tokens"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_sdk_exception_is_driver_unavailable(self) -> None:
        driver = _real_driver()
        driver._harness = FakeHarness([])  # raises RuntimeError on run()

        class Boom:
            def run(self, prompt, session_id=None) -> object:
                raise ConnectionError("network down")

        driver._harness = Boom()
        with pytest.raises(DriverUnavailable, match="SDK call failed"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_complete_goal_calls_harness(self) -> None:
        driver = _real_driver()
        harness = FakeHarness([""])
        driver._harness = harness
        driver.complete_goal(goal=_goal(), summary="harness_verified")
        assert len(harness.calls) == 1
        assert "mark it complete" in harness.calls[0][0]

    def test_complete_goal_failure_raises(self) -> None:
        driver = _real_driver()

        class Boom:
            def run(self, prompt, session_id=None) -> object:
                raise TimeoutError("sdk hung")

        driver._harness = Boom()
        with pytest.raises(DriverUnavailable, match="goal completion"):
            driver.complete_goal(goal=_goal(), summary="done")

    def test_close_closes_harness(self) -> None:
        driver = _real_driver()
        harness = FakeHarness([])
        driver._harness = harness
        driver.close()
        assert harness.closed

    def test_open_missing_sdk(self, monkeypatch) -> None:
        import sys

        driver = _real_driver()
        monkeypatch.setitem(sys.modules, "deepseek_harness", None)
        with pytest.raises(DriverUnavailable, match="not installed"):
            driver._open()


class TestCustomModelSupport:
    def test_driver_keeps_base_url(self) -> None:
        driver = _real_driver()
        driver.base_url = "https://proxy.example/v1"
        assert driver.base_url == "https://proxy.example/v1"

    def test_open_passes_base_url_to_sdk(self, monkeypatch) -> None:
        captured: dict = {}

        class FakeSDK:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def close(self):
                pass

        import sys

        monkeypatch.setitem(sys.modules, "deepseek_harness", type(sys)("dh"))
        import types

        fake_module = types.ModuleType("deepseek_harness")
        fake_module.DeepSeekHarness = FakeSDK
        monkeypatch.setitem(sys.modules, "deepseek_harness", fake_module)

        driver = _real_driver()
        driver.base_url = "https://proxy.example/v1"
        driver._open()
        assert captured.get("base_url") == "https://proxy.example/v1"
        assert captured.get("provider") == "deepseek-official"
        driver.close()


class TestTurnErrorExtraction:
    def test_extracts_reason_fields(self) -> None:
        from goaloop.driver import _extract_turn_error

        events = [
            {"type": "assistant/message", "data": {"message": {"content": []}}},
            {
                "type": "turn/end",
                "data": {"reason": {"kind": "error", "message": "HTTP 401", "detail": "invalid api key"}},
            },
        ]
        detail = _extract_turn_error(events, [])
        assert "401" in detail
        assert "invalid api key" in detail

    def test_extracts_error_notification(self) -> None:
        from types import SimpleNamespace

        from goaloop.driver import _extract_turn_error

        events = [{"type": "turn/end", "data": {"reason": {"kind": "error"}}}]
        notifications = [
            SimpleNamespace(method="model/error", payload={"status": 429, "message": "rate limited"})
        ]
        detail = _extract_turn_error(events, notifications)
        assert "429" in detail
        assert "rate limited" in detail

    def test_no_detail_returns_none(self) -> None:
        from goaloop.driver import _extract_turn_error

        assert _extract_turn_error([], []) is None
        assert _extract_turn_error([{"type": "turn/end", "data": {"reason": {"kind": "error"}}}], []) is None

    def test_error_reason_message_includes_detail(self) -> None:
        driver = _real_driver()
        events = [
            {"type": "turn/end", "data": {"reason": {"kind": "error", "message": "HTTP 429", "detail": "rate limited"}}}
        ]
        harness = FakeHarness([""], finish_reason="error")
        harness.events = events  # type: ignore[attr-defined]
        driver._harness = harness
        with pytest.raises(DriverUnavailable, match="HTTP 429"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
