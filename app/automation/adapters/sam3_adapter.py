from __future__ import annotations

from typing import Any

from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union

from app.automation.adapters.geo_utils import (
    geo_bbox_to_pixel_bbox,
    pixel_bbox_to_geo_polygon,
)

# Prompt-spec key the UI sends bbox exemplars under, in EPSG:4326
# ``[min_lon, min_lat, max_lon, max_lat]``. resolve_prompt reprojects these into
# each patch's pixel space and emits them under the generic ``bboxes`` key, which
# ``prompt_key_map`` then maps to the endpoint's expected request field.
GEO_BBOX_PROMPT_KEY = "bbox_prompts_4326"


def _geo_transform(context: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
    """Return (min_lon, max_lat, lon_span, lat_span, width, height) for the patch.

    Pixel (x, y) maps to geo via:
        lon = min_lon + (x / width) * lon_span
        lat = max_lat - (y / height) * lat_span
    """
    bbox = context.get("bbox")
    width = float(context.get("width") or 0)
    height = float(context.get("height") or 0)
    if not isinstance(bbox, list) or len(bbox) != 4 or width <= 0 or height <= 0:
        raise ValueError("SAM3 polygon conversion requires context bbox/width/height")
    min_lon, min_lat, max_lon, max_lat = [float(v) for v in bbox]
    return min_lon, max_lat, (max_lon - min_lon), (max_lat - min_lat), width, height


def _pixel_ring_to_geo(
    points: list[list[float]],
    *,
    min_lon: float,
    max_lat: float,
    lon_span: float,
    lat_span: float,
    width: float,
    height: float,
) -> list[tuple[float, float]]:
    ring: list[tuple[float, float]] = []
    for pt in points:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            continue
        x = float(pt[0])
        y = float(pt[1])
        lon = min_lon + (x / width) * lon_span
        lat = max_lat - (y / height) * lat_span
        ring.append((lon, lat))
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _to_geo_from_pixel_polygon(
    points: list[list[float]],
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    if len(points) < 3:
        raise ValueError("SAM3 polygon conversion requires at least 3 points")
    min_lon, max_lat, lon_span, lat_span, width, height = _geo_transform(context)
    ring = _pixel_ring_to_geo(
        points,
        min_lon=min_lon, max_lat=max_lat,
        lon_span=lon_span, lat_span=lat_span,
        width=width, height=height,
    )
    if len(ring) < 4:  # 3 distinct + closing point
        raise ValueError("SAM3 polygon has insufficient valid points")

    poly = Polygon(ring)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly.__geo_interface__


def _to_geo_from_pixel_polygons(
    polys: list[list[list[list[float]]]],
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Convert a list of pixel-space polygons (each ``[exterior, hole, ...]``)
    into a single geo ``(Multi)Polygon`` GeoJSON geometry, preserving holes and
    every disconnected blob. This is the high-fidelity path produced by the SAM3
    endpoint's ``polygons`` field (holes-aware mask vectorization)."""
    min_lon, max_lat, lon_span, lat_span, width, height = _geo_transform(context)

    shapely_polys: list[Polygon] = []
    for rings in polys:
        if not isinstance(rings, list) or not rings:
            continue
        geo_rings: list[list[tuple[float, float]]] = []
        for r in rings:
            if not isinstance(r, list) or len(r) < 3:
                continue
            gr = _pixel_ring_to_geo(
                r,
                min_lon=min_lon, max_lat=max_lat,
                lon_span=lon_span, lat_span=lat_span,
                width=width, height=height,
            )
            if len(gr) >= 4:
                geo_rings.append(gr)
        if not geo_rings:
            continue
        try:
            poly = Polygon(geo_rings[0], geo_rings[1:])
        except (ValueError, TypeError):
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            shapely_polys.append(poly)

    if not shapely_polys:
        raise ValueError("SAM3 polygons produced no valid geometry")

    geom = shapely_polys[0] if len(shapely_polys) == 1 else unary_union(shapely_polys)
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty:
        raise ValueError("SAM3 polygons produced empty geometry")
    # Normalize a single-part union back to Polygon; keep MultiPolygon otherwise.
    if isinstance(geom, MultiPolygon) and len(geom.geoms) == 1:
        geom = geom.geoms[0]
    return geom.__geo_interface__


def convert(raw: Any, config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Convert common SAM3-style outputs into platform prediction format.

    Supported structures:
    - {"predictions": [ ... ]}
    - {"instances": [ ... ]}
    - {"masks": [ ... ]}
    - [ ... ]  (list of prediction objects)

    Per-instance geometry priority:
    1) ``geometry`` (GeoJSON)
    2) configured ``polygons`` field (holes-aware pixel polygons -> geo MultiPolygon)
    3) configured ``polygon`` field (pixel points -> geo polygon)
    4) configured ``bbox`` field ([x, y, w, h] in pixels -> geo polygon)
    """
    if isinstance(raw, dict):
        entries = raw.get("predictions") or raw.get("instances") or raw.get("masks") or []
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("sam3 adapter expects dict or list output")

    label_field = str(config.get("label_field", "label"))
    score_field = str(config.get("score_field", "score"))
    polygons_field = str(config.get("polygons_field", "polygons"))
    polygon_field = str(config.get("polygon_field", "polygon"))
    bbox_field = str(config.get("bbox_field", "bbox"))
    default_label = str(config.get("default_label", "object"))
    min_score = float(config.get("min_score", 0.0))

    preds: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        confidence = float(entry.get(score_field, entry.get("confidence", 1.0)) or 1.0)
        if confidence < min_score:
            continue

        label = str(entry.get(label_field, default_label))

        # Geometry resolution cascades: each source is tried only if the higher
        # priority one is absent or fails to produce valid geometry.
        geom: dict[str, Any] | None = None
        if isinstance(entry.get("geometry"), dict):
            geom_candidate = shape(entry["geometry"])
            if not geom_candidate.is_valid:
                geom_candidate = geom_candidate.buffer(0)
            geom = geom_candidate.__geo_interface__
        if geom is None and isinstance(entry.get(polygons_field), list) and entry.get(polygons_field):
            try:
                geom = _to_geo_from_pixel_polygons(entry[polygons_field], context=context)
            except Exception:
                geom = None
        if geom is None and isinstance(entry.get(polygon_field), list):
            try:
                geom = _to_geo_from_pixel_polygon(entry[polygon_field], context=context)
            except Exception:
                geom = None
        if geom is None and isinstance(entry.get(bbox_field), list) and len(entry[bbox_field]) == 4:
            bbox = entry[bbox_field]
            geom = pixel_bbox_to_geo_polygon(
                float(bbox[0]),
                float(bbox[1]),
                float(bbox[2]),
                float(bbox[3]),
                context=context,
            )

        if geom is None:
            continue

        props = dict(entry.get("properties") or {})
        props["source"] = "sam3"
        preds.append(
            {
                "label": label,
                "confidence": confidence,
                "geometry": geom,
                "properties": props,
            }
        )

    return {
        "format_version": "1.0",
        "predictions": preds,
        "metadata": {"adapter_used": "sam3_to_platform"},
    }


def resolve_prompt(
    prompt_spec: dict[str, Any],
    patch_context: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve the model's static prompt spec into a per-patch prompt_payload.

    One registered SAM3 model serves all three prompt modes, decided purely by
    what the spec carries:

    - **text only** — no ``bbox_prompts_4326``: the spec is forwarded unchanged
      to every patch (text concepts are location-independent).
    - **bbox only** / **text + bbox** — ``bbox_prompts_4326`` present: each geo
      box is clipped to *this* patch and reprojected to patch-pixel ``xyxy``.
      Patches the boxes don't overlap return ``None`` so ModelManager skips them
      (the spatial prompt defines the region of interest). Any text in the spec
      rides along on the patches that do overlap.
    """
    spec = dict(prompt_spec or {})
    geo_boxes = spec.pop(GEO_BBOX_PROMPT_KEY, None)

    # No spatial prompt → pass the (text / empty) spec through to every patch.
    if not isinstance(geo_boxes, list) or not geo_boxes:
        return spec

    pixel_boxes: list[list[float]] = []
    for gb in geo_boxes:
        if not isinstance(gb, (list, tuple)) or len(gb) != 4:
            continue
        try:
            px = geo_bbox_to_pixel_bbox([float(v) for v in gb], context=patch_context)
        except (ValueError, TypeError):
            px = None
        if px is not None:
            pixel_boxes.append(px)

    if not pixel_boxes:
        return None  # spatial prompt doesn't touch this patch → skip it

    spec["bboxes"] = pixel_boxes
    return spec


def enrich_request(
    body: dict[str, Any],
    prompt_payload: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Map generic prompt_payload keys into SAM3-specific request keys.

    The generic ``prompt_payload`` is still preserved verbatim in ``body``.
    ``prompt_key_map`` lets the model owner define how frontend-sent keys
    should be renamed for the concrete SAM3 endpoint contract.
    """
    prompt_key_map = config.get("prompt_key_map") or {}
    if not isinstance(prompt_key_map, dict):
        return body

    for source_key, target_key in prompt_key_map.items():
        if (
            isinstance(source_key, str)
            and isinstance(target_key, str)
            and source_key in prompt_payload
        ):
            body[target_key] = prompt_payload[source_key]

    return body
