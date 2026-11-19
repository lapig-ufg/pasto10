"""Area registration tools — the approximate circle, a user boundary and a transect.

Flow driven by ``session_state["registration_state"]`` (see ``domain/agent.py``):
``start_buffer_registration_by_coordinate``, ``start_buffer_registration_by_url``
(the coordinate taken from a Google Maps link), ``register_polygon`` (a pasted
GeoJSON or an attached geospatial file) and
``start_transect_registration_by_coordinates`` leave a candidate pending,
``confirm_registration`` moves it to the naming step (or registers it at once
when a name is given), ``complete_registration`` registers it under a name
(and, for a transect, the total property area) and ``cancel_registration``
aborts at any step. The tools are thin: geometry and image rendering live in
``domain/services/geospatial/`` and every text returned to the model comes
from ``domain/prompts/tools.yml``. Unexpected errors are logged in full and
the model only gets a generic message.
"""

import math
from typing import List, Optional

from semente import Context as RunContext
from semente import File, Image, ToolResult, tool
from semente.logging import log_debug, log_warning
from semente.configs.prompts import get_agent_config, get_tool_description, get_tool_result_text

from domain.schemas.feature import Feature, FeatureMetadata
from domain.services.geospatial.candidate import polygon_candidate_to_feature
from domain.services.geospatial.geofile import (
    is_supported_geofile,
    max_geofile_mb,
    parse_converted_geojson,
    parse_geofile,
)
from domain.services.geospatial.geometry import MAX_ABS_LATITUDE, build_buffered_area, is_valid_coordinate
from domain.services.geospatial.image import RenderedImage, render_buffer_image, render_polygon_image, render_transect_image
from domain.services.geospatial.maps_url import MapsUrlError, extract_coordinates
from domain.services.geospatial.polygon_input import InvalidPolygon, max_polygon_ha, parse_geojson
from domain.services.geospatial.transect import (
    MAX_LENGTH_METERS as _MAX_TRANSECT_LENGTH_METERS,
    MAX_TOTAL_AREA_HA,
    MIN_LENGTH_METERS as _MIN_TRANSECT_LENGTH_METERS,
    MIN_TOTAL_AREA_HA,
    InvalidTransect,
    build_transect,
)
from domain.utils.feature_utils import get_registered_features, set_registered_features, summarize_feature
from domain.utils.tool_utils import unexpected_error

_COMPONENT = "property_tools"

# Bounds of the buffer radius accepted by the buffer registration tool (meters).
_MIN_RADIUS_METERS = 50
_MAX_RADIUS_METERS = 1000
_DEFAULT_RADIUS_METERS = 500


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
    valid = is_valid_coordinate(latitude, longitude)
    if not valid:
        log_warning(f"Invalid coordinates: lat={latitude}, lon={longitude}")
        return ToolResult(
            content=get_tool_result_text(
                "property_tools", tool_key, "invalid_coordinates",
                latitude=latitude, longitude=longitude, max_latitude=MAX_ABS_LATITUDE,
            )
        )
    return None


