"""SpreadsheetBench execution-sandbox and tool-runtime layer.

Public surface
--------------
execute.py
    ``run_in_sandbox``       — run a solution script in the Docker sandbox
    ``ExecutionResult``      — dataclass returned by run_in_sandbox

workspace.py
    ``TaskWorkspace``        — manages per-run directory tree

tools.py
    ``ToolDispatcher``       — allowlist-enforced tool router
    ``ToolNotAllowedError``  — raised on policy violation
    ``list_sheets``          — standalone inspection helper
    ``workbook_metadata``    — standalone inspection helper
    ``read_range``           — standalone inspection helper
    ``search_workbook``      — standalone inspection helper

Not re-exported here (import from the submodule directly):
    INSPECTION_TOOLS         — frozenset of inspection tool names
    _ALLOWLISTS              — internal condition/role → allowed tools map
"""

from .execute import DEFAULT_IMAGE, ExecutionResult, run_in_sandbox
from .tools import (
    ToolDispatcher,
    ToolNotAllowedError,
    list_sheets,
    workbook_metadata,
    read_range,
    search_workbook,
)
from .workspace import TaskWorkspace

__all__ = [
    # execute
    "DEFAULT_IMAGE",
    "ExecutionResult",
    "run_in_sandbox",
    # workspace
    "TaskWorkspace",
    # tools
    "ToolDispatcher",
    "ToolNotAllowedError",
    "list_sheets",
    "workbook_metadata",
    "read_range",
    "search_workbook",
]
