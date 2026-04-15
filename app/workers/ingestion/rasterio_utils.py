"""
COG validation, metadata extraction, and STAC object builders.

All rasterio / shapely imports are inside the functions (lazy) so that
importing this module at API startup does not load native libs.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Rendering metadata constants ─────────────────────────────────────────────

# Band name → common spectral name mapping (case-insensitive matching)
_BAND_NAME_MAP: dict[str, str] = {
    # Sentinel-2
    "b01": "coastal", "b02": "blue", "b03": "green", "b04": "red",
    "b05": "rededge1", "b06": "rededge2", "b07": "rededge3",
    "b08": "nir", "b8a": "nir08", "b09": "watervapor",
    "b10": "cirrus", "b11": "swir16", "b12": "swir22",
    # Landsat 8/9
    "sr_b1": "coastal", "sr_b2": "blue", "sr_b3": "green", "sr_b4": "red",
    "sr_b5": "nir", "sr_b6": "swir16", "sr_b7": "swir22",
    # Common names
    "red": "red", "green": "green", "blue": "blue",
    "nir": "nir", "nir1": "nir", "nir08": "nir08",
    "swir1": "swir16", "swir2": "swir22", "swir16": "swir16", "swir22": "swir22",
    "coastal": "coastal", "rededge": "rededge1",
    "pan": "pan", "panchromatic": "pan",
}

# Wavelength ranges (nm) → common spectral name
_WAVELENGTH_RANGES: list[tuple[float, float, str]] = [
    (430, 460, "coastal"),
    (460, 525, "blue"),
    (525, 600, "green"),
    (630, 690, "red"),
    (695, 715, "rededge1"),
    (730, 750, "rededge2"),
    (770, 795, "rededge3"),
    (780, 905, "nir"),
    (850, 880, "nir08"),
    (1360, 1390, "cirrus"),
    (1565, 1660, "swir16"),
    (2100, 2300, "swir22"),
]

# Rendering presets — each needs specific spectral bands
SPECTRAL_PRESETS: dict[str, dict] = {
    "natural_color": {
        "requires": ["red", "green", "blue"],
        "bands": ["red", "green", "blue"],
        "label": "Natural Color (RGB)",
    },
    "false_color": {
        "requires": ["nir", "red", "green"],
        "bands": ["nir", "red", "green"],
        "label": "False Color (Vegetation)",
    },
    "ndvi": {
        "requires": ["nir", "red"],
        "expression_tpl": "(b{nir}-b{red})/(b{nir}+b{red})",
        "colormap": "rdylgn",
        "rescale": "-1,1",
        "label": "NDVI (Vegetation Index)",
    },
    "swir_composite": {
        "requires": ["swir16", "nir", "red"],
        "bands": ["swir16", "nir", "red"],
        "label": "SWIR Composite",
    },
    "agriculture": {
        "requires": ["swir16", "nir", "blue"],
        "bands": ["swir16", "nir", "blue"],
        "label": "Agriculture",
    },
    "moisture": {
        "requires": ["nir", "swir16"],
        "expression_tpl": "(b{nir}-b{swir16})/(b{nir}+b{swir16})",
        "colormap": "blues_r",
        "rescale": "-1,1",
        "label": "Moisture Index",
    },
    "urban": {
        "requires": ["swir22", "swir16", "red"],
        "bands": ["swir22", "swir16", "red"],
        "label": "Urban",
    },
    "color_infrared": {
        "requires": ["nir", "red", "green"],
        "bands": ["nir", "red", "green"],
        "label": "Color Infrared (CIR)",
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _vsi_path(s3_uri: str) -> str:
    """Convert ``s3://bucket/key`` to GDAL VSI path ``/vsis3/bucket/key``."""
    return s3_uri.replace("s3://", "/vsis3/", 1)


