"""Tests of the transect registration flow and transect-specific tool behavior."""

import copy
import math

import pytest
from semente.backends.toolkit import StateContext

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.feature import FeatureMetadata, RegisteredFeatures
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial.transect import MAX_TOTAL_AREA_HA, build_transect
from domain.tools import biomass_tools, pasture_tools, property_tools, report_tools
from domain.utils.feature_utils import set_registered_features

PNG_SIGNATURE = b"\x89PNG"
START = (-15.5, -47.5)
END = (-15.51, -47.51)


def _context(**session_state) -> StateContext:
    return StateContext(session_state=session_state)


def _start(run_context, **kwargs):
    kwargs.setdefault("start_latitude", START[0])
    kwargs.setdefault("start_longitude", START[1])
    kwargs.setdefault("end_latitude", END[0])
    kwargs.setdefault("end_longitude", END[1])
    return property_tools.start_transect_registration_by_coordinates(run_context=run_context, **kwargs)


def _pending_context():
    run_context = _context()
    _start(run_context)
    return run_context


def _candidate(run_context) -> dict:
    return run_context.session_state["candidate_properties"][0]


def _registered_transect(name="North transect", total_area=500.0):
    transect = build_transect(*START, *END).model_copy(update={"total_area": total_area})
    transect.metadata.append(FeatureMetadata(key="name", value=name))
    state = {}
    set_registered_features(state, RegisteredFeatures(features=[transect]))
    return transect, state


def test_valid_transect_creates_a_pending_line_candidate_with_png():
    run_context = _context()

    result = _start(run_context)

    state = run_context.session_state
    assert state["registration_state"] == "pending"
    candidate = _candidate(run_context)
    assert candidate["feature_type"] == "transect"
    assert candidate["geometry_type"] == "LineString"
    metadata = {entry["key"]: entry["value"] for entry in candidate["metadata"]}
    assert metadata["start"] == f"{START[0]}, {START[1]}"
    assert metadata["end"] == f"{END[0]}, {END[1]}"
    assert metadata["length_m"] > 0
    assert result.images[0].content.startswith(PNG_SIGNATURE)
    assert "Total area: to be provided" in result.content


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_latitude": 85.1},
        {"end_longitude": 181.0},
        {"start_latitude": math.nan},
    ],
)
def test_invalid_transect_coordinates_leave_the_session_untouched(kwargs):
    run_context = _context(all_properties={"features": []})

    result = _start(run_context, **kwargs)

    assert not result.images
    assert run_context.session_state == {"all_properties": {"features": []}}


@pytest.mark.parametrize(
    "start, end",
    [
        ((-15.5, -47.5), (-15.5, -47.5)),
        ((0.0, 0.0), (0.0, 2.0)),
    ],
)
def test_invalid_transect_length_returns_instructions(start, end):
    run_context = _context()

    result = _start(
        run_context,
        start_latitude=start[0], start_longitude=start[1],
        end_latitude=end[0], end_longitude=end[1],
    )

    assert "at most 100000 meters long" in result.content
    assert not result.images
    assert run_context.session_state == {}


def test_confirm_transect_without_name_or_area_moves_to_final_and_asks_for_both():
    run_context = _pending_context()

    result = property_tools.confirm_registration(run_context=run_context)

    assert run_context.session_state["registration_state"] == "final"
    assert "total area of the property" in result.content
    assert "friendly name" in result.content


def test_complete_transect_without_area_keeps_the_registration_pending():
    run_context = _pending_context()
    property_tools.confirm_registration(run_context=run_context)
    before = copy.deepcopy(run_context.session_state)

    result = property_tools.complete_registration(run_context=run_context, name="North transect")

    assert result.content == property_tools.get_tool_result_text(
        "property_tools", "complete_registration", "needs_total_area",
        name="North transect", min_area_ha=0.01, max_area_ha=MAX_TOTAL_AREA_HA,
    )
    assert run_context.session_state == before


def test_complete_transect_with_area_registers_the_property_total():
    run_context = _pending_context()
    property_tools.confirm_registration(run_context=run_context)

    result = property_tools.complete_registration(
        run_context=run_context, name="North transect", total_area_ha=500,
    )

    assert run_context.session_state["registration_state"] is None
    assert run_context.session_state["candidate_properties"] == []
    feature = RegisteredFeatures.model_validate(run_context.session_state["all_properties"]).features[0]
    assert feature.feature_type == "transect" and feature.total_area == 500
    assert feature.name == "North transect"
    assert "500 ha" in result.content


def test_confirm_transect_with_name_and_area_registers_immediately():
    run_context = _pending_context()

    property_tools.confirm_registration(run_context=run_context, name="North transect", total_area_ha=500)

    feature = RegisteredFeatures.model_validate(run_context.session_state["all_properties"]).features[0]
    assert feature.name == "North transect" and feature.total_area == 500


