"""ZIP-member GeoTIFF → COG conversion (Milestone 1 completion)."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.workers.ingestion.tasks import (
    PermanentTaskError,
    _file_hash,
    _ingest_folder_group,
    _partition_zip_members,
    _prepare_zip_member_raster,
    _processed_zip_cog_object_key,
    _safe_raster_stem,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
DATASET_ID = UUID("00000000-0000-0000-0000-000000000002")
DATASET_ID_B = UUID("00000000-0000-0000-0000-000000000022")
BUCKET = f"org-{ORG_ID}"
COLLECTION = "org-col-dataset-1"
_TRANSFORM = from_origin(-10.0, 10.0, 1.0, 1.0)
_NON_COG_ISSUES = ["No compression — storage size will be larger than necessary"]
_INVALID_ISSUES = [
    "File has no coordinate reference system — not a valid georeferenced raster"
]


def _write_geotiff(path: Path, data: np.ndarray, *, crs="EPSG:4326", tiled: bool = False) -> Path:
    if data.ndim == 2:
        bands = data[np.newaxis, ...]
        height, width = data.shape
        count = 1
    else:
        bands = data
        count, height, width = data.shape
    profile = {
        "driver": "GTiff",
        "dtype": str(data.dtype),
        "count": count,
        "height": height,
        "width": width,
        "crs": crs,
        "transform": _TRANSFORM,
        "tiled": tiled,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(bands)
    return path


def _make_zip(tmp_path: Path, members: dict[str, Path]) -> Path:
    zpath = tmp_path / "upload.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for arcname, src in members.items():
            zf.write(src, arcname)
    return zpath


def _folder_mocks(dataset_id=DATASET_ID):
    session = MagicMock()
    job = MagicMock()
    job.organization_id = ORG_ID
    job.processed_items = 0
    job.failed_items = 0
    job.progress = 0
    dataset = MagicMock()
    dataset.id = dataset_id
    dataset.organization_id = ORG_ID
    dataset.dataset_type = "imagery"
    return session, job, dataset


def _fake_prepare(s3_uri, filename, collection, gdal_env, dataset_type="imagery"):
    item = {
        "id": f"stac-{filename}",
        "geometry": None,
        "properties": {"datetime": "2020-01-01T00:00:00Z"},
        "assets": {"data": {"href": s3_uri}},
    }
    return True, [], f"stac-{filename}", item


def _inspect_by_basename(path, _env):
    name = os.path.basename(path).lower()
    if name.startswith("plain"):
        return True, False, list(_NON_COG_ISSUES)
    if name.startswith("bad"):
        return False, False, list(_INVALID_ISSUES)
    return True, True, []


def _run_folder_group(tmp_path: Path, members: dict[str, Path], dataset_id=DATASET_ID, **patches):
    zpath = _make_zip(tmp_path, members)
    extract_base = str(tmp_path / "extracted")
    session, job, dataset = _folder_mocks(dataset_id)
    upsert = patches.get("upsert", MagicMock())
    prepare = patches.get("prepare", _fake_prepare)
    convert = patches.get("convert", MagicMock(return_value="/tmp/out.tif"))
    inspect = patches.get("inspect", _inspect_by_basename)
    upload = patches.get("upload", MagicMock())
    batch = patches.get("batch", MagicMock())

    with (
        zipfile.ZipFile(zpath, "r") as zf,
        patch("app.workers.ingestion.tasks.is_cloud_optimized_geotiff", side_effect=inspect),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog", convert),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path", upload),
        patch("app.workers.ingestion.tasks._prepare_single_cog", side_effect=prepare),
        patch("app.workers.ingestion.tasks._upsert_dataset_item", upsert),
        patch("app.workers.ingestion.tasks.batch_upsert_stac_items", batch),
        patch("app.workers.ingestion.tasks.settings.STAC_SYNC_DATABASE_URL", "postgresql://stac"),
    ):
        processed, failed = _ingest_folder_group(
            session, job, dataset, BUCKET, COLLECTION,
            list(members.keys()), zf, extract_base, {},
        )
    return processed, failed, upload, convert, upsert, batch, job


# ── Key helper ───────────────────────────────────────────────────────────────


def test_zip_processed_key_includes_hash_to_avoid_basename_collisions():
    key_a = _processed_zip_cog_object_key(DATASET_ID, "hashA" * 8, "img.tif")
    key_b = _processed_zip_cog_object_key(DATASET_ID, "hashB" * 8, "img.tif")
    assert key_a != key_b
    assert key_a == (
        f"datasets/{DATASET_ID}/processed/{'hashA' * 8}_img_cog.tif"
    )
    assert _safe_raster_stem("folder/My File.tif") == "My_File"


def test_partition_zip_members_unchanged_for_multi_folder_and_root():
    groups = _partition_zip_members(
        ["folder_A/img.tif", "folder_B/img.tif", "bare.tif"]
    )
    assert groups == {
        "folder_A": ["folder_A/img.tif"],
        "folder_B": ["folder_B/img.tif"],
        "_root": ["bare.tif"],
    }


# ── _prepare_zip_member_raster ───────────────────────────────────────────────


def test_zip_already_cog_uploads_original_once(tmp_path: Path):
    src = _write_geotiff(tmp_path / "scene.tif", np.arange(16, dtype=np.uint8).reshape(4, 4))
    fhash = _file_hash(str(src))
    uploaded: dict = {}

    def _upload(org_id, key, path, content_type="image/tiff"):
        uploaded["org_id"] = org_id
        uploaded["key"] = key
        uploaded["path"] = path
        uploaded["exists"] = os.path.isfile(path)

    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, True, []),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog") as convert,
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path", side_effect=_upload),
    ):
        uri = _prepare_zip_member_raster(
            organization_id=ORG_ID,
            dataset_id=DATASET_ID,
            local_path=str(src),
            basename="scene.tif",
            file_hash=fhash,
            bucket=BUCKET,
            gdal_env={},
        )

    convert.assert_not_called()
    assert uploaded["org_id"] == ORG_ID
    assert uploaded["key"] == f"datasets/{DATASET_ID}/{fhash}_scene.tif"
    assert uploaded["exists"] is True
    assert uri == f"s3://{BUCKET}/datasets/{DATASET_ID}/{fhash}_scene.tif"


def test_zip_non_cog_converts_locally_and_uploads_processed_only(tmp_path: Path):
    src = _write_geotiff(tmp_path / "plain.tif", np.arange(20, dtype=np.uint8).reshape(4, 5))
    fhash = _file_hash(str(src))
    uploaded: dict = {}

    def _upload(org_id, key, path, content_type="image/tiff"):
        uploaded["org_id"] = org_id
        uploaded["key"] = key
        uploaded["exists"] = os.path.isfile(path)
        uploaded["content_type"] = content_type
        uploaded["path"] = path

    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, list(_NON_COG_ISSUES)),
        ),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path", side_effect=_upload),
    ):
        uri = _prepare_zip_member_raster(
            organization_id=ORG_ID,
            dataset_id=DATASET_ID,
            local_path=str(src),
            basename="plain.tif",
            file_hash=fhash,
            bucket=BUCKET,
            gdal_env={"AWS_S3_ENDPOINT": "minio:9000"},
        )

    expected_key = f"datasets/{DATASET_ID}/processed/{fhash}_plain_cog.tif"
    assert uploaded["org_id"] == ORG_ID
    assert uploaded["key"] == expected_key
    assert uploaded["exists"] is True
    assert uploaded["content_type"] == "image/tiff"
    assert uri == f"s3://{BUCKET}/{expected_key}"
    assert not os.path.exists(uploaded["path"])
    assert os.path.isfile(src)  # extracted source is caller's to delete


def test_zip_member_invalid_raises_without_upload_or_convert(tmp_path: Path):
    src = tmp_path / "bad.tif"
    src.write_bytes(b"not-a-tiff")
    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(False, False, list(_INVALID_ISSUES)),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog") as convert,
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
    ):
        with pytest.raises(PermanentTaskError, match="coordinate reference system"):
            _prepare_zip_member_raster(
                organization_id=ORG_ID,
                dataset_id=DATASET_ID,
                local_path=str(src),
                basename="bad.tif",
                file_hash="abc",
                bucket=BUCKET,
                gdal_env={},
            )
    convert.assert_not_called()
    upload.assert_not_called()


def test_zip_converter_value_error_is_permanent_and_cleans_temp(tmp_path: Path):
    src = _write_geotiff(tmp_path / "plain.tif", np.ones((4, 4), dtype=np.uint8))
    recorded: dict = {}

    def _convert(_inp, out, **_kw):
        recorded["out"] = out
        Path(out).write_bytes(b"partial")
        raise ValueError("COG creation failed: boom")

    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, list(_NON_COG_ISSUES)),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog", side_effect=_convert),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
    ):
        with pytest.raises(PermanentTaskError, match="GeoTIFF to COG conversion failed"):
            _prepare_zip_member_raster(
                organization_id=ORG_ID,
                dataset_id=DATASET_ID,
                local_path=str(src),
                basename="plain.tif",
                file_hash="abc",
                bucket=BUCKET,
                gdal_env={},
            )
    upload.assert_not_called()
    assert recorded["out"]
    assert not os.path.exists(recorded["out"])


def test_zip_upload_failure_propagates_and_cleans_temp(tmp_path: Path):
    src = _write_geotiff(tmp_path / "plain.tif", np.ones((4, 4), dtype=np.uint8))
    recorded: dict = {}

    def _convert(_inp, out, **_kw):
        recorded["out"] = out
        Path(out).write_bytes(b"cog-bytes")

    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, list(_NON_COG_ISSUES)),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog", side_effect=_convert),
        patch(
            "app.workers.ingestion.tasks.storage_service.upload_from_path",
            side_effect=RuntimeError("minio unavailable"),
        ),
    ):
        with pytest.raises(RuntimeError, match="minio unavailable"):
            _prepare_zip_member_raster(
                organization_id=ORG_ID,
                dataset_id=DATASET_ID,
                local_path=str(src),
                basename="plain.tif",
                file_hash="abc",
                bucket=BUCKET,
                gdal_env={},
            )
    assert recorded["out"]
    assert not os.path.exists(recorded["out"])


# ── _ingest_folder_group ─────────────────────────────────────────────────────


def test_zip_folder_already_cog_skips_convert_and_uses_original_uri(tmp_path: Path):
    src = _write_geotiff(tmp_path / "scene.tif", np.arange(16, dtype=np.uint8).reshape(4, 4))
    fhash = _file_hash(str(src))
    processed, failed, upload, convert, upsert, batch, _job = _run_folder_group(
        tmp_path, {"scene.tif": src},
        inspect=lambda *_a, **_k: (True, True, []),
        convert=MagicMock(),
    )
    convert.assert_not_called()
    assert processed == 1
    assert failed == []
    assert upload.call_count == 1
    assert upload.call_args.args[0] == ORG_ID
    assert upload.call_args.args[1] == f"datasets/{DATASET_ID}/{fhash}_scene.tif"
    expected_uri = f"s3://{BUCKET}/datasets/{DATASET_ID}/{fhash}_scene.tif"
    assert upsert.call_args.kwargs["s3_uri"] == expected_uri
    assert upsert.call_args.kwargs["filename"] == "scene.tif"
    assert upsert.call_args.kwargs["organization_id"] == ORG_ID
    assert batch.call_count == 1
    assert batch.call_args.args[0][0]["assets"]["data"]["href"] == expected_uri


def test_zip_folder_non_cog_uses_processed_uri_for_stac_and_item(tmp_path: Path):
    src = _write_geotiff(tmp_path / "plain.tif", np.arange(20, dtype=np.uint8).reshape(4, 5))
    fhash = _file_hash(str(src))
    processed, failed, upload, convert, upsert, batch, _job = _run_folder_group(
        tmp_path, {"plain.tif": src},
        inspect=lambda *_a, **_k: (True, False, list(_NON_COG_ISSUES)),
        convert=MagicMock(side_effect=lambda _i, out, **_k: Path(out).write_bytes(b"cog")),
    )
    convert.assert_called_once()
    assert convert.call_args.args[0].endswith("plain.tif")
    assert convert.call_args.kwargs.get("gdal_env") == {}
    assert processed == 1
    assert failed == []
    expected_key = f"datasets/{DATASET_ID}/processed/{fhash}_plain_cog.tif"
    assert upload.call_args.args[1] == expected_key
    expected_uri = f"s3://{BUCKET}/{expected_key}"
    assert upsert.call_args.kwargs["s3_uri"] == expected_uri
    assert upsert.call_args.kwargs["filename"] == "plain.tif"
    assert batch.call_args.args[0][0]["assets"]["data"]["href"] == expected_uri


def test_zip_mixed_cog_and_non_cog_both_in_batch(tmp_path: Path):
    cog = _write_geotiff(tmp_path / "scene.tif", np.arange(16, dtype=np.uint8).reshape(4, 4))
    plain = _write_geotiff(tmp_path / "plain.tif", np.arange(20, dtype=np.uint8).reshape(4, 5))
    processed, failed, upload, convert, upsert, batch, _job = _run_folder_group(
        tmp_path,
        {"scene.tif": cog, "plain.tif": plain},
        convert=MagicMock(side_effect=lambda _i, out, **_k: Path(out).write_bytes(b"cog")),
    )
    assert processed == 2
    assert failed == []
    convert.assert_called_once()
    assert upload.call_count == 2
    uris = [c.kwargs["s3_uri"] for c in upsert.call_args_list]
    assert any("/processed/" in u for u in uris)
    assert any("_scene.tif" in u and "/processed/" not in u for u in uris)
    assert len(batch.call_args.args[0]) == 2


def test_zip_corrupt_member_partial_success(tmp_path: Path):
    bad = tmp_path / "bad.tif"
    bad.write_bytes(b"corrupt")
    good = _write_geotiff(tmp_path / "scene.tif", np.ones((4, 4), dtype=np.uint8))
    processed, failed, upload, convert, upsert, batch, _job = _run_folder_group(
        tmp_path,
        {"bad.tif": bad, "scene.tif": good},
        inspect=_inspect_by_basename,
        convert=MagicMock(),
    )
    convert.assert_not_called()
    assert processed == 1
    assert len(failed) == 1
    assert failed[0].startswith("bad.tif:")
    assert "coordinate reference system" in failed[0]
    assert upload.call_count == 1
    assert upsert.call_count == 1
    assert upsert.call_args.kwargs["filename"] == "scene.tif"
    assert len(batch.call_args.args[0]) == 1


def test_zip_converter_error_on_one_member_continues(tmp_path: Path):
    plain = _write_geotiff(tmp_path / "plain.tif", np.ones((4, 4), dtype=np.uint8))
    scene = _write_geotiff(tmp_path / "scene.tif", np.ones((4, 4), dtype=np.uint8))

    def _convert(src, dest, **_kw):
        if os.path.basename(src) == "plain.tif":
            raise ValueError("unreadable raster")
        Path(dest).write_bytes(b"cog")

    processed, failed, _upload, _convert_mock, upsert, batch, _job = _run_folder_group(
        tmp_path,
        {"plain.tif": plain, "scene.tif": scene},
        convert=MagicMock(side_effect=_convert),
    )
    assert processed == 1
    assert len(failed) == 1
    assert "GeoTIFF to COG conversion failed" in failed[0]
    assert upsert.call_args.kwargs["filename"] == "scene.tif"
    inserted_ids = [item["id"] for item in batch.call_args.args[0]]
    assert "stac-plain.tif" not in inserted_ids
    assert "stac-scene.tif" in inserted_ids


def test_zip_duplicate_basenames_get_distinct_processed_keys(tmp_path: Path):
    a = _write_geotiff(tmp_path / "a.tif", np.arange(16, dtype=np.uint8).reshape(4, 4))
    b = _write_geotiff(tmp_path / "b.tif", np.arange(16, dtype=np.uint8).reshape(4, 4) + 1)
    hash_a = _file_hash(str(a))
    hash_b = _file_hash(str(b))
    assert hash_a != hash_b
    processed, failed, upload, _convert, _upsert, _batch, _job = _run_folder_group(
        tmp_path,
        {"folder_A/sub/img.tif": a, "folder_A/other/img.tif": b},
        inspect=lambda *_a, **_k: (True, False, list(_NON_COG_ISSUES)),
        convert=MagicMock(side_effect=lambda _i, out, **_k: Path(out).write_bytes(b"cog")),
    )
    assert processed == 2
    assert failed == []
    keys = [c.args[1] for c in upload.call_args_list]
    assert f"datasets/{DATASET_ID}/processed/{hash_a}_img_cog.tif" in keys
    assert f"datasets/{DATASET_ID}/processed/{hash_b}_img_cog.tif" in keys
    assert keys[0] != keys[1]


def test_zip_multi_folder_conversion_per_dataset(tmp_path: Path):
    groups = _partition_zip_members(["folder_A/plain.tif", "folder_B/plain.tif"])
    assert set(groups) == {"folder_A", "folder_B"}

    tmp_a = tmp_path / "dsa"
    tmp_b = tmp_path / "dsb"
    tmp_a.mkdir()
    tmp_b.mkdir()
    a = _write_geotiff(tmp_a / "a.tif", np.ones((4, 4), dtype=np.uint8))
    b = _write_geotiff(tmp_b / "b.tif", np.ones((4, 4), dtype=np.uint8) * 2)
    hash_a = _file_hash(str(a))
    hash_b = _file_hash(str(b))

    processed_a, failed_a, upload_a, *_rest = _run_folder_group(
        tmp_a,
        {"folder_A/plain.tif": a},
        dataset_id=DATASET_ID,
        inspect=lambda *_a, **_k: (True, False, list(_NON_COG_ISSUES)),
        convert=MagicMock(side_effect=lambda _i, out, **_k: Path(out).write_bytes(b"cog")),
    )
    processed_b, failed_b, upload_b, *_restb = _run_folder_group(
        tmp_b,
        {"folder_B/plain.tif": b},
        dataset_id=DATASET_ID_B,
        inspect=lambda *_a, **_k: (True, False, list(_NON_COG_ISSUES)),
        convert=MagicMock(side_effect=lambda _i, out, **_k: Path(out).write_bytes(b"cog")),
    )
    assert processed_a == 1 and processed_b == 1
    assert failed_a == [] and failed_b == []
    assert upload_a.call_args.args[1] == (
        f"datasets/{DATASET_ID}/processed/{hash_a}_plain_cog.tif"
    )
    assert upload_b.call_args.args[1] == (
        f"datasets/{DATASET_ID_B}/processed/{hash_b}_plain_cog.tif"
    )


def test_zip_extracted_source_removed_after_success(tmp_path: Path):
    src = _write_geotiff(tmp_path / "scene.tif", np.ones((4, 4), dtype=np.uint8))
    extract_base = tmp_path / "extracted"
    _run_folder_group(
        tmp_path, {"scene.tif": src},
        inspect=lambda *_a, **_k: (True, True, []),
        convert=MagicMock(),
    )
    leftover = list(extract_base.rglob("*.tif")) if extract_base.exists() else []
    assert leftover == []


def test_zip_infra_upload_error_is_not_member_soft_failure(tmp_path: Path):
    src = _write_geotiff(tmp_path / "scene.tif", np.ones((4, 4), dtype=np.uint8))
    zpath = _make_zip(tmp_path, {"scene.tif": src})
    session, job, dataset = _folder_mocks()
    with (
        zipfile.ZipFile(zpath, "r") as zf,
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, True, []),
        ),
        patch(
            "app.workers.ingestion.tasks.storage_service.upload_from_path",
            side_effect=RuntimeError("minio unavailable"),
        ),
        pytest.raises(RuntimeError, match="minio unavailable"),
    ):
        _ingest_folder_group(
            session, job, dataset, BUCKET, COLLECTION,
            ["scene.tif"], zf, str(tmp_path / "ex"), {},
        )
