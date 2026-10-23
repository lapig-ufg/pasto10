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


AREA_DIFFERENCE_TOLERANCE_HA = 0.05


def area_notes(component: str, tool_key: str, feature: Feature, analyzed_ha: float) -> list[str]:
    """Builds the notes about the registered area that accompany a pasture or biomass result.

    ``note_approximate_area`` when the area is an approximate circle (``buffer_area``), and ``note_area_difference``
    when the area analyzed on the raster differs from the registered area by more than ``AREA_DIFFERENCE_TOLERANCE_HA``
    (raster resolution); hectares are shown with one decimal.

    Args:
        component (str): Tool group in ``tools.yml``.
        tool_key (str): Tool whose ``results`` texts are rendered.
        feature (Feature): The registered area.
        analyzed_ha (float): Total area covered by the analysis, in hectares.

    Returns:
        list[str]: Zero, one or two note texts.

    Raises:
        MissingPromptError: If the tool lacks those result texts.
    """
    notes = []
    if feature.feature_type == "buffer_area":
        notes.append(get_tool_result_text(component, tool_key, "note_approximate_area"))
    if abs(feature.total_area - analyzed_ha) > AREA_DIFFERENCE_TOLERANCE_HA:
        notes.append(get_tool_result_text(
            component, tool_key, "note_area_difference", registered_ha=f"{feature.total_area:.1f}", analyzed_ha=f"{analyzed_ha:.1f}",
        ))
    return notes


def latest_area_note(run_context: RunContext, area_name: Optional[str], feature: Feature) -> str:
    """Returns the note telling which area was used when the user did not name one and several are registered.

    The text is ``note_latest_area`` of ``agents.yml`` (``{name}`` and ``{count}`` filled in), so the assistant says which
    area it used instead of silently picking one.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): The name the user gave, if any.
        feature (Feature): The area that was resolved (the most recently registered one when no name was given).

    Returns:
        str: The note, or an empty string when a name was given or only one area is registered.
    """
    if area_name and area_name.strip():
        return ""
    count = len(get_registered_features(run_context.session_state or {}).features)
    if count < 2:
        return ""
    return get_agent_config("single_agent")["note_latest_area"].strip().format(name=area_label(feature), count=count)


def with_note(text: str, note: str) -> str:
    """Appends a note on its own line (the text unchanged when the note is empty)."""
    return f"{text}\n{note}" if note else text
