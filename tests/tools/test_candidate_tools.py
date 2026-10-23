"""Tests of the candidate polygon flow: find_property_candidates -> select_candidate -> confirm -> complete,
register_polygon, cancellation and failures. Offline: Overpass answers come from synthetic fixtures."""

import copy
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image as PilImage
from pypdf import PdfReader
from semente import File
from semente.backends.toolkit import StateContext
from semente.configs.prompts import get_tool_description

from domain.agent import get_instructions, get_tools
from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.feature import Feature
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial.image import RenderedImage, parse_center
from domain.services.property_sources import osm_source, registry
from domain.services.property_sources.base import PropertySourceError
from domain.tools import biomass_tools, pasture_tools, property_tools, report_tools
from domain.utils.feature_utils import get_registered_features, summarize_feature

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "overpass"
LAT, LON = -15.5, -47.5
SQUARE = {"type": "Polygon", "coordinates": [[[-47.51, -15.51], [-47.49, -15.51], [-47.49, -15.49], [-47.51, -15.49], [-47.51, -15.51]]]}
OSM_LABEL = "OpenStreetMap land-use polygon (may not be a farm)"


def _context(**state) -> StateContext:
    return StateContext(session_state=dict(state))


@pytest.fixture(autouse=True)
def osm(monkeypatch):
    monkeypatch.setenv(registry.SOURCES_ENV, "osm")
    osm_source._cache.clear()
    state = {"name": "several", "error": None}

    def get_overpass(query):
        if state["error"] is not None:
            raise state["error"]
        return json.loads((FIXTURES / f"synthetic_{state['name']}.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(osm_source, "get_overpass", get_overpass)
    yield state
    osm_source._cache.clear()


def _find(run_context, **kwargs):
    return property_tools.find_property_candidates(run_context=run_context, latitude=LAT, longitude=LON, **kwargs)


def test_find_lists_numbered_candidates_with_labels_and_enters_choosing():
    run_context = _context()

    result = _find(run_context)

    assert run_context.session_state["registration_state"] == "choosing"
    assert len(run_context.session_state["candidate_properties"]) == 5 and result.images
    for expected in ("5 candidate polygon(s)", "1. " + OSM_LABEL, "Fazenda Central", "contains the point", "m away", "does not prove it is a farm", "is their area"):
        assert expected in result.content
    assert "osm_landuse" not in result.content and "way/" not in result.content
    assert PilImage.open(BytesIO(result.images[0].content)).size == (640, 640)


def test_full_flow_choosing_pending_final_registered():
    run_context = _context()
    _find(run_context)

    selected = property_tools.select_candidate(run_context=run_context, selection=1)

    assert run_context.session_state["registration_state"] == "pending" and selected.images
    assert OSM_LABEL in selected.content and "is their area" in selected.content and "Fazenda Central" in selected.content
    feature = Feature.model_validate(run_context.session_state["candidate_properties"][0])
    assert feature.feature_type == "osm_landuse" and feature.get_metadata("origin") == "osm_landuse"
    assert summarize_feature(feature).startswith(OSM_LABEL + " · Fazenda Central · Area: ")

    property_tools.confirm_registration(run_context=run_context)
    assert run_context.session_state["registration_state"] == "final"
    done = property_tools.complete_registration(run_context=run_context, name="North Paddock")

    registered = get_registered_features(run_context.session_state).features
    assert len(registered) == 1 and registered[0].name == "North Paddock" and "North Paddock" in done.content
    assert run_context.session_state["registration_state"] is None and run_context.session_state["candidate_properties"] == []


def test_agent_states_show_the_candidates_and_the_label():
    run_context = _context()
    _find(run_context)

    choosing = get_instructions(run_context)
    tool_names = {getattr(tool, "name", None) for tool in get_tools(run_context)}
    property_tools.select_candidate(run_context=run_context, selection=2)
    pending = get_instructions(run_context)

    assert "1. " + OSM_LABEL in choosing and "select_candidate" in choosing and "does not prove it is a farm" in choosing
    assert {"select_candidate", "cancel_registration", "find_property_candidates", "register_polygon"} <= tool_names
    assert OSM_LABEL in pending and "whether this polygon is the user's area" in pending


@pytest.mark.parametrize("selection", [0, 6, -1, 99])
def test_invalid_selection_leaves_the_session_untouched(selection):
    run_context = _context()
    _find(run_context)
    before = copy.deepcopy(run_context.session_state)

    result = property_tools.select_candidate(run_context=run_context, selection=selection)

    assert "not valid" in result.content and "1 to 5" in result.content and not result.images
    assert run_context.session_state == before


def test_selecting_without_candidates_asks_for_a_coordinate():
    result = property_tools.select_candidate(run_context=_context(), selection=1)

    assert "no candidate polygons" in result.content.lower() or "There are no candidate polygons" in result.content


@pytest.mark.parametrize("state", ["pending", "final", None])
def test_cancel_clears_choosing_pending_and_final(state):
    run_context = _context()
    _find(run_context)
    run_context.session_state["registration_state"] = state or "choosing"

    property_tools.cancel_registration(run_context=run_context)

    assert run_context.session_state["registration_state"] is None and run_context.session_state["candidate_properties"] == []


@pytest.mark.parametrize(
    "setup, expected",
    [
        ({"name": "empty"}, "No polygon was found"),
        ({"name": "residential"}, "urban area"),
        ({"error": PropertySourceError("unavailable")}, "did not answer"),
        ({"error": PropertySourceError("timeout")}, "did not answer"),
    ],
)
def test_empty_urban_or_unavailable_source_offers_the_circle_and_the_polygon(osm, setup, expected):
    osm.update(setup)
    run_context = _context()

    result = _find(run_context)

    assert expected in result.content and "approximate circle" in result.content and "NOT a real property boundary" in result.content
    assert "GeoJSON" in result.content and "50 to 1000 m" in result.content and not result.images
    assert run_context.session_state == {}


def test_unknown_source_and_invalid_coordinates():
    run_context = _context()

    unknown = _find(run_context, source="ghost")
    invalid = property_tools.find_property_candidates(run_context=run_context, latitude=95, longitude=0)

    assert "ghost" in unknown.content and "approximate circle" in unknown.content
    assert "not valid" in invalid.content and run_context.session_state == {}


def test_a_disabled_source_list_falls_back_to_the_offer(monkeypatch):
    monkeypatch.setenv(registry.SOURCES_ENV, "ghost")

    assert "did not answer" in _find(_context()).content


def test_the_buffer_tool_still_works_and_says_it_is_an_approximate_circle():
    run_context = _context()

    result = property_tools.start_buffer_registration_by_coordinate(run_context=run_context, latitude=LAT, longitude=LON, radius=300)

    assert run_context.session_state["registration_state"] == "pending"
    assert "approximate circle" in result.content and "NOT a real property boundary" in result.content


def test_register_polygon_from_text_leaves_a_user_polygon_pending():
    run_context = _context()

    result = property_tools.register_polygon(run_context=run_context, geojson=json.dumps(SQUARE))

    feature = Feature.model_validate(run_context.session_state["candidate_properties"][0])
    assert run_context.session_state["registration_state"] == "pending" and result.images
    assert feature.feature_type == "user_polygon" and "user-provided polygon" in result.content
    assert get_registered_features(run_context.session_state).features == []


@pytest.mark.parametrize("fmt", ["geojson", "json"])
def test_register_polygon_reads_the_attached_file(fmt):
    run_context = _context()
    files = [File(content=b"ignored", format="png"), File(content=json.dumps({"type": "Feature", "geometry": SQUARE, "properties": {}}).encode(), format=fmt)]

    result = property_tools.register_polygon(run_context=run_context, files=files)

    assert run_context.session_state["registration_state"] == "pending" and "is their area" in result.content


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, "No GeoJSON was received"),
        ({"geojson": "not json"}, "could not be read"),
        ({"geojson": json.dumps({"type": "Point", "coordinates": [1, 2]})}, "could not be read"),
        ({"geojson": json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [60, 0], [60, 60], [0, 60], [0, 0]]]})}, "larger than the maximum of 50000 ha"),
    ],
)
def test_register_polygon_failures_leave_the_session_untouched(kwargs, expected):
    run_context = _context()

    result = property_tools.register_polygon(run_context=run_context, **kwargs)

    assert expected in result.content and not result.images and run_context.session_state == {}


