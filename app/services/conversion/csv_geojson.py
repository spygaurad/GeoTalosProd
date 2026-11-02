"""GeoJSON <-> CSV conversion for annotation features.

Uses only the standard ``csv`` / ``json`` modules plus Shapely for WKT, so no
dataframe dependency is needed for this lightweight exchange format.

- ``features_to_csv``: GeoJSON features -> CSV text. One row per feature with
  ``id``, ``class_id``, ``class_name``, ``confidence``, ``geometry_wkt`` (any
  geometry type), ``longitude`` / ``latitude`` (points only; ``x`` / ``y`` for
  projected exports), then one column per remaining property.
- ``csv_to_feature_collection``: CSV text -> GeoJSON FeatureCollection, reading
  geometry from a WKT column or from longitude / latitude columns. The result
  is fed to the existing GeoJSON import job, so geometry and class validation
  stay in one place.
"""

from __future__ import annotations

import csv
import io
import json
import math
from typing import Any

# Columns written first, in this order. Feature properties with the same name
# are not repeated as extra columns.
CSV_BASE_COLUMNS = (
    "id",
    "class_id",
    "class_name",
    "confidence",
    "geometry_wkt",
    "longitude",
    "latitude",
)
# Alternative property key for the class name (used by the automation export).
_CLASS_NAME_KEYS = ("class_name", "class")

WKT_COLUMN_CANDIDATES = ("geometry_wkt", "wkt", "geometry", "geom")
LON_COLUMN_CANDIDATES = ("longitude", "lon", "lng", "long", "x")
LAT_COLUMN_CANDIDATES = ("latitude", "lat", "y")

# A detailed polygon's WKT can exceed the csv module's default 131,072-character
# cell limit; allow cells up to the import size cap instead.
_MAX_CELL_CHARS = 50 * 1024 * 1024


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def features_to_csv(
    features: list[dict],
    class_names: dict[str, str] | None = None,
    *,
    xy_columns: tuple[str, str] = ("longitude", "latitude"),
) -> str:
    """Serialize GeoJSON features to CSV text.

    ``class_names`` maps class-id (str) -> name; when omitted the name is read
    from the feature's ``class_name`` / ``class`` property. ``xy_columns``
    names the point-coordinate columns (use ``("x", "y")`` for projected CRSs).
    """
    from shapely.geometry import shape

    class_names = class_names or {}
    base_columns = (*CSV_BASE_COLUMNS[:-2], *xy_columns)
    reserved = set(CSV_BASE_COLUMNS) | set(base_columns) | set(_CLASS_NAME_KEYS)

    extra_columns: list[str] = []
    seen_extra: set[str] = set()
    for feature in features:
        for key in (feature.get("properties") or {}):
            if key not in reserved and key not in seen_extra:
                seen_extra.add(key)
                extra_columns.append(key)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([*base_columns, *extra_columns])

    for feature in features:
        props = feature.get("properties") or {}
        geom = feature.get("geometry")
        class_id = props.get("class_id")
        class_name = class_names.get(str(class_id)) if class_id is not None else None
        if class_name is None:
            class_name = next((props[k] for k in _CLASS_NAME_KEYS if props.get(k)), None)

        wkt = ""
        lon = lat = None
        if geom:
            try:
                wkt = shape(geom).wkt
            except (ValueError, TypeError, AttributeError):
                wkt = ""
            if geom.get("type") == "Point":
                coords = geom.get("coordinates") or []
                if len(coords) >= 2:
                    lon, lat = coords[0], coords[1]

        writer.writerow([
            _cell(feature.get("id")),
            _cell(class_id),
            _cell(class_name),
            _cell(props.get("confidence")),
            wkt,
            _cell(lon),
            _cell(lat),
            *(_cell(props.get(col)) for col in extra_columns),
        ])

    return out.getvalue()


