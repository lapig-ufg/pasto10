"""Tests of the buffer registration flow (domain/tools/property_tools.py).

Flow: start_buffer_registration_by_coordinate (pending) -> confirm_registration
(final, or straight to registered when a name is given) -> complete_registration;
cancel_registration aborts at any step. No network (see tests/conftest.py).
"""

import math

import pytest
from semente.backends.toolkit import StateContext

from domain.tools import property_tools

PNG_SIGNATURE = b"\x89PNG"


def _context(**session_state) -> StateContext:
    return StateContext(session_state=session_state)


def _start(run_context, **kwargs):
    kwargs.setdefault("latitude", -15.5)
    kwargs.setdefault("longitude", -47.5)
    return property_tools.start_buffer_registration_by_coordinate(run_context=run_context, **kwargs)


def _pending_context():
    run_context = _context()
    _start(run_context)
    return run_context


@pytest.mark.parametrize("radius", [0, 49, 1001, 5000, -100])
def test_invalid_radius_keeps_session_state_intact(radius):
    run_context = _context(all_properties={"features": []})

    result = _start(run_context, radius=radius)

    assert "50" in result.content and "1000" in result.content
    assert not result.images
    assert run_context.session_state == {"all_properties": {"features": []}}


@pytest.mark.parametrize(
    "latitude, longitude",
    [(85.1, 0.0), (-89.0, 10.0), (91.0, 0.0), (0.0, 180.5), (0.0, -181.0), (math.nan, 0.0), (0.0, math.inf)],
)
def test_invalid_coordinates_keep_session_state_intact(latitude, longitude):
    run_context = _context()

    result = _start(run_context, latitude=latitude, longitude=longitude)

    expected = property_tools.get_tool_result_text(
        "property_tools", "start_buffer_registration_by_coordinate", "invalid_coordinates",
        latitude=latitude, longitude=longitude, max_latitude=85,
    )
    assert result.content == expected
    assert not result.images
    assert run_context.session_state == {}


def test_valid_radius_creates_pending_candidate_with_png():
    run_context = _context()

    result = _start(run_context, radius=350)

    state = run_context.session_state
    assert state["registration_state"] == "pending"
    assert len(state["candidate_properties"]) == 1
    candidate = state["candidate_properties"][0]
    assert candidate["feature_type"] == "buffer_area"
    metadata = {entry["key"]: entry["value"] for entry in candidate["metadata"]}
    assert metadata["radius"] == 350
    assert metadata["center"] == "-15.5, -47.5"
    assert result.images[0].content.startswith(PNG_SIGNATURE)
    assert "350 m" in result.content


def test_default_radius_is_200_and_cited_in_confirmation():
    run_context = _context()

    result = _start(run_context)

    metadata = run_context.session_state["candidate_properties"][0]["metadata"]
    assert {"key": "radius", "value": 200} in metadata
    assert "200 m" in result.content


@pytest.mark.parametrize("radius", [50, 1000])
def test_radius_bounds_are_inclusive(radius):
    run_context = _context()

    _start(run_context, radius=radius)

    assert run_context.session_state["registration_state"] == "pending"


def test_registration_proceeds_with_basemap_online(online_tiles):
    run_context = _context()

    result = _start(run_context)

    assert run_context.session_state["registration_state"] == "pending"
    assert result.images[0].content.startswith(PNG_SIGNATURE)


def test_confirm_without_name_moves_to_final():
    run_context = _pending_context()

    result = property_tools.confirm_registration(run_context=run_context)

    assert run_context.session_state["registration_state"] == "final"
    assert len(run_context.session_state["candidate_properties"]) == 1
    assert run_context.session_state.get("all_properties") is None
    feature_id = run_context.session_state["candidate_properties"][0]["feature_id"]
    assert feature_id in result.content


def test_confirm_with_name_registers_immediately():
    run_context = _pending_context()
    feature_id = run_context.session_state["candidate_properties"][0]["feature_id"]

    result = property_tools.confirm_registration(run_context=run_context, name="North Paddock")

    assert run_context.session_state["registration_state"] is None
    assert run_context.session_state["candidate_properties"] is None
    registered = run_context.session_state["all_properties"]["features"]
    assert [feature["feature_id"] for feature in registered] == [feature_id]
    assert {"key": "name", "value": "North Paddock"} in registered[0]["metadata"]
    assert "North Paddock" in result.content


def test_confirm_without_candidate_reports_no_pending_area():
    run_context = _context()

    result = property_tools.confirm_registration(run_context=run_context, name="North Paddock")

    expected = property_tools.get_tool_result_text("property_tools", "confirm_registration", "no_pending_area")
    assert result.content == expected
    assert run_context.session_state.get("all_properties") is None


def test_complete_registration_appends_named_feature_with_stable_id():
    run_context = _pending_context()
    property_tools.confirm_registration(run_context=run_context)
    feature_id = run_context.session_state["candidate_properties"][0]["feature_id"]

    result = property_tools.complete_registration(run_context=run_context, name="River Field")

    state = run_context.session_state
    assert state["registration_state"] is None
    assert state["candidate_properties"] is None
    registered = state["all_properties"]["features"]
    assert len(registered) == 1
    assert registered[0]["feature_id"] == feature_id
    assert {"key": "name", "value": "River Field"} in registered[0]["metadata"]
    assert "River Field" in result.content and feature_id in result.content


def test_complete_registration_keeps_previous_registrations():
    run_context = _pending_context()
    property_tools.confirm_registration(run_context=run_context, name="First")
    _start(run_context, latitude=10.0, longitude=20.0)

    property_tools.confirm_registration(run_context=run_context)
    property_tools.complete_registration(run_context=run_context, name="Second")

    names = [
        next(entry["value"] for entry in feature["metadata"] if entry["key"] == "name")
        for feature in run_context.session_state["all_properties"]["features"]
    ]
    assert names == ["First", "Second"]


def test_complete_registration_without_candidate_reports_no_pending_area():
    run_context = _context()

    result = property_tools.complete_registration(run_context=run_context, name="River Field")

    expected = property_tools.get_tool_result_text("property_tools", "complete_registration", "no_pending_area")
    assert result.content == expected


@pytest.mark.parametrize("tool_name", ["confirm_registration", "complete_registration"])
def test_stale_pending_state_without_candidate_is_reset(tool_name):
    run_context = _context(registration_state="pending", candidate_properties=[])

    result = getattr(property_tools, tool_name)(run_context=run_context, name="River Field")

    expected = property_tools.get_tool_result_text("property_tools", tool_name, "no_pending_area")
    assert result.content == expected
    assert run_context.session_state["registration_state"] is None
    assert run_context.session_state["candidate_properties"] == []


def test_cancel_registration_resets_state_without_touching_registrations():
    registered = {"features": [{"feature_id": "abc", "coords": [], "metadata": [], "total_area": 1.0, "feature_type": "buffer_area"}]}
    run_context = _pending_context()
    run_context.session_state["all_properties"] = registered

    result = property_tools.cancel_registration(run_context=run_context)

    assert run_context.session_state["registration_state"] is None
    assert run_context.session_state["candidate_properties"] is None
    assert run_context.session_state["all_properties"] is registered
    assert result.content


def test_geometry_failure_returns_error_and_keeps_state(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(property_tools, "build_buffered_area", broken)
    run_context = _context()

    result = _start(run_context)

    assert "boom" in result.content
    assert run_context.session_state == {}
