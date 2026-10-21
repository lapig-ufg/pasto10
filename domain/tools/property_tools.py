"""Area registration tools — circular (buffer) area around a coordinate.

Flow driven by ``session_state["registration_state"]`` (see ``domain/agent.py``):
``start_buffer_registration_by_coordinate`` leaves a candidate pending,
``confirm_registration`` moves it to the naming step (or registers it at once
when a name is given), ``complete_registration`` registers it under a name
and ``cancel_registration`` aborts at any step. The tools are thin: geometry
and image rendering live in ``domain/services/geospatial/`` and every text
returned to the model comes from ``domain/prompts/tools.yml``. Unexpected
errors are logged in full and the model only gets a generic message.
"""

import math
from typing import Optional

from semente import Context as RunContext
from semente import Image, ToolResult, tool
from semente.logging import log_debug, log_warning
from semente.configs.prompts import get_tool_description, get_tool_result_text

from domain.schemas.feature import Feature, FeatureMetadata
from domain.services.geospatial.geometry import build_buffered_area
from domain.services.geospatial.image import RenderedImage, render_buffer_image
from domain.utils.feature_utils import get_registered_features, set_registered_features, summarize_feature
from domain.utils.tool_utils import unexpected_error

_COMPONENT = "property_tools"

# Bounds of the buffer radius accepted by the buffer registration tool (meters).
_MIN_RADIUS_METERS = 50
_MAX_RADIUS_METERS = 1000
_DEFAULT_RADIUS_METERS = 200
# Web Mercator basemaps do not cover the poles; ±85° keeps the image meaningful.
_MAX_ABS_LATITUDE = 85
_MAX_ABS_LONGITUDE = 180


def _validate_buffer_radius(radius: float, tool_key: str) -> ToolResult | None:
    """Checks the buffer radius against the accepted bounds.

    Args:
        radius (float): Requested radius in meters.
        tool_key (str): Tool name whose ``results.invalid_radius`` text is rendered.

    Returns:
        ToolResult | None: The out-of-range instructions (with the min/max
        bounds) when the radius is invalid, or None when it is valid.
    """
    if not (isinstance(radius, (int, float)) and _MIN_RADIUS_METERS <= radius <= _MAX_RADIUS_METERS):
        log_warning(f"Buffer radius out of range: {radius}")
        return ToolResult(
            content=get_tool_result_text(
                "property_tools", tool_key, "invalid_radius",
                radius=radius, min_radius=_MIN_RADIUS_METERS, max_radius=_MAX_RADIUS_METERS,
            )
        )
    return None


def _validate_coordinates(latitude: float, longitude: float, tool_key: str) -> ToolResult | None:
    """Checks that a coordinate is finite and inside the supported range.

    Latitudes beyond ±85° are rejected because Web Mercator basemaps do not
    cover the polar regions.

    Args:
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.
        tool_key (str): Tool name whose ``results.invalid_coordinates`` text is rendered.

    Returns:
        ToolResult | None: The invalid-coordinate instructions, or None when valid.
    """
    try:
        valid = (
            math.isfinite(latitude) and math.isfinite(longitude)
            and abs(latitude) <= _MAX_ABS_LATITUDE
            and abs(longitude) <= _MAX_ABS_LONGITUDE
        )
    except TypeError:
        valid = False
    if not valid:
        log_warning(f"Invalid coordinates: lat={latitude}, lon={longitude}")
        return ToolResult(
            content=get_tool_result_text(
                "property_tools", tool_key, "invalid_coordinates",
                latitude=latitude, longitude=longitude, max_latitude=_MAX_ABS_LATITUDE,
            )
        )
    return None


