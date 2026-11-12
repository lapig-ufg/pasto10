"""Tests of ``start_buffer_registration_by_url`` (Google Maps link as another way to give the coordinate).

Same flow as the coordinate tool: pending state, silent replacement, atomic session. No network:
``requests.get`` is replaced by a redirect table.
"""

import pytest
from semente.backends.toolkit import StateContext

from domain.services.geospatial import maps_url
from domain.tools import property_tools

LONG_URL = "https://www.google.com/maps/place/Fazenda/@-15.5,-47.5,15z"
TOOL = "start_buffer_registration_by_url"


class _Response:
    def __init__(self, location=None):
        self.status_code = 302 if location else 200
        self.headers = {"Location": location} if location else {}

    def close(self):
        pass


@pytest.fixture
def short_links(monkeypatch):
    table = {}
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Response(table.get(url))

    monkeypatch.setattr(maps_url.requests, "get", fake_get)
    table["calls"] = calls
    return table


def _context(**session_state) -> StateContext:
    return StateContext(session_state=session_state)


def _start(run_context, url=LONG_URL, **kwargs):
    return property_tools.start_buffer_registration_by_url(run_context=run_context, url=url, **kwargs)


def _text(key, **values):
    return property_tools.get_tool_result_text("property_tools", TOOL, key, **values)


def test_long_link_leaves_a_pending_buffer_like_the_coordinate_tool():
    run_context = _context()

    result = _start(run_context, radius=300)

    state = run_context.session_state
    assert state["registration_state"] == "pending"
    candidate, = state["candidate_properties"]
    assert candidate["feature_type"] == "buffer_area"
    metadata = {entry["key"]: entry["value"] for entry in candidate["metadata"]}
    assert metadata["center"] == "-15.5, -47.5" and metadata["radius"] == 300
    assert result.images and "300 m" in result.content and "-15.5, -47.5" in result.content


def test_url_and_coordinate_tools_build_the_same_area():
    by_url, by_coordinate = _context(), _context()

    _start(by_url, radius=250)
    property_tools.start_buffer_registration_by_coordinate(
        run_context=by_coordinate, latitude=-15.5, longitude=-47.5, radius=250,
    )

    url_candidate = by_url.session_state["candidate_properties"][0]
    coordinate_candidate = by_coordinate.session_state["candidate_properties"][0]
    assert url_candidate["metadata"] == coordinate_candidate["metadata"]
    assert url_candidate["total_area"] == coordinate_candidate["total_area"]


def test_default_radius_is_200():
    run_context = _context()

    _start(run_context)

    metadata = {entry["key"]: entry["value"] for entry in run_context.session_state["candidate_properties"][0]["metadata"]}
    assert metadata["radius"] == 200


@pytest.mark.parametrize("radius", [0, 49, 1001, -5])
def test_invalid_radius_keeps_session_untouched_and_skips_the_network(radius, short_links):
    run_context = _context(all_properties={"features": []})

    result = _start(run_context, url="https://maps.app.goo.gl/abc", radius=radius)

    assert result.content == _text("invalid_radius", radius=radius, min_radius=50, max_radius=1000)
    assert not result.images
    assert run_context.session_state == {"all_properties": {"features": []}}
    assert short_links["calls"] == []


@pytest.mark.parametrize("radius", [50, 1000])
def test_radius_bounds_are_inclusive(radius):
    run_context = _context()

    _start(run_context, radius=radius)

    assert run_context.session_state["registration_state"] == "pending"


@pytest.mark.parametrize(
    "url, key",
    [
        ("https://www.google.com/maps/place/Brasilia", "no_coordinates"),
        ("https://evil.com/maps?q=-15.5,-47.5", "unsupported_link"),
        ("http://www.google.com/maps?q=-15.5,-47.5", "unsupported_link"),
        ("https://169.254.169.254/", "unsupported_link"),
        ("", "unsupported_link"),
    ],
)
def test_link_failures_render_the_matching_text_and_keep_the_session(url, key):
    run_context = _context(registration_state="pending", candidate_properties=[{"keep": True}])

    result = _start(run_context, url=url)

    assert result.content == _text(key)
    assert not result.images
    assert run_context.session_state == {"registration_state": "pending", "candidate_properties": [{"keep": True}]}


