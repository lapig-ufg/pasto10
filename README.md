# Pasto10

**Multi-agent decision-support tool for pasture management, built on the [Semente](https://github.com/lapig-ufg/semente) framework and powered by maps from the Global Pasture Watch and Time2Graze initiatives.**

---

## About

Pasto10 is a decision-support system that combines multi-agent AI with satellite-based pasture maps to help ranchers and pasture managers around the world monitor, diagnose, and improve their pasturelands. Instead of navigating dashboards and map layers, users converse with an assistant that interprets the data for them, answers questions about specific properties or municipalities, and turns map indicators into practical management recommendations.

The project is a **showcase and reference implementation of [Semente](https://github.com/lapig-ufg/semente)**, the multi-agent AI chat framework for land use and agriculture. Just as Pasto Legal (LAPIG/UFG's WhatsApp assistant for pasture diagnostics) was the seed from which Semente was extracted, Pasto10 demonstrates how the framework grows into a new domain — in this case, one grounded in Global Pasture Watch and Time2Graze map products.

## Data

Pasto10's knowledge comes from maps produced in the context of two initiatives:

- **Global Pasture Watch** — a global collaboration delivering the first open, satellite-derived maps of pastureland extent, condition, and productivity. These layers tell us where pastures are and how degraded or vigorous they are.
- **Time2Graze** — an initiative advancing temporal analysis of grazing lands, including pasture dynamics over time series that support management decisions.

Pasto10 reads these maps through Google Earth Engine:

| Years | Source | How it is used |
| --- | --- | --- |
| 2000-2022 | Global Pasture Watch grassland classes (`ggc-30m/v1/grassland_c`, 30 m) | Used as published: cultivated grassland, natural/semi-natural grassland and non-grassland. |
| After 2022 | [Google Satellite Embedding](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_SATELLITE_EMBEDDING_V1_ANNUAL) (AlphaEarth, 10 m) | No published v1 map exists: a Random Forest (100 trees, 300 samples per class and year, fixed seed) is trained on the fly, around the registered area, with the Global Pasture Watch v1-1 classes of the three previous years (2023 uses 2020-2022, 2024 uses 2021-2023, later years use 2022-2024) and the embedding of the same years, then applied to the embedding of the requested year. |

This is a demonstration of how to combine global datasets, not a production-grade product: each country would improve it with its own policies and registries. Cells that Global Pasture Watch leaves without data are counted as non-grassland, so the analyzed area equals the whole registered area.

When no year is given the tools use 2022, the latest published Global Pasture Watch v1 map; a later year must be requested explicitly and always returns an experimental estimate. Pasture area is the sum of cultivated and natural/semi-natural grassland, in hectares inside the registered area. Time2Graze is used by the biomass tools (below), not by the pasture tools.

### Validation of the post-2022 estimate

The v1-1 collection has labels for 2023 and 2024, so the estimate can be checked against official maps (`tools/benchmark_pasture.py`, 27 sites of 2 km radius on all continents, never training on the evaluated year, pasture versus not pasture at 30 m):

| Design | Accuracy | F1 (pasture) | Median relative error of pasture ha | Mean relative error |
| --- | --- | --- | --- | --- |
| Single year (2022) labels | 0.88 | 0.79 | 0.13 | 0.41 |
| Three previous years (adopted) | 0.90 | 0.83 | 0.10 | 0.46 |

(Relative error is `|estimate - official| / max(official, 2% of the area)`.) The three-year window improved accuracy, F1 and the median error, but the mean relative error got worse (0.41 to 0.46) because a few areas deviate strongly. On average the estimate agrees with the official map on about 90% of the pixels, but individual areas deviate strongly (for example a wetland mosaic in the Argentine Pampas: official 29 ha, estimate about 310 ha; on the 15 fixed test properties, Queensland 2023 was the worst case: official 896 ha, estimate 86 ha). Pooling several years, balancing classes differently, larger training regions and regularization were also tried: none beat the three-year window by a clear margin, so the simpler design was kept. On the 15 fixed test properties the mean accuracy was 0.88 and the median relative error 0.09. The estimate is therefore good as an order of magnitude and never as a measurement; for this reason the default year stays 2022 and the figures of 2022 and of later years must not be compared as a trend (different source and resolution, 30 m versus 10 m).

### Masked pixels of Global Pasture Watch

Global Pasture Watch leaves its class band masked where its model did not run (water, bare ground, dense urban areas). `tools/check_gpw_mask.py` compares those pixels with ESA WorldCover 2021 over 27 buffers on all continents: 74.5% of the masked pixels are built-up, bare, snow or water, 1.6% are WorldCover grassland, and only 1.0% of all WorldCover grassland is masked; the remainder is tree, shrub or cropland. Masked therefore means not grassland, and Pasto10 counts it as class 0.

### Biomass (dry-matter production)

`generate_biomass_map` and `get_biomass_total` report the dry-matter production of the pasture of a registered area: mean (t DM/ha) and total (t DM). The figures are the accumulated production of a period, not standing biomass nor available forage, and they say nothing about stocking capacity.

| Request | Source | Resolution |
| --- | --- | --- |
| No year, no month | Global Pasture Watch annual productivity (`ggpp-30m/v1-1/ugpp_m`, band `gc_m2`), latest year (2025) | 30 m |
| A year from 2000 to 2025 | The same collection, that year | 30 m |
| A month (with or without year) | Time2Graze productivity (`projects/wri-lcl-time2graze/assets/ugpp_prod_10m_v1`, band `ugpp`), average of the images of the month times its days | 10 m |
| A year after 2025 | Time2Graze accumulated over the part of the year with data (the real period and number of days are reported) | 10 m |

The source is chosen for the whole area, never pixel by pixel, and a missing period is reported as unavailable instead of being replaced by another source. Time2Graze covers only recent months (currently a few recent months, discovered dynamically and cached for a day) and only part of the world, so many areas have no monthly data. Pasture pixels are those of the Global Pasture Watch grassland map (`ggc-30m/v1/grassland_c`, masked pixels counted as not grassland); that map ends in 2022, so later periods use the 2022 mask and say so. An area with less than 1% of productive pasture returns no figures.

Productivity (gC/m²) becomes dry matter with a single constant, `DRY_MATTER_PER_GC_M2 = 0.0135` t DM/ha per gC/m² (0.50 light-use efficiency x 2.7 carbon-to-dry-matter x 0.01 unit conversion). It is a general approximation calibrated for cultivated tropical pastures; elsewhere read it as an order of magnitude. Annual and monthly figures, and Global Pasture Watch and Time2Graze figures, must not be compared with each other.

### Caches

Results are cached per process (areas, trained forests and the latest embedding year for an hour), and concurrent requests for the same geometry and year compute only once. Because the forest is not bit-for-bit reproducible, the first estimate of a year is kept until the process restarts. Set `PASTO10_CACHE_DIR` to a writable directory to also keep results on disk between runs (off by default; entries are keyed by geometry, year, source and method version).

Both map portfolios are developed with contributions from [LAPIG/UFG](https://lapig.iesa.ufg.br/), the Image Processing and Geoprocessing Laboratory of the Federal University of Goiás, which maintains this repository.

## Architecture

## Getting started

Install the dependencies with `uv sync` and run the tests with `uv run pytest` (offline). The pasture tools query Google Earth Engine, so they need the `GEE_*` variables described below.

## Configuration

### Google Earth Engine

The pasture map and area tools need an Earth Engine project and a service account (see `.env.example`):

- `GEE_PROJECT` — Google Cloud project registered for Earth Engine.
- `GEE_SERVICE_ACCOUNT` — service account e-mail.
- `GEE_KEY_FILE` — path to the service account JSON key (keep it out of version control).

Earth Engine is initialized lazily on the first pasture request, never at import. Without these variables the tools answer that the satellite data service is unavailable.

### Test properties and end-to-end tests

`tests/fixtures/global_properties.json` (used by the pasture and biomass e2e tests; loader `tests/fixtures/global_properties.py`) fixes 15 properties around the world: cultivated pasture (Goiás, Ireland, Queensland, New Zealand), natural grassland and steppe (Uruguay, Alberta, Mongolia, Kazakhstan, South African highveld, Kenyan savanna, Qinghai-Tibet Plateau), mosaics (Kansas, Sahel) and two negative controls (São Paulo, Sahara). The e2e test checks that each point really has its described profile against independent assets (GPW v1, ESA WorldCover, SRTM elevation).

`uv run pytest` skips the real Earth Engine tests. With the `GEE_*` variables exported run:

```
uv run pytest -m e2e -s                                # everything
uv run pytest -m e2e -s tests/e2e/test_biomass_e2e.py  # biomass tools on the 15 properties, prints a summary table
uv run pytest -m e2e -s tests/e2e/test_generalist_e2e.py -k tools_over   # the 15 properties, prints a summary table
PASTO10_E2E_OUT=/some/dir uv run pytest -m e2e -s      # also saves the generated maps
uv run python tools/benchmark_pasture.py --designs slide3 --out bench.jsonl   # validation against v1-1
```

### Basemap providers

The confirmation image of a registered area is drawn over a tile basemap, configured by environment variables (see `.env.example`):

- `PASTO10_BASEMAP_PROVIDERS` — providers tried in order (default `esri,osm`). Unknown names are ignored with a warning; an empty value or `none` disables the basemap and the image uses a neutral background.
- `ARCGIS_API_KEY` — optional [ArcGIS Location Platform](https://location.arcgis.com/) API key. When set, Esri tiles are requested from the authenticated basemap endpoint. Pasto10 masks the key in its own log messages, but avoid enabling DEBUG logging for urllib3 in production, as it would log request URLs including the token.

Terms of use:

- **Esri World Imagery** — without an API key the public tile service is used, whose use is governed by the [Esri terms of use](https://www.esri.com/en-us/legal/terms/full-master-agreement); production deployments should set `ARCGIS_API_KEY`. The image always shows the required attribution ("Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community" and "Powered by Esri").
- **OpenStreetMap** — tiles from `tile.openstreetmap.org` follow the [OSM Tile Usage Policy](https://operations.osmfoundation.org/policies/tiles/) (light use, identifying User-Agent, respect the HTTP caching headers, no bulk downloads) and carry the "© OpenStreetMap contributors" attribution. Pasto10 is designed for light use only (one tile mosaic per registration) and keeps no local tile cache; for heavier use, switch to a commercial OSM tile provider.

## Acknowledgments

Pasto10 is developed by [LAPIG/UFG](https://lapig.iesa.ufg.br/) in the context of the Global Pasture Watch and Time2Graze initiatives. We thank the partners of both initiatives for producing and maintaining the map products that make this assistant possible.

## License

This project is licensed under the [GPL-3.0-or-later](LICENSE). As a Semente derivative, it must remain open.