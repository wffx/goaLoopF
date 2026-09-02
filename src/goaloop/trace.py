"""Durable raw DSH trace recording and deterministic summary aggregation."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

TRACE_FILENAME = "dsh-trace.jsonl"
TRACE_SUMMARY_FILENAME = "dsh-trace-summary.json"
_STREAM_EVENT_TYPES = {"assistant/chunk", "reasoning-chunks", "text-chunks"}
_HIDDEN_SESSION_EVENT_TYPES = {
    "session",
    "session/title",
    "agent/inbox/spliced",
    "request/header",
    "request/context",
    "user/message",
    "tool-call-chunks",
}


class DshTraceTerminalFormatter:
    """Turn noisy DSH notifications into bounded, user-oriented progress lines."""

    def __init__(self, *, stream_report_chars: int = 2048) -> None:
        self.stream_report_chars = stream_report_chars
        self._streams: dict[str, dict[str, int]] = {}

    def format(self, method: str, payload: dict[str, Any]) -> list[str]:
        custom = self._format_goaloop_event(method, payload)
        if custom is not None:
            return custom
        if method == "session.event":
            event = payload.get("event")
            if not isinstance(event, dict):
                return []
            return self._format_session_event(str(payload.get("sessionId", "?")), event)
        if method in {"session.title", "session/title"}:
            return []
        if method in {"session.status", "session/status"}:
            status = payload.get("status") or payload.get("state") or "unknown"
            return [f"session status={status}"]
        return [f"event {method} {_compact_json(payload, 360)}"]

    def _format_goaloop_event(self, method: str, payload: dict[str, Any]) -> list[str] | None:
        if method == "goaloop.model_call.started":
            return [
                "model call started "
                f"session={payload.get('session_id')} "
                f"input≈{payload.get('estimated_input_tokens')} tokens "
                f"prompt={payload.get('prompt_chars')} chars"
            ]
        if method == "goaloop.model_call.completed":
            return [
                "model call completed "
                f"session={payload.get('session_id')} finish={payload.get('finish_reason')} "
                f"duration={payload.get('duration_seconds')}s response={payload.get('response_chars')} chars"
            ]
        if method == "goaloop.model_call.failed":
            return [
                "model call failed "
                f"session={payload.get('session_id')} duration={payload.get('duration_seconds')}s "
                f"error={payload.get('error_type')}: {_compact_text(str(payload.get('error', '')), 240)}"
            ]
        return None

    def _format_session_event(self, session_id: str, event: dict[str, Any]) -> list[str]:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return []
        if event_type in _STREAM_EVENT_TYPES:
            return self._stream_progress(session_id, event_type, event)
        if event_type in _HIDDEN_SESSION_EVENT_TYPES:
            return []
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        turn = data.get("turn")
        step = data.get("step")
        location = _turn_step(turn, step)
        if event_type == "turn/start":
            return [f"turn started turn={turn}"]
        if event_type == "turn/end":
            reason = data.get("reason")
            if isinstance(reason, dict):
                reason = reason.get("kind")
            return [f"turn completed turn={turn} reason={reason or 'unknown'}"]
        if event_type == "step/start":
            return [f"step started {location}"]
        if event_type == "step/end":
            return [f"step completed {location}"]
        if event_type == "assistant/message":
            self._streams.pop(_stream_key(session_id, turn, step), None)
            return self._assistant_message(data, location)
        if event_type == "tool/call":
            return [
                f"tool call {location} name={data.get('name')} "
                f"args={_compact_arguments(data.get('arguments'), 320)}"
            ]
        if event_type == "tool/result":
            is_error, chars, preview = _tool_result_summary(data)
            suffix = f" preview={preview}" if preview else ""
            return [f"tool result {location} error={is_error} output={chars} chars{suffix}"]
        if event_type == "goal/change":
            goal = data.get("goal")
            goal = goal if isinstance(goal, dict) else {}
            return [
                f"goal {data.get('operation', 'change')} phase={goal.get('phase', 'unknown')} "
                f"objective={_compact_text(str(goal.get('objective', '')), 220)}"
            ]
        return [f"{event_type} {location} {_compact_json(data, 320)}"]

    def _stream_progress(self, session_id: str, event_type: str, event: dict[str, Any]) -> list[str]:
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        turn = data.get("turn")
        step = data.get("step")
        key = _stream_key(session_id, turn, step)
        state = self._streams.setdefault(
            key,
            {"reasoning_chars": 0, "text_chars": 0, "reported_chars": 0, "chunks": 0},
        )
        reasoning_chars, text_chars, chunks = _stream_delta(event_type, data)
        state["reasoning_chars"] += reasoning_chars
        state["text_chars"] += text_chars
        state["chunks"] += chunks
        total = state["reasoning_chars"] + state["text_chars"]
        if total - state["reported_chars"] < self.stream_report_chars:
            return []
        state["reported_chars"] = total
        return [
            f"model streaming {_turn_step(turn, step)} reasoning={state['reasoning_chars']} chars "
            f"answer={state['text_chars']} chars chunks={state['chunks']}"
        ]

    def _assistant_message(self, data: dict[str, Any], location: str) -> list[str]:
        message = data.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        reasoning = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "reasoning"
        )
        answer = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        lines: list[str] = []
        if reasoning:
            lines.append(f"reasoning committed {location} chars={len(reasoning)} tail={_tail_text(reasoning, 260)}")
        artifact = _artifact_summary(answer)
        if artifact is not None:
            lines.append(f"assistant artifact {location} {artifact}")
        elif answer:
            lines.append(
                f"assistant response {location} chars={len(answer)} preview={_compact_text(answer, 260)}"
            )
        elif not lines:
            lines.append(f"assistant message {location} empty")
        return lines


def _stream_delta(event_type: str, data: dict[str, Any]) -> tuple[int, int, int]:
    if event_type == "assistant/chunk":
        chunk = data.get("chunk")
        if not isinstance(chunk, dict):
            return 0, 0, 1
        chunk_type = chunk.get("type")
        text = str(chunk.get("text", ""))
        if chunk_type == "reasoning-delta":
            return len(text), 0, 1
        if chunk_type == "text-delta":
            return 0, len(text), 1
        return 0, 0, 1
    texts = data.get("texts")
    chars = sum(len(str(item)) for item in texts) if isinstance(texts, list) else 0
    chunks = len(texts) if isinstance(texts, list) else 1
    return (chars, 0, chunks) if event_type == "reasoning-chunks" else (0, chars, chunks)


def _assistant_text_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    message = data.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return []
    return [block for block in message["content"] if isinstance(block, dict)]


def _tool_result_summary(data: dict[str, Any]) -> tuple[bool, int, str]:
    blocks = _assistant_text_blocks(data)
    texts: list[str] = []
    is_error = False
    for block in blocks:
        if block.get("type") != "tool-result":
            continue
        is_error = is_error or block.get("isError") is True
        content = block.get("content")
        if not isinstance(content, list):
            continue
        texts.extend(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    text = "".join(texts)
    return is_error, len(text), _compact_text(text, 180) if text else ""


def _artifact_summary(text: str) -> str | None:
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("phase") != "harness_generation":
        return None
    files = payload.get("files")
    file_count = len(files) if isinstance(files, list) else 0
    summary = _compact_text(str(payload.get("summary", "")), 220)
    return (
        f"loop={payload.get('generation_loop')} ready={payload.get('candidate_ready')} "
        f"files={file_count} summary={summary}"
    )


def _compact_arguments(value: object, limit: int) -> str:
    if isinstance(value, str):
        try:
            return _compact_json(json.loads(value), limit)
        except json.JSONDecodeError:
            return _compact_text(value, limit)
    return _compact_json(value, limit)


def _compact_json(value: object, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    return _compact_text(text, limit)


def _compact_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _tail_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else "..." + text[-(limit - 3) :]


def _turn_step(turn: object, step: object) -> str:
    return f"turn={turn if turn is not None else '?'} step={step if step is not None else '?'}"


def _stream_key(session_id: str, turn: object, step: object) -> str:
    return f"{session_id}:{turn}:{step}"


class DshTraceRecorder:
    """Append raw DSH notifications and maintain a resume-safe summary."""

    def __init__(self, *, run_id: str, logs_dir: Path) -> None:
        self.run_id = run_id
        self.logs_dir = logs_dir.resolve()
        self.trace_path = self.logs_dir / TRACE_FILENAME
        self.summary_path = self.logs_dir / TRACE_SUMMARY_FILENAME
        self._summary = _empty_summary(run_id)
        self._sequence = 0
        self._rebuild()

    def record(self, method: str, payload: dict[str, Any]) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        record = {
            "sequence": self._sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "method": method,
            "payload": payload,
        }
        self._ensure_line_boundary()
        with self.trace_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _accumulate(self._summary, record)
        self._write_summary()

    def snapshot(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(json.dumps(self._summary, ensure_ascii=False)))

    def _rebuild(self) -> None:
        if not self.trace_path.is_file():
            return
        try:
            lines = self.trace_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                self._summary["invalid_records"] = int(self._summary["invalid_records"]) + 1
                continue
            if not isinstance(record, dict):
                self._summary["invalid_records"] = int(self._summary["invalid_records"]) + 1
                continue
            sequence = record.get("sequence")
            if isinstance(sequence, int):
                self._sequence = max(self._sequence, sequence)
            _accumulate(self._summary, record)
        self._write_summary()

    def _ensure_line_boundary(self) -> None:
        if not self.trace_path.is_file() or self.trace_path.stat().st_size == 0:
            return
        with self.trace_path.open("rb+") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _write_summary(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self._summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=self.logs_dir,
            prefix=f".{TRACE_SUMMARY_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, self.summary_path)


def _empty_summary(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "trace_file": f"logs/{TRACE_FILENAME}",
        "event_count": 0,
        "invalid_records": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "methods": {},
        "session_event_types": {},
        "model_calls": {
            "started": 0,
            "completed": 0,
            "failed": 0,
            "duration_seconds": 0.0,
            "prompt_chars": 0,
            "estimated_input_tokens": 0,
            "response_chars": 0,
            "finish_reasons": {},
        },
        "tool_calls": 0,
        "tool_call_names": {},
        "tool_results": 0,
    }


def _accumulate(summary: dict[str, Any], record: dict[str, Any]) -> None:
    method = record.get("method")
    payload = record.get("payload")
    if not isinstance(method, str) or not isinstance(payload, dict):
        summary["invalid_records"] = int(summary["invalid_records"]) + 1
        return
    summary["event_count"] = int(summary["event_count"]) + 1
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str):
        if summary["first_timestamp"] is None:
            summary["first_timestamp"] = timestamp
        summary["last_timestamp"] = timestamp
    _increment(summary["methods"], method)

    if method == "session.event":
        event = payload.get("event")
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            event_type = event["type"]
            _increment(summary["session_event_types"], event_type)
            if event_type == "tool/call":
                summary["tool_calls"] = int(summary["tool_calls"]) + 1
                data = event.get("data")
                if isinstance(data, dict) and isinstance(data.get("name"), str):
                    _increment(summary["tool_call_names"], data["name"])
            elif event_type == "tool/result":
                summary["tool_results"] = int(summary["tool_results"]) + 1
    normalized = method.replace(".", "/")
    if normalized == "tools/result":
        summary["tool_results"] = int(summary["tool_results"]) + 1

    model_calls = summary["model_calls"]
    if method == "goaloop.model_call.started":
        model_calls["started"] = int(model_calls["started"]) + 1
        model_calls["prompt_chars"] = int(model_calls["prompt_chars"]) + _int_value(payload.get("prompt_chars"))
        model_calls["estimated_input_tokens"] = int(model_calls["estimated_input_tokens"]) + _int_value(
            payload.get("estimated_input_tokens")
        )
    elif method == "goaloop.model_call.completed":
        model_calls["completed"] = int(model_calls["completed"]) + 1
        model_calls["duration_seconds"] = round(
            float(model_calls["duration_seconds"]) + _float_value(payload.get("duration_seconds")), 6
        )
        model_calls["response_chars"] = int(model_calls["response_chars"]) + _int_value(
            payload.get("response_chars")
        )
        finish_reason = payload.get("finish_reason")
        if isinstance(finish_reason, str):
            _increment(model_calls["finish_reasons"], finish_reason)
    elif method == "goaloop.model_call.failed":
        model_calls["failed"] = int(model_calls["failed"]) + 1
        model_calls["duration_seconds"] = round(
            float(model_calls["duration_seconds"]) + _float_value(payload.get("duration_seconds")), 6
        )


def _increment(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _int_value(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float_value(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0
