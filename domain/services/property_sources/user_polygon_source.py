"""Polygons provided by the user (GeoJSON text or file).

``parse_geojson`` accepts a GeoJSON ``Feature``, ``FeatureCollection`` or bare
geometry in WGS84 lon/lat, repairs it, enforces size limits and returns a
``PropertyCandidate`` with origin ``user_polygon``. KML and shapefiles are not
supported yet (convert them to GeoJSON).

The input is hostile by default and defended in layers: (1) limits on bytes, features, rings and vertices, checked on the
raw JSON before any geometry is built, with finite lon/lat coordinates required; (2) invalid polygons with more than
``MAX_REPAIR_VERTICES`` vertices in total are refused instead of repaired (``make_valid`` can take minutes on large
self-intersecting input); (3) all shapely work runs in a child process (``polygon_guard.py``) killed after
``GUARD_TIMEOUT_SECONDS``, because GEOS cannot be interrupted from a thread.
"""

import json
import math
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Union

from domain.schemas.candidate import GeometryOrigin, PropertyCandidate

MAX_AREA_HA_ENV = "PASTO10_MAX_POLYGON_HA"
MAX_VERTICES_ENV = "PASTO10_MAX_POLYGON_VERTICES"
DEFAULT_MAX_AREA_HA = 50_000.0
DEFAULT_MAX_VERTICES = 2_000
MIN_AREA_HA = 0.01
MAX_BYTES = 1_000_000
MAX_FEATURES = 20
MAX_RINGS = 50
MAX_DEPTH = 16
MAX_REPAIR_VERTICES = 100
SIMPLIFY_METERS = 5.0
GUARD_TIMEOUT_SECONDS = 3.0
GUARD_CONCURRENCY_ENV = "PASTO10_GUARD_CONCURRENCY"
DEFAULT_GUARD_CONCURRENCY = 3
GUARD_SCRIPT = Path(__file__).with_name("polygon_guard.py")


def _guard_concurrency() -> int:
    """Maximum simultaneous guard processes (``PASTO10_GUARD_CONCURRENCY`` when a valid positive integer, else 3)."""
    try:
        value = int(os.getenv(GUARD_CONCURRENCY_ENV) or DEFAULT_GUARD_CONCURRENCY)
    except ValueError:
        return DEFAULT_GUARD_CONCURRENCY
    return value if value > 0 else DEFAULT_GUARD_CONCURRENCY


_GUARD_SLOTS = threading.BoundedSemaphore(_guard_concurrency())


class InvalidPolygon(Exception):
    """The user polygon cannot be used.

    Attributes:
        kind (str): ``invalid_json``, ``not_polygon``, ``invalid_geometry``, ``invalid_coordinates``,
            ``too_large`` (area), ``too_small``, ``too_many_vertices``, ``too_many_features``, ``too_many_rings``,
            ``too_complex`` (invalid and large, or the guard timed out) or ``file_too_large``.
    """

    def __init__(self, kind: str, message: str = ""):
        """Creates the error.

        Args:
            kind (str): Failure kind (see the class docstring).
            message (str): Description for the logs.
        """
        super().__init__(message or kind)
        self.kind = kind


def max_polygon_ha() -> float:
    """Returns the effective area limit in hectares (``PASTO10_MAX_POLYGON_HA`` when it is a valid positive number, else 50 000)."""
    try:
        value = float(os.getenv(MAX_AREA_HA_ENV) or DEFAULT_MAX_AREA_HA)
    except ValueError:
        return DEFAULT_MAX_AREA_HA
    return value if value > 0 else DEFAULT_MAX_AREA_HA


def max_polygon_vertices() -> int:
    """Returns the effective vertex limit (``PASTO10_MAX_POLYGON_VERTICES`` when a valid positive integer, else 2 000)."""
    try:
        value = int(os.getenv(MAX_VERTICES_ENV) or DEFAULT_MAX_VERTICES)
    except ValueError:
        return DEFAULT_MAX_VERTICES
    return value if value > 0 else DEFAULT_MAX_VERTICES


