"""Area registration tools — candidate polygons, user polygons and the approximate circle.

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
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import List, Optional

from semente import Context as RunContext
from semente import File, Image, ToolResult, tool
from semente.logging import log_debug, log_warning
from semente.configs.prompts import get_agent_config, get_tool_description, get_tool_result_text

from domain.schemas.candidate import PropertyCandidate
from domain.schemas.feature import Feature, FeatureMetadata
from domain.services.geospatial.geometry import build_buffered_area
from domain.services.geospatial.image import RenderedImage, render_buffer_image, render_candidate_image, render_candidates_image
from domain.services.property_sources import registry
from domain.services.property_sources.base import PropertySourceError
from domain.services.property_sources.user_polygon_source import InvalidPolygon, max_polygon_ha, parse_geojson
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
    rendered = render_buffer_image(buffer_area, origin_label=_origin_label("buffer_area"))
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


def _origin_label(origin_or_type: str) -> str:
    """Returns the display label of an origin / feature type (``feature_type_names`` in ``agents.yml``).

    Args:
        origin_or_type (str): A geometry origin or a feature type key (e.g. ``osm_landuse``, ``demo_registry``).

    Returns:
        str: The label, or ``feature_type_fallback`` for an unknown key.
    """
    config = get_agent_config("single_agent")
    return str(config["feature_type_names"].get(origin_or_type, config["feature_type_fallback"])).strip()


def _warning_notes(tool_key: str, warnings: set) -> str:
    """Renders the notes of the warning keys of the candidates, one per line.

    The texts are the ``warning_<key>`` results of ``find_property_candidates``, shared by all the candidate tools.

    Args:
        tool_key (str): Calling tool (for the logs only).
        warnings (set): Warning keys of the candidates.

    Returns:
        str: The notes (keys without a text are logged and skipped).
    """
    notes = []
    for key in sorted(warnings):
        try:
            notes.append(get_tool_result_text("property_tools", "find_property_candidates", f"warning_{key}"))
        except Exception:
            log_warning(f"{tool_key}: no text for warning {key!r}")
    return "\n".join(notes)


def _fallback_offer(tool_key: str, key: str, **values) -> ToolResult:
    """Returns a result text that offers the approximate circle or a user polygon (no candidate found or source down).

    Args:
        tool_key (str): Tool whose ``results`` texts are rendered.
        key (str): Result key (``no_candidates``, ``source_unavailable``, ``urban_area``...).
        **values: Extra placeholders of the template (the radius bounds are always provided).

    Returns:
        ToolResult: The instructions (no image).
    """
    return ToolResult(content=get_tool_result_text(
        "property_tools", tool_key, key, min_radius=_MIN_RADIUS_METERS, max_radius=_MAX_RADIUS_METERS,
        default_radius=_DEFAULT_RADIUS_METERS, **values,
    ))


def _make_pending(run_context: RunContext, candidate: PropertyCandidate, tool_key: str) -> ToolResult:
    """Turns a chosen candidate into the pending registration and returns the confirmation result.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        candidate (PropertyCandidate): The chosen or provided candidate.
        tool_key (str): Tool whose ``confirm_candidate`` text is rendered.

    Returns:
        ToolResult: The confirmation instructions (origin label, details, warnings) and the candidate image.

    Raises:
        Exception: Geometry, rendering or template failures, before the session is touched.

    Side effects:
        Sets ``candidate_properties`` to the serialized ``Feature`` and ``registration_state`` to ``"pending"``.
    """
    feature = candidate.to_feature()
    label = _candidate_label(candidate)
    rendered = render_candidate_image(candidate, label)
    content = get_tool_result_text(
        "property_tools", tool_key, "confirm_candidate", origin_label=label, area_details=summarize_feature(feature),
        notes=_warning_notes(tool_key, set(candidate.warnings)),
    )
    run_context.session_state["candidate_properties"] = [feature.model_dump()]
    run_context.session_state["registration_state"] = "pending"
    return ToolResult(content=content, images=[Image(content=rendered.content, format=rendered.format, mime_type=rendered.mime_type)])


def _search_sources(sources: list, latitude: float, longitude: float) -> tuple[list, list]:
    """Queries the property sources in parallel and merges their candidates (in the order of ``sources``).

    A source with ``response_timeout_s`` (the slow public OpenStreetMap service) is only waited for that long: if it does
    not answer in time the search goes on without it (its answer still fills the cache for the next search). Any failure of
    one source (typed error, unexpected exception, ``NotImplementedError``) is logged and reported as a failure of that
    source; the others still answer.

    Args:
        sources (list): Property sources to query.
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.

    Returns:
        tuple: (merged candidates in source order, failures as ``(source key, kind)`` with kind ``timeout``,
        ``unavailable`` or ``urban_area``).
    """
    candidates: list = []
    failures: list = []
    executor = ThreadPoolExecutor(max_workers=max(1, len(sources)))
    try:
        started = time.monotonic()
        futures = [(source, executor.submit(source.find_by_point, latitude, longitude)) for source in sources]
        for source, future in futures:
            limit = getattr(source, "response_timeout_s", None)
            remaining = None if limit is None else max(0.0, limit - (time.monotonic() - started))
            try:
                candidates += future.result(timeout=remaining)
            except FutureTimeout:
                failures.append((source.key, "timeout"))
            except PropertySourceError as error:
                log_warning(f"find_property_candidates: {source.key}: {error.kind}: {error}")
                failures.append((source.key, error.kind))
            except Exception as error:
                log_warning(f"find_property_candidates: {source.key} failed: {type(error).__name__}: {error}")
                failures.append((source.key, "unavailable"))
    finally:
        executor.shutdown(wait=False)
    return candidates, failures


def _source_label(source_key: str) -> str:
    """Returns the readable name of a source for the notes (``source_label_<key>`` result, or the key itself).

    Args:
        source_key (str): Source key (e.g. ``osm``).

    Returns:
        str: The label from ``tools.yml``, or the key when there is none.
    """
    try:
        return get_tool_result_text("property_tools", "find_property_candidates", f"source_label_{source_key}")
    except Exception:
        return source_key


def _candidate_label(candidate: PropertyCandidate) -> str:
    """Returns the display label of a candidate (``feature_type_names`` of its ``type_key``, so demo parcels say they are synthetic).

    Args:
        candidate (PropertyCandidate): The candidate.

    Returns:
        str: The label to show the user.
    """
    return _origin_label(candidate.type_key)


@tool(description=get_tool_description("property_tools", "find_property_candidates"))
def find_property_candidates(run_context: RunContext, latitude: float, longitude: float, source: Optional[str] = None):
    """Looks for candidate property polygons around a coordinate and shows them numbered.

    The enabled property sources (the demo registry and OpenStreetMap by default) are queried in parallel and their
    candidates merged; a source that does not answer in time is skipped and the user is told.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        latitude (float): Latitude of the point in decimal degrees (between -85 and 85).
        longitude (float): Longitude of the point in decimal degrees (between -180 and 180).
        source (str, optional): Key of one property source; every enabled source when omitted.

    Returns:
        ToolResult: ``candidates_list`` with the numbered candidates and a numbered image; or
        ``no_candidates``, ``source_unavailable``, ``urban_area``, ``unknown_source``,
        ``invalid_coordinates`` or ``error`` instructions (no image), which offer the approximate circle
        or a user polygon.

    Side effects:
        On success, sets ``candidate_properties`` (serialized candidates) and ``registration_state = "choosing"``;
        otherwise the session is untouched.
    """
    tool_key = "find_property_candidates"
    log_debug(f"{tool_key}: lat={latitude}, lon={longitude}, source={source}")
    try:
        invalid = _validate_coordinates(latitude, longitude, tool_key)
        if invalid is not None:
            return invalid
        try:
            sources = [registry.get_source(source)] if source else registry.enabled_sources()
        except KeyError:
            return _fallback_offer(tool_key, "unknown_source", source=source)
        if not sources:
            return _fallback_offer(tool_key, "source_unavailable")
        candidates, failures = _search_sources(sources, latitude, longitude)
        if not candidates:
            kinds = {kind for _, kind in failures}
            if kinds == {"urban_area"}:
                return _fallback_offer(tool_key, "urban_area")
            if kinds:
                return _fallback_offer(tool_key, "source_unavailable")
            return _fallback_offer(tool_key, "no_candidates", latitude=latitude, longitude=longitude)

        lines, captions = [], []
        for index, candidate in enumerate(candidates, start=1):
            distance = "" if candidate.distance_m is None else get_tool_result_text(
                "property_tools", tool_key, "distance_inside" if candidate.distance_m == 0 else "distance_away", distance=candidate.distance_m,
            )
            status = next((entry.value for entry in candidate.attributes if entry.key == "registry_status"), None)
            captions.append(" · ".join(part for part in (candidate.name or candidate.candidate_id, f"{candidate.area_ha:.1f} ha", status) if part))
            lines.append(get_tool_result_text(
                "property_tools", tool_key, "candidate_line", index=index, origin_label=_candidate_label(candidate),
                name=f" · {candidate.name}" if candidate.name else "", area=f"{candidate.area_ha:.1f}", distance=distance,
                status=get_tool_result_text("property_tools", tool_key, "status_text", status=status) if status else "",
            ))
        rendered = render_candidates_image(candidates, latitude, longitude, captions=captions)
        notes = [_warning_notes(tool_key, {key for candidate in candidates for key in candidate.warnings})]
        for source_key, kind in failures:
            if kind in ("timeout", "unavailable"):
                notes.append(get_tool_result_text("property_tools", tool_key, f"note_source_{kind}", source=_source_label(source_key)))
        content = get_tool_result_text(
            "property_tools", tool_key, "candidates_list", count=len(candidates), latitude=latitude, longitude=longitude,
            candidates="\n".join(lines), notes="\n".join(note for note in notes if note),
        )
        run_context.session_state["candidate_properties"] = [candidate.to_session() for candidate in candidates]
        run_context.session_state["registration_state"] = "choosing"
        image = Image(content=rendered.content, format=rendered.format, mime_type=rendered.mime_type)
        return ToolResult(content=content, images=[image])
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)


@tool(description=get_tool_description("property_tools", "find_property_by_registry_id"))
def find_property_by_registry_id(run_context: RunContext, identifier: str, source: Optional[str] = None):
    """Finds a property by its registry number and leaves it pending confirmation.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        identifier (str): Registry number of the property (for example ``DEMO-BRA-0001``).
        source (str, optional): Key of one property source; every enabled source is tried in order when omitted.

    Returns:
        ToolResult: ``confirm_candidate`` with the polygon image; or ``not_found``, ``source_unavailable``
        or ``error`` instructions (no image).

    Side effects:
        On success, sets ``candidate_properties`` (the ``Feature``) and ``registration_state = "pending"``;
        otherwise the session is untouched.
    """
    tool_key = "find_property_by_registry_id"
    log_debug(f"{tool_key}: identifier={identifier!r}, source={source}")
    try:
        try:
            sources = [registry.get_source(source)] if source else registry.enabled_sources()
        except KeyError:
            return _fallback_offer(tool_key, "source_unavailable")
        failed = False
        for chosen in sources:
            try:
                candidate = chosen.find_by_id(identifier)
            except (PropertySourceError, NotImplementedError) as error:
                log_warning(f"{tool_key}: {chosen.key}: {error}")
                failed = True
                continue
            if candidate is not None:
                return _make_pending(run_context, candidate, tool_key)
        return _fallback_offer(tool_key, "source_unavailable" if failed else "not_found", identifier=identifier)
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)


@tool(description=get_tool_description("property_tools", "select_candidate"))
def select_candidate(run_context: RunContext, selection: int):
    """Selects one of the numbered candidate polygons and leaves it pending confirmation.

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        selection (int): The number of the chosen candidate (1 = first).

    Returns:
        ToolResult: ``confirm_candidate`` (origin label, details, warnings) with the polygon image;
        or ``invalid_selection``, ``no_candidates_pending`` or ``error`` instructions.

    Side effects:
        On success, replaces ``candidate_properties`` with the chosen ``Feature`` and sets
        ``registration_state = "pending"``; an invalid selection leaves the session untouched.
    """
    tool_key = "select_candidate"
    log_debug(f"{tool_key}: selection={selection}")
    try:
        stored = run_context.session_state.get("candidate_properties")
        if run_context.session_state.get("registration_state") != "choosing" or not stored:
            return ToolResult(content=get_tool_result_text("property_tools", tool_key, "no_candidates_pending"))
        if not isinstance(selection, int) or not 1 <= selection <= len(stored):
            return ToolResult(content=get_tool_result_text("property_tools", tool_key, "invalid_selection", count=len(stored)))
        return _make_pending(run_context, PropertyCandidate.model_validate(stored[selection - 1]), tool_key)
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)


def _first_geojson_bytes(files: Optional[List[File]]) -> Optional[bytes]:
    """Returns the content of the first GeoJSON file attached to the conversation, or None.

    Args:
        files (List[File], optional): Files injected by the framework.

    Returns:
        bytes | None: Content of the first file whose ``format`` is ``geojson`` or ``json``.
    """
    for file in files or []:
        if getattr(file, "format", None) in ("geojson", "json") and file.content:
            return file.content
    return None


@tool(description=get_tool_description("property_tools", "register_polygon"))
def register_polygon(run_context: RunContext, geojson: Optional[str] = None, files: Optional[List[File]] = None):
    """Starts the registration of a polygon provided by the user (GeoJSON text or attached file).

    The GeoJSON comes from, in order: the ``geojson`` argument (text pasted in the message), the first GeoJSON of
    ``files`` (framework attachments) and ``session_state["uploaded_geojson"]`` (``{"name", "content"}``, written by the
    front end for the current turn when the user attaches a file). ``uploaded_geojson`` is always removed from the
    session when read, so an old upload can never be reused. Whatever the source, the content goes through the same
    safe parser and limits (``parse_geojson``).

    Args:
        run_context (RunContext): The run context holding ``session_state``.
        geojson (str, optional): GeoJSON text pasted by the user (Feature, FeatureCollection or geometry).
        files (List[File], optional): Files attached to the conversation, injected by the framework; the
            first GeoJSON file is used when ``geojson`` is not given.

    Returns:
        ToolResult: ``confirm_candidate`` with the polygon image; or ``missing_polygon``, ``invalid_polygon``,
        ``polygon_too_large`` or ``error`` instructions.

    Side effects:
        Removes ``uploaded_geojson`` from the session. On success, sets ``candidate_properties`` (the ``Feature``)
        and ``registration_state = "pending"``.
    """
    tool_key = "register_polygon"
    log_debug(f"{tool_key}: geojson={'yes' if geojson else 'no'}, files={len(files or [])}")
    try:
        uploaded = run_context.session_state.pop("uploaded_geojson", None)
        content = geojson or _first_geojson_bytes(files) or (uploaded.get("content") if isinstance(uploaded, dict) else None)
        if not content:
            return ToolResult(content=get_tool_result_text("property_tools", tool_key, "missing_polygon"))
        try:
            candidate = parse_geojson(content)
        except InvalidPolygon as error:
            log_warning(f"{tool_key}: {error.kind}: {error}")
            if error.kind == "too_large":
                return ToolResult(content=get_tool_result_text("property_tools", tool_key, "polygon_too_large", max_ha=f"{max_polygon_ha():g}"))
            if error.kind in ("file_too_large", "too_many_vertices", "too_many_features", "too_many_rings", "too_complex"):
                return ToolResult(content=get_tool_result_text("property_tools", tool_key, "polygon_too_complex"))
            return ToolResult(content=get_tool_result_text("property_tools", tool_key, "invalid_polygon"))
        return _make_pending(run_context, candidate, tool_key)
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)
