"""Tests of the user polygon parser (domain/services/property_sources/user_polygon_source.py)."""

import json

import pytest

from domain.services.property_sources.user_polygon_source import DEFAULT_MAX_VERTICES, InvalidPolygon, parse_geojson

SQUARE = {"type": "Polygon", "coordinates": [[[-47.51, -15.51], [-47.49, -15.51], [-47.49, -15.49], [-47.51, -15.49], [-47.51, -15.51]]]}


def _feature(geometry, **props):
    return {"type": "Feature", "properties": props, "geometry": geometry}


@pytest.mark.parametrize(
    "document",
    [SQUARE, _feature(SQUARE), {"type": "FeatureCollection", "features": [_feature(SQUARE)]}],
    ids=["geometry", "feature", "collection"],
)
def test_accepts_geometry_feature_and_collection(document):
    candidate = parse_geojson(json.dumps(document))

    assert candidate.origin.value == "user_polygon" and candidate.source == "user" and candidate.distance_m is None
    assert candidate.area_ha > 400


def test_area_is_computed_in_hectares_with_an_equal_area_projection():
    candidate = parse_geojson(json.dumps(SQUARE).encode())

    assert candidate.area_ha == pytest.approx(0.02 * 111.32 * 0.02 * 111.32 * 0.9637 * 100, rel=0.01)


def test_largest_polygon_of_a_collection_is_used():
    small = {"type": "Polygon", "coordinates": [[[-47.50, -15.50], [-47.499, -15.50], [-47.499, -15.499], [-47.50, -15.499], [-47.50, -15.50]]]}
    document = {"type": "FeatureCollection", "features": [_feature(small), _feature(SQUARE), _feature({"type": "Point", "coordinates": [-47.5, -15.5]})]}

    assert parse_geojson(json.dumps(document)).area_ha > 400


def test_a_self_intersecting_ring_is_repaired():
    bowtie = {"type": "Polygon", "coordinates": [[[-47.51, -15.51], [-47.49, -15.49], [-47.49, -15.51], [-47.51, -15.49], [-47.51, -15.51]]]}

    candidate = parse_geojson(json.dumps(bowtie))

    assert candidate.area_ha > 0 and candidate.geometry["type"] in ("Polygon", "MultiPolygon")


@pytest.mark.parametrize(
    "content, kind",
    [
        ("not json", "invalid_json"),
        ("[1, 2]", "invalid_json"),
        (json.dumps({"type": "Point", "coordinates": [1, 2]}), "not_polygon"),
        (json.dumps({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}), "not_polygon"),
        (json.dumps({"type": "FeatureCollection", "features": []}), "invalid_json"),
        (json.dumps({"type": "Polygon", "coordinates": [[[500000, 8000000], [500100, 8000000], [500100, 8000100], [500000, 8000100], [500000, 8000000]]]}), "invalid_coordinates"),
        (json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]}), "not_polygon"),
    ],
)
def test_invalid_documents_are_rejected_with_a_kind(content, kind):
    with pytest.raises(InvalidPolygon) as error:
        parse_geojson(content)

    assert error.value.kind == kind


def test_area_and_vertex_limits():
    with pytest.raises(InvalidPolygon) as large:
        parse_geojson(json.dumps(SQUARE), max_area_ha=100)
    tiny = {"type": "Polygon", "coordinates": [[[-47.5, -15.5], [-47.49999, -15.5], [-47.49999, -15.49999], [-47.5, -15.5]]]}
    with pytest.raises(InvalidPolygon) as small:
        parse_geojson(json.dumps(tiny))
    ring = [[-47.5 + i * 1e-7, -15.5 + (1e-7 if i % 2 else 0)] for i in range(DEFAULT_MAX_VERTICES + 5)]
    with pytest.raises(InvalidPolygon) as many:
        parse_geojson(json.dumps({"type": "Polygon", "coordinates": [ring + [ring[0]]]}))

    assert (large.value.kind, small.value.kind, many.value.kind) == ("too_large", "too_small", "too_many_vertices")


def test_area_limit_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PASTO10_MAX_POLYGON_HA", "100")

    with pytest.raises(InvalidPolygon):
        parse_geojson(json.dumps(SQUARE))


# --- hostile input: rejected quickly, in layers ----------------------------------------------------------------


