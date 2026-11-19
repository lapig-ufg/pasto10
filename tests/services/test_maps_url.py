"""Tests of the Google Maps link service (domain/services/geospatial/maps_url.py).

No network: ``requests.get`` is replaced by a fake that serves a redirect table.
"""

import pytest
import requests

from domain.services.geospatial import maps_url
from domain.services.geospatial.maps_url import MapsUrlError, extract_coordinates

LONG_URL = "https://www.google.com/maps/place/Bras%C3%ADlia/@-15.7801,-47.9292,12z/data=!3m1!4b1!4m6!3m5!1s0x935a3d18df9ae275:0x738470e469754a24!8m2!3d-15.7942287!4d-47.8821658"


class _Response:
    def __init__(self, status=302, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.closed = False

    def close(self):
        self.closed = True


class _Web:
    table: dict = {}
    calls: list = []
    responses: list = []
    error = None


@pytest.fixture
def web(monkeypatch):
    """Fake ``requests.get``: ``web.table`` maps URL -> Location; ``web.calls`` records requests."""
    _Web.table, _Web.calls, _Web.responses, _Web.error = {}, [], [], None

    def fake_get(url, **kwargs):
        _Web.calls.append((url, kwargs))
        if _Web.error:
            raise _Web.error
        response = _Response(status=302 if url in _Web.table else 200, location=_Web.table.get(url))
        _Web.responses.append(response)
        return response

    monkeypatch.setattr(maps_url.requests, "get", fake_get)
    return _Web


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.google.com/maps/place/Brasilia/@-15.7801,-47.9292,12z", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/place/Brasilia/@-15.7801,-47.9292,12.5z/data=!3m1", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/@-15.7801,-47.9292,15z", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/@-15.7801,-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/place/X/@-15.7801,-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/place/Fazenda/@-16.0,-48.0,17z/data=!3d-15.7942287!4d-47.8821658", (-15.7942287, -47.8821658)),
        (LONG_URL, (-15.7942287, -47.8821658)),
        ("https://www.google.com/maps/place/X/data=!4m2!3d-15.5!4d-47.5", (-15.5, -47.5)),
        ("https://www.google.com/maps?q=-15.7801,-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps?q=-15.7801%2C-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps?q=-15.7801%2C+-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps?q=-15.7801+-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps?q=-15.7801, -47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps?q=loc:-15.7801,-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps?q=loc%3A-15.7801%2C-47.9292", (-15.7801, -47.9292)),
        ("https://maps.google.com/?q=-15.7801,-47.9292&z=15", (-15.7801, -47.9292)),
        ("https://maps.google.com/?ll=-15.7801,-47.9292&z=14", (-15.7801, -47.9292)),
        ("https://maps.google.com/maps?ll=-15.7801%2C-47.9292&q=-15.7801%2C-47.9292&hl=en", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/search/?api=1&query=-15.7801%2C-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/search/-15.7801,-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/search/-15.7801,+-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/search/-15.7801,-47.9292/@-15.7801,-47.9292,17z", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/place/-15.7801,-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com/maps/search/-15.7801%2C-47.9292", (-15.7801, -47.9292)),
        ("https://www.google.com.br/maps/place/Goiania/@-16.6869,-49.2648,11z", (-16.6869, -49.2648)),
        ("https://www.google.co.uk/maps/@51.5074,-0.1278,10z", (51.5074, -0.1278)),
        ("https://google.com/maps?q=40.7128,-74.0060", (40.7128, -74.006)),
        ("https://www.google.com/maps/@33.8688,151.2093,12z", (33.8688, 151.2093)),
        ("https://www.google.com/maps/@-33.8688,151.2093,12z", (-33.8688, 151.2093)),
        ("https://www.google.com/maps/@64.1466,-21.9426,12z", (64.1466, -21.9426)),
        ("https://www.google.com/maps?q=+51.5,+0.12", (51.5, 0.12)),
        ("https://www.google.com/maps/@-15.780148123456,-47.929170987654,18z", (-15.780148123456, -47.929170987654)),
        ("https://www.google.com/maps?q=-15,-47", (-15.0, -47.0)),
        ("https://www.google.com/maps/@0.0,0.0,3z", (0.0, 0.0)),
        ("https://www.google.com/maps/@-84.9,179.9,3z", (-84.9, 179.9)),
        ("  <https://www.google.com/maps?q=-15.5,-47.5>.  ", (-15.5, -47.5)),
        ("HTTPS://WWW.GOOGLE.COM/maps?q=-15.5,-47.5", (-15.5, -47.5)),
        ("https://www.google.com:443/maps?q=-15.5,-47.5", (-15.5, -47.5)),
    ],
)
def test_extracts_coordinates_from_long_links_without_network(url, expected, web):
    assert extract_coordinates(url) == pytest.approx(expected)
    assert web.calls == []


