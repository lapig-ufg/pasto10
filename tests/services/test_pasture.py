"""Tests of the pasture service (domain/services/geospatial/pasture.py).

No Earth Engine: the small ``ee``-coupled functions are replaced by fakes.
"""

import threading

import pytest

from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial import pasture
from domain.services.geospatial.geometry import build_buffered_area

SENTINEL_GPW = object()
SENTINEL_RF = object()


@pytest.fixture
def fake_ee(monkeypatch):
    """Replaces every ee-coupled function; records the calls."""
    calls = {"gpw": [], "rf": [], "latest": 0, "area": []}
    state = {"latest": 2025, "areas": {1: 123_400.0, 2: 55_500.0, 0: 600_000.0}}

    def latest(roi):
        calls["latest"] += 1
        return state["latest"]

    monkeypatch.setattr(pasture, "feature_geometry", lambda feature: "roi")
    monkeypatch.setattr(pasture, "_latest_embedding_year", latest)
    monkeypatch.setattr(pasture, "_gpw_image", lambda year: calls["gpw"].append(year) or SENTINEL_GPW)
    monkeypatch.setattr(pasture, "_predicted_image", lambda roi, year, key: calls["rf"].append(year) or SENTINEL_RF)

    def area(image, roi, scale):
        calls["area"].append((image, scale))
        return state["areas"]

    monkeypatch.setattr(pasture, "_area_by_class", area)
    return calls, state


def _feature():
    return build_buffered_area(-15.5, -47.5, 500)


def test_constants():
    assert (pasture.GPW_FIRST_YEAR, pasture.GPW_LAST_YEAR, pasture.TRAIN_YEAR) == (2000, 2022, 2022)


@pytest.mark.parametrize("year", [2000, 2015, 2022])
def test_gpw_years_use_gpw_without_embedding_lookup(fake_ee, year):
    calls, _ = fake_ee

    classification, _roi = pasture.classify_pasture(_feature(), year)

    assert classification.source is PastureSource.GPW
    assert classification.year == year and classification.scale == 30
    assert calls["gpw"] == [year] and calls["rf"] == [] and calls["latest"] == 0


def test_later_year_uses_classification_at_10m(fake_ee):
    calls, _ = fake_ee

    classification, _roi = pasture.classify_pasture(_feature(), 2024)

    assert classification.source is PastureSource.CLASSIFICATION
    assert classification.scale == 10 and classification.year == 2024
    assert calls["rf"] == [2024] and calls["gpw"] == []


def test_no_year_defaults_to_last_gpw_year_without_classification(fake_ee):
    calls, _ = fake_ee

    classification, _roi = pasture.classify_pasture(_feature(), None)

    assert classification.year == 2022 and classification.source is PastureSource.GPW
    assert calls["latest"] == 0 and calls["rf"] == []


@pytest.mark.parametrize("year", [1999, 2026, 3000])
def test_unavailable_year_reports_latest(fake_ee, year):
    with pytest.raises(pasture.YearUnavailable) as error:
        pasture.classify_pasture(_feature(), year)

    assert error.value.latest == 2025


def test_measure_converts_to_hectares_and_sums_classes(fake_ee):
    classification, roi = pasture.classify_pasture(_feature(), 2020)

    stats = pasture.measure_pasture_area(classification, roi)

    assert stats.cultivated_ha == 12.34 and stats.natural_ha == 5.55
    assert stats.pasture_ha == 17.89
    assert stats.area_ha == 77.89
    assert stats.year == 2020 and stats.source is PastureSource.GPW
    assert stats.pasture_percent == 23.0


def test_measure_without_pasture_pixels_is_zero(fake_ee):
    _, state = fake_ee
    state["areas"] = {0: 780_000.0}

    stats = pasture.get_pasture_area(_feature(), 2020)

    assert stats.pasture_ha == 0 and stats.cultivated_ha == 0 and stats.natural_ha == 0
    assert stats.area_ha == 78.0


def test_get_pasture_area_measures_at_source_scale(fake_ee):
    calls, _ = fake_ee

    pasture.get_pasture_area(_feature(), 2024)

    assert calls["area"] == [(SENTINEL_RF, 10)]