def test_unexpected_errors_are_generic(monkeypatch):
    monkeypatch.setattr(property_tools, "render_candidates_image", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom secret")))
    run_context = _context()

    result = _find(run_context)

    assert "unexpected error" in result.content and "secret" not in result.content and run_context.session_state == {}


def test_tool_descriptions_explain_the_limits():
    assert "does NOT prove" in get_tool_description("property_tools", "find_property_candidates")
    assert "without arguments" in get_tool_description("property_tools", "register_polygon")


# --- contract: a candidate feature feeds the analysis tools unchanged --------------------------------------------


def _registered(run_context, name="Test area"):
    _find(run_context)
    property_tools.select_candidate(run_context=run_context, selection=1)
    property_tools.confirm_registration(run_context=run_context, name=name)
    return get_registered_features(run_context.session_state).features[0]


def test_a_candidate_feature_has_center_radius_and_the_polygon_coordinates():
    run_context = _context()

    feature = _registered(run_context)

    latitude, longitude = parse_center(feature)
    assert abs(latitude - LAT) < 0.01 and abs(longitude - LON) < 0.01
    assert 200 < float(feature.get_metadata("radius")) < 800 and len(feature.get_coords()[0][0]) >= 5


def test_pasture_and_biomass_tools_accept_a_polygon_feature(monkeypatch):
    run_context = _context()
    feature = _registered(run_context)
    seen = []
    pasture = PastureAreaStats(year=2022, source=PastureSource.GPW, pasture_ha=40, cultivated_ha=30, natural_ha=10, area_ha=feature.total_area)
    biomass = BiomassStats(
        source=BiomassSource.GPW_ANNUAL, period_label="2025", start="2025-01-01", end="2025-12-31", mean_t_ha=15, total_t=600,
        p02_t_ha=10, p98_t_ha=20, pasture_ha=40, area_ha=feature.total_area, scale_m=30, mask_year=2022, year=2025, days_covered=365,
    )
    monkeypatch.setattr(pasture_tools, "measure_area", lambda f, year: seen.append(f) or pasture)
    monkeypatch.setattr(biomass_tools, "measure_total", lambda f, year, month: seen.append(f) or biomass)

    area = pasture_tools.get_pasture_area(run_context=run_context)
    total = biomass_tools.get_biomass_total(run_context=run_context)

    assert "Test area" in area.content and "40.0 ha" in area.content and "15.0 t DM/ha" in total.content
    assert len(seen) == 2 and all(item.get_coords() == feature.get_coords() for item in seen)


def test_the_bulletin_accepts_a_polygon_feature(monkeypatch):
    run_context = _context()
    feature = _registered(run_context)
    buffer = BytesIO()
    PilImage.new("RGB", (64, 64), (60, 140, 70)).save(buffer, format="JPEG")
    jpeg = buffer.getvalue()
    pasture = PastureAreaStats(year=2022, source=PastureSource.GPW, pasture_ha=40, cultivated_ha=30, natural_ha=10, area_ha=feature.total_area)
    monkeypatch.setattr(report_tools, "_pasture_section", lambda f: (pasture, jpeg))
    monkeypatch.setattr(report_tools, "_biomass_section", lambda f: (_ for _ in ()).throw(RuntimeError("skip")))

    result = report_tools.generate_area_bulletin(run_context=run_context)

    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(result.files[0].content)).pages)
    assert "Test area" in text and "Location" in text and "Not available" in text


