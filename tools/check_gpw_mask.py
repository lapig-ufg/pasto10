"""Checks what the masked pixels of the Global Pasture Watch classes are.

Standalone script (not imported by the app). For buffers over many regions it
compares the pixels where the GPW ``dominant_class`` (v1, 2020) is masked with
ESA WorldCover 2021 (an independent 10 m land cover product, resampled to the
GPW 30 m grid by sampling). It reports the share of masked pixels that fall in
non-vegetated or non-grassland covers (built-up, bare, snow, water) versus
tree, shrub, cropland, wetland, and, as the critical counter-check, how much
WorldCover grassland ends up masked.

Usage (needs GEE_* variables)::

    uv run python tools/check_gpw_mask.py
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ee  # noqa: E402

from semente.configs.prompts import set_prompts_dir  # noqa: E402

set_prompts_dir(Path(__file__).resolve().parents[1] / "domain" / "prompts")

from domain.services.geospatial import pasture  # noqa: E402
from domain.services.geospatial.gee import initialize_ee  # noqa: E402
from domain.services.geospatial.geometry import build_buffered_area  # noqa: E402
from tools.benchmark_pasture import SITES  # noqa: E402

WORLDCOVER = {
    10: "tree", 20: "shrub", 30: "grassland", 40: "crop", 50: "built", 60: "bare",
    70: "snow", 80: "water", 90: "wetland", 95: "mangrove", 100: "moss",
}
NON_GRASSLAND_COVERS = ("built", "bare", "snow", "water")
RADIUS_METERS = 3000
YEAR = 2020


def masked_by_cover(site: str) -> dict:
    """Measures the WorldCover classes inside the masked and unmasked GPW pixels of a site (network).

    Args:
        site (str): Site name, a key of ``SITES``.

    Returns:
        dict: ``{"masked": {cover: m²}, "unmasked": {cover: m²}}`` where the
        covers are the names of ``WORLDCOVER``.

    Raises:
        KeyError: If the site is unknown.
        ee.EEException: On any Earth Engine error.
    """
    lat, lon = SITES[site]
    roi = pasture.feature_geometry(build_buffered_area(lat, lon, RADIUS_METERS))
    gpw = (
        ee.ImageCollection(pasture.GPW_COLLECTION).filter(ee.Filter.eq("system:index", str(YEAR)))
        .first().select(pasture.CLASS_BAND)
    )
    cover = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
    state = gpw.mask().Not().rename("masked")
    reducer = ee.Reducer.sum().group(groupField=1, groupName="cover").group(groupField=2, groupName="masked")
    result = ee.Image.pixelArea().addBands(cover.rename("cover")).addBands(state).reduceRegion(
        reducer, roi, 30, maxPixels=1e9).getInfo()
    table = {"masked": {}, "unmasked": {}}
    for outer in result["groups"]:
        bucket = "masked" if int(outer["masked"]) == 1 else "unmasked"
        for inner in outer["groups"]:
            name = WORLDCOVER.get(int(inner["cover"]), str(inner["cover"]))
            table[bucket][name] = table[bucket].get(name, 0.0) + float(inner["sum"])
    return table


def _percent(part: float, whole: float) -> float:
    """Returns ``100 * part / whole``, or ``nan`` when ``whole`` is zero."""
    return 100 * part / whole if whole else float("nan")


def main() -> None:
    """Prints, per site and overall, how the masked GPW pixels split by WorldCover class.

    Raises:
        ee.EEException: On any Earth Engine error.

    Side effects:
        Initializes Earth Engine and writes the table to stdout.
    """
    initialize_ee()
    with ThreadPoolExecutor(5) as executor:
        tables = dict(zip(SITES, executor.map(masked_by_cover, SITES)))
    print(f"{'site':16} {'masked%':>8} {'masked->non-grass%':>19} {'masked->grass%':>15} {'wc grass masked%':>17}")
    total = {"masked": 0.0, "non": 0.0, "grass": 0.0, "wc_grass": 0.0, "wc_grass_masked": 0.0}
    for site, table in tables.items():
        masked = sum(table["masked"].values())
        everything = masked + sum(table["unmasked"].values())
        non = sum(table["masked"].get(name, 0.0) for name in NON_GRASSLAND_COVERS)
        grass = table["masked"].get("grassland", 0.0)
        wc_grass = grass + table["unmasked"].get("grassland", 0.0)
        print(f"{site:16} {_percent(masked, everything):8.1f} {_percent(non, masked):19.1f} "
              f"{_percent(grass, masked):15.1f} {_percent(grass, wc_grass):17.1f}")
        for key, value in zip(total, (masked, non, grass, wc_grass, grass)):
            total[key] += value
    print(f"{'ALL':16} masked->non-grass {_percent(total['non'], total['masked']):.1f}% | masked->grass "
          f"{_percent(total['grass'], total['masked']):.1f}% | WorldCover grassland masked "
          f"{_percent(total['wc_grass_masked'], total['wc_grass']):.1f}%")


if __name__ == "__main__":
    main()
