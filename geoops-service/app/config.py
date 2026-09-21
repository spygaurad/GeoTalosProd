from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Bearer token every non-health route requires (Authorization: Bearer <token>).
    # Set explicitly via env — no default, so a misconfigured deploy fails
    # loudly (auth-disabled-by-accident) rather than silently accepting
    # unauthenticated requests. See AwakeForestProd's earlier decision: this
    # service requires auth from day one even while colocated on the same
    # docker network as the app, since it's meant to move to a real network
    # boundary later without a design change.
    API_TOKEN: str

    # Where backbone checkpoints get cached (HuggingFace hub downloads).
    MODEL_CACHE_DIR: str = "/srv/model-cache"

    # MinIO/S3 — same bucket-per-org convention app.services.storage_service
    # uses (bucket = f"{S3_BUCKET_PREFIX}{org_id}"). This service reads the
    # training-runs/{job_id}/... objects geoops.dino_tasks/yolo_tasks wrote.
    AWS_ENDPOINT_URL: str
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_S3_FORCE_PATH_STYLE: bool = True
    S3_BUCKET_PREFIX: str = "org-"


settings = Settings()
