"""Google Maps link service — extracts a coordinate from a shared location link.

Accepts the link formats Google Maps produces when a location is shared
(``/maps/place/.../@lat,lon,zoom``, ``!3dLAT!4dLON`` pin data, ``?q=``,
``?ll=``, ``?query=``, ``/maps/search/lat,lon``, ``q=loc:lat,lon``, a bare
``@lat,lon``) and resolves short links (``maps.app.goo.gl``, ``goo.gl/maps``,
``g.co``) by following their redirects by hand.

Security (SSRF): only ``https`` URLs on Google hosts are accepted, the host is
checked again on every redirect hop, at most ``MAX_REDIRECTS`` hops are
followed, connection and read timeouts are short and no response body is ever
downloaded (``stream=True`` and the response is closed at once). Literal IPs,
``localhost``, credentials and custom ports never pass the validation. Errors
carry only a reason code: the caller renders a fixed text, so no network
detail reaches the model.
"""

import math
import re
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import requests

from semente.logging import log_warning

from domain.services.geospatial.geometry import is_valid_coordinate

MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_QUERY_KEYS = ("q", "ll", "query", "center", "destination", "daddr")
_USER_AGENT = "Pasto10/1.0 (+https://github.com/lapig-ufg/pasto10)"

_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.co"}
_GOOGLE_HOST = re.compile(r"^(?:www\.|maps\.)?google\.(?:com|com?\.[a-z]{2}|[a-z]{2})$")

_NUMBER = r"[+-]?\d{1,3}(?:\.\d+)?"
_PAIR = re.compile(rf"^\s*(?:loc:)?\s*({_NUMBER})\s*[,\s]\s*({_NUMBER})\s*$", re.IGNORECASE)
_PIN = re.compile(rf"!3d({_NUMBER})!4d({_NUMBER})")
_AT = re.compile(rf"@({_NUMBER}),\+?({_NUMBER})")


class MapsUrlError(Exception):
    """A Google Maps link could not be turned into a coordinate.

    Attributes:
        reason (str): ``unsupported_link`` (not a valid ``https`` Google URL, host not allowed
            or redirected to one), ``link_unreachable`` (network failure, timeout, redirect
            loop or too many redirects), ``no_coordinates`` (valid link without a coordinate)
            or ``invalid_coordinates`` (coordinate out of range).
        latitude (float, optional): Offending latitude, for ``invalid_coordinates``.
        longitude (float, optional): Offending longitude, for ``invalid_coordinates``.
    """

    def __init__(self, reason: str, latitude: Optional[float] = None, longitude: Optional[float] = None):
        super().__init__(reason)
        self.reason = reason
        self.latitude = latitude
        self.longitude = longitude


def _clean(url: str) -> str:
    """Trims the user text around a URL (spaces, angle brackets, quotes, trailing punctuation).

    Args:
        url (str): Raw text received from the model.

    Returns:
        str: The trimmed URL.

    Raises:
        MapsUrlError: ``unsupported_link`` for non-text, empty, oversized or control-character input.
    """
    if not isinstance(url, str):
        raise MapsUrlError("unsupported_link")
    text = url.strip().lstrip("<(\"'").rstrip("<>)\"'.,;").strip()
    if not text or len(text) > MAX_URL_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise MapsUrlError("unsupported_link")
    return text


