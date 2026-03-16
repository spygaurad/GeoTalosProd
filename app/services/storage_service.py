"""
MinIO / S3 object storage helpers.

All methods are synchronous (boto3 has no async API). Call from FastAPI
endpoints via `asyncio.get_event_loop().run_in_executor` or directly —
boto3 calls are fast enough for metadata operations (initiate, complete,
abort, presign). Heavy I/O (actual file bytes) never touches this process;
clients upload directly to MinIO via the presigned URLs returned here.
"""

from __future__ import annotations

import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import settings

# S3 path: {S3_BUCKET_PREFIX}{org_id}/datasets/{dataset_id}/{filename}
_KEY_TEMPLATE = "datasets/{dataset_id}/{filename}"


def _s3_client() -> boto3.client:
    """Return a boto3 S3 client pointed at the internal MinIO endpoint."""
    return boto3.client(
        "s3",
        endpoint_url=settings.AWS_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
        config=Config(s3={"addressing_style": "path"}),
    )


def _s3_client_public() -> boto3.client:
    """Return a boto3 S3 client pointed at the PUBLIC MinIO endpoint.

    Used exclusively to generate presigned URLs that browsers can reach.
    The internal ``http://minio:9000`` hostname is not resolvable outside
    the Docker network, so any URL signed with that endpoint would fail in
    the browser.
    """
    return boto3.client(
        "s3",
        endpoint_url=settings.PUBLIC_MINIO_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
        config=Config(s3={"addressing_style": "path"}),
    )


def bucket_name(org_id: uuid.UUID) -> str:
    return f"{settings.S3_BUCKET_PREFIX}{org_id}"


def object_key(dataset_id: uuid.UUID, filename: str) -> str:
    return _KEY_TEMPLATE.format(dataset_id=dataset_id, filename=filename)


# ── Bucket management ─────────────────────────────────────────────────────────

def ensure_org_bucket(org_id: uuid.UUID) -> None:
    """Create the org bucket if it does not exist. Idempotent."""
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


# ── Multipart upload lifecycle ────────────────────────────────────────────────

def initiate_upload(org_id: uuid.UUID, dataset_id: uuid.UUID, filename: str) -> tuple[str, str]:
    """Start a multipart upload.

    Returns ``(s3_key, upload_id)``.  The caller stores both on the Job
    record so the upload can be completed or aborted later.
    """
    client = _s3_client()
    key = object_key(dataset_id, filename)
    resp = client.create_multipart_upload(
        Bucket=bucket_name(org_id),
        Key=key,
        ContentType="image/tiff",
    )
    return key, resp["UploadId"]


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


def complete_upload(
    org_id: uuid.UUID,
    s3_key: str,
    upload_id: str,
    parts: list[dict],
) -> None:
    """Complete a multipart upload.

    ``parts`` must be a list of ``{"PartNumber": int, "ETag": str}`` dicts
    returned by MinIO/S3 after each part was PUT successfully.
    """
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
