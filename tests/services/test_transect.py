"""Tests of the transect geometry (domain/services/geospatial/transect.py)."""

import pytest

from domain.services.geospatial.geometry import haversine_distance
from domain.services.geospatial.transect import (
    MAX_LENGTH_METERS,
    MIN_LENGTH_METERS,
    InvalidTransect,
    build_transect,
)

START = (-15.5, -47.5)
END = (-15.51, -47.51)


def test_build_transect_stores_a_line_and_its_metadata():
    feature = build_transect(*START, *END)
    length = haversine_distance(*START, *END)

    assert feature.feature_type == "transect"
    assert feature.geometry_type == "LineString" and feature.is_line
    assert feature.coords == [[START[1], START[0]], [END[1], END[0]]]
    assert feature.total_area == 0
    assert feature.get_metadata("origin") == "transect"
    assert feature.get_metadata("start") == f"{START[0]}, {START[1]}"
    assert feature.get_metadata("end") == f"{END[0]}, {END[1]}"
    assert feature.get_metadata("length_m") == pytest.approx(length, abs=0.1)
    assert feature.get_metadata("radius") == pytest.approx(length / 2, abs=0.1)


def test_transect_midpoint_is_between_the_endpoints():
    feature = build_transect(*START, *END)
    latitude, longitude = (float(part) for part in feature.get_metadata("center").split(","))

    assert -15.51 < latitude < -15.5
    assert -47.51 < longitude < -47.5
    assert feature.get_metadata("center")


def test_line_outside_the_length_bounds_is_rejected():
    with pytest.raises(InvalidTransect) as error:
        build_transect(-15.5, -47.5, -15.5, -47.5)

    assert error.value.length_m == pytest.approx(0)

    with pytest.raises(InvalidTransect):
        build_transect(0.0, 0.0, 0.0, 2.0)


def test_length_bounds_are_documented_in_meters():
    assert (MIN_LENGTH_METERS, MAX_LENGTH_METERS) == (100, 100_000)