def test_polygon_too_large_message_uses_the_effective_limit(monkeypatch):
    monkeypatch.setenv("PASTO10_MAX_POLYGON_HA", "100")

    result = property_tools.register_polygon(run_context=_context(), geojson=json.dumps(SQUARE))

    assert "maximum of 100 ha" in result.content


def test_hostile_polygons_are_rejected_at_the_tool_with_the_complexity_text():
    ring = [[-47.5 + (i % 2) * 0.01, -15.5 + i * 1e-6] for i in range(60_000)]
    run_context = _context()

    result = property_tools.register_polygon(run_context=run_context, geojson=json.dumps({"type": "Polygon", "coordinates": [ring + [ring[0]]]}, separators=(",", ":")))
    attached = property_tools.register_polygon(run_context=run_context, files=[File(content=b"x" * 2_000_000, format="geojson")])

    assert "too large or too complex" in result.content and "too large or too complex" in attached.content
    assert run_context.session_state == {}


def test_a_search_source_that_raises_does_not_break_the_others(monkeypatch):
    class Exploding:
        key = "exploding"

        def find_by_point(self, *args, **kwargs):
            raise ValueError("boom secret")

        def find_by_id(self, identifier):
            return None

    registry.register_source("exploding", Exploding)
    monkeypatch.setenv(registry.SOURCES_ENV, "exploding,osm")
    try:
        run_context = _context()
        result = _find(run_context)
    finally:
        registry._factories.pop("exploding", None)
        registry._instances.pop("exploding", None)

    assert run_context.session_state["registration_state"] == "choosing" and "secret" not in result.content
    assert "exploding is temporarily unavailable" in result.content


def test_the_candidates_image_gets_one_caption_per_candidate(monkeypatch):
    captured = []
    from domain.services.geospatial import image as image_module

    original = image_module.render_candidates_image
    monkeypatch.setattr(property_tools, "render_candidates_image", lambda *a, **k: captured.append(k.get("captions")) or original(*a, **k))

    _find(_context())

    assert len(captured[0]) == 5 and "ha" in captured[0][0]


