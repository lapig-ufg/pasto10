"""Tests of the OpenStreetMap property source (domain/services/property_sources/osm_source.py).

Offline: ``get_overpass`` (or ``requests.post``) is mocked and answers come from
``tests/fixtures/overpass/synthetic_*.json`` (empty, residential, several
candidates, a relation with a hole, a giant and a tiny polygon around -15.5, -47.5).
"""

import json
from pathlib import Path

import pytest
import requests

from domain.services.property_sources import osm_source
from domain.services.property_sources.base import PropertySourceError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "overpass"
LAT, LON = -15.5, -47.5


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"synthetic_{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(osm_source.CACHE_DIR_ENV, raising=False)
    monkeypatch.delenv(osm_source.URLS_ENV, raising=False)
    osm_source._cache.clear()
    yield
    osm_source._cache.clear()


@pytest.fixture
def serve(monkeypatch):
    calls = []

    def serve(name):
        monkeypatch.setattr(osm_source, "get_overpass", lambda query: calls.append(query) or load(name))
        return calls

    return serve


def test_empty_answer_gives_no_candidates(serve):
    serve("empty")

    assert osm_source.OsmSource().find_by_point(LAT, LON) == []


def test_point_inside_a_residential_land_use_is_an_urban_area(serve):
    serve("residential")

    with pytest.raises(PropertySourceError) as error:
        osm_source.OsmSource().find_by_point(LAT, LON)

    assert error.value.kind == "urban_area"


def test_a_farm_polygon_containing_the_point_wins_over_the_urban_land_use(serve):
    serve("residential_with_farm")

    candidates = osm_source.OsmSource().find_by_point(LAT, LON)

    assert [c.candidate_id for c in candidates] == ["way/2"] and candidates[0].distance_m == 0


def test_candidates_are_ordered_filtered_and_limited(serve):
    serve("several")

    candidates = osm_source.OsmSource().find_by_point(LAT, LON)

    assert len(candidates) == 5
    assert candidates[0].candidate_id == "way/10" and candidates[0].distance_m == 0
    distances = [c.distance_m for c in candidates[1:]]
    assert distances == sorted(distances) and all(d > 0 for d in distances)
    assert "way/16" not in [c.candidate_id for c in osm_source.OsmSource(max_candidates=10).find_by_point(LAT, LON)]


def test_candidate_carries_origin_name_tags_license_and_warnings(serve):
    serve("several")

    candidates = {c.candidate_id: c for c in osm_source.OsmSource(max_candidates=10).find_by_point(LAT, LON)}

    farm = candidates["way/10"]
    assert farm.origin.value == "osm_landuse" and farm.source == "osm" and farm.name == "Fazenda Central"
    assert {(a.key, a.value) for a in farm.attributes} >= {("osm_landuse", "farmland"), ("osm_crop", "soy")}
    assert farm.source_date == "2026-10-20T12:00:00Z" and "OpenStreetMap" in farm.license
    assert "may_not_be_a_farm" in farm.warnings and "does_not_contain_point" not in farm.warnings
    assert "may_not_be_a_farm" not in candidates["way/12"].warnings and "does_not_contain_point" in candidates["way/12"].warnings
    assert 45 < farm.area_ha < 51


def test_relation_merges_outer_ways_subtracts_inner_rings_and_drops_open_rings(serve):
    serve("relation_hole")

    [candidate] = osm_source.OsmSource().find_by_point(LAT, LON)

    assert candidate.candidate_id == "relation/77" and candidate.geometry["type"] == "Polygon"
    assert len(candidate.geometry["coordinates"]) == 2
    assert candidate.area_ha == pytest.approx(100 - 9, rel=0.05)