def test_out_of_range_link_renders_invalid_coordinates():
    run_context = _context()

    result = _start(run_context, url="https://www.google.com/maps/@-91.0,-47.9,12z")

    assert result.content == _text("invalid_coordinates", latitude=-91.0, longitude=-47.9, max_latitude=85)
    assert run_context.session_state == {}


def test_network_failure_gives_generic_text_without_details(monkeypatch):
    import requests

    def broken(url, **kwargs):
        raise requests.ConnectionError("secret-internal-host")

    monkeypatch.setattr(maps_url.requests, "get", broken)
    run_context = _context()

    result = _start(run_context, url="https://maps.app.goo.gl/abc")

    assert result.content == _text("link_unreachable")
    assert "secret" not in result.content
    assert run_context.session_state == {}


@pytest.mark.parametrize("state", ["pending", "final"])
def test_a_new_link_replaces_a_pending_or_final_registration_silently(state):
    run_context = _context()
    _start(run_context)
    run_context.session_state["registration_state"] = state
    first_id = run_context.session_state["candidate_properties"][0]["feature_id"]

    result = _start(run_context, url="https://www.google.com/maps?q=-33.0,-56.0", radius=1000)

    assert run_context.session_state["registration_state"] == "pending"
    candidates = run_context.session_state["candidate_properties"]
    assert len(candidates) == 1 and candidates[0]["feature_id"] != first_id
    assert "-33.0, -56.0" in result.content


def test_geometry_failure_is_generic_and_atomic(monkeypatch, caplog):
    def broken(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(property_tools, "build_buffered_area", broken)
    run_context = _context(registration_state="final", candidate_properties=[{"keep": True}])

    with caplog.at_level("ERROR"):
        result = _start(run_context)

    assert result.content == _text("error")
    assert "boom" not in result.content and "RuntimeError: boom" in caplog.text
    assert run_context.session_state == {"registration_state": "final", "candidate_properties": [{"keep": True}]}


def test_render_failure_is_generic_and_atomic(monkeypatch):
    def broken(feature, **kwargs):
        raise RuntimeError("secret-detail")

    monkeypatch.setattr(property_tools, "render_buffer_image", broken)
    run_context = _context()

    result = _start(run_context)

    assert result.content == _text("error") and "secret-detail" not in result.content
    assert run_context.session_state == {}


def test_full_flow_short_link_to_pending_to_named_registration(short_links):
    short_links["https://maps.app.goo.gl/AbCd123"] = (
        "https://www.google.com/maps/place/Fazenda+Boa+Vista/@-15.9,-47.7,17z/data=!3d-15.8123456!4d-47.7654321"
    )
    run_context = _context()

    started = _start(run_context, url="https://maps.app.goo.gl/AbCd123", radius=400)
    assert started.images and run_context.session_state["registration_state"] == "pending"
    assert "-15.8123, -47.7654" in started.content or "-15.81" in started.content

    confirmed = property_tools.confirm_registration(run_context=run_context, name="Boa Vista")

    assert "Boa Vista" in confirmed.content
    state = run_context.session_state
    assert state["registration_state"] is None and state["candidate_properties"] == []
    feature, = state["all_properties"]["features"]
    metadata = {entry["key"]: entry["value"] for entry in feature["metadata"]}
    assert metadata["name"] == "Boa Vista" and metadata["radius"] == 400
    assert short_links["calls"] == ["https://maps.app.goo.gl/AbCd123"]


@pytest.mark.parametrize(
    "link",
    [
        "https://www.google.com/maps/place/Bras%C3%ADlia/@-15.7801,-47.9292,12z/data=!3m1!4b1!1s0x935a3d18df9ae275:0x738470e469754a24!8m2!3d-15.7942287!4d-47.8821658",
        "https://maps.app.goo.gl/AbCd123",
        "https://www.google.com/maps?q=-15.780148123456%2C-47.929170987654",
    ],
)
def test_links_in_chat_text_pass_the_guardrail_chain_untouched(link):
    from semente.guardrails.base import GuardrailChain, GuardrailContext

    from domain.guardrails import guardrails

    text = f"Register this place {link} with 300 m"
    result = GuardrailChain(guardrails).redact(GuardrailContext(text=text, source="pipeline"))

    assert result.text == text and not result.blocked and not result.removed
