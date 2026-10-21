"""Tests of the lazy Earth Engine initialization (domain/services/geospatial/gee.py)."""

import pytest

from domain.services.geospatial import gee


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(gee, "_initialized", False)
    monkeypatch.setattr(gee.config, "GEE_PROJECT", "proj", raising=False)
    monkeypatch.setattr(gee.config, "GEE_SERVICE_ACCOUNT", "sa@example.com", raising=False)
    monkeypatch.setattr(gee.config, "GEE_KEY_FILE", "key.json", raising=False)


def test_import_does_not_initialize(monkeypatch):
    assert gee._initialized is False


def test_initialize_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(gee.ee, "ServiceAccountCredentials", lambda account, key: ("creds", account, key))
    monkeypatch.setattr(gee.ee, "Initialize", lambda *args, **kwargs: calls.append((args, kwargs)))

    gee.initialize_ee()
    gee.initialize_ee()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (("creds", "sa@example.com", "key.json"),)
    assert kwargs["project"] == "proj"


def test_missing_project_raises_unavailable(monkeypatch):
    monkeypatch.setattr(gee.config, "GEE_PROJECT", None, raising=False)

    with pytest.raises(gee.EarthEngineUnavailable):
        gee.initialize_ee()


def test_failed_initialization_is_wrapped_and_retried(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("no key file")

    monkeypatch.setattr(gee.ee, "ServiceAccountCredentials", fail)

    with pytest.raises(gee.EarthEngineUnavailable):
        gee.initialize_ee()
    assert gee._initialized is False
