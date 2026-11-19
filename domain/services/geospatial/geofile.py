"""Registration of a rural property from a geospatial file.

The user-facing intake formats (shapefile ``.zip``/``.rar``, KMZ, KML and
GeoJSON) are converted to a single GeoJSON ``FeatureCollection`` by Semente's
input step (``semente.services.geospatial.geojson_io.convert_geo_file_to_geojson``);
this module turns any of those documents into the Semente ``PropertyCandidate``
of a **rural property**.

The converted document is always one feature whose geometry is a Polygon or a
MultiPolygon, so a file with several paddocks becomes a single rural property
(the union of its parts), which is the area Pasto10 analyzes. The content then
goes through the same hardened parser as a pasted polygon
(``polygon_input.parse_geojson``), with limits and a guard timeout suited
to real files instead of hostile pasted JSON.
"""

import os
import re
from pathlib import Path
from typing import Optional, Union

from semente.property_sources import GeometryOrigin, PropertyCandidate
from semente.services.geospatial.geojson_io import (
    GeoFileError,
    UnsupportedFormatError,
    convert_geo_file_to_geojson,
    geopandas_import_available,
)

from domain.services.geospatial.polygon_input import InvalidPolygon, parse_geojson

MAX_GEOFILE_MB_ENV = "PASTO10_MAX_GEOFILE_MB"
MAX_GEOFILE_VERTICES_ENV = "PASTO10_MAX_GEOFILE_VERTICES"
MAX_GEOFILE_REPAIR_VERTICES_ENV = "PASTO10_MAX_GEOFILE_REPAIR_VERTICES"
GEOFILE_TIMEOUT_ENV = "PASTO10_GEOFILE_TIMEOUT"
DEFAULT_MAX_GEOFILE_MB = 25
DEFAULT_MAX_GEOFILE_VERTICES = 200_000
DEFAULT_MAX_GEOFILE_REPAIR_VERTICES = 200_000
DEFAULT_GEOFILE_TIMEOUT_SECONDS = 15.0
# The converted document is not bounded by the archive size (a small zip can
# expand a lot), so it has its own, larger ceiling before the JSON is parsed.
MAX_CONVERTED_BYTES = 64 * 1024 * 1024
SUPPORTED_EXTENSIONS = (".zip", ".rar", ".kmz", ".kml", ".geojson", ".json")
MAX_SOURCE_NAME_LENGTH = 120
RURAL_PROPERTY_FEATURE_TYPE = "rural_property"