def test_the_circle_image_is_labeled_as_an_approximate_area(monkeypatch):
    seen = []
    original = property_tools.render_buffer_image
    monkeypatch.setattr(property_tools, "render_buffer_image", lambda feature, **kwargs: seen.append(kwargs) or original(feature, **kwargs))

    property_tools.start_buffer_registration_by_coordinate(run_context=_context(), latitude=LAT, longitude=LON, radius=300)

    assert "not a real property boundary" in seen[0]["origin_label"]


NEW_POLYGON = {"type": "Polygon", "coordinates": [[[-56.02, -31.02], [-55.98, -31.02], [-55.98, -30.98], [-56.02, -30.98], [-56.02, -31.02]]]}


@pytest.mark.parametrize("first_state", ["choosing", "pending", "final"])
def test_register_polygon_replaces_whatever_registration_was_pending(first_state):
    run_context = _context()
    _find(run_context)
    if first_state != "choosing":
        property_tools.select_candidate(run_context=run_context, selection=1)
    if first_state == "final":
        property_tools.confirm_registration(run_context=run_context)
    assert run_context.session_state["registration_state"] == first_state

    result = property_tools.register_polygon(run_context=run_context, geojson=json.dumps(NEW_POLYGON))

    feature = Feature.model_validate(run_context.session_state["candidate_properties"][0])
    latitude, longitude = parse_center(feature)
    assert run_context.session_state["registration_state"] == "pending" and result.images
    assert len(run_context.session_state["candidate_properties"]) == 1 and feature.feature_type == "user_polygon"
    assert (latitude, longitude) == pytest.approx((-31.0, -56.0), abs=0.005) and "Location: -31.0" in result.content


def test_every_starter_replaces_the_pending_registration_silently(monkeypatch):
    monkeypatch.setenv(registry.SOURCES_ENV, "demo_registry,osm")
    run_context = _context()
    property_tools.start_buffer_registration_by_coordinate(run_context=run_context, latitude=-23.5, longitude=-69.0, radius=200)
    assert run_context.session_state["registration_state"] == "pending"

    _find(run_context)
    assert run_context.session_state["registration_state"] == "choosing" and len(run_context.session_state["candidate_properties"]) >= 5
    property_tools.find_property_by_registry_id(run_context=run_context, identifier="DEMO-BRA-0001")
    assert run_context.session_state["registration_state"] == "pending" and len(run_context.session_state["candidate_properties"]) == 1
    property_tools.start_buffer_registration_by_coordinate(run_context=run_context, latitude=LAT, longitude=LON, radius=300)
    assert run_context.session_state["candidate_properties"][0]["feature_type"] == "buffer_area"


def test_register_polygon_never_reuses_an_earlier_polygon_when_the_current_message_has_none():
    run_context = _context()
    property_tools.register_polygon(run_context=run_context, geojson=json.dumps(SQUARE))
    before = copy.deepcopy(run_context.session_state)

    result = property_tools.register_polygon(run_context=run_context)

    assert "No GeoJSON was received" in result.content and not result.images
    assert run_context.session_state == before


def test_the_polygon_tool_asks_for_the_geojson_of_the_current_message():
    description = get_tool_description("property_tools", "register_polygon")

    assert "CURRENT message" in description and "never reuse" in description.lower()


def _zigzag_document(vertices: int) -> str:
    ring = [[-47.5 + (i % 2) * 0.01, -15.5 + i * 1e-5] for i in range(vertices)]
    return json.dumps({"type": "Polygon", "coordinates": [ring + [ring[0]]]}, separators=(",", ":"))


def _many_features_document() -> str:
    feature = {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}
    return json.dumps({"type": "FeatureCollection", "features": [feature] * 300}, separators=(",", ":"))


