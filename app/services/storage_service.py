"""
MinIO / S3 object storage helpers.

All methods are synchronous (boto3 has no async API). Call from FastAPI
endpoints via ``asyncio.to_thread``. Clients are module-level singletons
so the SSL/credential setup cost is paid once, not per-call.
"""

from __future__ import annotations

import logging
import threading
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import settings

# S3 path: {S3_BUCKET_PREFIX}{org_id}/datasets/{dataset_id}/{filename}
_KEY_TEMPLATE = "datasets/{dataset_id}/{filename}"

# ── Shared boto3 client config ────────────────────────────────────────────────

_SHARED_CONFIG = Config(
    s3={"addressing_style": "path"},
    signature_version="s3v4",
    connect_timeout=10,
    read_timeout=30,
    max_pool_connections=25,
    tcp_keepalive=True,
    retries={"max_attempts": 3, "mode": "standard"},
)

# Module-level singletons — created once on first use (thread-safe).
_internal_client: boto3.client | None = None
_public_client: boto3.client | None = None
_client_lock = threading.Lock()


def _s3_client() -> boto3.client:
    """Return a cached boto3 S3 client pointed at the internal MinIO endpoint."""
    global _internal_client  # noqa: PLW0603
    if _internal_client is None:
        with _client_lock:
            if _internal_client is None:
                _internal_client = boto3.client(
                    "s3",
                    endpoint_url=settings.AWS_ENDPOINT_URL,
                    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                    region_name=settings.AWS_REGION,
                    config=_SHARED_CONFIG,
                )
    return _internal_client


def _s3_client_public() -> boto3.client:
    """Return a cached boto3 S3 client pointed at the PUBLIC MinIO endpoint.

    Used exclusively to generate presigned URLs that browsers can reach.
    The internal ``http://minio:9000`` hostname is not resolvable outside
    the Docker network, so any URL signed with that endpoint would fail in
    the browser.
    """
    global _public_client  # noqa: PLW0603
    if _public_client is None:
        with _client_lock:
            if _public_client is None:
                _public_client = boto3.client(
                    "s3",
                    endpoint_url=settings.PUBLIC_MINIO_URL,
                    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                    region_name=settings.AWS_REGION,
                    config=_SHARED_CONFIG,
                )
    return _public_client


def bucket_name(org_id: uuid.UUID) -> str:
    return f"{settings.S3_BUCKET_PREFIX}{org_id}"


def object_key(dataset_id: uuid.UUID, filename: str) -> str:
    return _KEY_TEMPLATE.format(dataset_id=dataset_id, filename=filename)


# ── Bucket management ─────────────────────────────────────────────────────────

def _apply_bucket_cors(client: boto3.client, name: str) -> None:
    """Apply a CORS policy that allows browsers to PUT parts directly."""
    
    # Ensure allowed_origins is always a list of strings
    raw_origin = settings.MINIO_CORS_ALLOW_ORIGIN
    if not raw_origin:
        allowed_origins = ["*"]
    elif isinstance(raw_origin, str):
        # Split by comma if you have multiple origins, otherwise wrap in list
        allowed_origins = [o.strip() for o in raw_origin.split(",")]
    else:
        allowed_origins = list(raw_origin)

    client.put_bucket_cors(
        Bucket=name,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "PUT", "HEAD"],
                    "AllowedOrigins": allowed_origins,  # Must be a list
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )

_log = logging.getLogger(__name__)


def ensure_org_bucket(org_id: uuid.UUID) -> None:
    """Create the org bucket if it does not exist, and ensure CORS is set. Idempotent."""
    client = _s3_client()
    name = bucket_name(org_id)
    try:
        client.head_bucket(Bucket=name)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            client.create_bucket(Bucket=name)
        else:
            raise
    try:
        _apply_bucket_cors(client, name)
    except ClientError as exc:
        # Some MinIO builds return NotImplemented for PutBucketCors.
        # CORS can also be configured via `mc cors set` in the minio-setup
        # container.  Log a warning and continue — bucket creation must not
        # be blocked by a CORS API limitation.
        _log.warning(
            "bucket_cors_set_failed bucket=%s code=%s — "
            "browser uploads may fail cross-origin; "
            "configure CORS via `mc cors set` on the MinIO server",
            name,
            exc.response["Error"].get("Code", "unknown"),
        )


