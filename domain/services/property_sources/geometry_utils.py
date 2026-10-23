"""Polygon helpers shared by the property sources (shapely, no projection library).

Areas are computed in a Lambert azimuthal equal-area projection centered on the
polygon, implemented here directly, so no ``pyproj`` is needed. Distances use
the haversine formula of ``domain/services/geospatial/geometry.py``.
"""

from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points

from domain.services.geospatial.geometry import haversine_distance
from domain.services.property_sources.geometry_core import (  # noqa: F401  (re-exported)
    EARTH_RADIUS_METERS,
    GeometryTooComplex,
    area_hectares,
    polygonal_part,
    simplify_meters,
    vertex_count,
)



def center_of(geometry: BaseGeometry) -> tuple[float, float]:
    """Returns a (latitude, longitude) inside the geometry: the centroid, or a representative point when it falls outside.

    Args:
        geometry (BaseGeometry): Polygon or MultiPolygon in lon/lat degrees.

    Returns:
        tuple[float, float]: (latitude, longitude) in decimal degrees.
    """
    point = geometry.centroid
    if not geometry.contains(point):
        point = geometry.representative_point()
    return point.y, point.x


def enclosing_radius_m(geometry: BaseGeometry, center: tuple[float, float]) -> float:
    """Returns the largest distance in meters from ``center`` to a vertex (the radius of the enclosing circle).

    Args:
        geometry (BaseGeometry): Polygon or MultiPolygon in lon/lat degrees.
        center (tuple[float, float]): (latitude, longitude).

    Returns:
        float: Distance in meters.
    """
    polygons = geometry.geoms if isinstance(geometry, MultiPolygon) else [geometry]
    return max(
        haversine_distance(center[0], center[1], lat, lon) for p in polygons for lon, lat in p.exterior.coords
    )


def distance_to_point_m(geometry: BaseGeometry, latitude: float, longitude: float) -> float:
    """Returns the distance in meters from a point to a geometry (0 when the point is inside; a point in a hole is measured to the hole edge).

    Args:
        geometry (BaseGeometry): Polygon or MultiPolygon in lon/lat degrees.
        latitude (float): Point latitude.
        longitude (float): Point longitude.

    Returns:
        float: Distance in meters.
    """
    point = Point(longitude, latitude)
    if geometry.contains(point):
        return 0.0
    nearest = nearest_points(geometry, point)[0]
    return haversine_distance(latitude, longitude, nearest.y, nearest.x)


def to_multipolygon_coords(geometry: BaseGeometry) -> list:
    """Converts a Polygon or MultiPolygon to GeoJSON MultiPolygon coordinates (``Feature.coords`` nesting).

    Args:
        geometry (BaseGeometry): Polygon or MultiPolygon in lon/lat degrees.

    Returns:
        list: Polygons -> rings -> ``[lon, lat]`` positions.
    """
    if isinstance(geometry, Polygon):
        return [mapping(geometry)["coordinates"]]
    return mapping(geometry)["coordinates"]


def from_geojson_geometry(geojson: dict) -> BaseGeometry:
    """Builds a shapely geometry from a GeoJSON geometry mapping.

    Args:
        geojson (dict): GeoJSON geometry (``type`` and ``coordinates``).

    Returns:
        BaseGeometry: The shapely geometry (not repaired; see ``polygonal_part``).

    Raises:
        ValueError: If the mapping is not a valid GeoJSON geometry.
    """
    return shape(geojson)
