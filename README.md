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
| After 2022 | [Google Satellite Embedding](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_SATELLITE_EMBEDDING_V1_ANNUAL) (AlphaEarth, 10 m) | No published map exists yet: a Random Forest (100 trees, 300 samples per class, fixed seed) is trained on the fly with the 2022 Global Pasture Watch classes (`ggc-30m/v1-1`) and applied to the embedding of the requested year. The result is an estimate, not an official map and can vary strongly from year to year. |

Cells that Global Pasture Watch leaves without data (water, bare ground, dense urban areas) are counted as non-grassland, so the analyzed area equals the whole registered area.

When no year is given the tools use 2022, the latest Global Pasture Watch map; a later year must be requested explicitly and always returns an experimental estimate. The estimate is trained once per area and cached per process, so repeated requests report the same figures until restart.

Pasture area is the sum of cultivated and natural/semi-natural grassland, measured in hectares inside the registered area. Time2Graze is not yet integrated into the pasture tools. Figures for 2000-2022 (Global Pasture Watch v1) and for later years (classification trained on v1-1 / 2022, different resolution) must not be compared directly as a trend. Known limitation: the classifier is trained on a single year (2022) and applied to the embeddings of other years, so estimates after 2022 can swing widely between years (in tests, the same 1 km area varied from about 1 to 60 ha in consecutive years) and, being trained with balanced classes, tend to overestimate pasture in the training year. In tests the estimate differed from the official map and between years by several times the value. Larger or differently weighted training samples did not make it stable, so treat post-2022 figures as indicative only. Satellite-derived figures support decisions but do not replace field verification.

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

### End-to-end test

`uv run pytest` skips the real Earth Engine test. With the `GEE_*` variables exported, run `uv run pytest -m e2e -s`: it analyzes a 500 m buffer at (-15.5, -47.5) for 2020 (Global Pasture Watch) and 2024 (classification), prints the timings and, when `PASTO10_E2E_OUT` points to a directory, saves the maps there.

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