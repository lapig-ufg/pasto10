"""Dependency-free polygon helpers (shapely and numpy only, no ``domain`` imports).

Kept apart from ``geometry_utils.py`` so the standalone guard script (``polygon_guard.py``), which runs in a separate
process with a hard timeout, can load it without importing the whole app. Areas are computed in a Lambert azimuthal
equal-area projection centered on the polygon, implemented here directly, so no ``pyproj`` is needed.
"""

import math
from collections.abc import Iterable

import numpy as np
import shapely
from shapely import make_valid as _make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

EARTH_RADIUS_METERS = 6_371_008.8
_SQUARE_METERS_PER_HECTARE = 10_000.0
_METERS_PER_DEGREE = 111_320.0


class GeometryTooComplex(ValueError):
    """The geometry is too complex to repair within a safe time."""


def polygonal_part(geometry: BaseGeometry, max_repair_vertices: int | None = None) -> BaseGeometry | None:
    """Keeps only the polygonal part of a geometry, repaired.

    Args:
        geometry (BaseGeometry): Any shapely geometry (a collection after ``make_valid`` may mix types).
        max_repair_vertices (int, optional): ``make_valid`` can take very long on large self-intersecting
            input, so when the geometry is invalid and has more vertices than this, it is refused instead
            of repaired (None repairs any size).

    Returns:
        BaseGeometry | None: A valid Polygon or MultiPolygon, or None when no area is left.

    Raises:
        GeometryTooComplex: If the geometry is invalid and has more than ``max_repair_vertices`` vertices.
    """
    if not geometry.is_valid and max_repair_vertices is not None and shapely.get_num_coordinates(geometry) > max_repair_vertices:
        raise GeometryTooComplex(f"invalid geometry with more than {max_repair_vertices} vertices")
    fixed = geometry if geometry.is_valid else _make_valid(geometry)
    parts: list[Polygon] = []
    stack = [fixed]
    while stack:
        item = stack.pop()
        if isinstance(item, Polygon):
            if not item.is_empty and item.area > 0:
                parts.append(item)
        elif hasattr(item, "geoms"):
            stack.extend(item.geoms)
    if not parts:
        return None
    merged = unary_union(parts)
    return merged if isinstance(merged, (Polygon, MultiPolygon)) else None


def area_hectares(geometry: BaseGeometry) -> float:
    """Computes the area of a lon/lat geometry in hectares (equal-area projection around its centroid).

    Args:
        geometry (BaseGeometry): Polygon or MultiPolygon in lon/lat degrees.

    Returns:
        float: Area in hectares.
    """
    center = geometry.centroid
    lon0, lat0 = math.radians(center.x), math.radians(center.y)

    def project(coords):
        lon, lat = np.radians(coords[:, 0]), np.radians(coords[:, 1])
        k = np.sqrt(2 / (1 + math.sin(lat0) * np.sin(lat) + math.cos(lat0) * np.cos(lat) * np.cos(lon - lon0)))
        return np.column_stack((
            EARTH_RADIUS_METERS * k * np.cos(lat) * np.sin(lon - lon0),
            EARTH_RADIUS_METERS * k * (math.cos(lat0) * np.sin(lat) - math.sin(lat0) * np.cos(lat) * np.cos(lon - lon0)),
        ))

    return shapely.transform(geometry, project).area / _SQUARE_METERS_PER_HECTARE


def simplify_meters(geometry: BaseGeometry, tolerance_m: float) -> BaseGeometry:
    """Simplifies a lon/lat geometry with a tolerance in meters, keeping it valid.

    Args:
        geometry (BaseGeometry): Polygon or MultiPolygon in lon/lat degrees.
        tolerance_m (float): Douglas-Peucker tolerance in meters.

    Returns:
        BaseGeometry: The simplified geometry (the original when simplification would empty it).
    """
    simplified = geometry.simplify(tolerance_m / _METERS_PER_DEGREE, preserve_topology=True)
    return simplified if not simplified.is_empty else geometry


def vertex_count(geometry: BaseGeometry) -> int:
    """Counts the vertices of the rings of a Polygon or MultiPolygon.

    Args:
        geometry (BaseGeometry): Polygon or MultiPolygon.

    Returns:
        int: Vertices of the exteriors and interiors (closing vertices included).
    """
    polygons: Iterable[Polygon] = geometry.geoms if isinstance(geometry, MultiPolygon) else [geometry]
    return sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors) for p in polygons)


