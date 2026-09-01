"""Shared builders for deterministic generated-artifact payloads in tests."""

from __future__ import annotations

from typing import Any

HARNESS_TEMPLATE = """#include <stdint.h>
#include <stddef.h>

int {function}(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {{
    {function}(data, size);
    return 0;
}}
"""

BROKEN_HARNESS_TEMPLATE = """#include <stdint.h>
#include <stddef.h>

int {function}(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {{
    {function}(data, size)
    return 0;
}}
"""

HARNESS_CRASH_TEMPLATE = """#include <stdint.h>
#include <stddef.h>

int {function}(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {{
    volatile char *p = 0;
    *p = 1;  /* harness self-error: null dereference */
    {function}(data, size);
    return 0;
}}
"""

NO_REACH_TEMPLATE = """#include <stdint.h>
#include <stddef.h>

int {function}(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {{
    (void)data;
    (void)size;
    return 0;  /* compiles and fuzzes but never reaches the target */
}}
"""

BUILD_DIR_HARNESS_TEMPLATE = """#include <stdint.h>
#include <stddef.h>

extern "C" int {function}(const uint8_t *data, size_t size);

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {{
    {function}(data, size);
    return 0;
}}
"""

MAKEFILE_CONTENT = "all:\n\tclang -fsanitize=fuzzer,address,undefined -o fuzzer harness.c src.c\n"
BUILD_SH_CONTENT = "#!/bin/sh\nexit 0\n"
README_CONTENT = "# Fuzz harness\n\nGenerated artifact for review only.\n"


def make_artifact_payload(
    project: str,
    function: str,
    *,
    harness_source: str | None = None,
    target_sources: list[str] | None = None,
    summary: str = "candidate harness",
    include_files: bool = True,
    harness_file: str | None = None,
) -> dict[str, Any]:
    """Build a valid GeneratedArtifactSet-shaped dict (without run metadata)."""
    harness = harness_source or HARNESS_TEMPLATE.format(function=function)
    sources = target_sources or [f"src/{project}.c"]
    harness_name = harness_file or f"harness_{project}.c"
    files = []
    if include_files:
        files = [
            {"path": harness_name, "content": harness, "purpose": "libFuzzer harness source"},
            {"path": "Makefile", "content": MAKEFILE_CONTENT, "purpose": "review only"},
            {"path": "build.sh", "content": BUILD_SH_CONTENT, "purpose": "review only"},
            {"path": "endpoint.json", "content": "{}", "purpose": "review only"},
            {"path": "README.fuzz.md", "content": README_CONTENT, "purpose": "review only"},
        ]
    return {
        "summary": summary,
        "candidate_ready": True,
        "format_retry": 0,
        "endpoint_plan": {
            "function": function,
            "signature": f"int {function}(const uint8_t *data, size_t size)",
            "location": f"src/{project}.c",
            "language": "c",
            "input_model": "raw bytes",
            "lifecycle": ["no persistent state"],
            "build": {
                "compiler": "clang",
                "harness_file": harness_name,
                "target_sources": sources,
                "include_dirs": [],
                "defines": [],
                "cflags": ["-g"],
                "ldflags": [],
                "libraries": [],
                "binary_name": "fuzzer",
            },
        },
        "files": files,
    }


def make_build_dir_artifact_payload(function: str, *, harness_source: str | None = None) -> dict[str, Any]:
    """Build the one-file artifact contract used with --build-dir."""
    harness = harness_source or BUILD_DIR_HARNESS_TEMPLATE.format(function=function)
    return {
        "summary": "build-directory harness",
        "candidate_ready": True,
        "format_retry": 0,
        "endpoint_plan": {
            "function": function,
            "signature": f"int {function}(const uint8_t *data, size_t size)",
            "location": "src/target.c",
            "language": "c",
            "input_model": "raw bytes",
            "lifecycle": [],
            "build": {
                "compiler": "clang",
                "harness_file": "harness.c",
                "target_sources": [],
                "include_dirs": [],
                "defines": [],
                "cflags": [],
                "ldflags": [],
                "libraries": [],
                "binary_name": "fuzzer",
            },
        },
        "files": [
            {"path": "harness.c", "content": harness, "purpose": "libFuzzer harness source"}
        ],
    }
