"""Adapter from a Semente ``PropertyCandidate`` to the Pasto10 ``Feature``.

The circle geometry comes from ``semente.property_sources.BufferSource``; a
boundary provided by the user (a geospatial file or GeoJSON) is also a Semente
candidate. This module maps either candidate onto the single Feature model used
by imaging, biomass, pasture, weather and the bulletin.
"""

from semente.property_sources import PropertyCandidate

from domain.schemas.feature import Feature, FeatureMetadata

BUFFER_FEATURE_TYPE = "buffer_area"
RURAL_PROPERTY_FEATURE_TYPE = "rural_property"
USER_POLYGON_FEATURE_TYPE = "user_polygon"


def candidate_to_feature(candidate: PropertyCandidate, latitude: float, longitude: float) -> Feature:
    """Converts a buffer candidate into a ``buffer_area`` Feature.

    The candidate does not keep the searched point, so the caller passes it
    to fill the ``center`` metadata exactly as the tools and renderers read it.

    Args:
        candidate (PropertyCandidate): Candidate produced by ``BufferSource``;
            ``attributes["radius_m"]`` holds the radius in meters.
        latitude (float): Latitude the candidate was searched at.
        longitude (float): Longitude the candidate was searched at.

    Returns:
        Feature: A new feature (fresh ``feature_id``, not stored anywhere) with
        the MultiPolygon coordinates, ``total_area`` in hectares and the
        ``radius`` (unit ``m``) and ``center`` (``"lat, lon"``) metadata.

    Raises:
        KeyError: If the candidate has no ``radius_m`` attribute.
        ValueError: If the geometry is not valid Feature coordinates.
    """
    return Feature(
        feature_id=Feature.generate_id(),
        coords=candidate.geometry["coordinates"],
        metadata=[
            FeatureMetadata(key="radius", value=candidate.attributes["radius_m"], unit="m"),
            FeatureMetadata(key="center", value=f"{latitude}, {longitude}"),
        ],
        total_area=candidate.area_ha,
        feature_type=BUFFER_FEATURE_TYPE,
    )


def polygon_candidate_to_feature(candidate: PropertyCandidate) -> Feature:
    """Converts a polygon candidate into a ``rural_property`` (or ``user_polygon``) Feature.

    The center and the enclosing radius are computed from the geometry, because
    the map renderers and the summaries read them from the metadata. A geofile
    candidate carries ``attributes["feature_type"] = "rural_property"``; any
    other user polygon is kept as ``user_polygon``.

    Args:
        candidate (PropertyCandidate): A polygon candidate (GeoJSON Polygon or
            MultiPolygon, lon/lat).

    Returns:
        Feature: A new feature (fresh ``feature_id``) with the MultiPolygon
        coordinates, the area, and the metadata ``origin``, ``source``,
        ``center`` (``"lat, lon"``), ``radius`` (enclosing circle, in meters),
        the candidate name (``source_name``) and its scalar attributes.

    Raises:
        ValueError: If the geometry has no polygon part.
    """
    from domain.services.geospatial import geometry_utils as geo

    shape = geo.polygonal_part(geo.from_geojson_geometry(candidate.geometry))
    if shape is None:
        raise ValueError("Candidate geometry has no polygon.")
    latitude, longitude = geo.center_of(shape)
    metadata = [
        FeatureMetadata(key="origin", value=candidate.origin.value),
        FeatureMetadata(key="source", value=candidate.source),
        FeatureMetadata(key="center", value=f"{latitude:.6f}, {longitude:.6f}"),
        FeatureMetadata(key="radius", value=round(geo.enclosing_radius_m(shape, (latitude, longitude)), 1), unit="m"),
    ]
    if candidate.name:
        metadata.append(FeatureMetadata(key="source_name", value=candidate.name))
    if candidate.source_date:
        metadata.append(FeatureMetadata(key="source_date", value=candidate.source_date))
    present = {entry.key for entry in metadata}
    for key, value in (candidate.attributes or {}).items():
        if key in present or isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        metadata.append(FeatureMetadata(key=key, value=value))
    feature_type = candidate.attributes.get("feature_type") or (
        RURAL_PROPERTY_FEATURE_TYPE if candidate.origin.value == RURAL_PROPERTY_FEATURE_TYPE else USER_POLYGON_FEATURE_TYPE
    )
    return Feature(
        feature_id=Feature.generate_id(),
        coords=geo.to_multipolygon_coords(shape),
        metadata=metadata,
        total_area=round(candidate.area_ha, 2),
        feature_type=str(feature_type),
    )