def _build_buffer_candidate(
    latitude: float,
    longitude: float,
    radius: float = _DEFAULT_RADIUS_METERS,
) -> tuple[Feature, RenderedImage, str]:
    """Builds a buffer area candidate, its confirmation image and result text.

    Pure with respect to the session: nothing is written to
    ``session_state``, so the caller can store the candidate only after
    every step (geometry, image, summary and template) has succeeded.
    Basemap network failures do not count as failures (the image falls back
    to a neutral background). Does not validate inputs; callers must do it
    beforehand.

    Args:
        latitude (float): Center latitude in decimal degrees.
        longitude (float): Center longitude in decimal degrees.
        radius (float): Buffer radius in meters.

    Returns:
        Tuple (Feature, RenderedImage, str): the candidate, its encoded image
        (JPEG over a tile basemap, PNG over the neutral background) and the
        ``confirm_buffer_area`` text (area summary, radius and its bounds).

    Raises:
        Exception: Any geometry, rendering, summary or template error; the
        caller renders the tool-specific error text.
    """
    buffer_area = build_buffered_area(latitude=latitude, longitude=longitude, radius=radius)
    rendered = render_buffer_image(buffer_area)
    content = get_tool_result_text(
        "property_tools", "start_buffer_registration_by_coordinate", "confirm_buffer_area",
        area_details=summarize_feature(buffer_area),
        radius=radius, default_radius=_DEFAULT_RADIUS_METERS,
        min_radius=_MIN_RADIUS_METERS, max_radius=_MAX_RADIUS_METERS,
    )
    return buffer_area, rendered, content


@tool(description=get_tool_description("property_tools", "start_buffer_registration_by_coordinate"))
def start_buffer_registration_by_coordinate(
    run_context: RunContext,
    latitude: float,
    longitude: float,
    radius: int = _DEFAULT_RADIUS_METERS,
):
    """Starts the registration of a circular buffer area around a coordinate.

    Validates the radius (50–1000 m) and the coordinate (finite, |lat| ≤ 85°,
    |lon| ≤ 180°), builds the buffer polygon, renders the confirmation image,
    builds the result text and only then leaves the area pending the user's
    confirmation.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        latitude (float): Latitude of the center point in decimal degrees.
        longitude (float): Longitude of the center point in decimal degrees.
        radius (int): Buffer radius in meters (50 to 1000). Default: 200.

    Returns:
        ToolResult: Confirmation instructions (area summary, radius and its
        bounds) plus the confirmation image (JPEG or PNG) on success;
        ``invalid_radius``/``invalid_coordinates``/``error`` instructions
        (no image) otherwise.

    Side effects:
        On success, sets ``candidate_properties`` (one ``buffer_area``
        feature) and ``registration_state = "pending"``. The image and the
        result text are built before the session is touched, so any failure
        (geometry, rendering, summary or template) leaves the session state
        untouched.
    """
    tool_key = "start_buffer_registration_by_coordinate"
    log_debug(f"{tool_key}: lat={latitude}, lon={longitude}, radius={radius}")
    try:
        invalid = _validate_buffer_radius(radius, tool_key) or _validate_coordinates(latitude, longitude, tool_key)
        if invalid is not None:
            return invalid

        buffer_area, rendered, content = _build_buffer_candidate(latitude, longitude, radius)
        image = Image(content=rendered.content, format=rendered.format, mime_type=rendered.mime_type)

        run_context.session_state["candidate_properties"] = [buffer_area.model_dump()]
        run_context.session_state["registration_state"] = "pending"

        log_debug(f"{tool_key}: area generated ({buffer_area.id}, {radius} m, {rendered.format})")
        return ToolResult(content=content, images=[image])
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)


def _persist_registration(run_context: RunContext, candidate_properties: list, name: str) -> ToolResult:
    """Completes the registration of the pending candidate under a name.

    Shared by ``complete_registration`` (name given in a separate turn) and
    ``confirm_registration`` (name given together with the confirmation),
    so the state never gets stuck in ``"final"``. The id stays stable; the
    name lives in ``metadata['name']``.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        candidate_properties (list): Serialized pending candidates (the first one is registered).
        name (str): User-chosen display name.

    Returns:
        ToolResult: The ``complete_registration.results.registration_complete``
        text (name and area summary; no internal id).

    Side effects:
        Appends the named feature to ``session_state["all_properties"]``,
        resets ``registration_state`` to None and ``candidate_properties`` to
        ``[]``. The result text is built before any write, so a failure
        (invalid candidate, summary or template error) leaves the session
        state untouched.

    Raises:
        Exception: Propagated to the calling tool, which returns ``error``.
    """
    candidate = Feature.model_validate(candidate_properties[0])
    metadata = [entry for entry in candidate.metadata if entry.key != "name"]
    metadata.append(FeatureMetadata(key="name", value=name))
    registered_feature = candidate.model_copy(update={"metadata": metadata})

    registered_features = get_registered_features(run_context.session_state)
    registered_features.features.append(registered_feature)
    content = get_tool_result_text(
        "property_tools", "complete_registration", "registration_complete",
        name=name, area_details=summarize_feature(registered_feature),
    )

    set_registered_features(run_context.session_state, registered_features)
    run_context.session_state["registration_state"] = None
    run_context.session_state["candidate_properties"] = []

    log_debug(f"_persist_registration: registration complete ({registered_feature.id})")
    return ToolResult(content=content)


