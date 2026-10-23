"""Generates the synthetic demo property registry (``domain/data/demo_registry.geojson``).

Standalone, deterministic script (fixed seed per property). For each of the 15
test properties of ``domain/data/test_properties.json`` it builds 3 or 4 irregular
parcels: a noisy blob of about 1.1 km radius around the property point is cut
into Voronoi cells around random seeds, keeping cells of 20 to 400 ha with at least 6 vertices, one of
them containing the point and the others its neighbors. The negative controls
(urban and desert) get no parcels. Parcels are SYNTHETIC illustrations, not real
boundaries, and carry no owner data.

Usage::

    uv run python tools/make_demo_registry.py
"""

import json
import math
import random
import sys
from pathlib import Path

from shapely.geometry import MultiPoint
from shapely.ops import voronoi_diagram
from shapely.geometry import Point, Polygon, mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domain.services.property_sources import geometry_utils as geo  # noqa: E402

PROPERTIES_FILE = ROOT / "domain" / "data" / "test_properties.json"
OUT_FILE = ROOT / "domain" / "data" / "demo_registry.geojson"
SEED_TAG = "pasto10-demo-registry-v1"
BLOB_RADIUS_M = 1100
MIN_HA, MAX_HA = 20.0, 400.0
GAP_M = 8  # parcels are shrunk by this many meters so neighbors never touch or overlap
ISO3 = {
    "Brazil": "BRA", "Bolivia": "BOL", "Paraguay": "PRY", "Colombia": "COL", "Peru": "PER", "Argentina": "ARG",
    "Uruguay": "URY", "Chile": "CHL", "Venezuela": "VEN", "United States": "USA", "Kenya": "KEN", "Australia": "AUS",
}
_METERS_PER_DEGREE = 111_320.0


def blob(rng: random.Random) -> Polygon:
    """Builds an irregular blob (in meters) around the origin: a circle whose radius oscillates with the angle."""
    phase_a, phase_b = rng.uniform(0, 6.28), rng.uniform(0, 6.28)
    points = []
    for step in range(72):
        angle = 2 * math.pi * step / 72
        radius = BLOB_RADIUS_M * (1 + 0.22 * math.sin(3 * angle + phase_a) + 0.12 * math.sin(5 * angle + phase_b))
        points.append((radius * math.cos(angle), radius * math.sin(angle)))
    return Polygon(points)


def parcels_for(property_id: str, count: int) -> list[Polygon]:
    """Returns ``count`` Voronoi parcels (in meters around the property point) with 20-400 ha, one containing the origin.

    Args:
        property_id (str): Test property id (seeds the random generator).
        count (int): Number of parcels (3 or 4).

    Returns:
        list[Polygon]: The parcels, the one containing the point first.

    Raises:
        RuntimeError: If no valid partition is found in 500 attempts.
    """
    rng = random.Random(f"{SEED_TAG}-{property_id}")
    for _ in range(500):
        region = blob(rng)
        seeds = [(rng.uniform(-150, 150), rng.uniform(-150, 150))]
        base = rng.uniform(0, 6.28)
        for index in range(count - 1):
            angle = base + 2 * math.pi * index / (count - 1) + rng.uniform(-0.5, 0.5)
            distance = rng.uniform(450, 850)
            seeds.append((distance * math.cos(angle), distance * math.sin(angle)))
        cells = [cell.intersection(region).buffer(-GAP_M) for cell in voronoi_diagram(MultiPoint(seeds), envelope=region.buffer(800)).geoms]
        cells = [cell for cell in cells if isinstance(cell, Polygon) and not cell.is_empty]
        if len(cells) != count or not all(MIN_HA <= cell.area / 10_000 <= MAX_HA for cell in cells):
            continue
        origin = Point(0, 0)
        cells.sort(key=lambda cell: not cell.contains(origin))
        simplified = [cell.simplify(4, preserve_topology=True) for cell in cells]
        if cells[0].contains(origin) and all(len(cell.exterior.coords) >= 7 for cell in simplified):
            return simplified
    raise RuntimeError(f"no valid demo parcels for {property_id}")


def to_lonlat(polygon: Polygon, latitude: float, longitude: float) -> Polygon:
    """Converts a polygon in meters around a point to lon/lat degrees (5 decimals)."""
    scale = _METERS_PER_DEGREE * math.cos(math.radians(latitude))
    return Polygon([(round(longitude + x / scale, 5), round(latitude + y / _METERS_PER_DEGREE, 5)) for x, y in polygon.exterior.coords])


def main() -> None:
    """Writes ``domain/data/demo_registry.geojson`` and prints the parcels per property.

    Side effects:
        Overwrites the output file (same content on every run).
    """
    properties = json.loads(PROPERTIES_FILE.read_text(encoding="utf-8"))
    counters: dict[str, int] = {}
    features = []
    for index, item in enumerate(properties):
        if item["kind"].startswith("control"):
            print(f"{item['id']:18} no parcels (negative control)")
            continue
        code = ISO3[item["country"]]
        parcels = parcels_for(item["id"], 3 + index % 2)
        areas = []
        for parcel in parcels:
            shape = to_lonlat(parcel, item["latitude"], item["longitude"])
            counters[code] = counters.get(code, 0) + 1
            area = round(geo.area_hectares(shape), 1)
            areas.append(area)
            features.append({
                "type": "Feature",
                "properties": {
                    "registry_id": f"DEMO-{code}-{counters[code]:04d}", "status": "active", "area_ha": area,
                    "region": f"Demo region {item['country']}", "country": item["country"], "property_id": item["id"],
                },
                "geometry": mapping(shape),
            })
        print(f"{item['id']:18} {len(parcels)} parcels  {areas} ha")
    collection = {"type": "FeatureCollection", "name": "pasto10_demo_registry", "features": features}
    OUT_FILE.write_text(json.dumps(collection, separators=(",", ":")), encoding="utf-8")
    print(f"{len(features)} parcels -> {OUT_FILE} ({OUT_FILE.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
