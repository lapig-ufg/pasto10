"""Pasture classification and area service (Earth Engine).

For the years covered by Global Pasture Watch (``GPW_FIRST_YEAR`` to
``GPW_LAST_YEAR``) the published annual grassland classes are used as is. For
later years, where no map exists yet, a Random Forest is trained on the fly
with samples of the ``TRAIN_YEAR`` Global Pasture Watch map and the Google
Satellite Embedding (AlphaEarth) of that year, then applied to the embedding
of the requested year. Both paths yield an image with a single band ``cls``:
0 = not grassland, 1 = cultivated grassland, 2 = natural/semi-natural grassland.

The ``ee`` coupling is confined to the small ``_``-prefixed functions below,
so tests can replace them without Earth Engine.
"""

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import ee

from semente.logging import log_debug

from domain.schemas.feature import Feature
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial.gee import initialize_ee

GPW_FIRST_YEAR = 2000
GPW_LAST_YEAR = 2022
TRAIN_YEAR = 2022

GPW_COLLECTION = "projects/global-pasture-watch/assets/ggc-30m/v1/grassland_c"
TRAIN_COLLECTION = "projects/global-pasture-watch/assets/ggc-30m/v1-1/grassland_c"
EMBEDDING_COLLECTION = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
CLASS_BAND = "dominant_class"
LABEL = "cls"
EMBEDDING_BANDS = [f"A{index:02d}" for index in range(64)]

NOT_GRASSLAND = 0
CULTIVATED = 1
NATURAL = 2
CLASS_VALUES = [NOT_GRASSLAND, CULTIVATED, NATURAL]

GPW_SCALE_METERS = 30
CLASSIFICATION_SCALE_METERS = 10
TRAIN_SCALE_METERS = 30
TRAIN_REGION_BUFFER_METERS = 5000
CLASS_POINTS = [300, 300, 300]
TREES = 100
SEED = 42
MAX_PIXELS = 1e9
_SQUARE_METERS_PER_HECTARE = 10_000.0
_STATS_CACHE_SIZE = 128
_CLASSIFIER_CACHE_SIZE = 16
_stats_cache: OrderedDict[tuple, PastureAreaStats] = OrderedDict()
_classifier_cache: OrderedDict[str, "ee.Classifier"] = OrderedDict()
_cache_lock = threading.Lock()


class YearUnavailable(Exception):
    """The requested year has no pasture data for the area.

    Attributes:
        latest (int): Most recent year that can be analyzed for the area.
    """

    def __init__(self, latest: int):
        super().__init__(f"Year unavailable; latest available year: {latest}")
        self.latest = latest


@dataclass(frozen=True)
class PastureClassification:
    """A pasture class image ready to be measured or drawn.

    Attributes:
        image (ee.Image): Single-band image ``cls`` (0, 1, 2).
        year (int): Reference year.
        source (PastureSource): Dataset behind the classes.
        scale (int): Analysis scale in meters.
        key (tuple, optional): Stats cache key (feature id, geometry hash,
            year, source); None disables caching.
    """

    image: "ee.Image"
    year: int
    source: PastureSource
    scale: int
    key: Optional[tuple] = None


def feature_geometry(feature: Feature) -> "ee.Geometry":
    """Converts a registered feature into an Earth Engine geometry.

    Args:
        feature (Feature): Feature whose coords are GeoJSON MultiPolygon nesting.

    Returns:
        ee.Geometry: Planar (non-geodesic) MultiPolygon in EPSG:4326.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
    """
    initialize_ee()
    return ee.Geometry.MultiPolygon(feature.get_coords(), proj="EPSG:4326", geodesic=False)


def _latest_embedding_year(roi: "ee.Geometry") -> Optional[int]:
    """Returns the latest year with a Satellite Embedding image over the area, or None (network)."""
    collection = ee.ImageCollection(EMBEDDING_COLLECTION).filterBounds(roi)
    latest = collection.aggregate_max("system:time_start").getInfo()
    return ee.Date(latest).get("year").getInfo() if latest is not None else None


def _gpw_image(year: int) -> "ee.Image":
    """Returns the Global Pasture Watch class image of a year, band renamed to ``cls``.

    The asset is masked (no data) wherever its model did not run (water, bare
    ground, dense urban areas...); probes showed it is unmasked, with class 0,
    over forests and croplands, so masked pixels are treated as class 0
    (not grassland) and the measured area covers the whole polygon.

    Raises:
        YearUnavailable: If the collection has no image for the year (network check).
    """
    collection = ee.ImageCollection(GPW_COLLECTION).filter(ee.Filter.eq("system:index", str(year)))
    if collection.size().getInfo() == 0:
        raise YearUnavailable(latest=GPW_LAST_YEAR)
    return collection.first().select(CLASS_BAND).rename(LABEL).unmask(NOT_GRASSLAND)