def test_missing_gpw_image_raises_year_unavailable(monkeypatch):
    class FakeCollection:
        def filter(self, _):
            return self

        def size(self):
            return self

        def getInfo(self):
            return 0

    monkeypatch.setattr(pasture.ee, "ImageCollection", lambda name: FakeCollection())
    monkeypatch.setattr(pasture.ee.Filter, "eq", staticmethod(lambda *args: None))

    with pytest.raises(pasture.YearUnavailable) as error:
        pasture._gpw_image(2010)

    assert error.value.latest == 2022


class _FakeImage:
    """Records the calls made on an ee.Image chain."""

    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        def method(*args):
            self.calls.append((name, args))
            return self

        return method


def test_gpw_image_unmasks_to_not_grassland(monkeypatch):
    calls = []

    class FakeCollection:
        def filter(self, _):
            return self

        def size(self):
            return type("Size", (), {"getInfo": staticmethod(lambda: 1)})()

        def first(self):
            return _FakeImage(calls)

    monkeypatch.setattr(pasture.ee, "ImageCollection", lambda name: FakeCollection())
    monkeypatch.setattr(pasture.ee.Filter, "eq", staticmethod(lambda *args: None))

    pasture._gpw_image(2010)

    assert calls[-1] == ("unmask", (pasture.NOT_GRASSLAND,))


def test_masked_pixels_count_as_area_through_unmask(fake_ee):
    _, state = fake_ee
    state["areas"] = {0: 3_110_000.0, 2: 31_000.0}

    stats = pasture.get_pasture_area(_feature(), 2020)

    assert stats.area_ha == 314.1 and stats.natural_ha == 3.1
    assert stats.pasture_percent == 1.0


def test_stats_are_cached_per_area_and_year(fake_ee, monkeypatch):
    calls, _ = fake_ee
    monkeypatch.setattr(pasture, "_stats_cache", pasture.OrderedDict())
    feature = _feature()

    first = pasture.get_pasture_area(feature, 2024)
    second = pasture.get_pasture_area(feature, 2024)
    pasture.get_pasture_area(feature, 2023)

    assert first is second
    assert len(calls["area"]) == 2


def test_cached_year_does_not_retrain_the_forest(fake_ee, monkeypatch):
    calls, _ = fake_ee
    monkeypatch.setattr(pasture, "_stats_cache", pasture.OrderedDict())
    feature = _feature()

    pasture.get_pasture_area(feature, 2024)
    pasture.get_pasture_area(feature, 2024)

    assert calls["rf"] == [2024]


def test_cache_key_includes_geometry(fake_ee, monkeypatch):
    calls, _ = fake_ee
    monkeypatch.setattr(pasture, "_stats_cache", pasture.OrderedDict())
    feature = _feature()
    moved = build_buffered_area(-16.5, -47.5, 500).model_copy(update={"feature_id": feature.feature_id})

    pasture.get_pasture_area(feature, 2020)
    pasture.get_pasture_area(moved, 2020)

    assert len(calls["area"]) == 2
    assert len({key[1] for key in pasture._stats_cache}) == 2


def test_stats_are_immutable_so_the_cache_cannot_be_modified():
    stats = PastureAreaStats(
        year=2020, source=PastureSource.GPW, pasture_ha=1, cultivated_ha=1, natural_ha=0, area_ha=2,
    )

    with pytest.raises(Exception):
        stats.pasture_ha = 5


def test_cache_is_guarded_by_a_lock():
    assert isinstance(pasture._cache_lock, type(threading.Lock()))


def test_cache_evicts_oldest_entries(fake_ee, monkeypatch):
    monkeypatch.setattr(pasture, "_stats_cache", pasture.OrderedDict())
    monkeypatch.setattr(pasture, "_STATS_CACHE_SIZE", 2)
    feature = _feature()

    for year in (2000, 2001, 2002):
        pasture.get_pasture_area(feature, year)

    assert [key[2] for key in pasture._stats_cache] == [2001, 2002]
