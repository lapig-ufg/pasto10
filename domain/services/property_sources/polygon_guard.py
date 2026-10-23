"""Standalone guard that validates a user polygon in a separate process.

GEOS (the engine behind shapely) cannot be interrupted from a thread, and repairing a large self-intersecting polygon
can take minutes, so ``user_polygon_source.parse_geojson`` runs the shapely work of a user polygon here, as a child
process killed after a few seconds. The repair limit is a TOTAL budget over the invalid geometries of the request, not per geometry. The script imports only ``geometry_core`` (shapely and numpy), never the app.

Protocol: reads ONE JSON document from stdin, ``{"geometries": [...], "max_area_ha": n, "min_area_ha": n,
"simplify_m": n, "max_repair_vertices": n}``, and prints one JSON line: ``{"ok": true, "geometry": {...},
"area_ha": n}`` or ``{"ok": false, "kind": "..."}`` (kinds of ``InvalidPolygon``). It never raises.
"""

import importlib.util
import json
import sys
from pathlib import Path

_CORE = Path(__file__).with_name("geometry_core.py")


def _load_core():
    """Imports ``geometry_core`` by path, without importing the ``domain`` package."""
    spec = importlib.util.spec_from_file_location("geometry_core", _CORE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["geometry_core"] = module
    spec.loader.exec_module(module)
    return module


def validate(request: dict) -> dict:
    """Builds, repairs, simplifies and measures the largest polygon of the request.

    Args:
        request (dict): The protocol document (see the module docstring).

    Returns:
        dict: ``{"ok": True, "geometry", "area_ha"}`` or ``{"ok": False, "kind"}``.
    """
    from shapely import get_num_coordinates
    from shapely.geometry import mapping, shape

    core = _load_core()
    try:
        shapes = [
            shape(geometry) for geometry in request["geometries"]
            if isinstance(geometry, dict) and geometry.get("type") in ("Polygon", "MultiPolygon")
        ]
        repair_budget = sum(get_num_coordinates(item) for item in shapes if not item.is_valid)
        if repair_budget > request["max_repair_vertices"]:
            return {"ok": False, "kind": "too_complex"}
        polygons = []
        for item in shapes:
            part = core.polygonal_part(item, request["max_repair_vertices"])
            if part is not None:
                polygons.append(part)
        if not polygons:
            return {"ok": False, "kind": "not_polygon"}
        polygon = max(polygons, key=lambda item: item.area)
        simplified = core.polygonal_part(core.simplify_meters(polygon, request["simplify_m"]), request["max_repair_vertices"]) or polygon
        area = core.area_hectares(simplified)
        if area > request["max_area_ha"]:
            return {"ok": False, "kind": "too_large"}
        if area < request["min_area_ha"]:
            return {"ok": False, "kind": "too_small"}
        return {"ok": True, "geometry": mapping(simplified), "area_ha": area}
    except core.GeometryTooComplex:
        return {"ok": False, "kind": "too_complex"}
    except Exception:
        return {"ok": False, "kind": "invalid_geometry"}


if __name__ == "__main__":
    try:
        result = validate(json.loads(sys.stdin.read()))
    except Exception:
        result = {"ok": False, "kind": "invalid_geometry"}
    sys.stdout.write(json.dumps(result))