def test_area_limits_are_configurable(serve):
    serve("giant")
    assert osm_source.OsmSource().find_by_point(LAT, LON) == []
    assert len(osm_source.OsmSource(max_area_ha=100_000).find_by_point(LAT, LON)) == 1
    osm_source._cache.clear()
    serve("tiny")
    assert osm_source.OsmSource().find_by_point(LAT, LON) == []
    assert len(osm_source.OsmSource(min_area_ha=0.01).find_by_point(LAT, LON)) == 1


def test_query_asks_for_geometry_tags_and_the_urban_land_use():
    query = osm_source.build_query(LAT, LON, 1500)

    for expected in ("[out:json]", "around:1500", "farmland|meadow|farmyard|grass|orchard|vineyard", '"place"="farm"', "residential|commercial|industrial|retail", "out geom tags"):
        assert expected in query


def test_answers_are_cached_per_rounded_coordinate(serve):
    calls = serve("several")
    source = osm_source.OsmSource()

    source.find_by_point(LAT, LON)
    source.find_by_point(LAT + 0.0001, LON - 0.0001)
    source.find_by_point(LAT + 1, LON)

    assert len(calls) == 2


def test_disk_cache_serves_a_cold_memory(serve, monkeypatch, tmp_path):
    monkeypatch.setenv(osm_source.CACHE_DIR_ENV, str(tmp_path))
    calls = serve("several")
    osm_source.OsmSource().find_by_point(LAT, LON)
    osm_source._cache.clear()

    osm_source.OsmSource().find_by_point(LAT, LON)

    assert len(calls) == 1 and len(list(tmp_path.glob("*.json"))) == 1


class _Response:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_get_overpass_falls_back_to_the_second_mirror_with_an_identifiable_agent(monkeypatch):
    seen = []

    def post(url, data, headers, timeout):
        seen.append((url, headers["User-Agent"], timeout))
        return _Response(502) if len(seen) == 1 else _Response(200, {"elements": []})

    monkeypatch.setattr(requests, "post", post)

    assert osm_source.get_overpass("q") == {"elements": []}
    assert [url for url, _, _ in seen] == list(osm_source.OVERPASS_URLS) and "pasto10" in seen[0][1]
    assert seen[0][2] == (5, 30)


@pytest.mark.parametrize(
    "outcome, kind",
    [
        (requests.Timeout("slow"), "timeout"),
        (_Response(502), "unavailable"),
        (_Response(429), "unavailable"),
        (_Response(200), "unavailable"),
        (requests.ConnectionError("down"), "unavailable"),
    ],
)
def test_all_mirrors_failing_raises_a_typed_error(monkeypatch, outcome, kind):
    def post(url, data, headers, timeout):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(requests, "post", post)

    with pytest.raises(PropertySourceError) as error:
        osm_source.get_overpass("q")

    assert error.value.kind == kind


def test_timeout_can_be_configured(monkeypatch):
    assert osm_source._timeout() == 30
    monkeypatch.setenv(osm_source.TIMEOUT_ENV, "12.5")
    assert osm_source._timeout() == 12.5
    monkeypatch.setenv(osm_source.TIMEOUT_ENV, "oops")
    assert osm_source._timeout() == 30


def test_mirrors_can_be_configured(monkeypatch):
    monkeypatch.setenv(osm_source.URLS_ENV, "https://a.example/api, https://b.example/api")

    assert osm_source._urls() == ("https://a.example/api", "https://b.example/api")


def test_source_failure_is_not_cached(monkeypatch):
    attempts = []

    def get(query):
        attempts.append(1)
        if len(attempts) == 1:
            raise PropertySourceError("unavailable")
        return load("empty")

    monkeypatch.setattr(osm_source, "get_overpass", get)

    with pytest.raises(PropertySourceError):
        osm_source.OsmSource().find_by_point(LAT, LON)
    assert osm_source.OsmSource().find_by_point(LAT, LON) == []


