"""Crash analysis tests: stack ownership and reproduction flow."""

from __future__ import annotations

from pathlib import Path

from goaloop.crash import analyze_crash, classify_stack
from goaloop.models import CrashOwnership, ProcessRequest, ProcessResult, ValidationProfile


class TestClassifyStack:
    def test_product_frame_wins(self, tmp_path: Path) -> None:
        source = tmp_path / "repos" / "fragile"
        candidate = tmp_path / "candidate"
        output = (
            "ERROR: AddressSanitizer: stack-buffer-overflow\n"
            f"#0 0x1234 in memcpy {source}/src/fragile.c:6\n"
            f"#1 0x5678 in fragile_parse {source}/src/fragile.c:7\n"
            f"#2 0x9abc in LLVMFuzzerTestOneInput {candidate}/harness.c:4\n"
        )
        assert classify_stack(output, source_root=source, candidate_dir=candidate) is CrashOwnership.PRODUCT

    def test_harness_only(self, tmp_path: Path) -> None:
        source = tmp_path / "repos" / "safe"
        candidate = tmp_path / "candidate"
        output = f"ERROR: AddressSanitizer: SEGV\n#1 0x5678 in LLVMFuzzerTestOneInput {candidate}/harness.c:3\n"
        assert classify_stack(output, source_root=source, candidate_dir=candidate) is CrashOwnership.HARNESS

    def test_no_frames_unknown(self, tmp_path: Path) -> None:
        assert classify_stack("ERROR: libFuzzer: timeout", source_root=tmp_path, candidate_dir=tmp_path) is (
            CrashOwnership.UNKNOWN
        )

    def test_inlined_target_function_is_product(self, tmp_path: Path) -> None:
        # The model inlined a copy of ares_create_query into harness.c; the
        # frame path points at the candidate dir, but the frame function is the
        # target function — that is product evidence, not a harness bug.
        candidate = tmp_path / "candidate"
        source = tmp_path / "repos" / "c-ares"
        output = (
            "ERROR: AddressSanitizer: heap-buffer-overflow\n"
            "#0 in __asan_memset (fuzzer+0xfffa8)\n"
            f"#1 in ares_create_query {candidate}/harness.c:127\n"
            f"#2 in LLVMFuzzerTestOneInput {candidate}/harness.c:155\n"
        )
        ownership = classify_stack(
            output, source_root=source, candidate_dir=candidate, target_function="ares_create_query"
        )
        assert ownership is CrashOwnership.PRODUCT


class FakeExecutor:
    def __init__(self, exit_codes: list[int], outputs: list[str] | None = None) -> None:
        self.exit_codes = exit_codes
        self.outputs = outputs or ["ERROR: AddressSanitizer: heap-buffer-overflow"] * len(exit_codes)
        self.calls: list[list[str]] = []

    def execute(self, request: ProcessRequest) -> ProcessResult:
        self.calls.append(request.argv)
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        output = self.outputs.pop(0) if self.outputs else ""
        return ProcessResult(
            argv=list(request.argv),
            exit_code=code,
            timed_out=False,
            duration_seconds=0.1,
            stdout=output,
        )


class TestAnalyzeCrash:
    def test_harness_ownership_skips_reproduction(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        (candidate / "fuzzer").write_bytes(b"binary")
        crashes = tmp_path / "crashes"
        crashes.mkdir()
        crash_file = crashes / "crash-abc"
        crash_file.write_bytes(b"x")
        executor = FakeExecutor([])
        profile = ValidationProfile(name="default")
        result = analyze_crash(
            source_root=tmp_path / "repos" / "safe",
            candidate_dir=candidate,
            run_dir=tmp_path,
            fuzzer_binary=candidate / "fuzzer",
            crash_files=[crash_file],
            output=f"ERROR: AddressSanitizer: SEGV\n#1 in LLVMFuzzerTestOneInput {candidate}/harness.c:3",
            profile=profile,
            backend=executor,
        )
        assert result.ownership is CrashOwnership.HARNESS
        assert result.reproductions == 0
        assert executor.calls == []

    def test_product_crash_reproduces(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        (candidate / "fuzzer").write_bytes(b"binary")
        crashes = tmp_path / "crashes"
        crashes.mkdir()
        crash_file = crashes / "crash-abc"
        crash_file.write_bytes(b"abcdef")
        source = tmp_path / "repos" / "fragile"
        executor = FakeExecutor([1, 1, 1, 1])  # minimize + 3 reproductions
        profile = ValidationProfile(name="default")
        result = analyze_crash(
            source_root=source,
            candidate_dir=candidate,
            run_dir=tmp_path,
            fuzzer_binary=candidate / "fuzzer",
            crash_files=[crash_file],
            output=f"ERROR: AddressSanitizer: heap-buffer-overflow\n#0 in memcpy {source}/src/fragile.c:6",
            profile=profile,
            backend=executor,
        )
        assert result.ownership is CrashOwnership.PRODUCT
        assert result.reproductions == 3
        assert result.minimized_artifact == "crashes/minimized.bin"
        # minimize call comes first, then 3 reproductions
        assert executor.calls[0][1] == "-minimize_crash=1"
        assert len(executor.calls) == 4

    def test_inconsistent_reproduction_is_unknown(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        (candidate / "fuzzer").write_bytes(b"binary")
        crashes = tmp_path / "crashes"
        crashes.mkdir()
        crash_file = crashes / "crash-abc"
        crash_file.write_bytes(b"abcdef")
        source = tmp_path / "repos" / "fragile"
        executor = FakeExecutor([1, 1, 0, 1])  # minimize ok, two repro, one clean run
        profile = ValidationProfile(name="default")
        result = analyze_crash(
            source_root=source,
            candidate_dir=candidate,
            run_dir=tmp_path,
            fuzzer_binary=candidate / "fuzzer",
            crash_files=[crash_file],
            output=f"ERROR: AddressSanitizer: heap-buffer-overflow\n#0 in memcpy {source}/src/fragile.c:6",
            profile=profile,
            backend=executor,
        )
        assert result.reproductions == 2
        assert result.ownership is CrashOwnership.UNKNOWN