@pytest.mark.parametrize("host", ["maps.app.goo.gl", "goo.gl", "g.co"])
def test_short_links_follow_the_redirect_to_the_long_url(host, web):
    web.table[f"https://{host}/abc123"] = LONG_URL

    assert extract_coordinates(f"https://{host}/abc123") == pytest.approx((-15.7942287, -47.8821658))
    (_, kwargs), = web.calls
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert kwargs["timeout"] == (3, 5)


def test_short_link_with_relative_location_and_several_hops(web):
    web.table["https://goo.gl/maps/abc"] = "https://maps.app.goo.gl/xyz"
    web.table["https://maps.app.goo.gl/xyz"] = "https://www.google.com/maps/place/X/@-15.5,-47.5,15z"

    assert extract_coordinates("https://goo.gl/maps/abc") == (-15.5, -47.5)
    assert len(web.calls) == 2


def test_short_link_with_a_relative_location_stays_on_the_same_host(web):
    web.table["https://maps.app.goo.gl/x"] = "/maps?q=-15.5,-47.5"

    assert extract_coordinates("https://maps.app.goo.gl/x") == (-15.5, -47.5)


def test_short_link_resolves_to_query_style_url(web):
    web.table["https://maps.app.goo.gl/q"] = "https://maps.google.com/?q=-15.5%2C-47.5"

    assert extract_coordinates("https://maps.app.goo.gl/q") == (-15.5, -47.5)


def test_responses_are_closed_without_reading_the_body(web):
    web.table["https://maps.app.goo.gl/abc"] = LONG_URL
    extract_coordinates("https://maps.app.goo.gl/abc")

    assert web.responses and all(response.closed for response in web.responses)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/maps?q=-15.5,-47.5",
        "http://www.google.com/maps?q=-15.5,-47.5",
        "file:///etc/passwd",
        "ftp://www.google.com/maps?q=-15.5,-47.5",
        "javascript:alert(1)",
        "https://google.com.evil.com/maps?q=-15.5,-47.5",
        "https://www.google.com.evil.com/maps/@-15.5,-47.5,15z",
        "https://evil.com/?u=https://maps.google.com/maps?q=-15.5,-47.5",
        "https://evil.com/maps.google.com/@-15.5,-47.5,15z",
        "https://notgoogle.com/maps?q=-15.5,-47.5",
        "https://maps.google.com.evil.com/",
        "https://www.google.com@evil.com/maps?q=-15.5,-47.5",
        "https://evil.com@www.google.com/maps?q=-15.5,-47.5",
        "https://user:pw@www.google.com/maps?q=-15.5,-47.5",
        "https://www.google.com:8443/maps?q=-15.5,-47.5",
        "https://127.0.0.1/maps?q=-15.5,-47.5",
        "https://localhost/maps?q=-15.5,-47.5",
        "https://[::1]/maps?q=-15.5,-47.5",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.1/",
        "https://2130706433/",
        "https://evil.goo.gl.attacker.com/abc",
        "https://sub.maps.app.goo.gl/abc",
        "https://google.com.br.evil.com/maps",
        "https://www.google.zip.evil.com/maps",
        "www.google.com/maps?q=-15.5,-47.5",
        "//www.google.com/maps?q=-15.5,-47.5",
        "-15.5,-47.5",
        "",
        "   ",
        "<>",
        "https://www.google.com/maps?q=-15.5,-47.5\n@evil.com",
        "https://www.google.com/\x00maps",
    ],
)
def test_malicious_or_unsupported_urls_are_rejected_without_network(url, web):
    with pytest.raises(MapsUrlError) as error:
        extract_coordinates(url)

    assert error.value.reason == "unsupported_link"
    assert web.calls == []


def test_non_text_input_is_rejected(web):
    for value in (None, 123, ["https://www.google.com"]):
        with pytest.raises(MapsUrlError) as error:
            extract_coordinates(value)
        assert error.value.reason == "unsupported_link"


