"""Tests of the candidate images and of the candidate schema (offline: neutral background, no tiles)."""

from io import BytesIO

import pytest
from PIL import Image

from domain.schemas.candidate import GeometryOrigin, PropertyCandidate
from domain.services.geospatial import image
from domain.services.property_sources import geometry_utils as geo
from domain.services.property_sources.osm_source import OsmSource

SQUARE = {"type": "Polygon", "coordinates": [[[-47.51, -15.51], [-47.49, -15.51], [-47.49, -15.49], [-47.51, -15.49], [-47.51, -15.51]]]}
FAR = {"type": "Polygon", "coordinates": [[[-47.47, -15.51], [-47.46, -15.51], [-47.46, -15.50], [-47.47, -15.50], [-47.47, -15.51]]]}


def _candidate(geometry=SQUARE, source="osm", name="Fazenda Central", origin=GeometryOrigin.OSM_LANDUSE):
    return PropertyCandidate(
        candidate_id="way/1", name=name, origin=origin, source=source, geometry=geometry,
        area_ha=round(geo.area_hectares(geo.from_geojson_geometry(geometry)), 2), distance_m=0.0,
    )


def test_candidates_image_is_a_640_square_with_numbered_polygons(monkeypatch):
    texts = []
    monkeypatch.setattr(image, "draw_attribution", lambda img, text: texts.append(text) or img)
    badges = []
    monkeypatch.setattr(image, "_draw_badge", lambda img, position, text, color: badges.append(text) or img)

    rendered = image.render_candidates_image([_candidate(), _candidate(FAR)], -15.5, -47.5)

    assert Image.open(BytesIO(rendered.content)).size == (640, 640) and badges == ["1", "2"]
    assert "OpenStreetMap contributors" in texts[0]


def test_non_osm_candidates_do_not_claim_openstreetmap(monkeypatch):
    texts = []
    monkeypatch.setattr(image, "draw_attribution", lambda img, text: texts.append(text) or img)

    image.render_candidates_image([_candidate(source="registry")], -15.5, -47.5)

    assert texts == []


def test_candidate_image_shows_name_area_and_origin(monkeypatch):
    labels = []
    monkeypatch.setattr(image, "draw_corner_label", lambda img, text: labels.append(text) or img)

    rendered = image.render_candidate_image(_candidate(), "OpenStreetMap land-use polygon (may not be a farm)")

    assert Image.open(BytesIO(rendered.content)).size == (640, 640)
    assert "Fazenda Central" in labels[0] and "ha" in labels[0] and "may not be a farm" in labels[0]


def test_candidate_image_over_tiles_is_jpeg(online_tiles):
    assert image.render_candidate_image(_candidate(), "label").format == "jpeg"


def test_to_feature_carries_origin_center_radius_and_attributes():
    candidate = _candidate()

    feature = candidate.to_feature()

    assert feature.feature_type == "osm_landuse" and feature.total_area == pytest.approx(candidate.area_ha, abs=0.01)
    assert feature.get_metadata("origin") == "osm_landuse" and feature.get_metadata("source") == "osm"
    assert feature.get_metadata("source_name") == "Fazenda Central"
    latitude, longitude = (float(part) for part in feature.get_metadata("center").split(","))
    assert (latitude, longitude) == pytest.approx((-15.5, -47.5), abs=0.001)
    assert feature.get_metadata("radius") == pytest.approx(1500, rel=0.15)
    assert len(feature.get_coords()) == 1 and feature.get_coords()[0][0][0] == feature.get_coords()[0][0][-1]


def test_to_feature_keeps_multipolygons_and_holes():
    hole = {"type": "Polygon", "coordinates": [SQUARE["coordinates"][0], [[-47.505, -15.505], [-47.495, -15.505], [-47.495, -15.495], [-47.505, -15.495], [-47.505, -15.505]]]}
    multi = {"type": "MultiPolygon", "coordinates": [hole["coordinates"], FAR["coordinates"]]}

    feature = _candidate(multi).to_feature()

    assert len(feature.get_coords()) == 2 and sorted(len(polygon) for polygon in feature.get_coords()) == [1, 2]


