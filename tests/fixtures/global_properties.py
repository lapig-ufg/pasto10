"""Fixed set of 15 test properties around the world.

Shared by the end-to-end tests and the benchmarks of the pasture tools (and,
later, the biomass tools). Each entry of ``global_properties.json`` has an
``id``, ``name``, ``country``, ``continent``, a center (``latitude``,
``longitude``), a buffer ``radius_m``, the expected IANA ``timezone``, a ``profile`` (what the place
represents), a ``kind`` and the ``expect`` ranges that the e2e profile test
checks against independent assets (GPW v1 2022, ESA WorldCover 2021, SRTM):

- ``gpw_2022_pasture_fraction``: [min, max] share of the area that is pasture;
- ``dominant_class``: ``cultivated``, ``natural`` or null (controls);
- ``worldcover``: minimum/maximum share per WorldCover cover;
- ``elevation_min_m`` (optional): minimum mean elevation.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

_FILE = Path(__file__).with_name("global_properties.json")


@dataclass(frozen=True)
class GlobalProperty:
    """One test property.

    Attributes:
        id (str): Short identifier, usable as pytest id.
        name (str): Display name used when registering the area.
        country (str): Country.
        continent (str): Continent.
        latitude (float): Center latitude in decimal degrees.
        longitude (float): Center longitude in decimal degrees.
        radius_m (int): Buffer radius in meters.
        timezone (str): IANA timezone of the place (expected from Open-Meteo ``timezone=auto``).
        profile (str): What the place represents.
        kind (str): ``cultivated``, ``natural``, ``mosaic``, ``control_urban`` or ``control_desert``.
        expect (dict): Expected profile ranges (see the module docstring).
    """

    id: str
    name: str
    country: str
    continent: str
    latitude: float
    longitude: float
    radius_m: int
    timezone: str
    profile: str
    kind: str
    expect: dict = field(default_factory=dict)

    @property
    def is_control(self) -> bool:
        """True for the negative controls (no pasture expected)."""
        return self.kind.startswith("control")


def load_global_properties() -> list[GlobalProperty]:
    """Loads the fixed set of test properties.

    Returns:
        list[GlobalProperty]: The 15 properties, in file order.
    """
    return [GlobalProperty(**entry) for entry in json.loads(_FILE.read_text(encoding="utf-8"))]