def test_gigantic_text_is_rejected_without_network(web):
    for url in ("https://www.google.com/maps?q=" + "1" * 10_000, "x" * 1_000_000):
        with pytest.raises(MapsUrlError) as error:
            extract_coordinates(url)
        assert error.value.reason == "unsupported_link"
    assert web.calls == []


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.com/maps?q=-15.5,-47.5",
        "http://www.google.com/maps?q=-15.5,-47.5",
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1/",
        "https://localhost:8080/",
        "file:///etc/passwd",
        "https://google.com.evil.com/",
    ],
)
def test_redirect_to_a_disallowed_target_is_rejected_before_requesting_it(target, web):
    web.table["https://maps.app.goo.gl/abc"] = target

    with pytest.raises(MapsUrlError) as error:
        extract_coordinates("https://maps.app.goo.gl/abc")

    assert error.value.reason == "unsupported_link"
    assert [url for url, _ in web.calls] == ["https://maps.app.goo.gl/abc"]


def test_redirect_through_an_allowed_host_to_a_disallowed_one_is_rejected(web):
    web.table["https://goo.gl/maps/a"] = "https://maps.app.goo.gl/b"
    web.table["https://maps.app.goo.gl/b"] = "https://evil.com/"

    with pytest.raises(MapsUrlError) as error:
        extract_coordinates("https://goo.gl/maps/a")

    assert error.value.reason == "unsupported_link"
    assert len(web.calls) == 2


def test_redirect_loop_is_cut(web):
    web.table["https://goo.gl/a"] = "https://goo.gl/b"
    web.table["https://goo.gl/b"] = "https://goo.gl/a"

    with pytest.raises(MapsUrlError) as error:
        extract_coordinates("https://goo.gl/a")

    assert error.value.reason == "link_unreachable"
    assert len(web.calls) == 2


def test_too_many_redirects_are_cut_at_the_limit(web):
    for index in range(20):
        web.table[f"https://goo.gl/{index}"] = f"https://goo.gl/{index + 1}"

    with pytest.raises(MapsUrlError) as error:
        extract_coordinates("https://goo.gl/0")

    assert error.value.reason == "link_unreachable"
    assert len(web.calls) == maps_url.MAX_REDIRECTS + 1


def test_redirect_chain_within_the_limit_succeeds(web):
    for index in range(maps_url.MAX_REDIRECTS - 1):
        web.table[f"https://goo.gl/{index}"] = f"https://goo.gl/{index + 1}"
    web.table[f"https://goo.gl/{maps_url.MAX_REDIRECTS - 1}"] = LONG_URL

    assert extract_coordinates("https://goo.gl/0") == pytest.approx((-15.7942287, -47.8821658))


@pytest.mark.parametrize(
    "error", [requests.ConnectionError("secret-host.internal refused"), requests.Timeout("slow"), requests.exceptions.SSLError("bad")],
)
def test_network_failures_become_a_generic_reason_without_details(error, web):
    web.error = error

    with pytest.raises(MapsUrlError) as raised:
        extract_coordinates("https://maps.app.goo.gl/abc")

    assert raised.value.reason == "link_unreachable"
    assert "secret" not in str(raised.value)


def test_short_link_that_does_not_redirect_has_no_coordinates(web):
    with pytest.raises(MapsUrlError) as error:
        extract_coordinates("https://maps.app.goo.gl/dead")

    assert error.value.reason == "no_coordinates"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/maps/place/Brasilia",
        "https://www.google.com/maps/place/Bras%C3%ADlia,+DF/",
        "https://www.google.com/maps?q=Brasilia",
        "https://www.google.com/maps/search/restaurants+near+me",
        "https://www.google.com/maps",
        "https://www.google.com/",
        "https://www.google.com/maps/place/Rua+15,+47+Centro",
    ],
)
def test_links_with_only_a_place_name_have_no_coordinates(url, web):
    with pytest.raises(MapsUrlError) as error:
        extract_coordinates(url)

    assert error.value.reason == "no_coordinates"
    assert web.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/maps/@-91.0,-47.9,12z",
        "https://www.google.com/maps/@-85.5,-47.9,12z",
        "https://www.google.com/maps/@85.1,10.0,12z",
        "https://www.google.com/maps?q=-15.5,-181",
        "https://www.google.com/maps?q=-15.5,200.5",
        "https://www.google.com/maps/place/X/data=!3d-15.5!4d-190.0",
    ],
)
def test_out_of_range_coordinates_are_rejected_with_the_values(url, web):
    with pytest.raises(MapsUrlError) as error:
        extract_coordinates(url)

    assert error.value.reason == "invalid_coordinates"
    assert error.value.latitude is not None and error.value.longitude is not None