def _pick_column(header: list[str], explicit: str | None, candidates: tuple[str, ...]) -> str | None:
    if explicit:
        if explicit not in header:
            raise ValueError(f"Column {explicit!r} not found in CSV header")
        return explicit
    lowered = {name.strip().lower(): name for name in header}
    return next((lowered[c] for c in candidates if c in lowered), None)


def _row_geometry(row: dict[str, str], wkt_column: str | None, lon_column: str | None, lat_column: str | None):
    from shapely import wkt as shapely_wkt
    from shapely.geometry import mapping

    if wkt_column:
        text = (row.get(wkt_column) or "").strip()
        if not text:
            return None
        try:
            return mapping(shapely_wkt.loads(text))
        except Exception:  # noqa: BLE001 — shapely raises several parse errors
            return None

    try:
        lon = float((row.get(lon_column) or "").strip())
        lat = float((row.get(lat_column) or "").strip())
    except ValueError:
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return {"type": "Point", "coordinates": [lon, lat]}


def csv_to_feature_collection(
    text: str,
    *,
    wkt_column: str | None = None,
    lon_column: str | None = None,
    lat_column: str | None = None,
) -> dict:
    """Parse CSV text into a GeoJSON FeatureCollection (EPSG:4326).

    Geometry comes from ``wkt_column`` or from ``lon_column`` + ``lat_column``;
    when none are given the columns are detected from common header names
    (WKT preferred). Rows whose geometry cannot be parsed keep ``geometry:
    None`` so the import job reports them as failed rows. Empty cells are
    dropped; every other column becomes a string property, and an ``id``
    column becomes the feature id.

    Raises ``ValueError`` when the CSV has no header, no usable geometry
    columns, or cannot be parsed.
    """
    if csv.field_size_limit() < _MAX_CELL_CHARS:
        csv.field_size_limit(_MAX_CELL_CHARS)
    try:
        return _parse_csv(text, wkt_column=wkt_column, lon_column=lon_column, lat_column=lat_column)
    except csv.Error as exc:
        raise ValueError(f"Could not parse CSV: {exc}") from exc


def _parse_csv(
    text: str,
    *,
    wkt_column: str | None,
    lon_column: str | None,
    lat_column: str | None,
) -> dict:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [h for h in (reader.fieldnames or []) if h is not None]
    if not header:
        raise ValueError("CSV has no header row")

    if lon_column or lat_column:
        if not (lon_column and lat_column):
            raise ValueError("Both lon_column and lat_column are required")
        lon_col = _pick_column(header, lon_column, ())
        lat_col = _pick_column(header, lat_column, ())
        wkt_col = None
    else:
        wkt_col = _pick_column(header, wkt_column, WKT_COLUMN_CANDIDATES)
        lon_col = lat_col = None
        if wkt_col is None:
            lon_col = _pick_column(header, None, LON_COLUMN_CANDIDATES)
            lat_col = _pick_column(header, None, LAT_COLUMN_CANDIDATES)
            if lon_col is None or lat_col is None:
                raise ValueError(
                    "CSV needs a WKT geometry column (e.g. 'geometry_wkt') or "
                    "'longitude' and 'latitude' columns"
                )

    # Coordinate columns are geometry, not properties — including the
    # longitude/latitude columns that our own export writes next to WKT.
    geometry_columns = {
        c
        for c in (
            wkt_col,
            lon_col,
            lat_col,
            _pick_column(header, None, LON_COLUMN_CANDIDATES),
            _pick_column(header, None, LAT_COLUMN_CANDIDATES),
        )
        if c
    }
    features = []
    for row in reader:
        properties = {
            key: value
            for key, value in row.items()
            if key is not None and key not in geometry_columns and key != "id"
            and value not in (None, "")
        }
        feature: dict[str, Any] = {
            "type": "Feature",
            "geometry": _row_geometry(row, wkt_col, lon_col, lat_col),
            "properties": properties,
        }
        if row.get("id"):
            feature["id"] = row["id"]
        features.append(feature)

    return {"type": "FeatureCollection", "features": features}