def _geometries(data: dict) -> list[dict]:
    """Collects the geometries of a GeoJSON Feature, FeatureCollection or geometry (at most ``MAX_FEATURES`` features)."""
    kind = data.get("type")
    if kind == "FeatureCollection":
        features = data.get("features", [])
        if not isinstance(features, list):
            return []
        if len(features) > MAX_FEATURES:
            raise InvalidPolygon("too_many_features", f"{len(features)} features > {MAX_FEATURES}")
        return [feature.get("geometry") for feature in features if isinstance(feature, dict) and feature.get("geometry")]
    if kind == "Feature":
        return [data["geometry"]] if data.get("geometry") else []
    return [data] if kind else []


def _check_position(position: list) -> None:
    """Requires a position to be finite numbers inside the lon/lat ranges (booleans, NaN and infinities are refused)."""
    if len(position) < 2 or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in position[:2]):
        raise InvalidPolygon("invalid_coordinates", "positions must be finite numbers")
    if not (-180 <= position[0] <= 180 and -90 <= position[1] <= 90):
        raise InvalidPolygon("invalid_coordinates", "coordinates must be lon/lat degrees")


def raw_vertex_count(geometries: list, limit: Optional[int] = None) -> int:
    """Scans raw GeoJSON geometries without building any geometry object, enforcing the cheap limits.

    Iterative (no recursion), with bounded nesting. Counts the positions of all geometries together, the rings of each
    polygon, and checks that every position is a pair of finite numbers within the lon/lat ranges. It stops as soon as
    the vertex ``limit`` is exceeded, so a hostile document costs almost nothing before it is rejected.

    Args:
        geometries (list): GeoJSON geometry mappings (any type).
        limit (int, optional): Vertex limit; defaults to ``max_polygon_vertices()``.

    Returns:
        int: The number of positions found (greater than ``limit`` when the limit was exceeded).

    Raises:
        InvalidPolygon: ``too_many_rings`` (more than ``MAX_RINGS`` rings in a polygon) or ``invalid_coordinates``
        (non-finite, out-of-range or malformed positions, or nesting deeper than ``MAX_DEPTH``).
    """
    limit = limit if limit is not None else max_polygon_vertices()
    total = 0
    stack = []
    for geometry in geometries:
        if not isinstance(geometry, dict):
            continue
        coordinates = geometry.get("coordinates")
        kind = geometry.get("type")
        if kind == "Polygon" and isinstance(coordinates, list):
            if len(coordinates) > MAX_RINGS:
                raise InvalidPolygon("too_many_rings", f"{len(coordinates)} rings > {MAX_RINGS}")
        elif kind == "MultiPolygon" and isinstance(coordinates, list):
            for polygon in coordinates:
                if isinstance(polygon, list) and len(polygon) > MAX_RINGS:
                    raise InvalidPolygon("too_many_rings", f"{len(polygon)} rings > {MAX_RINGS}")
        stack.append((coordinates, 0))
    while stack:
        item, depth = stack.pop()
        if not isinstance(item, list):
            continue
        if depth > MAX_DEPTH:
            raise InvalidPolygon("invalid_coordinates", "coordinates nested too deeply")
        if item and not isinstance(item[0], list):
            _check_position(item)
            total += 1
            if total > limit:
                return total
        else:
            stack.extend((child, depth + 1) for child in item)
    return total


def _reject_constant(name: str):
    """``json.loads`` hook: NaN and Infinity are not valid JSON numbers for coordinates."""
    raise ValueError(f"non-finite number {name}")