# ── Multipart upload lifecycle ────────────────────────────────────────────────

def initiate_upload(
    org_id: uuid.UUID,
    dataset_id: uuid.UUID,
    filename: str,
    content_type: str = "image/tiff",
) -> tuple[str, str]:
    """Start a multipart upload.

    Returns ``(s3_key, upload_id)``.  The caller stores both on the Job
    record so the upload can be completed or aborted later.
    """
    client = _s3_client()
    key = object_key(dataset_id, filename)
    resp = client.create_multipart_upload(
        Bucket=bucket_name(org_id),
        Key=key,
        ContentType=content_type,
    )
    return key, resp["UploadId"]


def upload_from_path(org_id: uuid.UUID, s3_key: str, file_path: str, content_type: str = "image/tiff") -> None:
    """Upload a local file to S3/MinIO as a single PUT (used by Celery workers).

    Uses the internal endpoint — not for generating URLs the browser will hit.
    """
    client = _s3_client()
    with open(file_path, "rb") as fobj:
        client.upload_fileobj(
            fobj,
            bucket_name(org_id),
            s3_key,
            ExtraArgs={"ContentType": content_type},
        )


def generate_part_url(
    org_id: uuid.UUID,
    s3_key: str,
    upload_id: str,
    part_number: int,
    ttl_seconds: int = 3600,
) -> str:
    """Return a presigned PUT URL for a single multipart part.

    The URL is signed against ``PUBLIC_MINIO_URL`` so browsers can PUT
    directly without proxying through the API container.
    Part numbers must be in the range 1–10 000 (S3 / MinIO limit).
    """
    client = _s3_client_public()
    return client.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": bucket_name(org_id),
            "Key": s3_key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=ttl_seconds,
    )


def generate_part_urls_batch(
    org_id: uuid.UUID,
    s3_key: str,
    upload_id: str,
    part_numbers: list[int],
    ttl_seconds: int = 3600,
) -> list[tuple[int, str]]:
    """Return presigned PUT URLs for multiple parts in one call.

    Uses a single cached client for all signatures — avoids per-call
    client creation overhead.  Returns ``[(part_number, url), ...]``.
    """
    client = _s3_client_public()
    bkt = bucket_name(org_id)
    return [
        (
            n,
            client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": bkt,
                    "Key": s3_key,
                    "UploadId": upload_id,
                    "PartNumber": n,
                },
                ExpiresIn=ttl_seconds,
            ),
        )
        for n in part_numbers
    ]


def upload_part(
    org_id: uuid.UUID,
    s3_key: str,
    upload_id: str,
    part_number: int,
    data: bytes,
) -> str:
    """Upload a single part and return its ETag.

    Used by the API proxy endpoint so the browser never needs to PUT directly
    to MinIO (avoids MinIO Community's S3 CORS limitation).
    """
    client = _s3_client()
    resp = client.upload_part(
        Bucket=bucket_name(org_id),
        Key=s3_key,
        UploadId=upload_id,
        PartNumber=part_number,
        Body=data,
    )
    return resp["ETag"]


def list_parts(org_id: uuid.UUID, s3_key: str, upload_id: str) -> list[dict]:
    """Return all uploaded parts as ``{"PartNumber": int, "ETag": str}`` dicts.

    Used when the client cannot read ETags from PUT responses (e.g. MinIO
    Community edition does not support per-bucket CORS, so
    Access-Control-Expose-Headers cannot include ETag).
    """
    client = _s3_client()
    name = bucket_name(org_id)
    parts = []
    kwargs: dict = {"Bucket": name, "Key": s3_key, "UploadId": upload_id}
    while True:
        resp = client.list_parts(**kwargs)
        for p in resp.get("Parts", []):
            parts.append({"PartNumber": p["PartNumber"], "ETag": p["ETag"]})
        if resp.get("IsTruncated"):
            kwargs["PartNumberMarker"] = resp["NextPartNumberMarker"]
        else:
            break
    return parts