def _embedding_image(roi: "ee.Geometry", year: int) -> "ee.Image":
    """Returns the mosaic of the year's Satellite Embedding tiles over ``roi``."""
    return (
        ee.ImageCollection(EMBEDDING_COLLECTION)
        .filterBounds(roi)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .mosaic()
        .select(EMBEDDING_BANDS)
    )


def _trained_classifier(roi: "ee.Geometry", geometry_key: str) -> "ee.Classifier":
    """Returns the Random Forest of an area, training it once per process (network).

    Training labels are unmasked to class 0 like ``_gpw_image`` so built-up,
    water and bare areas give the forest examples of non-grassland. Samples
    are drawn (stratified, ``CLASS_POINTS`` per class, fixed seed) from the
    ``TRAIN_YEAR`` Global Pasture Watch map around the area, paired with the
    embedding of that year. The trees are embedded as an explicit classifier,
    so area measurement and thumbnail reuse the same model, and the model is
    cached by geometry (thread-safe, per process): every year, tool call and
    the map of the same area share one forest instead of retraining it.
    """
    with _cache_lock:
        cached = _classifier_cache.get(geometry_key)
        if cached is not None:
            _classifier_cache.move_to_end(geometry_key)
            return cached
    train_region = roi.buffer(TRAIN_REGION_BUFFER_METERS)
    labels = (
        ee.ImageCollection(TRAIN_COLLECTION)
        .filter(ee.Filter.eq("system:index", str(TRAIN_YEAR)))
        .first()
        .select(CLASS_BAND)
        .rename(LABEL)
        .unmask(NOT_GRASSLAND)
    )
    samples = labels.addBands(_embedding_image(train_region, TRAIN_YEAR)).stratifiedSample(
        numPoints=0,
        classBand=LABEL,
        region=train_region,
        scale=TRAIN_SCALE_METERS,
        classValues=CLASS_VALUES,
        classPoints=CLASS_POINTS,
        seed=SEED,
        dropNulls=True,
    )
    trained = ee.Classifier.smileRandomForest(TREES).train(samples, LABEL, EMBEDDING_BANDS)
    classifier = ee.Classifier.decisionTreeEnsemble(trained.explain().get("trees").getInfo())
    with _cache_lock:
        _classifier_cache[geometry_key] = classifier
        while len(_classifier_cache) > _CLASSIFIER_CACHE_SIZE:
            _classifier_cache.popitem(last=False)
    return classifier


def _predicted_image(roi: "ee.Geometry", year: int, geometry_key: str) -> "ee.Image":
    """Classifies the embedding of ``year`` with the area's Random Forest (see ``_trained_classifier``)."""
    classifier = _trained_classifier(roi, geometry_key)
    return _embedding_image(roi, year).classify(classifier).rename(LABEL).unmask(NOT_GRASSLAND)


def _area_by_class(image: "ee.Image", roi: "ee.Geometry", scale: int) -> dict[int, float]:
    """Sums the pixel area (m²) of each class of ``image`` inside ``roi`` (network)."""
    reducer = ee.Reducer.sum().group(groupField=1, groupName=LABEL)
    result = (
        ee.Image.pixelArea()
        .addBands(image)
        .reduceRegion(reducer=reducer, geometry=roi, scale=scale, maxPixels=MAX_PIXELS)
        .getInfo()
    )
    return {int(group[LABEL]): float(group["sum"]) for group in result.get("groups", [])}


def _to_hectares(square_meters: float) -> float:
    """Converts m² to hectares, rounded to 2 decimals."""
    return round(square_meters / _SQUARE_METERS_PER_HECTARE, 2)


def resolve_year(roi: "ee.Geometry", year: Optional[int]) -> int:
    """Validates the requested year; without one, picks ``GPW_LAST_YEAR``.

    Years from ``GPW_FIRST_YEAR`` to ``GPW_LAST_YEAR`` are always available
    (published Global Pasture Watch maps). Later years are on-the-fly
    estimates and need a Satellite Embedding image over the area, so they must
    be requested explicitly: the default is never an estimate.

    Args:
        roi (ee.Geometry): Area of interest.
        year (int | None): Requested year, or None for ``GPW_LAST_YEAR``.

    Returns:
        int: The year to analyze.

    Raises:
        YearUnavailable: If the year is before ``GPW_FIRST_YEAR`` or after the
        latest year with an embedding (``latest`` carries that year).
    """
    if year is None:
        return GPW_LAST_YEAR
    if GPW_FIRST_YEAR <= year <= GPW_LAST_YEAR:
        return year
    latest = max(_latest_embedding_year(roi) or GPW_LAST_YEAR, GPW_LAST_YEAR)
    if year < GPW_FIRST_YEAR or year > latest:
        raise YearUnavailable(latest=latest)
    return year