def _run_guard(geometries: list, limit_ha: float) -> dict:
    """Runs ``polygon_guard.py`` in a child process with a hard timeout and returns its answer.

    Args:
        geometries (list): Raw GeoJSON geometries (already counted and range-checked).
        limit_ha (float): Area limit in hectares.

    Returns:
        dict: The guard's ``{"ok": ...}`` answer; ``too_complex`` when it did not finish within ``GUARD_TIMEOUT_SECONDS``
        (the child is killed), ``invalid_geometry`` when it crashed or printed invalid JSON. At most ``PASTO10_GUARD_CONCURRENCY``
        (default 3) guards run at once; others wait for a slot. The child runs with ``-I`` so the package directory is
        not put on its ``sys.path``.
    """
    request = {
        "geometries": geometries, "max_area_ha": limit_ha, "min_area_ha": MIN_AREA_HA, "simplify_m": SIMPLIFY_METERS,
        "max_repair_vertices": MAX_REPAIR_VERTICES,
    }
    try:
        with _GUARD_SLOTS:
            done = subprocess.run(
                [sys.executable, "-I", str(GUARD_SCRIPT)], input=json.dumps(request), capture_output=True, text=True,
                timeout=GUARD_TIMEOUT_SECONDS,
            )
        return json.loads(done.stdout, parse_constant=_reject_constant)
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "too_complex"}
    except (ValueError, TypeError, OSError):
        return {"ok": False, "kind": "invalid_geometry"}


def parse_geojson(content: Union[str, bytes], max_area_ha: Optional[float] = None) -> PropertyCandidate:
    """Turns GeoJSON into a user-polygon candidate.

    Defends the app in layers (see the module docstring): the document may not exceed ``MAX_BYTES`` (UTF-8 bytes), at
    most ``MAX_FEATURES`` features and ``MAX_RINGS`` rings per polygon, and ``max_polygon_vertices()`` positions (all
    features together), all checked on the raw JSON; coordinates must be finite lon/lat numbers; an invalid polygon with
    more than ``MAX_REPAIR_VERTICES`` vertices is refused; and the shapely work runs in a child process killed after
    ``GUARD_TIMEOUT_SECONDS``. With several polygonal features the largest is used; invalid small rings are repaired and
    the geometry is simplified to about 5 m.

    Args:
        content (str | bytes): GeoJSON text (Feature, FeatureCollection or geometry).
        max_area_ha (float, optional): Area limit in hectares; defaults to ``max_polygon_ha()``.

    Returns:
        PropertyCandidate: Candidate with origin ``user_polygon``, source ``user`` and no distance.

    Raises:
        InvalidPolygon: If the content is too large or complex, is not valid JSON/GeoJSON, has no polygon, has
        non-finite or out-of-range coordinates, or exceeds the area or vertex limits (``kind`` tells which).
    """
    limit = max_area_ha or max_polygon_ha()
    size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
    if size > MAX_BYTES:
        raise InvalidPolygon("file_too_large", f"{size} bytes > {MAX_BYTES}")
    try:
        data = json.loads(content, parse_constant=_reject_constant)
        geometries = _geometries(data) if isinstance(data, dict) else []
        vertices = raw_vertex_count(geometries)
    except InvalidPolygon:
        raise
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidPolygon("invalid_json", str(error)) from error
    if vertices > max_polygon_vertices():
        raise InvalidPolygon("too_many_vertices", f"more than {max_polygon_vertices()} vertices")
    if not any(isinstance(geometry, dict) and geometry.get("type") in ("Polygon", "MultiPolygon") for geometry in geometries):
        raise InvalidPolygon("not_polygon" if geometries else "invalid_json", "no polygon found")
    answer = _run_guard(geometries, limit)
    if not answer.get("ok"):
        raise InvalidPolygon(answer.get("kind", "invalid_geometry"), f"guard: {answer.get('kind')}")
    return PropertyCandidate(
        candidate_id="user-polygon", origin=GeometryOrigin.USER_POLYGON, source="user", geometry=answer["geometry"],
        area_ha=round(answer["area_ha"], 2), distance_m=None, warnings=[],
    )
