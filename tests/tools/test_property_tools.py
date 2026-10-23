"""Tests of the buffer registration flow (domain/tools/property_tools.py).

Flow: start_buffer_registration_by_coordinate (pending) -> confirm_registration
(final, or straight to registered when a name is given) -> complete_registration;
cancel_registration aborts at any step. No network (see tests/conftest.py).
"""

import copy
import math

import pytest
from semente.backends.toolkit import StateContext

from domain.schemas.feature import Feature
from domain.tools import property_tools

PNG_SIGNATURE = b"\x89PNG"
JPEG_SIGNATURE = b"\xff\xd8\xff"
INTERNAL_MARKERS = ("Feature ID", "buffer_area")


def _context(**session_state) -> StateContext:
    return StateContext(session_state=session_state)


def _metadata(feature: dict) -> dict:
    return {entry["key"]: entry["value"] for entry in feature["metadata"]}


def _assert_user_facing_summary(text: str, feature_id: str) -> None:
    """The text shows lat/lon, the 300 m radius and the area, and no internal ids."""
    assert "-15.5, -47.5" in text
    assert "300 m" in text
    assert "28.27 ha" in text
    assert feature_id not in text
    for marker in INTERNAL_MARKERS:
        assert marker not in text


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
    metadata = _metadata(candidate)
    assert metadata["radius"] == 350
    assert metadata["center"] == "-15.5, -47.5"
    assert result.images[0].content.startswith(PNG_SIGNATURE)
    assert (result.images[0].format, result.images[0].mime_type) == ("png", "image/png")
    assert "350 m" in result.content


def test_radius_metadata_carries_meter_unit():
    run_context = _context()

    _start(run_context, radius=300)

    radius_entry = next(
        entry for entry in run_context.session_state["candidate_properties"][0]["metadata"]
        if entry["key"] == "radius"
    )
    assert radius_entry["unit"] == "m"
    assert "radius: 300 m" in Feature.model_validate(run_context.session_state["candidate_properties"][0]).describe()


def test_feature_serialized_without_unit_still_validates():
    legacy = {
        "feature_id": "abc", "coords": [], "total_area": 1.0, "feature_type": "buffer_area",
        "metadata": [{"key": "radius", "value": 200}, {"key": "center", "value": "0.0, 0.0"}],
    }

    feature = Feature.model_validate(legacy)

    assert feature.metadata[0].unit is None
    assert "radius: 200," in feature.describe()


def test_confirm_buffer_area_shows_summary_and_radius_choice():
    run_context = _context()

    result = _start(run_context, radius=300)

    feature_id = run_context.session_state["candidate_properties"][0]["feature_id"]
    _assert_user_facing_summary(result.content, feature_id)
    assert (
        "Mention that the radius is 300 m and that they can choose another radius "
        "between 50 and 1000 m (default: 200 m)."
    ) in result.content


def test_default_radius_is_200_and_cited_in_confirmation():
    run_context = _context()

    result = _start(run_context)

    assert _metadata(run_context.session_state["candidate_properties"][0])["radius"] == 200
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
    assert result.images[0].content.startswith(JPEG_SIGNATURE)
    assert (result.images[0].format, result.images[0].mime_type) == ("jpeg", "image/jpeg")


def test_confirm_without_name_moves_to_final():
    run_context = _context()
    _start(run_context, radius=300)

    result = property_tools.confirm_registration(run_context=run_context)

    assert run_context.session_state["registration_state"] == "final"
    assert len(run_context.session_state["candidate_properties"]) == 1
    assert run_context.session_state.get("all_properties") is None
    feature_id = run_context.session_state["candidate_properties"][0]["feature_id"]
    _assert_user_facing_summary(result.content, feature_id)


def test_confirm_with_name_registers_immediately():
    run_context = _pending_context()
    feature_id = run_context.session_state["candidate_properties"][0]["feature_id"]

    result = property_tools.confirm_registration(run_context=run_context, name="North Paddock")

    assert run_context.session_state["registration_state"] is None
    assert run_context.session_state["candidate_properties"] == []
    registered = run_context.session_state["all_properties"]["features"]
    assert [feature["feature_id"] for feature in registered] == [feature_id]
    assert _metadata(registered[0])["name"] == "North Paddock"
    assert "North Paddock" in result.content


