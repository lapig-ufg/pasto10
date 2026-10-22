"""Benchmark of the post-2022 pasture classifier against Global Pasture Watch v1-1.

Standalone script (not imported by the app). The v1-1 collection has labels
for 2000-2024, so the on-the-fly classification of 2023 and 2024 can be
validated: for the 15 fixed test properties (``tests/fixtures``) plus extra
sites, spread over biomes and continents, it compares the binary pasture map (classes 1 and 2 versus 0) of each classifier design with
the v1-1 map of the same year at 30 m, with masked v1-1 pixels counted as 0
like in the app.

Per site and year it reports accuracy, F1 of the pasture class, truth and
predicted pasture hectares and the relative error
``|pred - truth| / max(truth, 2% of the area)``. Designs never train on the
year being evaluated.

Usage (needs GEE_PROJECT, GEE_SERVICE_ACCOUNT and GEE_KEY_FILE)::

    uv run python tools/benchmark_pasture.py --designs baseline,pooled3 --out results.jsonl
"""

import argparse
import json
import math
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ee  # noqa: E402

from semente.configs.prompts import set_prompts_dir  # noqa: E402

set_prompts_dir(Path(__file__).resolve().parents[1] / "domain" / "prompts")

from domain.services.geospatial import pasture  # noqa: E402
from domain.services.geospatial.gee import initialize_ee  # noqa: E402
from domain.services.geospatial.geometry import build_buffered_area  # noqa: E402

RADIUS_METERS = 2000
EVAL_YEARS = (2023, 2024)
MIN_TRUTH_FRACTION = 0.02

FIXTURE_FILE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "global_properties.json"
FIXTURE_SITES = {
    entry["id"]: (entry["latitude"], entry["longitude"]) for entry in json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
}

EXTRA_SITES = {
    "cerrado_go": (-15.5, -47.5), "cerrado_mt": (-13.0, -55.0), "amazon_pasture": (-8.0, -55.0),
    "amazon_forest": (-3.5, -60.0), "pampas_ar": (-34.0, -59.0), "pampas_uy": (-33.0, -56.0),
    "patagonia": (-45.5, -70.5), "steppe_mn": (47.9, 106.9), "steppe_kz": (50.0, 70.0),
    "inner_mongolia": (43.5, 116.0), "tibet": (30.5, 91.5), "sahel_ne": (13.5, 2.1),
    "sahel_ml": (14.5, -4.0), "kenya_rangeland": (-2.0, 37.0), "highveld_za": (-27.0, 28.5),
    "savanna_au_qld": (-27.5, 150.0), "savanna_au_nt": (-14.0, 132.0), "nz_pasture": (-40.0, 175.5),
    "europe_fr": (47.0, 3.0), "europe_ie": (53.0, -8.0), "us_nebraska": (42.5, -100.5),
    "us_kansas": (38.5, -99.0), "india_crop": (26.0, 80.0), "urban_sp": (-23.55, -46.63),
    "urban_london": (51.5, -0.12), "water_victoria": (-1.0, 33.0), "desert_sahara": (25.0, 10.0),
}
SITES = {**FIXTURE_SITES, **EXTRA_SITES}


@dataclass(frozen=True)
class Design:
    """A classifier design.

    Attributes:
        name (str): Identifier used on the command line.
        train_years (Callable[[int], list]): Label years used to predict a given year.
        class_points (list): Stratified samples per class (0, 1, 2) and year.
        region_buffer (int): Training region buffer around the area, meters.
        trees (int): Number of trees.
        min_leaf (int): Minimum leaf population.
        variables_per_split (int, optional): Variables tried per split (None: sqrt of bands).
        bag_fraction (float): Fraction of the samples used per tree.
    """

    name: str
    train_years: Callable[[int], list]
    class_points: tuple = (300, 300, 300)
    region_buffer: int = 5000
    trees: int = 100
    min_leaf: int = 1
    variables_per_split: Optional[int] = None
    bag_fraction: float = 0.5


def _slide(count: int) -> Callable[[int], list]:
    return lambda year: list(range(year - count, year))


DESIGNS = {design.name: design for design in (
    Design("baseline", lambda year: [2022]),
    Design("pooled3", lambda year: [2020, 2021, 2022]),
    Design("slide3", _slide(3)),
    Design("slide3_prior", _slide(3), class_points=(600, 300, 300)),
    Design("slide3_reg", _slide(3), trees=200, min_leaf=5, bag_fraction=0.7),
    Design("slide3_wide", _slide(3), region_buffer=20000, class_points=(500, 500, 500)),
    Design("slide3_wide_reg", _slide(3), region_buffer=20000, class_points=(500, 500, 500), trees=200, min_leaf=5, bag_fraction=0.7),
    Design("slide5_wide", _slide(5), region_buffer=20000, class_points=(300, 300, 300)),
)}


