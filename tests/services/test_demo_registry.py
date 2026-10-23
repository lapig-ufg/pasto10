"""Tests of the synthetic demo registry (data file, generator and DemoRegistrySource)."""

import json
import re
import sys
from pathlib import Path

import pytest
from shapely.geometry import shape

from domain.schemas.candidate import GeometryOrigin
from domain.services.property_sources import geometry_utils as geo
from domain.services.property_sources.demo_registry_source import DATA_FILE, SOURCE_NAME, DemoRegistrySource

ROOT = Path(__file__).resolve().parents[2]
PROPERTIES = json.loads((ROOT / "domain" / "data" / "test_properties.json").read_text(encoding="utf-8"))
REAL = [item for item in PROPERTIES if not item["kind"].startswith("control")]
CONTROLS = [item for item in PROPERTIES if item["kind"].startswith("control")]
FEATURES = json.loads(DATA_FILE.read_text(encoding="utf-8"))["features"]
FORBIDDEN_KEYS = ("owner", "name_owner", "cpf", "email", "phone", "address")


def test_the_data_file_is_small_and_has_registry_style_attributes_without_personal_data():
    assert DATA_FILE.stat().st_size < 120_000 and len(FEATURES) > 30
    ids = [item["properties"]["registry_id"] for item in FEATURES]
    assert len(set(ids)) == len(ids) and all(re.fullmatch(r"DEMO-[A-Z]{3}-\d{4}", registry_id) for registry_id in ids)
    for item in FEATURES:
        properties = item["properties"]
        assert properties["status"] == "active" and properties["region"].startswith("Demo region")
        assert not any(key in properties for key in FORBIDDEN_KEYS)
        assert 20 <= properties["area_ha"] <= 400 and item["geometry"]["type"] == "Polygon"


def test_parcels_are_irregular_valid_polygons():
    for item in FEATURES:
        polygon = shape(item["geometry"])
        assert polygon.is_valid and len(polygon.exterior.coords) >= 7
        assert polygon.area / polygon.minimum_rotated_rectangle.area < 0.97
        assert geo.area_hectares(polygon) == pytest.approx(item["properties"]["area_ha"], rel=0.02)


@pytest.mark.parametrize("item", REAL, ids=[item["id"] for item in REAL])
def test_every_test_property_has_a_demo_parcel_containing_its_point_and_neighbors(item):
    candidates = DemoRegistrySource().find_by_point(item["latitude"], item["longitude"])

    assert 3 <= len(candidates) <= 4
    assert candidates[0].distance_m == 0 and sum(1 for c in candidates if c.distance_m == 0) == 1
    distances = [c.distance_m for c in candidates[1:]]
    assert distances == sorted(distances) and all(d > 0 for d in distances)
    assert all(c.origin is GeometryOrigin.REGISTERED_BOUNDARY and c.warnings == ["synthetic_demo"] for c in candidates)


@pytest.mark.parametrize("item", CONTROLS, ids=[item["id"] for item in CONTROLS])
def test_negative_controls_have_no_parcel(item):
    assert DemoRegistrySource().find_by_point(item["latitude"], item["longitude"]) == []


def test_candidates_are_labeled_synthetic_with_registry_attributes():
    [first, *_] = DemoRegistrySource().find_by_point(-15.5, -47.5)

    assert first.source == SOURCE_NAME and "synthetic" in first.source and first.candidate_id == first.name
    assert {(a.key, a.value) for a in first.attributes} >= {("registry_id", first.candidate_id), ("registry_status", "active")}
    assert first.license and "no real property boundaries" in first.license and first.type_key == "demo_registry"


def test_find_by_id_is_case_insensitive_and_unknown_ids_return_none():
    source = DemoRegistrySource()
    expected = FEATURES[0]["properties"]["registry_id"]

    found = source.find_by_id(f"  {expected.lower()} ")

    assert found.candidate_id == expected and found.distance_m is None
    assert source.find_by_id("DEMO-XXX-9999") is None and source.find_by_id("") is None


def test_search_radius_limits_the_neighbors():
    near = DemoRegistrySource().find_by_point(-15.5, -47.5, search_radius_m=1)

    assert len(near) == 1 and near[0].distance_m == 0


def test_to_feature_marks_the_demo_type_and_feeds_the_rest_of_the_app():
    candidate = DemoRegistrySource().find_by_point(-15.5, -47.5)[0]

    feature = candidate.to_feature()

    assert feature.feature_type == "demo_registry" and feature.get_metadata("source") == SOURCE_NAME
    assert feature.get_metadata("registry_id") == candidate.candidate_id and feature.get_metadata("radius") > 100
    assert feature.total_area == pytest.approx(candidate.area_ha, abs=0.01)


def test_the_generator_is_deterministic_and_reproduces_the_shipped_file(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT))
    from tools import make_demo_registry

    monkeypatch.setattr(make_demo_registry, "OUT_FILE", tmp_path / "demo.geojson")
    make_demo_registry.main()

    assert (tmp_path / "demo.geojson").read_text(encoding="utf-8") == DATA_FILE.read_text(encoding="utf-8")


def test_parcels_of_the_same_property_never_overlap_or_touch():
    from collections import defaultdict

    groups = defaultdict(list)
    for item in FEATURES:
        groups[item["properties"]["property_id"]].append(shape(item["geometry"]))

    for property_id, polygons in groups.items():
        for index, first in enumerate(polygons):
            for second in polygons[index + 1:]:
                assert first.intersection(second).area == pytest.approx(0, abs=1e-12), property_id
                assert not first.touches(second) and first.distance(second) > 0, property_id
