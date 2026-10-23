"""Measures the OpenStreetMap (Overpass) candidate coverage over the 15 fixed test properties.

Standalone script (not imported by the app). For each property it queries the
Overpass mirrors (retrying with a pause), prints the number of useful
candidates, the latency, the mirror that answered and the land-use tags, and
saves the raw answers as offline fixtures in ``tests/fixtures/overpass/``.

Usage::

    uv run python tools/measure_osm_coverage.py [--radius 2000] [--out tests/fixtures/overpass]
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.services.property_sources import osm_source  # noqa: E402
from domain.services.property_sources.base import PropertySourceError  # noqa: E402
from tests.fixtures.global_properties import load_global_properties  # noqa: E402

ATTEMPTS_PER_MIRROR = 2
PAUSE_SECONDS = 5
# Overpass often queues requests for ~30 s: the measurement waits longer than the app (15 s) to see the real coverage.


def query_with_retries(query: str) -> tuple[dict, str, float]:
    """Runs a query on the mirrors with retries and a pause between attempts.

    Args:
        query (str): Overpass QL text.

    Returns:
        tuple: (answer, mirror URL that answered, seconds spent in the successful request).

    Raises:
        PropertySourceError: ``unavailable`` when every attempt on every mirror failed.
    """
    for url in osm_source._urls():
        for attempt in range(ATTEMPTS_PER_MIRROR):
            start = time.monotonic()
            try:
                response = requests.post(
                    url, data={"data": query}, headers={"User-Agent": osm_source.USER_AGENT}, timeout=(5, 60),
                )
                response.raise_for_status()
                return response.json(), url, time.monotonic() - start
            except (requests.RequestException, ValueError) as error:
                print(f"   {url} attempt {attempt + 1}: {type(error).__name__}", file=sys.stderr)
                time.sleep(PAUSE_SECONDS)
    raise PropertySourceError("unavailable", "all mirrors failed")


def main() -> None:
    """Measures every property and prints the summary table (also saved as ``summary.json`` next to the fixtures).

    Side effects:
        Writes one ``<id>.json`` raw answer per property and ``summary.json`` in the output directory.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--radius", type=float, default=2000)
    parser.add_argument("--out", default="tests/fixtures/overpass")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    source = osm_source.OsmSource()
    rows = []
    print(f"{'id':18}{'result':14}{'cand':>5}{'secs':>7}  mirror / tags")
    for item in load_global_properties():
        query = osm_source.build_query(item.latitude, item.longitude, args.radius)
        try:
            data, mirror, seconds = query_with_retries(query)
        except PropertySourceError:
            rows.append({"id": item.id, "result": "unavailable"})
            print(f"{item.id:18}{'unavailable':14}", flush=True)
            continue
        (out / f"{item.id}.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        osm_source._cache.clear()
        original = osm_source.get_overpass
        osm_source.get_overpass = lambda q, data=data: data
        try:
            candidates = source.find_by_point(item.latitude, item.longitude, args.radius)
            result = "ok"
        except PropertySourceError as error:
            candidates, result = [], error.kind
        finally:
            osm_source.get_overpass = original
        tags = sorted({entry.value for c in candidates for entry in c.attributes if entry.key in ("osm_landuse", "osm_place")})
        inside = sum(1 for c in candidates if c.distance_m == 0)
        rows.append({
            "id": item.id, "result": result, "candidates": len(candidates), "containing_point": inside, "seconds": round(seconds, 1),
            "mirror": mirror.split("//")[1].split("/")[0], "tags": tags, "elements": len(data.get("elements", [])),
            "areas_ha": [c.area_ha for c in candidates],
        })
        print(f"{item.id:18}{result:14}{len(candidates):5d}{seconds:7.1f}  {rows[-1]['mirror']} {tags} inside={inside}", flush=True)
        time.sleep(PAUSE_SECONDS)
    (out / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