def test_confirm_transect_with_area_only_keeps_the_area_for_the_name_step():
    run_context = _pending_context()

    result = property_tools.confirm_registration(run_context=run_context, total_area_ha=500)

    assert run_context.session_state["registration_state"] == "final"
    assert _candidate(run_context)["total_area"] == 500
    assert "name" in result.content.lower()

    property_tools.complete_registration(run_context=run_context, name="North transect")
    feature = RegisteredFeatures.model_validate(run_context.session_state["all_properties"]).features[0]
    assert feature.total_area == 500 and feature.name == "North transect"


def test_confirm_transect_with_name_only_asks_for_the_area_and_keeps_the_name():
    run_context = _pending_context()

    result = property_tools.confirm_registration(run_context=run_context, name="North transect")

    assert run_context.session_state["registration_state"] == "final"
    assert "North transect" in result.content
    assert "total area" in result.content
    assert _candidate(run_context)["metadata"][-1] == {"key": "name", "value": "North transect", "unit": None}


@pytest.mark.parametrize("area", [0, -1, MAX_TOTAL_AREA_HA + 1, math.nan])
def test_invalid_transect_area_keeps_the_session_untouched(area):
    run_context = _pending_context()
    before = copy.deepcopy(run_context.session_state)

    result = property_tools.confirm_registration(run_context=run_context, total_area_ha=area)

    assert result.content == property_tools.get_tool_result_text(
        "property_tools", "confirm_registration", "invalid_total_area",
        total_area_ha=area, min_area_ha=0.01, max_area_ha=MAX_TOTAL_AREA_HA,
    )
    assert run_context.session_state == before


def test_cancel_transect_clears_the_pending_registration():
    run_context = _pending_context()

    property_tools.cancel_registration(run_context=run_context)

    assert run_context.session_state["registration_state"] is None
    assert run_context.session_state["candidate_properties"] == []


@pytest.mark.parametrize(
    "tool_module, tool_name, patch_name",
    [
        (pasture_tools, "generate_pasture_map", "classify_pasture"),
        (biomass_tools, "generate_biomass_map", "build_biomass"),
        (biomass_tools, "generate_biomass_video", "render_video"),
        (report_tools, "generate_area_bulletin", "_pasture_section"),
    ],
)
def test_map_video_and_bulletin_tools_are_locked_for_transects(monkeypatch, tool_module, tool_name, patch_name):
    _, state = _registered_transect()
    run_context = _context(**state)

    def should_not_run(*args, **kwargs):
        raise AssertionError("the analysis service must not run for a transect")

    monkeypatch.setattr(tool_module, patch_name, should_not_run)

    result = getattr(tool_module, tool_name)(run_context=run_context)

    component = {
        "generate_pasture_map": "pasture_tools",
        "generate_biomass_map": "biomass_tools",
        "generate_biomass_video": "biomass_tools",
        "generate_area_bulletin": "report_tools",
    }[tool_name]
    assert result.content == tool_module.get_tool_result_text(component, tool_name, "not_available_for_transect")
    assert not result.images and not result.videos and not result.files


def test_get_pasture_area_uses_the_transect_success_text(monkeypatch):
    feature, state = _registered_transect()
    run_context = _context(**state)
    stats = PastureAreaStats(
        year=2022, source=PastureSource.GPW, pasture_ha=250, cultivated_ha=175,
        natural_ha=75, area_ha=feature.total_area,
    )
    monkeypatch.setattr(pasture_tools, "measure_area", lambda feature, year: stats)

    result = pasture_tools.get_pasture_area(run_context=run_context)

    assert "Mean pasture share along the" in result.content
    assert "Estimated pasture area for the 500 ha of the property: 250.0 ha" in result.content


def test_get_biomass_total_uses_the_transect_success_text(monkeypatch):
    feature, state = _registered_transect()
    run_context = _context(**state)
    stats = BiomassStats(
        source=BiomassSource.GPW_ANNUAL, period_label="2025", start="2025-01-01",
        end="2025-12-31", mean_t_ha=10, total_t=5000, p02_t_ha=5, p98_t_ha=15,
        pasture_ha=feature.total_area, area_ha=feature.total_area, scale_m=30,
        mask_year=2022, year=2025, days_covered=365,
    )
    monkeypatch.setattr(biomass_tools, "measure_total", lambda feature, year, month: stats)

    result = biomass_tools.get_biomass_total(run_context=run_context)

    assert "Mean along the" in result.content
    assert "Estimated total for the 500 ha of the property: 5000.0 t DM" in result.content
