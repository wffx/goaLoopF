"""Four-phase run controller with checkpointing and resume.

Split into three modules for maintainability:

- ``controller.py`` — ``RunController`` core: state machine loop, lifecycle,
  event/checkpoint persistence, preprocess phase.
- ``generation.py`` — ``GenerationMixin``: the harness-generation loop
  (model call, static policy, compile/fuzz/coverage, decision).
- ``report.py`` — ``ReportMixin``: crash analysis, terminal mapping, and the
  report/metrics writing.
"""

from .controller import RunController

__all__ = ["RunController"]