@tool(description=get_tool_description("property_tools", "confirm_registration"))
def confirm_registration(run_context: RunContext, name: Optional[str] = None):
    """Confirms the pending buffer area shown to the user.

    Without ``name`` the flow moves to the naming step; with ``name`` (the
    user confirmed and named the area in the same message) the registration
    is completed immediately.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        name (str, optional): Display name, when the user already gave one.

    Returns:
        ToolResult: ``confirmed_prompt_name`` (no name), the registration
        completion text (with name), ``no_pending_area`` when there is no
        candidate, or ``error``.

    Side effects:
        Without name: sets ``registration_state = "final"``. With name: see
        ``_persist_registration``. With no candidate: resets a stale
        ``registration_state`` to None and ``candidate_properties`` to ``[]``.
        The candidate is validated and the result text built before any
        write, so an invalid candidate or a summary failure leaves the
        session state untouched (``error`` is returned).
    """
    log_debug(f"confirm_registration: name={name}")
    try:
        candidate_properties = run_context.session_state.get("candidate_properties")
        if not candidate_properties:
            log_warning("confirm_registration called without a pending area")
            run_context.session_state["registration_state"] = None
            run_context.session_state["candidate_properties"] = []
            return ToolResult(content=get_tool_result_text("property_tools", "confirm_registration", "no_pending_area"))

        if name:
            return _persist_registration(run_context, candidate_properties, name)

        candidate = Feature.model_validate(candidate_properties[0])
        content = get_tool_result_text(
            "property_tools", "confirm_registration", "confirmed_prompt_name",
            area_details=summarize_feature(candidate),
        )

        run_context.session_state["registration_state"] = "final"
        run_context.session_state["candidate_properties"] = [candidate_properties[0]]

        log_debug(f"confirm_registration: area {candidate.id} confirmed")
        return ToolResult(content=content)
    except Exception:
        return unexpected_error(_COMPONENT, "confirm_registration")


@tool(description=get_tool_description("property_tools", "complete_registration"))
def complete_registration(run_context: RunContext, name: str):
    """Completes the registration of the confirmed area with the user-chosen name.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        name (str): Display name for the area.

    Returns:
        ToolResult: The registration completion text, ``no_pending_area``
        when there is no candidate, or ``error``.

    Side effects:
        See ``_persist_registration``. With no candidate: resets a stale
        ``registration_state`` to None and ``candidate_properties`` to ``[]``.
    """
    log_debug(f"complete_registration: name={name}")
    try:
        candidate_properties = run_context.session_state.get("candidate_properties")
        if not candidate_properties:
            log_warning("complete_registration called without a pending area")
            run_context.session_state["registration_state"] = None
            run_context.session_state["candidate_properties"] = []
            return ToolResult(content=get_tool_result_text("property_tools", "complete_registration", "no_pending_area"))
        return _persist_registration(run_context, candidate_properties, name)
    except Exception:
        return unexpected_error(_COMPONENT, "complete_registration")


@tool(description=get_tool_description("property_tools", "cancel_registration"))
def cancel_registration(run_context: RunContext):
    """Cancels the pending registration (wrong area, or the user gave up).

    Args:
        run_context (RunContext): The run context holding ``session_state``.

    Returns:
        ToolResult: The ``cancelled`` text, or ``error``.

    Side effects:
        Resets ``registration_state`` to None and ``candidate_properties``
        to ``[]``; ``all_properties`` is never touched.
    """
    log_debug("cancel_registration")
    try:
        run_context.session_state["registration_state"] = None
        run_context.session_state["candidate_properties"] = []
        return ToolResult(content=get_tool_result_text("property_tools", "cancel_registration", "cancelled"))
    except Exception:
        return unexpected_error(_COMPONENT, "cancel_registration")
