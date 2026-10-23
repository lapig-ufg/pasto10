"""Tests of the registry flow with the synthetic demo registry: coordinate -> parcels (id, area, status) -> choice,
registry-number lookup, the slow OpenStreetMap source that must not block the answer, and the DEMO labeling."""

import json
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image as PilImage
from pypdf import PdfReader
from semente.backends.toolkit import StateContext

from domain.agent import get_instructions
from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial import image as image_module
from domain.services.property_sources import osm_source, registry
from domain.services.property_sources.base import PropertySourceError
from domain.tools import biomass_tools, pasture_tools, property_tools, report_tools
from domain.utils.feature_utils import get_registered_features

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "overpass"
GOIAS = (-15.5, -47.5)
DEMO_LABEL = "DEMO registry parcel (synthetic sample, not a real property boundary)"


def _context(**state) -> StateContext:
    return StateContext(session_state=dict(state))


@pytest.fixture(autouse=True)
def sources(monkeypatch):
    monkeypatch.delenv(registry.SOURCES_ENV, raising=False)
    osm_source._cache.clear()
    state = {"delay": 0.0, "error": None}

    def find(self, latitude, longitude, search_radius_m=2000):
        time.sleep(state["delay"])
        if state["error"] is not None:
            raise state["error"]
        return osm_source.OsmSource.__dict__["_real_find"](self, latitude, longitude, search_radius_m)

    monkeypatch.setattr(osm_source.OsmSource, "_real_find", osm_source.OsmSource.find_by_point, raising=False)
    monkeypatch.setattr(osm_source.OsmSource, "find_by_point", find)
    monkeypatch.setattr(osm_source, "get_overpass", lambda query: json.loads((FIXTURES / "synthetic_several.json").read_text(encoding="utf-8")))
    monkeypatch.setattr(osm_source.OsmSource, "response_timeout_s", 0.3)
    yield state
    osm_source._cache.clear()


def _find(run_context, point=GOIAS, **kwargs):
    return property_tools.find_property_candidates(run_context=run_context, latitude=point[0], longitude=point[1], **kwargs)


