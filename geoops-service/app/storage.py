"""Minimal MinIO/S3 client — reads/writes the same bucket-per-org layout
app.services.storage_service uses in the main app, independently (this
service has its own boto3 dependency, no import from app/ across the
process boundary).
"""
from __future__ import annotations

import json
from uuid import UUID

import boto3
from botocore.client import Config

from app.config import settings

_client = boto3.client(
    "s3",
    endpoint_url=settings.AWS_ENDPOINT_URL,
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    region_name=settings.AWS_REGION,
    config=Config(
        s3={"addressing_style": "path" if settings.AWS_S3_FORCE_PATH_STYLE else "virtual"},
        signature_version="s3v4",
    ),
)


def bucket_name(org_id: str | UUID) -> str:
    return f"{settings.S3_BUCKET_PREFIX}{org_id}"


def get_bytes(org_id: str | UUID, key: str) -> bytes:
    resp = _client.get_object(Bucket=bucket_name(org_id), Key=key)
    return resp["Body"].read()


def get_json(org_id: str | UUID, key: str) -> dict:
    return json.loads(get_bytes(org_id, key))


def put_bytes(org_id: str | UUID, key: str, data: bytes, content_type: str) -> None:
    _client.put_object(Bucket=bucket_name(org_id), Key=key, Body=data, ContentType=content_type)