def _positive_int(env: str, default: int) -> int:
    """Reads a positive integer from the environment (``default`` when absent or invalid)."""
    try:
        value = int(os.getenv(env) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_float(env: str, default: float) -> float:
    """Reads a positive float from the environment (``default`` when absent or invalid)."""
    try:
        value = float(os.getenv(env) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def max_geofile_mb() -> int:
    """Returns the upload size limit in MB (``PASTO10_MAX_GEOFILE_MB`` when valid, else 25)."""
    return _positive_int(MAX_GEOFILE_MB_ENV, DEFAULT_MAX_GEOFILE_MB)


def max_geofile_bytes() -> int:
    """Returns the upload size limit in bytes."""
    return max_geofile_mb() * 1024 * 1024


def max_geofile_vertices() -> int:
    """Returns the vertex limit of a converted file (``PASTO10_MAX_GEOFILE_VERTICES`` when valid, else 200 000)."""
    return _positive_int(MAX_GEOFILE_VERTICES_ENV, DEFAULT_MAX_GEOFILE_VERTICES)


def max_geofile_repair_vertices() -> int:
    """Returns the total repair budget of a converted file (``PASTO10_MAX_GEOFILE_REPAIR_VERTICES``)."""
    return _positive_int(MAX_GEOFILE_REPAIR_VERTICES_ENV, DEFAULT_MAX_GEOFILE_REPAIR_VERTICES)


def geofile_timeout_seconds() -> float:
    """Returns the guard timeout of a converted file (``PASTO10_GEOFILE_TIMEOUT`` when valid, else 15 s)."""
    return _positive_float(GEOFILE_TIMEOUT_ENV, DEFAULT_GEOFILE_TIMEOUT_SECONDS)


def is_supported_geofile(filename: str) -> bool:
    """Whether ``filename`` has one of the extensions Semente's geo intake converts."""
    return Path(filename or "").suffix.lower() in SUPPORTED_EXTENSIONS


def source_name(filename: str) -> str:
    """Returns a safe, length-limited display name for the uploaded file.

    The filename is untrusted: only the basename is kept and characters outside
    ``[A-Za-z0-9._ -]`` become ``_``, so it cannot inject text into a prompt or
    a log line.
    """
    name = Path(filename or "property").name.strip() or "property"
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    return name[:MAX_SOURCE_NAME_LENGTH]


def _attributes(name: str) -> dict:
    """Attributes carried by a geofile candidate (the file name and its feature type)."""
    return {"source_file": name, "feature_type": RURAL_PROPERTY_FEATURE_TYPE}


def parse_converted_geojson(content: Union[bytes, str], filename: str) -> PropertyCandidate:
    """Turns a GeoJSON already converted by Semente's input step into a rural-property candidate.

    Args:
        content: GeoJSON text (bytes or str) of the converted document.
        filename: Name of the converted file (used for the display name only).

    Returns:
        PropertyCandidate: Origin ``user_polygon`` with the ``rural_property``
        feature type in its attributes, source ``user``, named after the file
        stem, with the file name in the ``source_file`` attribute.

    Raises:
        InvalidPolygon: The kinds of ``polygon_input.parse_geojson``.
    """
    name = source_name(filename)
    return parse_geojson(
        content,
        max_bytes=MAX_CONVERTED_BYTES,
        max_vertices=max_geofile_vertices(),
        max_repair_vertices=max_geofile_repair_vertices(),
        timeout_seconds=geofile_timeout_seconds(),
        origin=GeometryOrigin.USER_POLYGON,
        candidate_id="geofile",
        name=Path(name).stem or "property",
        attributes=_attributes(name),
    )


def parse_geofile(content: Union[bytes, bytearray], filename: str) -> PropertyCandidate:
    """Converts a geospatial document and turns it into a rural-property candidate.

    Args:
        content: Raw bytes of the uploaded file.
        filename: Original filename (its extension chooses the reader).

    Returns:
        PropertyCandidate: Origin ``user_polygon`` with the ``rural_property``
        feature type in its attributes, source ``user``, named after the file
        stem, with the file name in the ``source_file`` attribute. A file with
        several polygons becomes one MultiPolygon property (the union of the
        parts).

    Raises:
        InvalidPolygon: ``geo_stack_missing`` when the geo extra is not installed,
            ``file_too_large`` (upload or converted document), ``unsupported_format``
            (not a geo-intake extension), ``unreadable_file`` (corrupted or
            unreadable document, or no polygon inside), or one of the geometry
            kinds of ``polygon_input`` (``too_large``, ``too_many_vertices``,
            ``too_complex``...).
    """
    name = source_name(filename)
    if not is_supported_geofile(filename):
        raise InvalidPolygon("unsupported_format", f"unsupported extension for {name!r}")
    size = len(content)
    if size > max_geofile_bytes():
        raise InvalidPolygon("file_too_large", f"{size} bytes > {max_geofile_bytes()}")
    if not geopandas_import_available():
        raise InvalidPolygon("geo_stack_missing", "geopandas is not installed")

    try:
        converted = convert_geo_file_to_geojson(bytes(content), name)
    except UnsupportedFormatError as error:
        raise InvalidPolygon("unsupported_format", str(error)) from error
    except GeoFileError as error:
        raise InvalidPolygon("unreadable_file", str(error)) from error
    except ImportError as error:  # pragma: no cover - defensive (the extra is a dependency)
        raise InvalidPolygon("geo_stack_missing", str(error)) from error
    except Exception as error:  # pragma: no cover - defensive
        raise InvalidPolygon("unreadable_file", f"{type(error).__name__}: {error}") from error

    return parse_converted_geojson(converted, name)


def attached_geofile(filename: str, content: bytes) -> Optional[PropertyCandidate]:
    """Parses an attached file that is already GeoJSON or still a raw geofile.

    Args:
        filename: Original filename of the attachment.
        content: Raw bytes of the attachment.

    Returns:
        PropertyCandidate | None: The candidate, or None when the file is not a
        supported geofile (the caller then reports a missing boundary).
    """
    if not is_supported_geofile(filename):
        return None
    if Path(filename).suffix.lower() in (".geojson", ".json"):
        return parse_converted_geojson(content, filename)
    return parse_geofile(content, filename)