def _zigzag(vertices: int) -> dict:
    """A self-intersecting ring (a zig-zag crossing itself) with ``vertices`` positions."""
    ring = [[-47.5 + (i % 2) * 0.01, -15.5 + i * 1e-5] for i in range(vertices)]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def _timed(content):
    import time

    start = time.perf_counter()
    with pytest.raises(InvalidPolygon) as error:
        parse_geojson(content)
    return error.value.kind, time.perf_counter() - start


@pytest.mark.parametrize("vertices", [500, 1000])
def test_large_self_intersecting_polygons_are_refused_without_being_repaired(vertices):
    kind, seconds = _timed(json.dumps(_zigzag(vertices), separators=(",", ":")))

    assert kind == "too_complex" and seconds < 2.0


def test_many_features_are_refused_before_any_geometry_work():
    features = [{"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}} for _ in range(300)]
    document = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))

    kind, seconds = _timed(document)

    assert kind == "too_many_features" and seconds < 2.0


def test_thousands_of_holes_are_refused_before_any_geometry_work():
    holes = [[[-47.5 + (i % 40) * 0.001, -15.5 + (i // 40) * 0.001 + d, ] for d in (0, 0.0002, 0.0004, 0)] for i in range(2000)]
    outer = [[-47.6, -15.6], [-47.3, -15.6], [-47.3, -15.3], [-47.6, -15.3], [-47.6, -15.6]]
    document = json.dumps({"type": "Polygon", "coordinates": [outer] + holes}, separators=(",", ":"))

    kind, seconds = _timed(document)

    assert kind == "too_many_rings" and seconds < 2.0


def test_the_guard_process_is_killed_when_it_takes_too_long(monkeypatch):
    import time

    from domain.services.property_sources import user_polygon_source as source

    monkeypatch.setattr(source, "GUARD_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(source, "GUARD_SCRIPT", source.Path(__file__).with_name("sleeping_guard.py"))
    (source.Path(__file__).with_name("sleeping_guard.py")).write_text("import time\ntime.sleep(30)\n")
    try:
        start = time.perf_counter()
        with pytest.raises(InvalidPolygon) as error:
            parse_geojson(json.dumps(SQUARE))
        elapsed = time.perf_counter() - start
    finally:
        (source.Path(__file__).with_name("sleeping_guard.py")).unlink()

    assert error.value.kind == "too_complex" and elapsed < 2.0


def test_a_valid_polygon_at_the_vertex_limit_is_still_accepted():
    import math

    points = [[-47.5 + 0.02 * math.cos(2 * math.pi * i / 1900), -15.5 + 0.02 * math.sin(2 * math.pi * i / 1900)] for i in range(1900)]
    document = json.dumps({"type": "Polygon", "coordinates": [points + [points[0]]]}, separators=(",", ":"))

    candidate = parse_geojson(document)

    assert 400 < candidate.area_ha < 1600


def test_a_small_self_intersecting_polygon_is_repaired():
    bowtie = {"type": "Polygon", "coordinates": [[[-47.51, -15.51], [-47.49, -15.49], [-47.49, -15.51], [-47.51, -15.49], [-47.51, -15.51]]]}

    assert parse_geojson(json.dumps(bowtie)).area_ha > 0


def test_the_size_limit_counts_utf8_bytes_not_characters():
    content = '{"type":"Point","coordinates":[0,0],"note":"' + "ã" * 600_000 + '"}'

    assert len(content) < 1_000_000 < len(content.encode("utf-8"))
    with pytest.raises(InvalidPolygon) as error:
        parse_geojson(content)

    assert error.value.kind == "file_too_large"


def test_the_vertex_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("PASTO10_MAX_POLYGON_VERTICES", "6")
    ring = [[-47.5 + i * 0.001, -15.5 + (0.001 if i % 2 else 0)] for i in range(8)]

    with pytest.raises(InvalidPolygon) as error:
        parse_geojson(json.dumps({"type": "Polygon", "coordinates": [ring + [ring[0]]]}))

    assert error.value.kind == "too_many_vertices"
    monkeypatch.setenv("PASTO10_MAX_POLYGON_VERTICES", "oops")
    assert parse_geojson(json.dumps(SQUARE)).area_ha > 0


@pytest.mark.parametrize(
    "coordinates",
    ["[[NaN, 1], [2, 2], [3, 1], [NaN, 1]]", "[[Infinity, 1], [2, 2], [3, 1], [1, 1]]", "[[1e999, 1], [2, 2], [3, 1], [1, 1]]", "[[-1e999, 1], [2, 2], [3, 1], [1, 1]]"],
)
def test_non_finite_numbers_are_rejected_without_a_geos_exception(coordinates):
    kind, seconds = _timed('{"type":"Polygon","coordinates":[' + coordinates + "]}")

    assert kind in ("invalid_json", "invalid_coordinates") and seconds < 2.0


@pytest.mark.parametrize("position", ["[500, 1]", "[1, 95]", "[true, 1]", '["1", 2]', "[1]"])
def test_out_of_range_or_malformed_positions_are_invalid_coordinates(position):
    document = '{"type":"Polygon","coordinates":[[' + position + ",[2,2],[3,1]," + position + "]]}"

    kind, _ = _timed(document)

    assert kind == "invalid_coordinates"


def test_geos_failures_in_the_guard_become_invalid_geometry():
    from domain.services.property_sources import polygon_guard

    answer = polygon_guard.validate({
        "geometries": [{"type": "Polygon", "coordinates": [[[0, 0]]]}], "max_area_ha": 10, "min_area_ha": 0.01, "simplify_m": 5, "max_repair_vertices": 100,
    })

    assert answer == {"ok": False, "kind": "invalid_geometry"}


@pytest.mark.parametrize("value, expected", [("100", 100.0), ("oops", 50_000.0), ("-5", 50_000.0), ("0", 50_000.0)])
def test_the_area_limit_from_the_environment_is_validated(monkeypatch, value, expected):
    from domain.services.property_sources.user_polygon_source import max_polygon_ha

    monkeypatch.setenv("PASTO10_MAX_POLYGON_HA", value)

    assert max_polygon_ha() == expected
    monkeypatch.delenv("PASTO10_MAX_POLYGON_HA")
    assert max_polygon_ha() == 50_000.0


def test_repair_limit_is_a_total_budget_over_all_invalid_features():
    features = [_feature(_zigzag(99), index=i) for i in range(20)]
    document = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))

    kind, seconds = _timed(document)

    assert kind == "too_complex" and seconds < 2.0


def test_guard_runs_isolated_and_only_imports_the_core():
    import subprocess
    import sys

    from domain.services.property_sources import user_polygon_source as source

    code = (
        "import sys, runpy; sys.argv=['g']; "
        f"ns = runpy.run_path({str(source.GUARD_SCRIPT)!r}, run_name='guard'); "
        "bad = [m for m in ('base', 'registry', 'domain', 'semente') if m in sys.modules]; print(bad)"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)

    assert done.stdout.strip() == "[]", done.stderr
    assert source._run_guard([SQUARE], 50_000.0)["ok"] is True


def test_guard_concurrency_is_bounded(monkeypatch):
    import threading
    import time

    from domain.services.property_sources import user_polygon_source as source

    running, peak, lock = 0, 0, threading.Lock()

    class _Done:
        stdout = '{"ok": false, "kind": "too_small"}'

    def fake_run(*args, **kwargs):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return _Done()

    monkeypatch.setattr(source.subprocess, "run", fake_run)
    monkeypatch.setattr(source, "_GUARD_SLOTS", threading.BoundedSemaphore(2))
    threads = [threading.Thread(target=source._run_guard, args=([SQUARE], 1.0)) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert peak == 2


def test_guard_failures_map_to_invalid_geometry(monkeypatch):
    from domain.services.property_sources import user_polygon_source as source

    monkeypatch.setattr(source.sys, "executable", None)
    assert source._run_guard([SQUARE], 1.0) == {"ok": False, "kind": "invalid_geometry"}


def test_guard_output_with_nan_is_rejected(monkeypatch):
    from domain.services.property_sources import user_polygon_source as source

    class _Done:
        stdout = '{"ok": true, "geometry": {}, "area_ha": NaN}'

    monkeypatch.setattr(source.subprocess, "run", lambda *a, **k: _Done())
    assert source._run_guard([SQUARE], 1.0) == {"ok": False, "kind": "invalid_geometry"}
