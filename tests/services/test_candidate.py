"""Equivalence of the Semente-based buffer Feature with the former in-house implementation."""

import math

import pytest
from semente.property_sources import BufferSource

from domain.schemas.feature import Feature, FeatureMetadata
from domain.services.geospatial.candidate import candidate_to_feature
from domain.services.geospatial.geometry import build_buffered_area

_EARTH_RADIUS_METERS = 6_371_008.8


def _former_feature(latitude, longitude, radius):
    """The pre-Semente ``build_buffered_area``, kept here as the reference."""
    ring = []
    for i in range(64):
        theta = math.radians(360.0 * i / 64)
        phi1, lambda1, delta = math.radians(latitude), math.radians(longitude), radius / _EARTH_RADIUS_METERS
        sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
        lambda2 = lambda1 + math.atan2(
            math.sin(theta) * math.sin(delta) * math.cos(phi1), math.cos(delta) - math.sin(phi1) * sin_phi2
        )
        ring.append([(math.degrees(lambda2) + 180.0) % 360.0 - 180.0, math.degrees(math.asin(sin_phi2))])
    ring.append(list(ring[0]))
    return Feature(
        feature_id="x",
        coords=[[ring]],
        metadata=[
            FeatureMetadata(key="radius", value=radius, unit="m"),
            FeatureMetadata(key="center", value=f"{latitude}, {longitude}"),
        ],
        total_area=round(math.pi * radius ** 2 / 10_000.0, 2),
        feature_type="buffer_area",
    )


POINTS = [(0.0, 0.0), (-15.5, -47.5), (47.9, 106.9), (-33.9, 18.4), (60.0, 24.9), (10.0, 179.999), (-84.9, -179.5), (85, 180)]
RADII = [50, 300, 500, 750.5, 1000]


@pytest.mark.parametrize("latitude, longitude", POINTS)
@pytest.mark.parametrize("radius", RADII)
def test_new_feature_matches_the_former_one(latitude, longitude, radius):
    new = build_buffered_area(latitude, longitude, radius)
    old = _former_feature(latitude, longitude, radius)

    assert new.model_dump(exclude={"feature_id"}) == old.model_dump(exclude={"feature_id"})
    assert new.feature_id and new.feature_id != old.feature_id


def test_candidate_to_feature_uses_the_given_center_and_candidate_attributes():
    candidate = BufferSource(radius_m=200).find_by_point(-15.5, -47.5)[0]

    feature = candidate_to_feature(candidate, -15.5, -47.5)

    assert feature.feature_type == "buffer_area"
    assert feature.get_metadata("radius") == 200
    assert feature.get_metadata("center") == "-15.5, -47.5"
    assert feature.total_area == candidate.area_ha
    assert feature.coords == candidate.geometry["coordinates"]


def test_candidate_without_radius_raises():
    candidate = BufferSource().find_by_point(0.0, 0.0)[0].model_copy(update={"attributes": {}})

    with pytest.raises(KeyError):
        candidate_to_feature(candidate, 0.0, 0.0)