def _labels(year: int) -> "ee.Image":
    return (
        ee.ImageCollection(pasture.TRAIN_COLLECTION)
        .filter(ee.Filter.eq("system:index", str(year))).first()
        .select(pasture.CLASS_BAND).rename(pasture.LABEL).unmask(0)
    )


def train(roi: "ee.Geometry", design: Design, year: int) -> "ee.Classifier":
    """Trains the forest of a design to predict ``year`` and materializes its trees (network).

    Args:
        roi (ee.Geometry): Area of interest; the training region is its buffer.
        design (Design): Classifier design (label years, samples, forest parameters).
        year (int): Year to predict; ``design.train_years`` never includes it.

    Returns:
        ee.Classifier | int: The forest, or the single class as an ``int`` when
        the training region holds only one class (Earth Engine cannot train on one).

    Raises:
        ee.EEException: On any training failure other than a single-class region.
    """
    region = roi.buffer(design.region_buffer)
    samples = ee.FeatureCollection([
        _labels(label_year).addBands(pasture._embedding_image(region, label_year)).stratifiedSample(
            numPoints=0, classBand=pasture.LABEL, region=region, scale=30,
            classValues=pasture.CLASS_VALUES, classPoints=list(design.class_points),
            seed=pasture.SEED + label_year, dropNulls=True,
        )
        for label_year in design.train_years(year)
    ]).flatten()
    forest = ee.Classifier.smileRandomForest(
        design.trees, design.variables_per_split, design.min_leaf, design.bag_fraction, None, pasture.SEED,
    ).train(samples, pasture.LABEL, pasture.EMBEDDING_BANDS)
    try:
        return ee.Classifier.decisionTreeEnsemble(forest.explain().get("trees").getInfo())
    except ee.EEException:
        classes = samples.aggregate_array(pasture.LABEL).distinct().getInfo()
        if len(classes) > 1:
            raise
        return int(classes[0]) if classes else 0


def confusion(roi: "ee.Geometry", predicted: "ee.Image", year: int) -> dict:
    """Pasture-versus-rest confusion areas (m²) against v1-1 of ``year`` at 30 m (network).

    Args:
        roi (ee.Geometry): Area of interest.
        predicted (ee.Image): Predicted classes, band ``cls`` (0, 1, 2).
        year (int): Year of the v1-1 map used as truth (masked pixels count as 0).

    Returns:
        dict: Areas in m² under ``tn``, ``fp``, ``fn`` and ``tp`` (positive = pasture).

    Raises:
        ee.EEException: On any Earth Engine computation error.
    """
    truth = _labels(year).gt(0)
    code = truth.multiply(2).add(predicted.gt(0)).rename("code")
    reducer = ee.Reducer.sum().group(groupField=1, groupName="code")
    result = ee.Image.pixelArea().addBands(code).reduceRegion(reducer, roi, 30, maxPixels=1e9).getInfo()
    cells = {int(group["code"]): float(group["sum"]) for group in result["groups"]}
    return {"tn": cells.get(0, 0.0), "fp": cells.get(1, 0.0), "fn": cells.get(2, 0.0), "tp": cells.get(3, 0.0)}


def metrics(cells: dict) -> dict:
    """Computes the agreement metrics of one confusion.

    Args:
        cells (dict): Areas under ``tn``, ``fp``, ``fn`` and ``tp`` (see ``confusion``).

    Returns:
        dict: ``accuracy``, ``f1`` (None when there is no pasture in truth nor
        prediction), ``truth_ha``, ``pred_ha``, ``area_ha`` and ``rel_err``
        (``|pred - truth| / max(truth, MIN_TRUTH_FRACTION * area)``).

    Raises:
        KeyError: If a cell is missing.
        ZeroDivisionError: If the total area is zero.
    """
    tn, fp, fn, tp = (cells[key] for key in ("tn", "fp", "fn", "tp"))
    total = tn + fp + fn + tp
    truth, pred = tp + fn, tp + fp
    f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else None
    return {
        "accuracy": (tn + tp) / total, "f1": f1,
        "truth_ha": truth / 1e4, "pred_ha": pred / 1e4, "area_ha": total / 1e4,
        "rel_err": abs(pred - truth) / max(truth, MIN_TRUTH_FRACTION * total),
    }