def complete_upload(
    org_id: uuid.UUID,
    s3_key: str,
    upload_id: str,
    parts: list[dict] | None = None,
) -> None:
    """Complete a multipart upload.

    ``parts`` is a list of ``{"PartNumber": int, "ETag": str}`` dicts.
    If omitted, the parts are fetched from MinIO via ``list_parts`` — this
    is the normal path because MinIO Community does not expose ETag via CORS.
    """
    if not parts:
        parts = list_parts(org_id, s3_key, upload_id)
    client = _s3_client()
    client.complete_multipart_upload(
        Bucket=bucket_name(org_id),
        Key=s3_key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )


def abort_upload(org_id: uuid.UUID, s3_key: str, upload_id: str) -> None:
    """Abort an in-progress multipart upload and release its parts."""
    client = _s3_client()
    try:
        client.abort_multipart_upload(
            Bucket=bucket_name(org_id),
            Key=s3_key,
            UploadId=upload_id,
        )
    except ClientError:
        # Already aborted or never started — safe to ignore.
        pass


# ── Object deletion ──────────────────────────────────────────────────────────

def delete_object(org_id: uuid.UUID, s3_key: str) -> None:
    """Delete a single object from the org bucket.

    No-op if the object does not exist (S3 DeleteObject is idempotent).
    """
    client = _s3_client()
    client.delete_object(Bucket=bucket_name(org_id), Key=s3_key)


def delete_objects_by_prefix(org_id: uuid.UUID, prefix: str) -> int:
    """Delete all objects under *prefix* in the org bucket.

    Returns the number of objects deleted.  Uses batched delete (up to 1000
    keys per request) for efficiency.
    """
    client = _s3_client()
    bkt = bucket_name(org_id)
    deleted_count = 0

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bkt, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if not objects:
            continue
        client.delete_objects(Bucket=bkt, Delete={"Objects": objects, "Quiet": True})
        deleted_count += len(objects)

    return deleted_count


# ── Download presigning ───────────────────────────────────────────────────────

def generate_download_url(
    org_id: uuid.UUID,
    s3_key: str,
    ttl_seconds: int = 3600,
) -> str:
    """Return a presigned GET URL for browser download.

    Signed against ``PUBLIC_MINIO_URL`` for the same reason as part URLs.
    """
    client = _s3_client_public()
    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket_name(org_id),
            "Key": s3_key,
        },
        ExpiresIn=ttl_seconds,
    )


# ── Stale upload cleanup ─────────────────────────────────────────────────────

def list_stale_multipart_uploads(
    org_id: uuid.UUID,
    older_than_hours: int = 24,
) -> list[dict]:
    """List multipart uploads older than *older_than_hours*.

    Returns a list of ``{"Key": str, "UploadId": str, "Initiated": datetime}``
    dicts for uploads that can be safely aborted.
    """
    from datetime import datetime, timezone, timedelta

    client = _s3_client()
    bkt = bucket_name(org_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    stale: list[dict] = []

    try:
        paginator = client.get_paginator("list_multipart_uploads")
        for page in paginator.paginate(Bucket=bkt):
            for upload in page.get("Uploads", []):
                initiated = upload.get("Initiated")
                if initiated and initiated < cutoff:
                    stale.append({
                        "Key": upload["Key"],
                        "UploadId": upload["UploadId"],
                        "Initiated": initiated,
                    })
    except ClientError as exc:
        # Bucket may not exist yet if org never uploaded anything
        if exc.response["Error"]["Code"] in ("404", "NoSuchBucket"):
            return []
        raise

    return stale


def abort_stale_multipart_uploads(
    org_id: uuid.UUID,
    older_than_hours: int = 24,
) -> int:
    """Abort all multipart uploads older than *older_than_hours*.

    Returns the number of uploads aborted. Safe to call repeatedly.
    """
    stale = list_stale_multipart_uploads(org_id, older_than_hours)
    if not stale:
        return 0

    client = _s3_client()
    bkt = bucket_name(org_id)
    aborted = 0

    for upload in stale:
        try:
            client.abort_multipart_upload(
                Bucket=bkt,
                Key=upload["Key"],
                UploadId=upload["UploadId"],
            )
            aborted += 1
            _log.info(
                "aborted_stale_upload bucket=%s key=%s upload_id=%s initiated=%s",
                bkt, upload["Key"], upload["UploadId"], upload["Initiated"],
            )
        except ClientError:
            # Already aborted or completed — safe to ignore
            pass

    return aborted