def _build_buffer_candidate(
    latitude: float,
    longitude: float,
    radius: float = _DEFAULT_RADIUS_METERS,
) -> tuple[Feature, RenderedImage, str]:
    """Builds a buffer area candidate, its confirmation image and result text.

    The circle comes from Semente's ``BufferSource`` through
    ``build_buffered_area``. Pure with respect to the session: nothing is
    written to ``session_state``, so the caller can store the candidate only
    after every step (geometry, image, summary and template) has succeeded.
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
    rendered = render_buffer_image(buffer_area, origin_label=_origin_label("buffer_area"))
    content = get_tool_result_text(
        "property_tools", "start_buffer_registration_by_coordinate", "confirm_buffer_area",
        area_details=summarize_feature(buffer_area),
        radius=radius, default_radius=_DEFAULT_RADIUS_METERS,
        min_radius=_MIN_RADIUS_METERS, max_radius=_MAX_RADIUS_METERS,
    )
    return buffer_area, rendered, content


def _stage_buffer_candidate(
    run_context: RunContext, tool_key: str, latitude: float, longitude: float, radius: float,
) -> ToolResult:
    """Validates the coordinate, builds the buffer candidate and leaves it pending.

    Shared by the coordinate and the link starters, so both behave exactly
    the same. The radius must have been validated by the caller.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        tool_key (str): Calling tool, whose ``results`` texts are rendered.
        latitude (float): Center latitude in decimal degrees.
        longitude (float): Center longitude in decimal degrees.
        radius (float): Buffer radius in meters (already validated).

    Returns:
        ToolResult: Confirmation text plus image on success; the
        ``invalid_coordinates`` instructions (no image) otherwise.

    Raises:
        Exception: Any geometry, rendering, summary or template error; the
        calling tool renders its generic ``error`` text.

    Side effects:
        On success, sets ``candidate_properties`` (one ``buffer_area``
        feature) and ``registration_state = "pending"`` after every step has
        succeeded; any failure leaves the session state untouched.
    """
    invalid = _validate_coordinates(latitude, longitude, tool_key)
    if invalid is not None:
        return invalid

    buffer_area, rendered, content = _build_buffer_candidate(latitude, longitude, radius)
    image = Image(content=rendered.content, format=rendered.format, mime_type=rendered.mime_type)

    run_context.session_state["candidate_properties"] = [buffer_area.model_dump()]
    run_context.session_state["registration_state"] = "pending"

    log_debug(f"{tool_key}: area generated ({buffer_area.id}, {radius} m, {rendered.format})")
    return ToolResult(content=content, images=[image])


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
        radius (int): Buffer radius in meters (50 to 1000). Default: 500.

    Returns:
        ToolResult: Confirmation instructions (area summary, radius and its
        bounds) plus the confirmation image (JPEG or PNG) on success;
        ``invalid_radius``/``invalid_coordinates``/``error`` instructions
        (no image) otherwise.

    Side effects:
        See ``_stage_buffer_candidate``: the session changes only on success.
    """
    tool_key = "start_buffer_registration_by_coordinate"
    log_debug(f"{tool_key}: lat={latitude}, lon={longitude}, radius={radius}")
    try:
        invalid = _validate_buffer_radius(radius, tool_key)
        if invalid is not None:
            return invalid
        return _stage_buffer_candidate(run_context, tool_key, latitude, longitude, radius)
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)


@tool(description=get_tool_description("property_tools", "start_buffer_registration_by_url"))
def start_buffer_registration_by_url(
    run_context: RunContext,
    url: str,
    radius: int = _DEFAULT_RADIUS_METERS,
):
    """Starts the registration of a buffer area around the coordinate of a Google Maps link.

    The link is only another way to give the coordinate: after extracting it
    (``extract_coordinates``: https Google hosts only, short links resolved
    with validated redirects) the flow is exactly that of
    ``start_buffer_registration_by_coordinate``.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        url (str): The Google Maps location link sent by the user.
        radius (int): Buffer radius in meters (50 to 1000). Default: 500.

    Returns:
        ToolResult: Confirmation instructions plus image on success;
        ``invalid_radius``, ``invalid_coordinates``, ``no_coordinates``,
        ``unsupported_link``, ``link_unreachable`` or ``error`` instructions
        (no image, no network detail) otherwise.

    Side effects:
        See ``_stage_buffer_candidate``: the session changes only on success.
        Short links trigger up to a few HTTPS requests to Google hosts.
    """
    tool_key = "start_buffer_registration_by_url"
    log_debug(f"{tool_key}: radius={radius}")
    try:
        invalid = _validate_buffer_radius(radius, tool_key)
        if invalid is not None:
            return invalid
        try:
            latitude, longitude = extract_coordinates(url)
        except MapsUrlError as error:
            log_warning(f"{tool_key}: link rejected ({error.reason})")
            if error.reason == "invalid_coordinates":
                return _validate_coordinates(error.latitude, error.longitude, tool_key)
            return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, error.reason))
        return _stage_buffer_candidate(run_context, tool_key, latitude, longitude, radius)
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)


