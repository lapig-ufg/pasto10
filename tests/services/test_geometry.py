"""Tests of the pure-Python buffer geometry (domain/services/geospatial/geometry.py)."""

import math

import pytest

from domain.services.geospatial.geometry import (
    BUFFER_VERTICES,
    build_buffered_area,
    create_buffer_polygon,
    haversine_distance,
)

LOCATIONS = [
    pytest.param(0.0, 0.0, id="equator"),
    pytest.param(60.0, 24.9, id="60N"),
    pytest.param(-33.9, 18.4, id="southern-hemisphere"),
]


@pytest.mark.parametrize("latitude, longitude", LOCATIONS)
@pytest.mark.parametrize("radius", [50, 200, 1000])
def test_vertices_lie_at_radius_from_center(latitude, longitude, radius):
    ring = create_buffer_polygon(latitude, longitude, radius)[0][0]

    for lon, lat in ring:
        assert haversine_distance(latitude, longitude, lat, lon) == pytest.approx(radius, rel=0.01)


@pytest.mark.parametrize("latitude, longitude", LOCATIONS)
def test_ring_is_closed_with_expected_vertex_count(latitude, longitude):
    ring = create_buffer_polygon(latitude, longitude, 200)[0][0]

    assert ring[0] == ring[-1]
    assert len(ring) == BUFFER_VERTICES + 1


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
    ring = create_buffer_polygon(10.0, 179.999, 1000)[0][0]

    assert all(-180.0 <= lon < 180.0 for lon, _ in ring)
    for lon, lat in ring:
        assert haversine_distance(10.0, 179.999, lat, lon) == pytest.approx(1000, rel=0.01)


def test_non_positive_radius_is_rejected():
    with pytest.raises(ValueError):
        create_buffer_polygon(0.0, 0.0, 0)