def _extract_datetime_from_filename_patterns(
    filename_or_path: str,
) -> tuple[str | None, str | None]:
    """Extract datetime(s) from filename patterns, including hash-prefixed names.

    Supports:
    1. YYYYMMDDTHHMMSSZ_[name].tif         → single datetime
    2. YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ_[name].tif → datetime range (start, end)

    Returns (datetime_str, end_datetime_str) or (None, None) if no match.
    Both datetimes are returned as ISO-8601 UTC strings.
    """
    basename = os.path.basename(filename_or_path or "")
    # Accept '_'/'-'/'.' separators so hashed ZIP member names still match.
    ts_token = r"\d{8}T\d{6}[Zz]"

    # Pattern 2: timestamp range
    match_range = re.search(
        rf"(?<!\d)({ts_token})[_\-.]+({ts_token})(?!\d)",
        basename,
    )
    if match_range:
        try:
            start_str, end_str = match_range.groups()
            # Parse compact ISO format: YYYYMMDDTHHMMSSZ
            start_dt = datetime.strptime(start_str.upper(), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            end_dt = datetime.strptime(end_str.upper(), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            return start_dt.isoformat(), end_dt.isoformat()
        except ValueError:
            pass

    # Pattern 1: single timestamp
    match_single = re.search(rf"(?<!\d)({ts_token})(?!\d)", basename)
    if match_single:
        try:
            ts_str = match_single.group(1)
            dt = datetime.strptime(ts_str.upper(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.isoformat(), None
        except ValueError:
            pass

    return None, None


def _extract_datetime(tags: dict, filename: str) -> str:
    """Return an ISO-8601 UTC datetime string for the image acquisition time.

    Sources tried in order:
    1. Filename patterns: YYYYMMDDTHHMMSSZ_[name] or YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ_[name]
    2. TIFFTAG_DATETIME (format ``YYYY:MM:DD HH:MM:SS``)
    3. ACQUISITIONDATETIME, DATE, date_acquired, acquisition_date metadata tags
    4. ISO date ``YYYY-MM-DD`` anywhere in the filename
    5. Compact date ``YYYYMMDD`` anywhere in the filename
    6. Year-month ``YYYY-MM`` anywhere in the filename (defaults to 1st of month)
    7. Current UTC time (logged as a warning)
    """
    # Step 1: Check for specific timestamp patterns in filename
    dt_str, _ = _extract_datetime_from_filename_patterns(filename)
    if dt_str:
        return dt_str

    def _norm(raw: str) -> str:
        """Normalize common datetime string variants to improve parsing."""
        s = raw.strip().strip('"').strip("'")
        # Handle trailing UTC designators and compact separators.
        s = s.replace("UTC", "").replace("Z", "").strip()
        s = re.sub(r"\s+", " ", s)
        return s

    # 1 + 2: GDAL / TIFF metadata tags (case-insensitive search)
    tag_keys = (
        "TIFFTAG_DATETIME", "ACQUISITIONDATETIME", "DATE",
        "date_acquired", "DATE_ACQUIRED", "acquisition_date",
        "ACQUISITION_DATE", "datetime", "DATETIME", "time",
        "system:time_start", "system:time_end", "TIMESTAMP", "timestamp",
        "START_DATETIME", "END_DATETIME", "acquisition_time",
    )
    tag_items = {str(k): str(v) for k, v in (tags or {}).items()}
    lower_map = {k.lower(): v for k, v in tag_items.items()}
    for key in tag_keys:
        raw = tag_items.get(key) or lower_map.get(key.lower()) or ""
        raw = _norm(raw)
        if not raw:
            continue
        # Earth Engine often stores epoch milliseconds in system:time_start.
        if re.fullmatch(r"\d{13}", raw):
            try:
                dt = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
                return dt.isoformat()
            except Exception:
                pass
        # Epoch seconds
        if re.fullmatch(r"\d{10}", raw):
            try:
                dt = datetime.fromtimestamp(int(raw), tz=timezone.utc)
                return dt.isoformat()
            except Exception:
                pass
        for fmt in (
            "%Y:%m:%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d",
            "%Y%m%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
        # Handle quarter labels like 2024_Q1 / 2024-Q1 / Q1_2024
        qmatch = (
            re.search(r"(\d{4})[_-]?Q([1-4])", raw, flags=re.IGNORECASE)
            or re.search(r"Q([1-4])[_-]?(\d{4})", raw, flags=re.IGNORECASE)
        )
        if qmatch:
            try:
                if raw.upper().startswith("Q"):
                    quarter = int(qmatch.group(1))
                    year = int(qmatch.group(2))
                else:
                    year = int(qmatch.group(1))
                    quarter = int(qmatch.group(2))
                month = ((quarter - 1) * 3) + 1
                dt = datetime(year, month, 1, tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:
                pass

    basename = os.path.basename(filename)
    
    # 3: ISO date in filename (YYYY-MM-DD)
    match = re.search(r"(\d{4}-\d{2}-\d{2})", basename)
    if match:
        try:
            dt = datetime.fromisoformat(match.group(1))
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    # 4: Compact date in filename (YYYYMMDD) - common in satellite data
    match = re.search(r"(?:^|[_\-\.])(\d{8})(?:[_\-\.]|$)", basename)
    if match:
        try:
            dt = datetime.strptime(match.group(1), "%Y%m%d")
            # Validate it's a reasonable date (1970-2100)
            if 1970 <= dt.year <= 2100:
                return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    
    # 5: Year-month in filename (YYYY-MM) - default to 1st of month
    match = re.search(r"(\d{4})-(\d{2})(?:[_\-\.]|$)", basename)
    if match:
        try:
            year, month = int(match.group(1)), int(match.group(2))
            if 1970 <= year <= 2100 and 1 <= month <= 12:
                dt = datetime(year, month, 1)
                return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    # 5b: Quarter in filename (YYYY_Qn / YYYY-Qn / Qn_YYYY)
    qmatch = (
        re.search(r"(\d{4})[_-]?Q([1-4])", basename, flags=re.IGNORECASE)
        or re.search(r"Q([1-4])[_-]?(\d{4})", basename, flags=re.IGNORECASE)
    )
    if qmatch:
        try:
            if basename.upper().startswith("Q"):
                quarter = int(qmatch.group(1))
                year = int(qmatch.group(2))
            else:
                year = int(qmatch.group(1))
                quarter = int(qmatch.group(2))
            month = ((quarter - 1) * 3) + 1
            dt = datetime(year, month, 1)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    # 6: fallback
    logger.warning("datetime_not_found filename=%s — using current UTC", filename)
    return datetime.now(timezone.utc).isoformat()


# ── Rendering metadata extraction ────────────────────────────────────────────


def _safe_float(val: float, default: float = 0.0) -> float:
    """Convert a float to JSON-safe value, replacing NaN/Inf with default."""
    import math
    if val is None or math.isnan(val) or math.isinf(val):
        return default
    return float(val)


def _sanitize_for_json(obj: object) -> object:
    """Recursively sanitize an object for JSON serialization.
    
    Converts NaN/Inf floats to None, handles nested dicts and lists.
    """
    import math
    
    if obj is None:
        return None
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_for_json(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(_sanitize_for_json(item) for item in obj)
    else:
        return obj


def _compute_band_stats(src: object, band_idx: int, nodata: float | None, max_size: int = 1024) -> dict:
    """Compute min/max/mean/p2/p98 for a single band via decimated read.
    
    Returns JSON-safe values (NaN/Inf converted to 0).
    """
    import numpy as np

    h, w = src.height, src.width  # type: ignore[attr-defined]
    # Decimated read — read at overview resolution for speed
    out_h = min(h, max_size)
    out_w = min(w, max_size)
    data = src.read(band_idx, out_shape=(out_h, out_w))  # type: ignore[attr-defined]

    # Mask nodata and NaN values
    if nodata is not None:
        masked = np.ma.masked_equal(data, nodata)
    else:
        masked = np.ma.array(data)
    
    # Also mask any NaN values in the data itself
    masked = np.ma.masked_invalid(masked)

    if masked.count() == 0:
        return {"min": 0, "max": 0, "mean": 0, "p2": 0, "p98": 0}

    compressed = masked.compressed()
    
    # Handle edge case where compressed array might still have issues
    if len(compressed) == 0:
        return {"min": 0, "max": 0, "mean": 0, "p2": 0, "p98": 0}
    
    try:
        p2, p98 = np.percentile(compressed, [2, 98]).tolist()
    except Exception:
        p2, p98 = 0.0, 0.0
    
    return {
        "min": _safe_float(compressed.min()),
        "max": _safe_float(compressed.max()),
        "mean": round(_safe_float(compressed.mean()), 2),
        "p2": round(_safe_float(p2), 2),
        "p98": round(_safe_float(p98), 2),
    }


def _identify_spectral_band(
    index: int,
    colorinterp_name: str,
    description: str,
    tags: dict,
) -> str | None:
    """Try to identify a band's spectral role (red, green, blue, nir, etc.).

    Cascade: description → wavelength → colorinterp.
    """
    # 1. Match description against known band names
    if description:
        key = description.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        if key in _BAND_NAME_MAP:
            return _BAND_NAME_MAP[key]

    # 2. Check tags for center_wavelength
    for wl_key in ("center_wavelength", "WAVELENGTH", "wavelength", "CenterWavelength"):
        raw = tags.get(wl_key)
        if raw is not None:
            try:
                wl = float(raw)
                # If value < 10, assume micrometers → convert to nm
                if wl < 10:
                    wl *= 1000
                for lo, hi, name in _WAVELENGTH_RANGES:
                    if lo <= wl <= hi:
                        return name
            except (ValueError, TypeError):
                pass

    # 3. Fall back to colorinterp
    ci = colorinterp_name.lower()
    if ci in ("red", "green", "blue"):
        return ci

    return None


def _classify_data_category(
    dtype: str, band_count: int, colorinterp: list[str],
) -> str:
    """Classify raster into a rendering category."""
    ci = [c.lower() for c in colorinterp]
    is_rgb = ci[:3] == ["red", "green", "blue"]
    is_gray = len(ci) > 0 and ci[0] == "gray"
    is_16bit = dtype in ("uint16", "int16")
    is_float = dtype in ("float32", "float64")

    if is_rgb and band_count == 4 and len(ci) >= 4 and ci[3] == "alpha":
        return "rgba_16bit" if is_16bit else "rgba"
    if is_rgb:
        return "rgb_16bit" if is_16bit else "rgb"
    if is_float and band_count == 1:
        return "dem"
    if is_gray or band_count == 1:
        return "grayscale_16bit" if is_16bit else "grayscale"
    if band_count >= 4:
        return "multispectral"
    if band_count == 3:
        # 3 bands but not tagged as RGB — assume RGB (common for drone data)
        return "rgb_16bit" if is_16bit else "rgb"
    return "singleband"


def _detect_presets(
    data_category: str,
    bands_info: list[dict],
    asset_name: str = "data",
) -> tuple[str, dict[str, dict]]:
    """Detect available rendering presets. Returns (default_preset, presets_dict)."""
    # Build spectral_name → band_index mapping
    spectral_map: dict[str, int] = {}
    for bi in bands_info:
        sn = bi.get("spectral_name")
        if sn and sn not in spectral_map:
            spectral_map[sn] = bi["index"]

    presets: dict[str, dict] = {}

    # Always add a raw/default preset based on category
    if data_category in ("rgb", "rgba", "rgb_16bit", "rgba_16bit"):
        # RGB-type data
        bidx = [1, 2, 3]
        params: dict[str, str] = {"asset_bidx": f"{asset_name}|{','.join(str(b) for b in bidx)}"}
        # Add rescale for 16-bit
        if "16bit" in data_category:
            p2_vals = [bands_info[i - 1]["stats"]["p2"] for i in bidx if i <= len(bands_info)]
            p98_vals = [bands_info[i - 1]["stats"]["p98"] for i in bidx if i <= len(bands_info)]
            if p2_vals and p98_vals:
                params["rescale"] = f"{int(min(p2_vals))},{int(max(p98_vals))}"
        presets["natural_color"] = {"label": "Natural Color (RGB)", "params": params}
        default_preset = "natural_color"

    elif data_category in ("grayscale", "grayscale_16bit"):
        params = {"asset_bidx": f"{asset_name}|1", "colormap_name": "gray"}
        if "16bit" in data_category and bands_info:
            s = bands_info[0]["stats"]
            params["rescale"] = f"{int(s['p2'])},{int(s['p98'])}"
        presets["grayscale"] = {"label": "Grayscale", "params": params}
        default_preset = "grayscale"

    elif data_category == "dem":
        params = {"asset_bidx": f"{asset_name}|1", "colormap_name": "terrain"}
        if bands_info:
            s = bands_info[0]["stats"]
            params["rescale"] = f"{int(s['p2'])},{int(s['p98'])}"
        presets["terrain"] = {"label": "Terrain Elevation", "params": params}
        # Also add hillshade-style gray
        gray_params = dict(params)
        gray_params["colormap_name"] = "gray"
        presets["grayscale"] = {"label": "Grayscale", "params": gray_params}
        default_preset = "terrain"

    elif data_category == "multispectral":
        # Default: first 3 bands as RGB composite
        params = {"asset_bidx": f"{asset_name}|1,2,3"}
        if len(bands_info) >= 3:
            p2_vals = [bands_info[i]["stats"]["p2"] for i in range(3)]
            p98_vals = [bands_info[i]["stats"]["p98"] for i in range(3)]
            params["rescale"] = f"{int(min(p2_vals))},{int(max(p98_vals))}"
        presets["bands_123"] = {"label": "Bands 1-2-3", "params": params}
        default_preset = "bands_123"

        # Single-band grayscale of band 1
        gray_params_ms: dict[str, str] = {"asset_bidx": f"{asset_name}|1", "colormap_name": "gray"}
        if bands_info:
            s = bands_info[0]["stats"]
            gray_params_ms["rescale"] = f"{int(s['p2'])},{int(s['p98'])}"
        presets["grayscale"] = {"label": "Band 1 Grayscale", "params": gray_params_ms}

    else:
        # singleband fallback
        params = {"asset_bidx": f"{asset_name}|1", "colormap_name": "gray"}
        if bands_info:
            s = bands_info[0]["stats"]
            params["rescale"] = f"{int(s['p2'])},{int(s['p98'])}"
        presets["grayscale"] = {"label": "Grayscale", "params": params}
        default_preset = "grayscale"

    # Add spectral presets for multispectral data
    if spectral_map:
        for preset_id, preset_def in SPECTRAL_PRESETS.items():
            required = preset_def["requires"]
            if not all(r in spectral_map for r in required):
                continue
            if preset_id in presets:
                continue  # already added (e.g. natural_color for RGB)

            if "expression_tpl" in preset_def:
                # Index expression — titiler needs asset_as_band=True
                expr_map = {name: spectral_map[name] for name in required}
                expression = preset_def["expression_tpl"].format(**expr_map)
                p: dict[str, str] = {
                    "expression": expression,
                    "asset_as_band": "True",
                }
                if "colormap" in preset_def:
                    p["colormap_name"] = preset_def["colormap"]
                if "rescale" in preset_def:
                    p["rescale"] = preset_def["rescale"]
                presets[preset_id] = {"label": preset_def["label"], "params": p}
            elif "bands" in preset_def:
                # Band composite
                bidx = [spectral_map[name] for name in preset_def["bands"]]
                bidx_str = ",".join(str(b) for b in bidx)
                p = {"asset_bidx": f"{asset_name}|{bidx_str}"}
                # Rescale from the selected bands' stats
                p2_list = [bands_info[b - 1]["stats"]["p2"] for b in bidx if b <= len(bands_info)]
                p98_list = [bands_info[b - 1]["stats"]["p98"] for b in bidx if b <= len(bands_info)]
                if p2_list and p98_list:
                    p["rescale"] = f"{int(min(p2_list))},{int(max(p98_list))}"
                presets[preset_id] = {"label": preset_def["label"], "params": p}

        # If natural_color was added via spectral matching, prefer it as default
        if "natural_color" in presets and data_category == "multispectral":
            default_preset = "natural_color"

    return default_preset, presets


def _extract_rendering_config(src: object, asset_name: str = "data") -> dict:
    """Extract full rendering configuration from an open rasterio dataset.

    Called inside extract_cog_metadata() while the file is already open.
    Returns a rendering_config dict ready for JSONB storage.
    """
    from rasterio.enums import ColorInterp

    band_count = src.count  # type: ignore[attr-defined]
    dtype = str(src.dtypes[0]) if src.dtypes else "uint8"  # type: ignore[attr-defined]
    nodata = src.nodata  # type: ignore[attr-defined]

    # Color interpretation
    colorinterp: list[str] = []
    try:
        colorinterp = [ci.name.lower() if hasattr(ci, "name") else str(ci).lower()
                       for ci in src.colorinterp]  # type: ignore[attr-defined]
    except Exception:
        colorinterp = ["undefined"] * band_count

    # Per-band info
    bands_info: list[dict] = []
    for i in range(1, band_count + 1):
        ci_name = colorinterp[i - 1] if i <= len(colorinterp) else "undefined"
        desc = ""
        try:
            desc = src.descriptions[i - 1] or ""  # type: ignore[attr-defined]
        except (IndexError, TypeError):
            pass

        tags = {}
        try:
            tags = src.tags(bidx=i) or {}  # type: ignore[attr-defined]
        except Exception:
            pass

        # Compute stats (decimated for speed)
        try:
            stats = _compute_band_stats(src, i, nodata)
        except Exception as exc:
            logger.debug("Failed to compute stats for band %d: %s", i, exc)
            stats = {"min": 0, "max": 0, "mean": 0, "p2": 0, "p98": 0}

        spectral_name = _identify_spectral_band(i, ci_name, desc, tags)

        bands_info.append({
            "index": i,
            "dtype": str(src.dtypes[i - 1]) if i <= len(src.dtypes) else dtype,  # type: ignore[attr-defined]
            "colorinterp": ci_name,
            "description": desc,
            "spectral_name": spectral_name,
            "stats": stats,
        })

    data_category = _classify_data_category(dtype, band_count, colorinterp)
    default_preset, presets = _detect_presets(data_category, bands_info, asset_name)

    # Add nodata to all preset params
    if nodata is not None:
        for preset in presets.values():
            preset["params"].setdefault("nodata", str(nodata))

    config: dict = {
        "version": 1,
        "dtype": dtype,
        "band_count": band_count,
        "nodata_value": _safe_float(nodata) if nodata is not None else None,
        "colorinterp": colorinterp,
        "bands": bands_info,
        "data_category": data_category,
        "default_preset": default_preset,
        "presets": presets,
    }

    # Ensure the entire config is JSON-safe (no NaN/Inf values)
    config = _sanitize_for_json(config)

    logger.info(
        "rendering_config extracted: category=%s bands=%d dtype=%s presets=%s default=%s",
        data_category, band_count, dtype, list(presets.keys()), default_preset,
    )
    return config


# ── Public API ────────────────────────────────────────────────────────────────

def validate_cog(s3_uri: str, s3_config: dict) -> tuple[bool, list[str]]:
    """Validate that the file at *s3_uri* can be ingested as a raster.

    Hard failures (returns False — file is rejected):
    - File cannot be opened via GDAL VSI
    - File has no raster bands

    Soft warnings (returns True with non-empty issues — file is ingested
    but the issues are logged so operators know it is not COG-optimised):
    - No block (tile) structure (non-tiled TIFF)
    - Block size > 512 px
    - No overview levels (tiles will be slow at low zoom)
    - No compression (file will be large in storage)

    This follows industry practice: accept any valid GeoTIFF for ingestion,
    but flag files that are not Cloud Optimized so they can be re-uploaded
    as COGs for better tile performance.

    Returns ``(is_valid, issues)`` where *issues* lists all problems found.
    *is_valid* is False only on hard failures.
    """
    import rasterio
    from rasterio.env import Env

    vsi = _vsi_path(s3_uri)
    issues: list[str] = []
    hard_failure = False

    with Env(**s3_config):
        try:
            with rasterio.open(vsi) as src:
                if not src.indexes:
                    issues.append("File has no raster bands")
                    hard_failure = True
                elif src.crs is None:
                    issues.append("File has no coordinate reference system — not a valid georeferenced raster")
                    hard_failure = True
                else:
                    # Soft: tiling
                    block_shapes = src.block_shapes
                    if not block_shapes or all(bs is None for bs in block_shapes):
                        issues.append("No tile block structure — not COG-optimised (will render slowly)")
                    else:
                        for bs in block_shapes:
                            if bs is not None and (bs[0] > 512 or bs[1] > 512):
                                issues.append(
                                    f"Block size {bs} > 512 px — consider retiling at 256 or 512"
                                )
                                break

                    # Soft: overviews
                    if not any(src.overviews(i) for i in src.indexes):
                        issues.append("No overview levels — low-zoom tiles will be slow")

                    # Soft: compression
                    if src.profile.get("compress") is None:
                        issues.append("No compression — storage size will be larger than necessary")

        except Exception as exc:
            issues.append(f"Could not open file: {exc}")
            hard_failure = True

    return not hard_failure, issues


def extract_cog_metadata(s3_uri: str, s3_config: dict, filename: str | None = None) -> dict:
    """Extract spatial and radiometric metadata from a COG.

    Args:
        s3_uri: S3 URI of the COG (s3://bucket/key)
        s3_config: S3 configuration dict for rasterio environment
        filename: Original filename (optional, used to extract timestamp patterns)

    Returns a dict with keys:
        bbox            [west, south, east, north]  EPSG:4326
        center_point    [lon, lat] center of the bbox (validated, not earth center)
        native_crs      CRS string, e.g. "EPSG:32637"
        width           pixel columns
        height          pixel rows
        gsd_meters      ground sample distance in metres
        bands           list of {index, dtype, nodata}
        datetime        ISO-8601 UTC string
        end_datetime    ISO-8601 UTC string (only if extracted from filename range)
        file_size_bytes int or None
        geometry_valid  bool - False if bbox looks invalid (covers earth or at origin)
    """
    import rasterio
    from rasterio.env import Env
    from rasterio.warp import transform_bounds

    vsi = _vsi_path(s3_uri)

    with Env(**s3_config):
        with rasterio.open(vsi) as src:
            # Reproject bounds to EPSG:4326
            west, south, east, north = transform_bounds(src.crs, "EPSG:4326", *src.bounds)

            # Validate geometry - detect suspicious bounds
            geometry_valid = True
            geometry_warning = None
            
            # Check for earth-center (0,0) or very small bbox near origin
            center_lon = (west + east) / 2
            center_lat = (south + north) / 2
            bbox_width = abs(east - west)
            bbox_height = abs(north - south)
            
            # Suspicious: center near (0,0) with small extent (likely CRS issue)
            if abs(center_lon) < 1 and abs(center_lat) < 1 and bbox_width < 2 and bbox_height < 2:
                geometry_warning = "Geometry centered near (0,0) - possible CRS transformation issue"
                geometry_valid = False
                logger.warning("geometry_suspicious: %s - %s", s3_uri, geometry_warning)
            
            # Suspicious: covers most of earth (likely wrong CRS assumption)
            if bbox_width > 350 or bbox_height > 170:
                geometry_warning = "Geometry covers most of earth - possible CRS issue"
                geometry_valid = False
                logger.warning("geometry_suspicious: %s - %s", s3_uri, geometry_warning)
            
            # If geometry is invalid, try to use native CRS bounds as fallback info
            if not geometry_valid:
                # Log the native bounds for debugging
                logger.warning(
                    "geometry_native_bounds: %s CRS=%s bounds=%s",
                    s3_uri, src.crs, src.bounds
                )

            # GSD: native pixel size in metres (approximate for geographic CRS)
            res_x = abs(src.res[0])
            if src.crs and src.crs.is_geographic:
                gsd_meters = round(res_x * 111_000, 4)
            else:
                gsd_meters = round(res_x, 4)

            bands = [
                {
                    "index": i,
                    "dtype": str(src.dtypes[i - 1]),
                    "nodata": _safe_float(src.nodata) if src.nodata is not None else None,
                }
                for i in src.indexes
            ]

            tags = src.tags()
            # Prefer caller-provided name for datetime extraction, then S3 URI.
            datetime_filename = filename if filename else s3_uri
            item_datetime = _extract_datetime(tags, datetime_filename)

            # Check for datetime range in filename patterns
            _, end_datetime = _extract_datetime_from_filename_patterns(datetime_filename)

            # File size: attempt GDAL VSIStatL, fall back to None
            file_size_bytes: int | None = None
            try:
                from osgeo import gdal  # optional; only available with GDAL Python bindings

                stat = gdal.VSIStatL(vsi)
                if stat is not None:
                    file_size_bytes = stat.size
            except Exception:
                pass

            # Extract rendering config (band stats, presets) while file is open
            try:
                rendering_config = _extract_rendering_config(src)
            except Exception as exc:
                logger.warning("rendering_config extraction failed: %s", exc)
                rendering_config = None

            result = {
                "bbox": [west, south, east, north],
                "center_point": [round(center_lon, 6), round(center_lat, 6)],
                "geometry_valid": geometry_valid,
                "geometry_warning": geometry_warning,
                "native_crs": src.crs.to_string() if src.crs else None,
                "width": src.width,
                "height": src.height,
                "gsd_meters": gsd_meters,
                "bands": bands,
                "datetime": item_datetime,
                "file_size_bytes": file_size_bytes,
                "rendering_config": rendering_config,
            }

            # Add end_datetime if extracted from filename range pattern
            if end_datetime:
                result["end_datetime"] = end_datetime

            return result


def build_stac_item(
    item_id: str,
    collection_id: str,
    s3_uri: str,
    metadata: dict,
) -> dict:
    """Build a STAC 1.0 Item dict for a single COG.

    The asset ``href`` is the raw ``s3://`` URI.  titiler-pgstac resolves it
    via its own MinIO / S3 environment configuration.
    """
    west, south, east, north = metadata["bbox"]
    center_point = metadata.get("center_point", [(west + east) / 2, (south + north) / 2])

    # Check if this is a datetime range (extracted from filename patterns)
    has_range = "end_datetime" in metadata

    # Build properties dict, then sanitize for JSON safety
    properties = {
        "gsd": metadata.get("gsd_meters"),
        "proj:epsg": _epsg_code(metadata.get("native_crs")),
        "proj:shape": [metadata.get("height"), metadata.get("width")],
        "native_crs": metadata.get("native_crs"),
        "file_size_bytes": metadata.get("file_size_bytes"),
        # Center point of the raster [lon, lat]
        "center_point": center_point,
        # Geometry validation status
        "geometry_valid": metadata.get("geometry_valid", True),
        "geometry_warning": metadata.get("geometry_warning"),
        # Store band dtypes as a flat list for collection-level aggregation
        "bands": [b["dtype"] for b in metadata.get("bands", [])],
        # Pre-computed rendering config (band stats, presets, data category)
        "rendering_config": metadata.get("rendering_config"),
    }

    # Add datetime properties: either single datetime or range (start/end)
    if has_range:
        properties["datetime"] = None  # null in STAC when using range
        properties["start_datetime"] = metadata["datetime"]
        properties["end_datetime"] = metadata["end_datetime"]
    else:
        properties["datetime"] = metadata["datetime"]
    
    # Ensure all values are JSON-safe (no NaN/Inf)
    properties = _sanitize_for_json(properties)

    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "collection": collection_id,
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        },
        "bbox": [west, south, east, north],
        "properties": properties,
        "assets": {
            "data": {
                "href": s3_uri,
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data", "visual"],
                "title": metadata.get("filename", "COG"),
            }
        },
        "links": [],
    }


def build_stac_collection(
    collection_id: str,
    org_id: str,
    dataset_name: str,
) -> dict:
    """Build a minimal STAC 1.0 Collection shell.

    pgSTAC will update ``extent`` incrementally as items are upserted, so
    the initial values are placeholders.
    """
    return {
        "type": "Collection",
        "id": collection_id,
        "stac_version": "1.0.0",
        "title": dataset_name,
        "description": f"Imagery collection for dataset '{dataset_name}' (org {org_id})",
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [[None, None]]},
        },
        "links": [],
    }


def _epsg_code(crs_string: str | None) -> int | None:
    """Extract numeric EPSG code from a CRS string like ``EPSG:32637``."""
    if not crs_string:
        return None
    match = re.search(r":(\d+)$", crs_string)
    return int(match.group(1)) if match else None