def _validate_total_area(total_area_ha: float, tool_key: str) -> ToolResult | None:
    """Checks the total property area of a transect against the accepted bounds.

    Args:
        total_area_ha (float): Requested total property area in hectares.
        tool_key (str): Tool name whose ``results.invalid_total_area`` text is rendered.

    Returns:
        ToolResult | None: The instructions to ask for a valid area, or None when valid.
    """
    try:
        valid = (
            isinstance(total_area_ha, (int, float))
            and not isinstance(total_area_ha, bool)
            and math.isfinite(total_area_ha)
            and MIN_TOTAL_AREA_HA <= total_area_ha <= MAX_TOTAL_AREA_HA
        )
    except TypeError:
        valid = False
    if not valid:
        log_warning(f"Total property area out of range: {total_area_ha}")
        return ToolResult(content=get_tool_result_text(
            _COMPONENT, tool_key, "invalid_total_area",
            total_area_ha=total_area_ha, min_area_ha=MIN_TOTAL_AREA_HA, max_area_ha=MAX_TOTAL_AREA_HA,
        ))
    return None


def _named_candidate(candidate: Feature, name: str) -> Feature:
    """Returns a copy of a candidate with the user-chosen name in metadata."""
    metadata = [entry for entry in candidate.metadata if entry.key != "name"]
    metadata.append(FeatureMetadata(key="name", value=name))
    return candidate.model_copy(update={"metadata": metadata})


def _build_transect_candidate(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> tuple[Feature, RenderedImage, str]:
    """Builds a transect candidate, its confirmation image and result text.

    Pure with respect to the session: nothing is written to
    ``session_state``, so the caller stores the candidate only after every
    step has succeeded. Does not validate inputs; callers must do it
    beforehand.

    Args:
        start_latitude (float): Start latitude in decimal degrees.
        start_longitude (float): Start longitude in decimal degrees.
        end_latitude (float): End latitude in decimal degrees.
        end_longitude (float): End longitude in decimal degrees.

    Returns:
        Tuple (Feature, RenderedImage, str): the candidate, its encoded image
        and the ``confirm_transect`` text.

    Raises:
        InvalidTransect: If the line length is outside the accepted bounds.
        Exception: Any rendering, summary or template error.
    """
    transect = build_transect(start_latitude, start_longitude, end_latitude, end_longitude)
    rendered = render_transect_image(transect, origin_label=_origin_label("transect"))
    content = get_tool_result_text(
        _COMPONENT, "start_transect_registration_by_coordinates", "confirm_transect",
        area_details=summarize_feature(transect),
        length_km=f"{float(transect.get_metadata('length_m', 0)) / 1000:g}",
    )
    return transect, rendered, content


@tool(description=get_tool_description("property_tools", "start_transect_registration_by_coordinates"))
def start_transect_registration_by_coordinates(
    run_context: RunContext,
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
):
    """Starts the registration of a transect line between two coordinates.

    Validates both coordinates (finite, |lat| ≤ 85°, |lon| ≤ 180°) and the
    line length, builds the line, renders the confirmation image, builds the
    result text and only then leaves the transect pending the user's
    confirmation. The total property area is collected later, in the final
    registration step.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        start_latitude (float): Start latitude in decimal degrees.
        start_longitude (float): Start longitude in decimal degrees.
        end_latitude (float): End latitude in decimal degrees.
        end_longitude (float): End longitude in decimal degrees.

    Returns:
        ToolResult: Confirmation instructions plus the confirmation image on
        success; ``invalid_coordinates``/``invalid_length``/``error``
        instructions (no image) otherwise.

    Side effects:
        On success, sets ``candidate_properties`` (one ``transect`` feature)
        and ``registration_state = "pending"``. Any failure leaves the
        session state untouched.
    """
    tool_key = "start_transect_registration_by_coordinates"
    log_debug(
        f"{tool_key}: start=({start_latitude}, {start_longitude}), "
        f"end=({end_latitude}, {end_longitude})"
    )
    try:
        invalid = (
            _validate_coordinates(start_latitude, start_longitude, tool_key)
            or _validate_coordinates(end_latitude, end_longitude, tool_key)
        )
        if invalid is not None:
            return invalid

        transect, rendered, content = _build_transect_candidate(
            start_latitude, start_longitude, end_latitude, end_longitude,
        )
        image = Image(content=rendered.content, format=rendered.format, mime_type=rendered.mime_type)

        run_context.session_state["candidate_properties"] = [transect.model_dump()]
        run_context.session_state["registration_state"] = "pending"

        log_debug(f"{tool_key}: transect generated ({transect.id}, {rendered.format})")
        return ToolResult(content=content, images=[image])
    except InvalidTransect as error:
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(
            _COMPONENT, tool_key, "invalid_length",
            length_m=round(error.length_m, 1),
            min_length_m=_MIN_TRANSECT_LENGTH_METERS, max_length_m=_MAX_TRANSECT_LENGTH_METERS,
        ))
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)


