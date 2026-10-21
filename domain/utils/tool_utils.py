"""Helpers shared by the domain tools."""

import traceback

from semente import ToolResult
from semente.logging import log_error
from semente.configs.prompts import get_tool_result_text


def unexpected_error(component: str, tool_key: str) -> ToolResult:
    """Logs the exception being handled and returns the tool's generic error text.

    Must be called from an ``except`` block. The full traceback goes to the
    error log only; the model receives the ``results.error`` text of the
    tool, which carries no exception details.

    Args:
        component (str): Tool group in ``tools.yml`` (e.g. ``property_tools``).
        tool_key (str): Tool name under ``component`` in ``tools.yml``.

    Returns:
        ToolResult: The ``error`` instructions.

    Raises:
        MissingPromptError: If the tool has no ``results.error`` text.
    """
    log_error(f"{tool_key}: unexpected error\n{traceback.format_exc()}")
    return ToolResult(content=get_tool_result_text(component, tool_key, "error"))
