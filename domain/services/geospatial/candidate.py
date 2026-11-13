"""Adapter from a Semente ``PropertyCandidate`` to the Pasto10 ``Feature``.

The circle geometry itself comes from ``semente.property_sources.BufferSource``;
this module only maps its candidate onto the single Feature model used by
imaging, biomass, pasture, weather and the bulletin.
"""

from semente.property_sources import PropertyCandidate

from domain.schemas.feature import Feature, FeatureMetadata

BUFFER_FEATURE_TYPE = "buffer_area"


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