def _first_attached_geofile(files: Optional[List[File]]) -> Optional[tuple[bytes, str, bool]]:
    """Returns the first geospatial file attached to the conversation, or None.

    Semente's input step already converts every supported geospatial document
    (shapefile .zip/.rar, KMZ, KML and GeoJSON) to GeoJSON and attaches it as a
    file whose ``format`` is ``geojson``; a raw geofile (another client or a
    direct tool call) is returned with ``converted=False`` so this tool converts
    it itself.

    Args:
        files (List[File], optional): Files injected by the framework.

    Returns:
        tuple[bytes, str, bool] | None: ``(content, filename, converted)`` of the
        first geospatial file, or None when there is none.
    """
    for file in files or []:
        filename = getattr(file, "name", None) or getattr(file, "filename", None) or ""
        content = getattr(file, "content", None)
        if not content:
            continue
        if getattr(file, "format", None) in ("geojson", "json"):
            return content, filename or "property.geojson", True
        if is_supported_geofile(filename):
            return content, filename, False
    return None


def _invalid_polygon_result(tool_key: str, error: InvalidPolygon, from_file: bool) -> ToolResult:
    """Maps an ``InvalidPolygon`` kind to the result text that tells the model what to say.

    Args:
        tool_key (str): Tool whose ``results`` texts are rendered.
        error (InvalidPolygon): The parser failure.
        from_file (bool): Whether the boundary came from an attached geofile (larger limits).

    Returns:
        ToolResult: ``polygon_too_large``, ``polygon_too_complex``/``pasted_too_complex``,
        ``unreadable_file`` or ``invalid_polygon`` instructions.
    """
    if error.kind == "too_large":
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "polygon_too_large", max_ha=f"{max_polygon_ha():g}"))
    if error.kind in ("file_too_large", "too_many_vertices", "too_many_features", "too_many_rings", "too_complex"):
        if from_file:
            return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "polygon_too_complex", max_mb=max_geofile_mb()))
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "pasted_too_complex"))
    if error.kind in ("unsupported_format", "unreadable_file", "geo_stack_missing"):
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "unreadable_file"))
    return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "invalid_polygon"))


def _make_pending(run_context: RunContext, candidate, tool_key: str) -> ToolResult:
    """Turns a polygon candidate into the pending registration and returns the confirmation result.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        candidate (PropertyCandidate): The boundary provided by the user.
        tool_key (str): Tool whose ``confirm_candidate`` text is rendered.

    Returns:
        ToolResult: The confirmation instructions and the boundary image.

    Raises:
        Exception: Geometry, rendering or template failures, before the session is touched.

    Side effects:
        Sets ``candidate_properties`` to the serialized ``Feature`` and ``registration_state`` to ``"pending"``.
    """
    feature = polygon_candidate_to_feature(candidate)
    label = _origin_label(feature.feature_type)
    rendered = render_polygon_image(feature, origin_label=label)
    content = get_tool_result_text(
        _COMPONENT, tool_key, "confirm_candidate",
        origin_label=label, area_details=summarize_feature(feature), notes="",
    )
    run_context.session_state["candidate_properties"] = [feature.model_dump()]
    run_context.session_state["registration_state"] = "pending"
    return ToolResult(content=content, images=[Image(content=rendered.content, format=rendered.format, mime_type=rendered.mime_type)])


