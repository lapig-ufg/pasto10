"""Tests of the source contract, registry, buffer source and the national registry placeholder."""

import pytest

from domain.schemas.candidate import GeometryOrigin
from domain.services.property_sources import registry
from domain.services.property_sources.base import PropertySource, PropertySourceError
from domain.services.property_sources.buffer_source import BufferSource
from domain.services.property_sources.national_registry_source import NationalRegistrySource
from domain.services.property_sources.osm_source import OsmSource


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(registry.SOURCES_ENV, raising=False)
    snapshot = dict(registry._factories)
    yield
    registry._factories.clear()
    registry._factories.update(snapshot)
    registry._instances.clear()


def test_every_shipped_source_follows_the_protocol():
    for source in (OsmSource(), BufferSource(), NationalRegistrySource()):
        assert isinstance(source, PropertySource)


def test_demo_registry_then_osm_are_the_default_enabled_sources(tmp_path):
    assert registry.enabled_keys(tmp_path / "missing.yml") == ["demo_registry", "osm"]
    assert [source.key for source in registry.enabled_sources(tmp_path / "missing.yml")] == ["demo_registry", "osm"]


def test_environment_overrides_the_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "semente.yml"
    manifest.write_text("property_sources: [buffer]\n")
    assert registry.enabled_keys(manifest) == ["buffer"]

    monkeypatch.setenv(registry.SOURCES_ENV, "osm, registry")

    assert registry.enabled_keys(manifest) == ["osm", "registry"]


def test_the_shipped_manifest_lists_the_sources_and_still_loads_in_semente():
    from semente.manifest import Manifest

    assert registry.enabled_keys() == ["demo_registry", "osm"]
    assert Manifest.load(registry.MANIFEST_PATH).name == "pasto10"


def test_unknown_keys_are_ignored_and_custom_sources_can_be_registered(monkeypatch):
    class MySource:
        key = "mine"

        def find_by_point(self, latitude, longitude, search_radius_m=2000):
            return []

        def find_by_id(self, identifier):
            return None

    registry.register_source("mine", MySource)
    monkeypatch.setenv(registry.SOURCES_ENV, "ghost,mine")

    sources = registry.enabled_sources()

    assert [source.key for source in sources] == ["mine"] and registry.get_source("mine") is sources[0]
    with pytest.raises(KeyError):
        registry.get_source("ghost")


def test_registering_again_replaces_the_instance():
    registry.register_source("osm", BufferSource)

    assert registry.get_source("osm").key == "buffer"


def test_buffer_source_returns_one_honest_circle():
    [candidate] = BufferSource(radius_m=300).find_by_point(-15.5, -47.5)

    assert candidate.origin is GeometryOrigin.APPROXIMATE_CIRCLE and candidate.area_ha == pytest.approx(28.27, abs=0.01)
    assert candidate.distance_m == 0 and "approximate_area" in candidate.warnings
    feature = candidate.to_feature()
    assert feature.feature_type == "approximate_circle" and feature.get_metadata("radius") == pytest.approx(300, abs=2)
    assert BufferSource().find_by_id("x") is None


def test_national_registry_is_a_documented_placeholder():
    source = NationalRegistrySource()

    with pytest.raises(NotImplementedError):
        source.find_by_point(0, 0)
    with pytest.raises(NotImplementedError):
        source.find_by_id("1")
    import domain.services.property_sources.national_registry_source as module

    assert "PropertyCandidate" in module.__doc__ and "REGISTERED_BOUNDARY" in module.__doc__ and "PropertySourceError" in module.__doc__


def test_error_kinds_are_validated_by_contract():
    assert PropertySourceError("timeout").kind == "timeout"
    assert set(PropertySourceError.KINDS) == {"unavailable", "timeout", "not_found", "urban_area"}


def test_registry_is_disabled_by_default_and_can_be_enabled(monkeypatch):
    assert "registry" not in registry.enabled_keys()
    monkeypatch.setenv(registry.SOURCES_ENV, "registry")

    assert isinstance(registry.enabled_sources()[0], NationalRegistrySource)