def test_default_search_lists_demo_parcels_first_with_id_area_status_and_the_demo_warning():
    run_context = _context()

    result = _find(run_context)

    assert run_context.session_state["registration_state"] == "choosing" and result.images
    lines = [line for line in result.content.splitlines() if line[:2] in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.")]
    assert lines[0].startswith("1. " + DEMO_LABEL + " · DEMO-BRA-0001") and "status active" in lines[0] and "contains the point" in lines[0]
    assert any("OpenStreetMap land-use polygon" in line for line in lines)
    for expected in ("DEMO registry", "synthetic sample parcels invented for illustration", "demonstration", "is their area"):
        assert expected in result.content
    assert "registered_boundary" not in result.content and "demo_registry" not in result.content


def test_the_image_carries_the_demo_banner(monkeypatch):
    marks = []
    original = image_module.draw_demo_mark
    monkeypatch.setattr(image_module, "draw_demo_mark", lambda img: marks.append(1) or original(img))

    _find(_context())

    assert marks == [1]
    selected = _context()
    _find(selected)
    property_tools.select_candidate(run_context=selected, selection=1)
    assert len(marks) == 3


def test_a_slow_openstreetmap_does_not_block_and_the_user_is_told(sources):
    sources["delay"] = 3.0
    run_context = _context()
    start = time.perf_counter()

    result = _find(run_context)

    assert time.perf_counter() - start < 2.0
    assert "DEMO-BRA-0001" in result.content and "The OpenStreetMap lookup did not answer in time" in result.content and "demo registry only" in result.content
    assert all(item["source"] != "osm" for item in run_context.session_state["candidate_properties"])


def test_osm_failure_keeps_the_demo_candidates_and_notes_it(sources):
    sources["error"] = PropertySourceError("unavailable")

    result = _find(_context())

    assert "DEMO-BRA-0001" in result.content and "The OpenStreetMap lookup is temporarily unavailable" in result.content


def test_only_a_slow_source_and_no_demo_parcel_means_source_unavailable(sources):
    sources["delay"] = 3.0

    result = _find(_context(), point=(-12.05, -77.04))

    assert "OpenStreetMap lookup did not answer" in result.content and "approximate circle" in result.content and not result.images


def test_negative_control_without_parcels_and_with_empty_osm_has_no_candidates(monkeypatch):
    monkeypatch.setattr(osm_source, "get_overpass", lambda query: json.loads((FIXTURES / "synthetic_empty.json").read_text(encoding="utf-8")))
    run_context = _context()

    result = _find(run_context, point=(25.0, 10.0))

    assert "No polygon was found" in result.content and "approximate circle" in result.content
    assert run_context.session_state == {}


def test_demo_flow_choosing_pending_final_registered_with_the_demo_label():
    run_context = _context()
    _find(run_context, source="demo_registry")

    selected = property_tools.select_candidate(run_context=run_context, selection=1)

    assert DEMO_LABEL in selected.content and "synthetic sample" in selected.content and selected.images
    property_tools.confirm_registration(run_context=run_context, name="Parcela demo")
    feature = get_registered_features(run_context.session_state).features[0]
    assert feature.feature_type == "demo_registry" and feature.get_metadata("registry_id") == "DEMO-BRA-0001"


def test_agent_instructions_show_the_demo_label_while_choosing_and_confirming():
    run_context = _context()
    _find(run_context, source="demo_registry")

    choosing = get_instructions(run_context)
    property_tools.select_candidate(run_context=run_context, selection=2)
    pending = get_instructions(run_context)

    assert "1. " + DEMO_LABEL in choosing and DEMO_LABEL in pending and "DEMO registry" in choosing


@pytest.mark.parametrize("identifier", ["DEMO-BRA-0001", " demo-bra-0001 "])
def test_registry_number_lookup_leaves_the_parcel_pending(identifier):
    run_context = _context()

    result = property_tools.find_property_by_registry_id(run_context=run_context, identifier=identifier)

    assert run_context.session_state["registration_state"] == "pending" and result.images
    assert DEMO_LABEL in result.content and "synthetic sample" in result.content and "DEMO-BRA-0001" in result.content


def test_unknown_registry_number_is_reported_and_leaves_the_session_untouched():
    run_context = _context()

    result = property_tools.find_property_by_registry_id(run_context=run_context, identifier="DEMO-XXX-0000")

    assert "No area with the registry number" in result.content and "DEMO-XXX-0000" in result.content and run_context.session_state == {}


def test_registry_lookup_failures_and_unknown_source(monkeypatch):
    class Broken:
        key = "broken"

        def find_by_point(self, *args, **kwargs):
            return []

        def find_by_id(self, identifier):
            raise PropertySourceError("unavailable")

    registry.register_source("broken", Broken)
    monkeypatch.setenv(registry.SOURCES_ENV, "broken")
    try:
        down = property_tools.find_property_by_registry_id(run_context=_context(), identifier="X")
    finally:
        registry._factories.pop("broken", None)
        registry._instances.pop("broken", None)
    ghost = property_tools.find_property_by_registry_id(run_context=_context(), identifier="X", source="ghost")

    assert "temporarily unavailable" in down.content and "temporarily unavailable" in ghost.content


def test_the_tool_description_mentions_registry_numbers():
    from semente.configs.prompts import get_tool_description

    assert "registry number" in get_tool_description("property_tools", "find_property_by_registry_id")
    assert "DEMO" in get_tool_description("property_tools", "find_property_candidates")


def test_a_demo_parcel_feeds_the_pasture_biomass_and_bulletin_tools(monkeypatch):
    run_context = _context()
    property_tools.find_property_by_registry_id(run_context=run_context, identifier="DEMO-BRA-0001")
    property_tools.confirm_registration(run_context=run_context, name="Demo parcel")
    feature = get_registered_features(run_context.session_state).features[0]
    seen = []
    pasture = PastureAreaStats(year=2022, source=PastureSource.GPW, pasture_ha=90, cultivated_ha=80, natural_ha=10, area_ha=feature.total_area)
    biomass = BiomassStats(
        source=BiomassSource.GPW_ANNUAL, period_label="2025", start="2025-01-01", end="2025-12-31", mean_t_ha=15, total_t=1300,
        p02_t_ha=10, p98_t_ha=20, pasture_ha=90, area_ha=feature.total_area, scale_m=30, mask_year=2022, year=2025, days_covered=365,
    )
    monkeypatch.setattr(pasture_tools, "measure_area", lambda f, year: seen.append(f) or pasture)
    monkeypatch.setattr(biomass_tools, "measure_total", lambda f, year, month: seen.append(f) or biomass)
    buffer = BytesIO()
    PilImage.new("RGB", (64, 64), (60, 140, 70)).save(buffer, format="JPEG")
    monkeypatch.setattr(report_tools, "_pasture_section", lambda f: (pasture, buffer.getvalue()))
    monkeypatch.setattr(report_tools, "_biomass_section", lambda f: (biomass, buffer.getvalue()))

    area = pasture_tools.get_pasture_area(run_context=run_context)
    total = biomass_tools.get_biomass_total(run_context=run_context)
    bulletin = report_tools.generate_area_bulletin(run_context=run_context)

    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(bulletin.files[0].content)).pages)
    assert "Demo parcel" in area.content and "15.0 t DM/ha" in total.content and "Demo parcel" in text
    assert len(seen) == 2 and all(item.feature_id == feature.feature_id for item in seen)
