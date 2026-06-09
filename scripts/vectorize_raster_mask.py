#!/usr/bin/env python3
"""Vectorize a raster segmentation-mask annotation set into a vector set.

Reads a raster-backed annotation set's ``raster_config`` (value -> class map +
backing COG), turns each contiguous class region into a polygon, and writes a
NEW vector annotation set with the mapped classes — directly comparable to
vector model predictions (e.g. SAM3) via the Ground Truth Comparison node.

Run inside the backend environment (it uses the worker DB role + object store):

    python -m scripts.vectorize_raster_mask --set-id <uuid>
    python -m scripts.vectorize_raster_mask --name "Mask Kotsimba classified cog fixed"
    python -m scripts.vectorize_raster_mask --set-id <uuid> --simplify 0.5 --min-area-px 25
    python -m scripts.vectorize_raster_mask --set-id <uuid> --dissolve   # one geom per class

Prints the new annotation set id and per-class feature counts.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import select

from app.config import settings
from app.models.annotation_set import AnnotationSet
from app.services.conversion import vectorize_raster_mask_set
from app.workers.db import WorkerSession


def _gdal_env() -> dict:
    """rasterio Env options (credentials come from the env via boto3 chain)."""
    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    use_https = settings.AWS_ENDPOINT_URL.startswith("https://")
    return {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if use_https else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    }


def _resolve_set(session, *, set_id: str | None, name: str | None) -> AnnotationSet:
    if set_id:
        row = session.get(AnnotationSet, uuid.UUID(set_id))
        if row is None:
            sys.exit(f"No annotation set with id {set_id}")
        return row
    rows = session.execute(
        select(AnnotationSet).where(
            AnnotationSet.name == name,
            AnnotationSet.deleted_at.is_(None),
        )
    ).scalars().all()
    if not rows:
        sys.exit(f"No annotation set named {name!r}")
    if len(rows) > 1:
        ids = ", ".join(str(r.id) for r in rows)
        sys.exit(f"{len(rows)} sets named {name!r}; pass --set-id (candidates: {ids})")
    return rows[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--set-id", help="UUID of the raster-mask annotation set")
    group.add_argument("--name", help="Exact name of the raster-mask annotation set")
    parser.add_argument("--out-name", help="Name for the new vector set (default: '<src> · vectorized')")
    parser.add_argument("--simplify", type=float, default=None,
                        help="Douglas-Peucker tolerance in source-CRS units (default: full detail)")
    parser.add_argument("--min-area-px", type=float, default=0.0,
                        help="Drop regions smaller than this many pixels")
    parser.add_argument("--connectivity", type=int, choices=(4, 8), default=4,
                        help="Pixel connectivity for region growing (default: 4)")
    parser.add_argument("--dissolve", action="store_true",
                        help="Store one merged geometry per class (semantic IoU) instead of per region")
    parser.add_argument("--confidence", type=float, default=1.0,
                        help="Confidence written to each annotation (default: 1.0)")
    parser.add_argument("--created-by", default=None,
                        help="User UUID to attribute the new set to (default: source set's creator)")
    args = parser.parse_args()

    with WorkerSession() as session:
        raster_set = _resolve_set(session, set_id=args.set_id, name=args.name)
        print(f"Source set: {raster_set.id}  '{raster_set.name}'  schema_id={raster_set.schema_id}")

        result = vectorize_raster_mask_set(
            session,
            raster_set,
            gdal_env=_gdal_env(),
            name=args.out_name,
            created_by_user_id=uuid.UUID(args.created_by) if args.created_by else None,
            simplify_tolerance=args.simplify,
            min_area_px=args.min_area_px,
            connectivity=args.connectivity,
            dissolve_by_class=args.dissolve,
            confidence=args.confidence,
        )

    print(f"\nNew vector set: {result.annotation_set_id}")
    print(f"Features written: {result.feature_count}")
    for class_id, count in sorted(result.class_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {class_id}: {count}")
    if result.feature_count == 0:
        print("\n⚠ No features produced. Check the band_index, value_class_map, and nodata "
              "in the source set's raster_config.")


if __name__ == "__main__":
    main()
