"""Log and research-export redaction.

Never leaks credentials, workspace source text, absolute paths, or usernames
into model feedback or the public research export. Event relationships are kept
by replacing concrete values with stable placeholders.
"""

from __future__ import annotations

import re
from pathlib import Path

_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)([A-Za-z0-9_\-\.]{8,})"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9_\-\.=]{8,})"),
)

_PATH_PATTERN = re.compile(r"(?:/|~)[A-Za-z0-9_./\-]*(?:/[A-Za-z0-9_.\-]+)+")
_HOME = str(Path.home())


def redact(text: str, workspace_root: Path) -> str:
    """Replace credentials and filesystem paths with neutral placeholders."""
    result = text
    for pattern in _CREDENTIAL_PATTERNS:
        result = (
            pattern.sub("<redacted>", result)
            if pattern.groups == 0  # whole-match secret like sk-...
            else pattern.sub(r"\1<redacted>", result)
        )
    root = str(workspace_root.resolve())
    result = result.replace(root, "<workspace>")
    result = result.replace(_HOME, "~")
    result = _PATH_PATTERN.sub("<path>", result)
    return result
