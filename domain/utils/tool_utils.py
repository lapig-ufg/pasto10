"""Helpers shared by the domain tools."""

import traceback
from typing import Optional

from semente import Context as RunContext
from semente import ToolResult
from semente.logging import log_error, log_warning
from semente.configs.prompts import get_agent_config, get_tool_result_text

from domain.schemas.feature import Feature
from domain.utils.feature_utils import find_registered_feature, get_registered_features


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


def lookup_area(
    run_context: RunContext, area_name: Optional[str], component: str, tool_key: str,
) -> tuple[Optional[Feature], Optional[ToolResult]]:
    """Resolves the registered area a tool must analyze.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Display name given by the user; None for the latest area.
        component (str): Tool group in ``tools.yml`` (e.g. ``biomass_tools``).
        tool_key (str): Tool name under ``component`` whose ``results`` texts are rendered.

    Returns:
        tuple: (Feature, None) when found; (None, ToolResult) with the
        ``no_registered_area`` or ``area_not_found`` instructions otherwise.

    Raises:
        MissingPromptError: If the tool lacks those result texts.
    """
    session_state = run_context.session_state or {}
    if not get_registered_features(session_state).features:
        return None, ToolResult(content=get_tool_result_text(component, tool_key, "no_registered_area"))
    feature = find_registered_feature(session_state, area_name)
    if feature is None:
        log_warning(f"{tool_key}: area not found: {area_name!r}")
        return None, ToolResult(content=get_tool_result_text(component, tool_key, "area_not_found", area_name=area_name))
    return feature, None


def area_label(feature: Feature) -> str:
    """Returns the display name of an area, or the generic type name (``feature_type_fallback``) when unnamed.

    Args:
        feature (Feature): The registered area.

    Returns:
        str: The name to show the user, never empty.
    """
    return feature.name or get_agent_config("single_agent")["feature_type_fallback"].strip()
