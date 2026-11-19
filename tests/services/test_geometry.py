"""Tests of the buffer geometry helpers (domain/services/geospatial/geometry.py)."""

import math

import pytest

from domain.services.geospatial.geometry import build_buffered_area, haversine_distance, is_valid_coordinate

LOCATIONS = [
    pytest.param(0.0, 0.0, id="equator"),
    pytest.param(60.0, 24.9, id="60N"),
    pytest.param(-33.9, 18.4, id="southern-hemisphere"),
]


def _ring(feature):
    return feature.coords[0][0]


@pytest.mark.parametrize("latitude, longitude", LOCATIONS)
@pytest.mark.parametrize("radius", [50, 200, 1000])
def test_vertices_lie_at_radius_from_center(latitude, longitude, radius):
    for lon, lat in _ring(build_buffered_area(latitude, longitude, radius)):
        assert haversine_distance(latitude, longitude, lat, lon) == pytest.approx(radius, rel=0.01)


@pytest.mark.parametrize("latitude, longitude", LOCATIONS)
def test_ring_is_closed_with_expected_vertex_count(latitude, longitude):
    ring = _ring(build_buffered_area(latitude, longitude, 200))

    assert ring[0] == ring[-1]
    assert len(ring) == 64 + 1


@pytest.mark.parametrize("latitude, longitude", LOCATIONS)
@pytest.mark.parametrize("radius", [50, 200, 1000])
def test_buffered_area_feature(latitude, longitude, radius):
    feature = build_buffered_area(latitude, longitude, radius)

    assert feature.feature_type == "buffer_area"
    assert feature.total_area == pytest.approx(math.pi * radius ** 2 / 10_000, rel=0.01)
    assert feature.get_metadata("radius") == radius
    assert feature.get_metadata("center") == f"{latitude}, {longitude}"
    assert feature.feature_id


def test_ring_crossing_antimeridian_keeps_longitudes_in_range():
    ring = _ring(build_buffered_area(10.0, 179.999, 1000))

    assert all(-180.0 <= lon < 180.0 for lon, _ in ring)
    for lon, lat in ring:
        assert haversine_distance(10.0, 179.999, lat, lon) == pytest.approx(1000, rel=0.01)


@pytest.mark.parametrize("radius", [0, -5, float("nan")])
def test_invalid_radius_is_rejected(radius):
    with pytest.raises(ValueError):
        build_buffered_area(0.0, 0.0, radius)


@pytest.mark.parametrize("latitude, longitude", [(86, 0), (0, 181), (float("nan"), 0), ("x", 0)])
def test_invalid_coordinate_is_rejected(latitude, longitude):
    assert not is_valid_coordinate(latitude, longitude)
    with pytest.raises(ValueError):
        build_buffered_area(latitude, longitude, 100)