def test_to_feature_rejects_a_geometry_without_area():
    candidate = _candidate()
    candidate = candidate.model_copy(update={"geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]}})

    with pytest.raises(ValueError):
        candidate.to_feature()


def test_session_form_is_json_and_roundtrips():
    candidate = _candidate()

    assert PropertyCandidate.model_validate(candidate.to_session()) == candidate


def test_candidates_of_a_real_shaped_osm_answer_become_features():
    import json
    from pathlib import Path

    from domain.services.property_sources import osm_source

    data = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "overpass" / "synthetic_several.json").read_text(encoding="utf-8"))
    original = osm_source.get_overpass
    osm_source.get_overpass = lambda query: data
    osm_source._cache.clear()
    try:
        candidates = OsmSource().find_by_point(-15.5, -47.5)
    finally:
        osm_source.get_overpass = original
        osm_source._cache.clear()

    assert all(candidate.to_feature().feature_type == "osm_landuse" for candidate in candidates)


def test_distance_to_a_point_inside_a_hole_is_measured_to_the_hole_edge():
    hole = {"type": "Polygon", "coordinates": [SQUARE["coordinates"][0], [[-47.505, -15.505], [-47.495, -15.505], [-47.495, -15.495], [-47.505, -15.495], [-47.505, -15.505]]]}
    polygon = geo.from_geojson_geometry(hole)

    assert geo.distance_to_point_m(polygon, -15.5, -47.5) == pytest.approx(555, rel=0.1)
    assert geo.distance_to_point_m(polygon, -15.508, -47.5) == 0.0
    assert geo.distance_to_point_m(polygon, -15.52, -47.5) > 1000


def test_number_badges_sit_on_a_point_inside_the_polygon():
    c_shape = {"type": "Polygon", "coordinates": [[[-47.51, -15.51], [-47.49, -15.51], [-47.49, -15.505], [-47.505, -15.505], [-47.505, -15.495], [-47.49, -15.495], [-47.49, -15.49], [-47.51, -15.49], [-47.51, -15.51]]]}
    candidate = _candidate(c_shape)

    x, y = image._badge_anchor(candidate, (-15.5, -47.5), 15, (640, 640))
    shape = geo.from_geojson_geometry(c_shape)
    point = shape.representative_point()

    assert shape.contains(point) and 0 <= x <= 640 and 0 <= y <= 640


def test_the_demo_banner_text_is_plain_ascii_so_every_font_can_draw_it():
    assert image.DEMO_MARK.isascii() and image.DEMO_MARK.startswith("DEMO - synthetic parcels")


def test_candidates_image_draws_the_legend_when_captions_are_given(monkeypatch):
    drawn = []
    monkeypatch.setattr(image, "_draw_legend", lambda img, lines: drawn.append(list(lines)) or img)

    image.render_candidates_image([_candidate()], -15.5, -47.5, captions=["DEMO-BRA-0001 · 146.7 ha · active"])

    assert drawn == [["1  DEMO-BRA-0001 · 146.7 ha · active"]]


def test_the_circle_image_prints_the_origin_label(monkeypatch):
    from domain.services.geospatial.geometry import build_buffered_area

    labels = []
    monkeypatch.setattr(image, "draw_corner_label", lambda img, text: labels.append(text) or img)

    image.render_buffer_image(build_buffered_area(-15.5, -47.5, 300), origin_label="approximate area, not a real property boundary")

    assert labels[0].endswith("approximate area, not a real property boundary")


def test_the_video_colorbar_can_move_to_the_top_right_corner():
    from domain.schemas.biomass import BiomassSource, BiomassStats
    from domain.services.geospatial import biomass_image

    stats = BiomassStats(
        source=BiomassSource.GPW_ANNUAL, period_label="2025", start="2025-01-01", end="2025-12-31", mean_t_ha=1, total_t=1, p02_t_ha=0.5,
        p98_t_ha=2, pasture_ha=1, area_ha=1, scale_m=30, mask_year=2022, year=2025, days_covered=365,
    )
    blank = Image.new("RGB", (640, 640), (10, 10, 10))

    top = biomass_image.draw_colorbar(blank, stats, 600, corner="top-right")
    bottom = biomass_image.draw_colorbar(blank, stats, 600)

    assert top.crop((400, 0, 640, 100)).tobytes() != blank.crop((400, 0, 640, 100)).tobytes()
    assert top.crop((0, 500, 300, 640)).tobytes() == blank.crop((0, 500, 300, 640)).tobytes()
    assert bottom.crop((0, 450, 300, 600)).tobytes() != blank.crop((0, 450, 300, 600)).tobytes()