def evaluate(name: str, design_name: str) -> list:
    """Evaluates one design on one site for every evaluation year (network).

    Args:
        name (str): Site name, a key of ``SITES``.
        design_name (str): Design name, a key of ``DESIGNS``.

    Returns:
        list[dict]: One row per evaluation year with the design, site, year and metrics.

    Raises:
        KeyError: If the site or the design is unknown.
        ee.EEException: On any Earth Engine error.
    """
    lat, lon = SITES[name]
    design = DESIGNS[design_name]
    roi = pasture.feature_geometry(build_buffered_area(lat, lon, RADIUS_METERS))
    rows, classifiers = [], {}
    for year in EVAL_YEARS:
        key = tuple(design.train_years(year))
        if key not in classifiers:
            classifiers[key] = train(roi, design, year)
        classifier = classifiers[key]
        predicted = (
            ee.Image.constant(classifier).rename("cls") if isinstance(classifier, int)
            else pasture._embedding_image(roi, year).classify(classifier).rename("cls").unmask(0)
        )
        rows.append({"design": design_name, "site": name, "year": year, **metrics(confusion(roi, predicted, year))})
    return rows


def summarize(rows: list) -> None:
    """Prints, per design, the mean accuracy and F1 and the median/mean relative error.

    Args:
        rows (list): Result rows of ``evaluate``. Relative errors and F1 only
            consider rows whose truth has at least ``MIN_TRUTH_FRACTION`` of
            pasture; they print as ``nan`` when no row qualifies.

    Side effects:
        Writes the table to stdout.
    """
    print(f"{'design':18} {'n':>3} {'acc':>6} {'f1':>6} {'rel_med':>8} {'rel_mean':>8} {'abs%area':>9}")
    for design in sorted({row["design"] for row in rows}):
        subset = [row for row in rows if row["design"] == design]
        f1s = [row["f1"] for row in subset if row["f1"] is not None and row["truth_ha"] > MIN_TRUTH_FRACTION * row["area_ha"]]
        has_truth = [row for row in subset if row["truth_ha"] > MIN_TRUTH_FRACTION * row["area_ha"]]
        rel_median = statistics.median(r["rel_err"] for r in has_truth) if has_truth else math.nan
        rel_mean = statistics.mean(r["rel_err"] for r in has_truth) if has_truth else math.nan
        print(f"{design:18} {len(subset):3d} {statistics.mean(r['accuracy'] for r in subset):6.3f} "
              f"{(statistics.mean(f1s) if f1s else math.nan):6.3f} {rel_median:8.3f} {rel_mean:8.3f} "
              f"{100 * statistics.mean(abs(r['pred_ha'] - r['truth_ha']) / r['area_ha'] for r in subset):8.2f}%")


def main() -> None:
    """Runs the benchmark from the command line.

    Evaluates the chosen designs on the chosen sites in parallel, appends each
    result row to the ``--out`` JSON-lines file (already evaluated design/site
    pairs found there are skipped, so interrupted runs resume), and prints the
    summary table.

    Raises:
        SystemExit: If a design or site name is unknown.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--designs", default="baseline", help=f"comma list of {sorted(DESIGNS)}")
    parser.add_argument("--sites", default="fixture", help="fixture (the 15 test properties), all, or a comma list")
    parser.add_argument("--out", default="benchmark.jsonl")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    sites = {"all": list(SITES), "fixture": list(FIXTURE_SITES)}.get(args.sites) or args.sites.split(",")
    design_names = args.designs.split(",")
    unknown = [name for name in sites if name not in SITES] + [name for name in design_names if name not in DESIGNS]
    if unknown:
        raise SystemExit(f"Unknown site or design: {', '.join(unknown)}. Sites: {sorted(SITES)}; designs: {sorted(DESIGNS)}")
    initialize_ee()
    out = Path(args.out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()] if out.exists() else []
    done = {(row["design"], row["site"]) for row in rows}
    jobs = [(site, design) for design in design_names for site in sites if (design, site) not in done]
    lock = threading.Lock()

    def run(job):
        try:
            result = evaluate(*job)
        except Exception as error:
            print(f"FAILED {job}: {error}", flush=True)
            return
        with lock:
            rows.extend(result)
            with out.open("a", encoding="utf-8") as handle:
                for row in result:
                    handle.write(json.dumps(row) + "\n")
        print(f"done {job}", flush=True)

    with ThreadPoolExecutor(args.workers) as executor:
        list(executor.map(run, jobs))
    summarize([row for row in rows if row["design"] in design_names])


if __name__ == "__main__":
    main()