@tool(description=get_tool_description("property_tools", "register_polygon"))
def register_polygon(run_context: RunContext, geojson: Optional[str] = None, files: Optional[List[File]] = None):
    """Starts the registration of a rural property provided by the user (GeoJSON text or geospatial file).

    The boundary comes from, in order: the ``geojson`` argument (text pasted in the message), the first geospatial
    file of ``files`` (framework attachments, already converted by Semente's input step) and
    ``session_state["uploaded_geojson"]`` (``{"name", "content"}``, written by the front end for the current turn).
    ``uploaded_geojson`` is always removed from the session when read, so an old upload can never be reused.

    A pasted polygon goes through the strict pasted-input limits (``parse_geojson``); an attached file goes through
    the geofile limits and is converted to GeoJSON first when it is still raw (shapefile .zip/.rar, KMZ, KML or
    GeoJSON). Either way the registered feature is a ``rural_property``; a file with several polygons becomes one
    property (the union of its parts).

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        geojson (str, optional): GeoJSON text pasted by the user (Feature, FeatureCollection or geometry).
        files (List[File], optional): Files attached to the conversation, injected by the framework; the
            first geospatial file is used when ``geojson`` is not given.

    Returns:
        ToolResult: ``confirm_candidate`` with the polygon image; or ``missing_polygon``, ``invalid_polygon``,
        ``polygon_too_large``, ``polygon_too_complex``, ``unreadable_file`` or ``error`` instructions.

    Side effects:
        Removes ``uploaded_geojson`` from the session. On success, sets ``candidate_properties`` (the ``Feature``)
        and ``registration_state = "pending"``.
    """
    tool_key = "register_polygon"
    log_debug(f"{tool_key}: geojson={'yes' if geojson else 'no'}, files={len(files or [])}")
    try:
        uploaded = run_context.session_state.pop("uploaded_geojson", None)
        from_file = False
        try:
            if geojson:
                candidate = parse_geojson(geojson, attributes={"feature_type": "rural_property"})
            elif (attached := _first_attached_geofile(files)) is not None:
                from_file = True
                content, filename, converted = attached
                candidate = parse_converted_geojson(content, filename) if converted else parse_geofile(content, filename)
            elif isinstance(uploaded, dict) and uploaded.get("content"):
                from_file = True
                candidate = parse_converted_geojson(uploaded["content"], str(uploaded.get("name") or "property.geojson"))
            else:
                return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "missing_polygon"))
        except InvalidPolygon as error:
            log_warning(f"{tool_key}: {error.kind}: {error}")
            return _invalid_polygon_result(tool_key, error, from_file)
        return _make_pending(run_context, candidate, tool_key)
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
def confirm_registration(
    run_context: RunContext,
    name: Optional[str] = None,
    total_area_ha: Optional[float] = None,
):
    """Confirms the pending area or transect shown to the user.

    Without ``name`` the flow moves to the naming step; with ``name`` (the
    user confirmed and named the area in the same message) the registration
    is completed immediately. For a transect, the total property area in
    hectares is also required (``total_area_ha``) and can be given in this
    same message.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        name (str, optional): Display name, when the user already gave one.
        total_area_ha (float, optional): Total property area in hectares, only
            for a transect.

    Returns:
        ToolResult: ``confirmed_prompt_name`` (no name), the registration
        completion text (with name), ``confirmed_prompt_name_area`` when a
        transect is confirmed without a name or area, ``confirmed_prompt_area``
        when a transect name is given without its area, ``invalid_total_area``
        for an invalid area, ``no_pending_area`` when there is no candidate, or
        ``error``.

    Side effects:
        Without name: sets ``registration_state = "final"``. With name: see
        ``_persist_registration``. With no candidate: resets a stale
        ``registration_state`` to None and ``candidate_properties`` to ``[]``.
        A provided transect area is validated and stored in the candidate
        before any state change. The candidate is validated and the result
        text built before any write, so failures leave the session state
        untouched (``error`` is returned).
    """
    log_debug(f"confirm_registration: name={name}, total_area_ha={total_area_ha}")
    try:
        candidate_properties = run_context.session_state.get("candidate_properties")
        if not candidate_properties:
            log_warning("confirm_registration called without a pending area")
            run_context.session_state["registration_state"] = None
            run_context.session_state["candidate_properties"] = []
            return ToolResult(content=get_tool_result_text("property_tools", "confirm_registration", "no_pending_area"))

        candidate = Feature.model_validate(candidate_properties[0])
        if candidate.feature_type == "transect" and total_area_ha is not None:
            invalid = _validate_total_area(total_area_ha, "confirm_registration")
            if invalid is not None:
                return invalid
            candidate = candidate.model_copy(update={"total_area": round(float(total_area_ha), 2)})
            candidate_properties = [candidate.model_dump()]
            run_context.session_state["candidate_properties"] = candidate_properties

        if name:
            if candidate.feature_type == "transect" and candidate.total_area <= 0:
                candidate = _named_candidate(candidate, name)
                run_context.session_state["candidate_properties"] = [candidate.model_dump()]
                run_context.session_state["registration_state"] = "final"
                content = get_tool_result_text(
                    "property_tools", "confirm_registration", "confirmed_prompt_area",
                    name=name, area_details=summarize_feature(candidate),
                    min_area_ha=MIN_TOTAL_AREA_HA, max_area_ha=MAX_TOTAL_AREA_HA,
                )
                log_debug(f"confirm_registration: transect {candidate.id} confirmed; area pending")
                return ToolResult(content=content)
            return _persist_registration(run_context, candidate_properties, name)

        if candidate.feature_type == "transect" and candidate.total_area <= 0:
            key = "confirmed_prompt_name_area"
        else:
            key = "confirmed_prompt_name"
        content = get_tool_result_text(
            "property_tools", "confirm_registration", key,
            area_details=summarize_feature(candidate),
        )

        run_context.session_state["registration_state"] = "final"
        run_context.session_state["candidate_properties"] = [candidate.model_dump()]

        log_debug(f"confirm_registration: area {candidate.id} confirmed")
        return ToolResult(content=content)
    except Exception:
        return unexpected_error(_COMPONENT, "confirm_registration")