def _many_holes_document() -> str:
    holes = [[[-47.5 + (i % 40) * 0.001, -15.5 + (i // 40) * 0.001 + d] for d in (0, 0.0002, 0.0004, 0)] for i in range(2000)]
    outer = [[-47.6, -15.6], [-47.3, -15.6], [-47.3, -15.3], [-47.6, -15.3], [-47.6, -15.6]]
    return json.dumps({"type": "Polygon", "coordinates": [outer] + holes}, separators=(",", ":"))


@pytest.mark.parametrize("make", [lambda: _zigzag_document(500), lambda: _zigzag_document(1000), _many_features_document, _many_holes_document], ids=["zigzag-500", "zigzag-1000", "features-300", "holes-2000"])
@pytest.mark.parametrize("via", ["argument", "file"])
def test_hostile_payloads_are_rejected_quickly_through_the_tool(make, via):
    import time

    run_context = _context()
    document = make()
    kwargs = {"geojson": document} if via == "argument" else {"files": [File(content=document.encode(), format="geojson")]}
    start = time.perf_counter()

    result = property_tools.register_polygon(run_context=run_context, **kwargs)

    assert time.perf_counter() - start < 2.0
    assert "too large or too complex" in result.content and "unexpected error" not in result.content
    assert run_context.session_state == {} and not result.images


@pytest.mark.parametrize("document", ['{"type":"Polygon","coordinates":[[[NaN,1],[2,2],[3,1],[NaN,1]]]}', '{"type":"Polygon","coordinates":[[[1e999,1],[2,2],[3,1],[1,1]]]}'])
def test_non_finite_numbers_get_the_invalid_polygon_text_not_an_unexpected_error(document):
    result = property_tools.register_polygon(run_context=_context(), geojson=document)

    assert "could not be read" in result.content and "unexpected error" not in result.content


# --- uploaded_geojson: the front end stores an attached file in the session for the current turn -------------------


def _uploaded(document=None):
    return {"name": "area.geojson", "content": json.dumps(document or NEW_POLYGON)}


def test_an_uploaded_file_in_the_session_is_used_when_the_arguments_are_empty():
    run_context = _context(uploaded_geojson=_uploaded())

    result = property_tools.register_polygon(run_context=run_context)

    feature = Feature.model_validate(run_context.session_state["candidate_properties"][0])
    assert run_context.session_state["registration_state"] == "pending" and result.images
    assert parse_center(feature) == pytest.approx((-31.0, -56.0), abs=0.005) and "Location: -31.0" in result.content


def test_the_geojson_argument_has_precedence_over_the_uploaded_file_and_the_upload_is_consumed():
    run_context = _context(uploaded_geojson=_uploaded())

    property_tools.register_polygon(run_context=run_context, geojson=json.dumps(SQUARE))

    feature = Feature.model_validate(run_context.session_state["candidate_properties"][0])
    assert parse_center(feature) == pytest.approx((-15.5, -47.5), abs=0.005) and "uploaded_geojson" not in run_context.session_state


def test_attached_framework_files_have_precedence_over_the_uploaded_file():
    run_context = _context(uploaded_geojson=_uploaded())
    files = [File(content=json.dumps(SQUARE).encode(), format="geojson")]

    property_tools.register_polygon(run_context=run_context, files=files)

    assert parse_center(Feature.model_validate(run_context.session_state["candidate_properties"][0])) == pytest.approx((-15.5, -47.5), abs=0.005)


def test_without_argument_file_or_upload_the_polygon_is_missing_and_an_old_one_is_not_reused():
    run_context = _context()
    property_tools.register_polygon(run_context=run_context, geojson=json.dumps(SQUARE))
    before = copy.deepcopy(run_context.session_state)

    result = property_tools.register_polygon(run_context=run_context)

    assert "No GeoJSON was received" in result.content and not result.images and run_context.session_state == before


def test_the_upload_is_removed_even_when_it_cannot_be_used():
    run_context = _context(uploaded_geojson={"name": "bad.geojson", "content": "not json"})

    result = property_tools.register_polygon(run_context=run_context)

    assert "could not be read" in result.content and "uploaded_geojson" not in run_context.session_state
    assert "uploaded_geojson" not in property_tools.register_polygon(run_context=_context(uploaded_geojson="junk")).content


@pytest.mark.parametrize("make", [lambda: _zigzag_document(500), _many_features_document, _many_holes_document, lambda: "x" * 2_000_000], ids=["zigzag", "features", "holes", "huge"])
def test_a_hostile_uploaded_file_goes_through_the_same_limits(make):
    import time

    run_context = _context(uploaded_geojson={"name": "x.geojson", "content": make()})
    start = time.perf_counter()

    result = property_tools.register_polygon(run_context=run_context)

    assert time.perf_counter() - start < 2.0 and "too large or too complex" in result.content
    assert "uploaded_geojson" not in run_context.session_state and run_context.session_state.get("registration_state") is None


def test_the_instructions_tell_the_model_what_to_do_with_the_attachment_note():
    from semente.configs.prompts import get_agent_config

    config = get_agent_config("single_agent")

    for key in ("instructions_default", "instructions_pending_single", "instructions_choosing", "instructions_final"):
        assert "[GeoJSON file attached" in config[key] and "`geojson` EMPTY" in config[key], key
    assert "geojson` EMPTY" in get_tool_description("property_tools", "register_polygon")
