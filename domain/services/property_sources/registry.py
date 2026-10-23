"""Registry of property sources and the choice of the enabled ones.

Sources are registered by key with a factory (a class or a callable returning a
``PropertySource``). Which ones are enabled comes, in this order, from the
``PASTO10_PROPERTY_SOURCES`` environment variable (comma separated keys), the
``property_sources`` list of the ``semente.yaml`` manifest, or the default
``["demo_registry", "osm"]``. The first enabled source is the default of the candidate search.
"""

import os
from pathlib import Path
from typing import Callable, Optional

import yaml

from semente.logging import log_warning

from domain.services.property_sources.base import PropertySource
from domain.services.property_sources.buffer_source import BufferSource
from domain.services.property_sources.demo_registry_source import DemoRegistrySource
from domain.services.property_sources.national_registry_source import NationalRegistrySource
from domain.services.property_sources.osm_source import OsmSource

SOURCES_ENV = "PASTO10_PROPERTY_SOURCES"
DEFAULT_SOURCES = ["demo_registry", "osm"]
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "semente.yaml"

_factories: dict[str, Callable[[], PropertySource]] = {}
_instances: dict[str, PropertySource] = {}


def register_source(key: str, factory: Callable[[], PropertySource]) -> None:
    """Registers (or replaces) a property source.

    Args:
        key (str): Source key used in configuration and tool arguments.
        factory (Callable): Class or callable returning a ``PropertySource`` (called once, lazily).

    Side effects:
        Drops any instance already created for the key.
    """
    _factories[key] = factory
    _instances.pop(key, None)


def get_source(key: str) -> PropertySource:
    """Returns the source registered under a key (created on first use).

    Args:
        key (str): Source key.

    Returns:
        PropertySource: The shared instance.

    Raises:
        KeyError: If no source is registered under the key.
    """
    if key not in _instances:
        _instances[key] = _factories[key]()
    return _instances[key]


def enabled_keys(manifest_path: Optional[Path] = None) -> list[str]:
    """Returns the keys of the enabled sources, in priority order.

    Args:
        manifest_path (Path, optional): Manifest to read ``property_sources`` from (defaults to ``semente.yaml``).

    Returns:
        list[str]: Keys from the environment, the manifest or the default.
    """
    configured = os.getenv(SOURCES_ENV)
    if configured:
        return [key.strip() for key in configured.split(",") if key.strip()]
    try:
        data = yaml.safe_load((manifest_path or MANIFEST_PATH).read_text(encoding="utf-8")) or {}
        keys = data.get("property_sources")
        if isinstance(keys, list) and keys:
            return [str(key) for key in keys]
    except (OSError, yaml.YAMLError):
        pass
    return list(DEFAULT_SOURCES)


def enabled_sources(manifest_path: Optional[Path] = None) -> list[PropertySource]:
    """Returns the enabled source instances, skipping (and logging) unknown keys.

    Args:
        manifest_path (Path, optional): Manifest to read the configuration from.

    Returns:
        list[PropertySource]: Enabled sources in priority order.
    """
    sources = []
    for key in enabled_keys(manifest_path):
        try:
            sources.append(get_source(key))
        except KeyError:
            log_warning(f"property sources: unknown source {key!r} ignored")
    return sources


register_source("demo_registry", DemoRegistrySource)
register_source("osm", OsmSource)
register_source("buffer", BufferSource)
register_source("registry", NationalRegistrySource)