@tool(description=get_tool_description("property_tools", "complete_registration"))
def complete_registration(
    run_context: RunContext,
    name: str,
    total_area_ha: Optional[float] = None,
):
    """Completes the registration of the confirmed area with the user-chosen name.

    For a transect, the total property area in hectares is also required
    (``total_area_ha``); it can be given here or was already stored in the
    pending candidate.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        name (str): Display name for the area.
        total_area_ha (float, optional): Total property area in hectares, only
            for a transect.

    Returns:
        ToolResult: The registration completion text, ``needs_total_area`` or
        ``invalid_total_area`` for a transect without a valid area,
        ``no_pending_area`` when there is no candidate, or ``error``.

    Side effects:
        See ``_persist_registration``. With no candidate: resets a stale
        ``registration_state`` to None and ``candidate_properties`` to ``[]``.
        A provided transect area is validated and stored in the candidate
        before the registration is written.
    """
    log_debug(f"complete_registration: name={name}, total_area_ha={total_area_ha}")
    try:
        candidate_properties = run_context.session_state.get("candidate_properties")
        if not candidate_properties:
            log_warning("complete_registration called without a pending area")
            run_context.session_state["registration_state"] = None
            run_context.session_state["candidate_properties"] = []
            return ToolResult(content=get_tool_result_text("property_tools", "complete_registration", "no_pending_area"))

        candidate = Feature.model_validate(candidate_properties[0])
        if candidate.feature_type == "transect":
            if total_area_ha is not None:
                invalid = _validate_total_area(total_area_ha, "complete_registration")
                if invalid is not None:
                    return invalid
                candidate = candidate.model_copy(update={"total_area": round(float(total_area_ha), 2)})
            elif candidate.total_area <= 0:
                return ToolResult(content=get_tool_result_text(
                    "property_tools", "complete_registration", "needs_total_area",
                    name=name, min_area_ha=MIN_TOTAL_AREA_HA, max_area_ha=MAX_TOTAL_AREA_HA,
                ))
            candidate_properties = [candidate.model_dump()]
            run_context.session_state["candidate_properties"] = candidate_properties

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


def _origin_label(origin_or_type: str) -> str:
    """Returns the display label of a feature type (``feature_type_names`` in ``agents.yml``).

    Args:
        origin_or_type (str): A feature type key (e.g. ``buffer_area``).

    Returns:
        str: The label, or ``feature_type_fallback`` for an unknown key.
    """
    config = get_agent_config("single_agent")
    return str(config["feature_type_names"].get(origin_or_type, config["feature_type_fallback"])).strip()