def test_confirm_without_candidate_reports_no_pending_area():
    run_context = _context()

    result = property_tools.confirm_registration(run_context=run_context, name="North Paddock")

    expected = property_tools.get_tool_result_text("property_tools", "confirm_registration", "no_pending_area")
    assert result.content == expected
    assert run_context.session_state.get("all_properties") is None


def test_complete_registration_appends_named_feature_with_stable_id():
    run_context = _context()
    _start(run_context, radius=300)
    property_tools.confirm_registration(run_context=run_context)
    feature_id = run_context.session_state["candidate_properties"][0]["feature_id"]

    result = property_tools.complete_registration(run_context=run_context, name="River Field")

    state = run_context.session_state
    assert state["registration_state"] is None
    assert state["candidate_properties"] == []
    registered = state["all_properties"]["features"]
    assert len(registered) == 1
    assert registered[0]["feature_id"] == feature_id
    assert _metadata(registered[0])["name"] == "River Field"
    assert "River Field" in result.content
    _assert_user_facing_summary(result.content, feature_id)


def test_complete_registration_keeps_previous_registrations():
    run_context = _pending_context()
    property_tools.confirm_registration(run_context=run_context, name="First")
    _start(run_context, latitude=10.0, longitude=20.0)

    property_tools.confirm_registration(run_context=run_context)
    property_tools.complete_registration(run_context=run_context, name="Second")

    names = [_metadata(feature)["name"] for feature in run_context.session_state["all_properties"]["features"]]
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
    assert run_context.session_state["candidate_properties"] == []
    assert run_context.session_state["all_properties"] is registered
    assert result.content