def _parse_allowed(url: str):
    """Parses a URL and checks scheme and host against the allow-list.

    Args:
        url (str): Absolute URL.

    Returns:
        tuple: ``(SplitResult, is_short_link)``.

    Raises:
        MapsUrlError: ``unsupported_link`` when the scheme is not ``https``, there are
        credentials or a non-default port, or the host is not a Google Maps host.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        raise MapsUrlError("unsupported_link")
    if parts.scheme.lower() != "https" or parts.username is not None or port not in (None, 443):
        raise MapsUrlError("unsupported_link")
    if host in _SHORT_HOSTS:
        return parts, True
    if _GOOGLE_HOST.match(host):
        return parts, False
    raise MapsUrlError("unsupported_link")


def _to_float_pair(latitude: str, longitude: str) -> Optional[tuple[float, float]]:
    """Converts two numeric strings, returning None when either is not finite."""
    try:
        lat, lon = float(latitude), float(longitude)
    except ValueError:
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) else None


def _find_coordinates(parts) -> Optional[tuple[float, float]]:
    """Looks for a coordinate in a parsed Google Maps URL, most precise source first.

    Order: ``!3d<lat>!4d<lon>`` pin data, the ``q``/``ll``/``query``/``center``/
    ``destination``/``daddr`` parameters (``%2C``, spaces, ``+`` and ``loc:`` accepted),
    ``@lat,lon`` (map center, with or without zoom) and finally a path segment that
    is a bare ``lat,lon`` (``/maps/search/-15.78,-47.93``).

    Args:
        parts (SplitResult): The parsed URL.

    Returns:
        tuple | None: ``(latitude, longitude)`` as found (not range-checked), or None.
    """
    path = unquote(parts.path)
    pin = _PIN.search(path)
    if pin:
        return _to_float_pair(*pin.groups())

    query = parse_qs(parts.query)
    for key in _QUERY_KEYS:
        for value in query.get(key, []):
            match = _PAIR.match(value)
            if match:
                return _to_float_pair(*match.groups())

    at = _AT.search(path)
    if at:
        return _to_float_pair(*at.groups())

    for segment in path.split("/"):
        match = _PAIR.match(segment.replace(",+", ",") if "," in segment else segment.replace("+", " "))
        if match:
            return _to_float_pair(*match.groups())
    return None


def _next_location(url: str) -> Optional[str]:
    """Requests a short link without following redirects and returns its ``Location``.

    The body is never downloaded (``stream=True``) and the response is closed
    straight away.

    Args:
        url (str): An already validated URL.

    Returns:
        str | None: The absolute redirect target, or None when the response is not a redirect.

    Raises:
        MapsUrlError: ``link_unreachable`` on any network error or timeout.
    """
    try:
        response = requests.get(
            url, allow_redirects=False, stream=True, headers={"User-Agent": _USER_AGENT},
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
    except requests.RequestException as error:
        log_warning(f"maps_url: request failed ({type(error).__name__})")
        raise MapsUrlError("link_unreachable")
    try:
        location = response.headers.get("Location")
        if response.status_code in _REDIRECT_STATUSES and location:
            return urljoin(url, location)
        return None
    finally:
        response.close()


def extract_coordinates(url: str) -> tuple[float, float]:
    """Extracts the coordinate a Google Maps link points to.

    Long links are parsed without any network access. Short links
    (``maps.app.goo.gl``, ``goo.gl``, ``g.co``) are resolved by following up to
    ``MAX_REDIRECTS`` redirects manually; scheme and host are validated on the
    first URL and on every hop, and the coordinate is taken from the first hop
    that carries one. A place name without coordinates is never geocoded.

    Args:
        url (str): The link as sent by the user.

    Returns:
        tuple[float, float]: ``(latitude, longitude)`` in decimal degrees, finite and
        within ``|lat| <= 85``, ``|lon| <= 180``.

    Raises:
        MapsUrlError: With ``reason`` ``unsupported_link``, ``link_unreachable``,
        ``no_coordinates`` or ``invalid_coordinates`` (see the class). Details of
        network failures are only logged.
    """
    current = _clean(url)
    visited = set()
    for _ in range(MAX_REDIRECTS + 1):
        parts, is_short = _parse_allowed(current)
        found = _find_coordinates(parts)
        if found is not None:
            if not is_valid_coordinate(*found):
                log_warning("maps_url: coordinate out of range in link")
                raise MapsUrlError("invalid_coordinates", latitude=found[0], longitude=found[1])
            return found
        if not is_short:
            raise MapsUrlError("no_coordinates")
        if current in visited:
            raise MapsUrlError("link_unreachable")
        visited.add(current)
        target = _next_location(current)
        if target is None:
            raise MapsUrlError("no_coordinates")
        current = target
    raise MapsUrlError("link_unreachable")