def _geometry_key(feature: Feature) -> str:
    """Returns a short hash of the feature geometry, so edited areas never hit stale cache entries."""
    return hashlib.sha1(json.dumps(feature.get_coords()).encode()).hexdigest()[:16]


def _build_classification(feature: Feature, roi: "ee.Geometry", year: int) -> PastureClassification:
    """Builds the class image of an already validated year (trains the forest when needed)."""
    geometry_key = _geometry_key(feature)
    if year <= GPW_LAST_YEAR:
        source, image, scale = PastureSource.GPW, _gpw_image(year), GPW_SCALE_METERS
    else:
        source, scale = PastureSource.CLASSIFICATION, CLASSIFICATION_SCALE_METERS
        image = _predicted_image(roi, year, geometry_key)
    key = (feature.feature_id, geometry_key, year, source) if feature.feature_id else None
    return PastureClassification(image, year, source, scale, key)


def classify_pasture(feature: Feature, year: Optional[int] = None) -> tuple[PastureClassification, "ee.Geometry"]:
    """Builds the pasture class image of an area for a year.

    Args:
        feature (Feature): Registered area.
        year (int | None): Reference year; None selects ``GPW_LAST_YEAR``.

    Returns:
        tuple: (PastureClassification, the area as ee.Geometry). For years
        after ``GPW_LAST_YEAR`` the area's forest is trained here the first time.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
        YearUnavailable: If the year has no data (see ``resolve_year``).
    """
    roi = feature_geometry(feature)
    classification = _build_classification(feature, roi, resolve_year(roi, year))
    log_debug(f"classify_pasture: year={classification.year}, source={classification.source.name}")
    return classification, roi


def _cache_get(key: Optional[tuple]) -> Optional[PastureAreaStats]:
    """Returns the cached stats of a key (thread-safe), refreshing its recency."""
    if key is None:
        return None
    with _cache_lock:
        stats = _stats_cache.get(key)
        if stats is not None:
            _stats_cache.move_to_end(key)
        return stats


def measure_pasture_area(classification: PastureClassification, roi: "ee.Geometry") -> PastureAreaStats:
    """Measures cultivated, natural and total pasture area inside ``roi``.

    Masked pixels were already turned into class 0 (see ``_gpw_image``), so
    ``area_ha`` covers the whole polygon. The Random Forest is not bit-for-bit
    reproducible between runs, so results are cached in memory, per process
    (last ``_STATS_CACHE_SIZE`` entries, thread-safe), under
    ``classification.key``: the same area, geometry and year always report the
    same figures, which freezes the first estimate of a year until restart.
    ``PastureAreaStats`` is immutable, so cached objects can be shared.

    Args:
        classification (PastureClassification): Class image to measure.
        roi (ee.Geometry): Area of interest.

    Returns:
        PastureAreaStats: Hectares rounded to 2 decimals; ``pasture_ha`` is
        the sum of the two grassland classes.

    Raises:
        Exception: Any Earth Engine error (computation, quota, network).

    Side effects:
        Stores the result in the process-wide stats cache when the classification has a key.
    """
    cached = _cache_get(classification.key)
    if cached is not None:
        return cached
    square_meters = _area_by_class(classification.image, roi, classification.scale)
    cultivated = _to_hectares(square_meters.get(CULTIVATED, 0.0))
    natural = _to_hectares(square_meters.get(NATURAL, 0.0))
    stats = PastureAreaStats(
        year=classification.year,
        source=classification.source,
        pasture_ha=round(cultivated + natural, 2),
        cultivated_ha=cultivated,
        natural_ha=natural,
        area_ha=_to_hectares(sum(square_meters.values())),
    )
    if classification.key is not None:
        with _cache_lock:
            _stats_cache[classification.key] = stats
            while len(_stats_cache) > _STATS_CACHE_SIZE:
                _stats_cache.popitem(last=False)
    return stats


def get_pasture_area(feature: Feature, year: Optional[int] = None) -> PastureAreaStats:
    """Measures the pasture area of a registered area for a year.

    The stats cache is consulted before anything is built, so repeating a call
    for the same area, geometry and year neither retrains the forest nor
    queries Earth Engine again.

    Args:
        feature (Feature): Registered area.
        year (int | None): Reference year; None selects ``GPW_LAST_YEAR``
            (the latest published Global Pasture Watch map).

    Returns:
        PastureAreaStats: The measured areas.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
        YearUnavailable: If the year has no data.
        Exception: Any Earth Engine computation error.
    """
    roi = feature_geometry(feature)
    resolved = resolve_year(roi, year)
    source = PastureSource.GPW if resolved <= GPW_LAST_YEAR else PastureSource.CLASSIFICATION
    if feature.feature_id:
        cached = _cache_get((feature.feature_id, _geometry_key(feature), resolved, source))
        if cached is not None:
            return cached
    return measure_pasture_area(_build_classification(feature, roi, resolved), roi)