def test_geometry_failure_returns_generic_error_and_keeps_state(monkeypatch, caplog):
    def broken(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(property_tools, "build_buffered_area", broken)
    run_context = _context()

    with caplog.at_level("ERROR"):
        result = _start(run_context)

    expected = property_tools.get_tool_result_text(
        "property_tools", "start_buffer_registration_by_coordinate", "error",
    )
    assert result.content == expected
    assert "boom" not in result.content
    assert "RuntimeError: boom" in caplog.text
    assert run_context.session_state == {}


def test_render_failure_hides_details_from_model_and_keeps_state(monkeypatch, caplog):
    def broken(feature, **kwargs):
        raise RuntimeError("secret-detail")

    monkeypatch.setattr(property_tools, "render_buffer_image", broken)
    run_context = _context(all_properties={"features": []})

    with caplog.at_level("ERROR"):
        result = _start(run_context)

    assert "secret-detail" not in result.content
    assert not result.images
    assert "Traceback" in caplog.text and "secret-detail" in caplog.text
    assert run_context.session_state == {"all_properties": {"features": []}}


def test_confirm_with_invalid_candidate_keeps_state_and_hides_details(caplog):
    run_context = _context(
        registration_state="pending",
        candidate_properties=[{"feature_id": "x", "coords": "not-a-polygon"}],
        all_properties={"features": []},
    )
    before = copy.deepcopy(run_context.session_state)

    with caplog.at_level("ERROR"):
        result = property_tools.confirm_registration(run_context=run_context)

    expected = property_tools.get_tool_result_text("property_tools", "confirm_registration", "error")
    assert result.content == expected
    assert "ValidationError" not in result.content and "not-a-polygon" not in result.content
    assert "ValidationError" in caplog.text
    assert run_context.session_state == before


def test_cancelled_text_mentions_the_cancellation_once_and_continues_with_the_request():
    result = property_tools.cancel_registration(run_context=_pending_context())

    assert "at most once" in result.content and "continue with what the user asked next" in result.content
    assert "repeat the new value back" not in result.content


def test_summarize_feature_without_template_hides_internal_markers(monkeypatch):
    from domain.utils import feature_utils

    run_context = _pending_context()
    feature = Feature.model_validate(run_context.session_state["candidate_properties"][0])
    monkeypatch.setattr(feature_utils, "get_agent_config", lambda name: {})

    summary = feature_utils.summarize_feature(feature)

    assert feature.id not in summary
    for marker in INTERNAL_MARKERS:
        assert marker not in summary
    assert f"{feature.total_area} ha" in summary


def test_summarize_feature_without_center_or_radius_hides_internal_markers():
    from domain.utils import feature_utils

    run_context = _pending_context()
    data = copy.deepcopy(run_context.session_state["candidate_properties"][0])
    data["metadata"] = [entry for entry in data["metadata"] if entry["key"] not in ("center", "radius")]
    feature = Feature.model_validate(data)

    summary = feature_utils.summarize_feature(feature)

    assert feature.id not in summary
    for marker in INTERNAL_MARKERS:
        assert marker not in summary
    assert summary == f"approximate circular area (not a real property boundary) · {feature.total_area} ha"


@pytest.mark.parametrize("name", [None, "North Paddock"])
@pytest.mark.parametrize("tool_name", ["confirm_registration", "complete_registration"])
def test_summary_failure_leaves_session_state_untouched(monkeypatch, tool_name, name):
    run_context = _pending_context()
    if tool_name == "complete_registration":
        property_tools.confirm_registration(run_context=run_context)
        name = name or "River Field"
    run_context.session_state["all_properties"] = {"features": []}
    before = copy.deepcopy(run_context.session_state)

    def broken(feature, **kwargs):
        raise RuntimeError("summary-boom")

    monkeypatch.setattr(property_tools, "summarize_feature", broken)
    kwargs = {"name": name} if name else {}
    result = getattr(property_tools, tool_name)(run_context=run_context, **kwargs)

    expected = property_tools.get_tool_result_text("property_tools", tool_name, "error")
    assert result.content == expected
    assert "summary-boom" not in result.content
    assert run_context.session_state == before


def _previous_registration_state():
    previous = [{"feature_id": "old", "coords": [], "metadata": [], "total_area": 1.0, "feature_type": "buffer_area"}]
    return _context(registration_state="final", candidate_properties=previous, all_properties={"features": []})


def _assert_start_failed_atomically(result, run_context, before):
    expected = property_tools.get_tool_result_text(
        "property_tools", "start_buffer_registration_by_coordinate", "error",
    )
    assert result.content == expected
    assert not result.images
    assert run_context.session_state["registration_state"] == before["registration_state"]
    assert run_context.session_state["candidate_properties"] == before["candidate_properties"]
    assert run_context.session_state == before


def test_start_summary_failure_keeps_previous_state(monkeypatch):
    def broken(feature, **kwargs):
        raise RuntimeError("summary-boom")

    monkeypatch.setattr(property_tools, "summarize_feature", broken)
    run_context = _previous_registration_state()
    before = copy.deepcopy(run_context.session_state)

    result = _start(run_context)

    _assert_start_failed_atomically(result, run_context, before)


def test_start_confirmation_text_failure_keeps_previous_state(monkeypatch):
    original = property_tools.get_tool_result_text

    def flaky(group, tool_key, result_key, **kwargs):
        if result_key == "confirm_buffer_area":
            raise RuntimeError("template-boom")
        return original(group, tool_key, result_key, **kwargs)

    monkeypatch.setattr(property_tools, "get_tool_result_text", flaky)
    run_context = _previous_registration_state()
    before = copy.deepcopy(run_context.session_state)

    result = _start(run_context)

    _assert_start_failed_atomically(result, run_context, before)


def test_summarize_feature_without_total_area_uses_safe_summary():
    from domain.utils import feature_utils

    run_context = _pending_context()
    feature = Feature.model_validate(run_context.session_state["candidate_properties"][0])
    feature = feature.model_copy(update={"total_area": None})

    summary = feature_utils.summarize_feature(feature)

    assert "None" not in summary
    assert summary == "approximate circular area (not a real property boundary)"


def test_summarize_feature_with_unknown_placeholder_uses_safe_summary(monkeypatch):
    from domain.utils import feature_utils

    run_context = _pending_context()
    feature = Feature.model_validate(run_context.session_state["candidate_properties"][0])
    config = dict(feature_utils.get_agent_config("single_agent"))
    config["candidate_summary"] = "Location: {latitude}, {longitude} · {foo}"
    monkeypatch.setattr(feature_utils, "get_agent_config", lambda name: config)

    summary = feature_utils.summarize_feature(feature)

    assert summary == feature_utils._safe_summary(feature, config)
    assert feature.id not in summary
    assert "buffer_area" not in summary
