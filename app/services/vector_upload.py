"""Turn an uploaded vector file into validated GeoJSON features for ``feature_layers``.

GeoJSON (.geojson, .json) and CSV (.csv, geometry from a WKT column or lon/lat columns) are
read here. Anything else, such as a zipped shapefile, is refused with a hint to export to
GeoJSON first. Coordinates in another CRS are reprojected to EPSG:4326. Bad features are
skipped and counted, and the first few reasons are returned so the user can fix the file.
"""

import json
import re
from typing import Any

from app.core.geometry import validate_wgs84_geometry
from app.services.conversion.csv_geojson import csv_to_feature_collection
from app.services.conversion.reproject import WGS84, parse_crs

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_FEATURES = 100_000
_MAX_REASONS = 5


class UploadError(ValueError):
    """The file cannot be used; the message is safe to show the user."""


def _scalar(value: Any) -> Any:
    """Tiles carry flat values only: keep numbers, bools and text, store the rest as JSON text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False)


# Plain decimal numbers only: "00123" (a code) and "1e400" stay text.
_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?")


def _csv_value(value: Any) -> Any:
    """CSV cells arrive as text. Numbers become numbers so they can weight a heatmap."""
    if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        v = value.strip()
        return float(v) if "." in v else int(v)
    return value


def _features_of(doc: Any) -> list[dict]:
    if not isinstance(doc, dict):
        raise UploadError("The file is not a GeoJSON object")
    kind = doc.get("type")
    if kind == "FeatureCollection":
        feats = doc.get("features")
        if not isinstance(feats, list):
            raise UploadError("FeatureCollection has no features list")
        return feats
    if kind == "Feature":
        return [doc]
    if kind in {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}:
        return [{"type": "Feature", "geometry": doc, "properties": {}}]
    raise UploadError("Not GeoJSON: expected a FeatureCollection, Feature or geometry")


def _reproject(features: list[dict], src_crs: str) -> list[dict]:
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform

    t = Transformer.from_crs(src_crs, WGS84, always_xy=True).transform
    out = []
    for f in features:
        copy = dict(f)
        if f.get("geometry"):
            try:
                copy["geometry"] = mapping(transform(t, shape(f["geometry"])))
            except Exception:  # bad geometry is reported by the validation step
                pass
        out.append(copy)
    return out


def parse_upload(filename: str, data: bytes, *, crs: str | None = None) -> tuple[list[dict], int, list[str]]:
    """Return (features, skipped_count, reasons). Raises UploadError when nothing is usable."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    name = filename.lower()
    try:
        if name.endswith(".csv"):
            text = data.decode("utf-8-sig")
            features = csv_to_feature_collection(text)["features"]
            for f in features:
                f["properties"] = {k: _csv_value(v) for k, v in (f.get("properties") or {}).items()}
        elif name.endswith((".geojson", ".json")):
            features = _features_of(json.loads(data.decode("utf-8-sig")))
        else:
            raise UploadError(
                "Upload a .geojson, .json or .csv file. For a shapefile or GeoPackage, "
                "export it to GeoJSON first (QGIS: right click the layer, Export, Save Features As)."
            )
    except UnicodeDecodeError as exc:
        raise UploadError("The file is not UTF-8 text") from exc
    except json.JSONDecodeError as exc:
        raise UploadError(f"The file is not valid JSON: {exc.msg}") from exc
    except ValueError as exc:
        if isinstance(exc, UploadError):
            raise
        raise UploadError(str(exc)) from exc

    if len(features) > MAX_FEATURES:
        raise UploadError(f"More than {MAX_FEATURES:,} features. Split the file or build a tileset with the reference layer script.")
    if crs:
        try:
            src = parse_crs(crs)
        except ValueError as exc:
            raise UploadError(str(exc)) from exc
        if src != WGS84:
            features = _reproject(features, src)

    good: list[dict] = []
    reasons: list[str] = []
    skipped = 0
    for i, f in enumerate(features):
        try:
            if not isinstance(f, dict) or not f.get("geometry"):
                raise ValueError("no geometry")
            validate_wgs84_geometry(f["geometry"])
        except ValueError as exc:
            skipped += 1
            if len(reasons) < _MAX_REASONS:
                reasons.append(f"feature {i + 1}: {exc}")
            continue
        props = f.get("properties") or {}
        good.append({"geometry": f["geometry"], "properties": {str(k): _scalar(v) for k, v in props.items()}})
    if not good:
        raise UploadError("No usable features. " + ("; ".join(reasons) if reasons else "The file is empty."))
    return good, skipped, reasons
