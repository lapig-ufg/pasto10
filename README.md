# Pasto10

**Multi-agent decision-support tool for pasture management, built on the [Semente](https://github.com/lapig-ufg/semente) framework and powered by maps from the Global Pasture Watch and Time2Graze initiatives.**

---

## About

Pasto10 is a decision-support system that combines multi-agent AI with satellite-based pasture maps to help Brazilian ranchers monitor, diagnose, and improve their pasturelands. Instead of navigating dashboards and map layers, users converse with an assistant that interprets the data for them, answers questions about specific properties or municipalities, and turns map indicators into practical management recommendations.

The project is a **showcase and reference implementation of [Semente](https://github.com/lapig-ufg/semente)**, the multi-agent AI chat framework for land use and agriculture. Just as Pasto Legal (LAPIG/UFG's WhatsApp assistant for pasture diagnostics) was the seed from which Semente was extracted, Pasto10 demonstrates how the framework grows into a new domain — in this case, one grounded in Global Pasture Watch and Time2Graze map products.

## Data

Pasto10's knowledge comes from maps produced in the context of two initiatives:

- **Global Pasture Watch** — a global collaboration delivering the first open, satellite-derived maps of pastureland extent, condition, and productivity. These layers tell us where pastures are and how degraded or vigorous they are.
- **Time2Graze** — an initiative advancing temporal analysis of grazing lands, including pasture dynamics over time series that support management decisions.

Both map portfolios are developed with contributions from [LAPIG/UFG](https://lapig.iesa.ufg.br/), the Image Processing and Geoprocessing Laboratory of the Federal University of Goiás, which maintains this repository.

## Architecture

## Getting started

## Configuration

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