def test_find_by_id_reads_an_element(monkeypatch):
    monkeypatch.setattr(osm_source, "get_overpass", lambda query: {"osm3s": {}, "elements": [load("several")["elements"][0]]})

    candidate = osm_source.OsmSource().find_by_id("way/10")

    assert candidate.candidate_id == "way/10" and candidate.name == "Fazenda Central"
    assert osm_source.OsmSource().find_by_id("node/1") is None and osm_source.OsmSource().find_by_id("garbage") is None


def test_assemble_rings_joins_in_any_direction_and_drops_open_chains():
    square = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
    pieces = [square[:3], square[2:][::-1][::-1], [(5, 5), (6, 6), (7, 5)]]

    rings = osm_source.assemble_rings(pieces)

    assert len(rings) == 1 and rings[0][0] == rings[0][-1]


# --- real Overpass answers recorded by tools/measure_osm_coverage.py (trimmed) -------------------------------------


def load_real(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["goias", "beni", "chaco", "llanos_colombia", "kansas", "queensland", "desert_control"])
def test_real_empty_answers_from_regions_without_mapped_land_use(monkeypatch, name):
    monkeypatch.setattr(osm_source, "get_overpass", lambda query: load_real(name))
    lat, lon = {"goias": (-15.5, -47.5), "beni": (-14.0, -65.5), "chaco": (-22.5, -59.5), "llanos_colombia": (4.5, -72.0), "kansas": (38.5, -99.0), "queensland": (-27.5, 150.0), "desert_control": (25.0, 10.0)}.get(name, (0, 0))

    assert osm_source.OsmSource().find_by_point(lat, lon) == []


def test_real_answer_santa_cruz_gives_a_farm_and_a_farmyard(monkeypatch):
    monkeypatch.setattr(osm_source, "get_overpass", lambda query: load_real("santa_cruz"))

    candidates = osm_source.OsmSource(min_area_ha=0.5).find_by_point(-15.5, -60.5)

    assert len(candidates) == 2 and {a.value for c in candidates for a in c.attributes if a.key in ("osm_landuse", "osm_place")} == {"farm", "farmyard"}
    assert all(c.origin.value == "osm_landuse" and 0.5 < c.area_ha < 10 for c in candidates)
    assert candidates[0].distance_m <= candidates[1].distance_m
    assert "may_not_be_a_farm" not in next(c for c in candidates if any(a.value == "farm" for a in c.attributes)).warnings


def test_real_answer_normandy_is_ordered_and_limited(monkeypatch):
    monkeypatch.setattr(osm_source, "get_overpass", lambda query: load_real("real_normandy"))

    candidates = osm_source.OsmSource().find_by_point(49.2764, -0.7024)

    assert 1 <= len(candidates) <= 5
    assert [c.distance_m for c in candidates] == sorted(c.distance_m for c in candidates)
    assert all(c.to_feature().total_area == pytest.approx(c.area_ha, abs=0.01) for c in candidates)


def test_real_answer_city_control_returns_parks_without_claiming_a_farm(monkeypatch):
    monkeypatch.setattr(osm_source, "get_overpass", lambda query: load_real("city_control"))

    candidates = osm_source.OsmSource().find_by_point(-12.05, -77.04)

    assert candidates and all("may_not_be_a_farm" in c.warnings for c in candidates)


def test_the_query_is_built_from_the_rounded_coordinate_that_keys_the_cache(monkeypatch):
    queries = []
    monkeypatch.setattr(osm_source, "get_overpass", lambda query: queries.append(query) or load("empty"))

    osm_source.OsmSource().find_by_point(-15.50049, -47.49951)
    osm_source.OsmSource().find_by_point(-15.50012, -47.49988)

    assert len(queries) == 1 and "-15.5,-47.5" in queries[0]


@pytest.mark.parametrize("value, hours", [("2", 2), ("oops", 24), ("-1", 24), ("0", 24)])
def test_disk_cache_ttl_is_configurable_and_validated(monkeypatch, value, hours):
    monkeypatch.setenv(osm_source.CACHE_HOURS_ENV, value)

    assert osm_source._cache_ttl_seconds() == hours * 3600
