from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.enums import JobStatus, JobType
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.user import User
from app.schemas.job import JobRead

from geoops.schemas import (
    AnomalyDetectionRequest,
    AOIScanRequest,
    EmbeddingCreateRequest,
    EmbeddingRead,
    EmbeddingSearchMatch,
    EmbeddingSearchRequest,
)
from geoops.service import EmbeddingService

router = APIRouter(prefix="/embeddings", tags=["embeddings"])


@router.post("", response_model=EmbeddingRead, status_code=status.HTTP_201_CREATED)
async def create_embedding(
    payload: EmbeddingCreateRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Crop the selected bbox/mask, embed it, and store the vector.

    Synchronous — a single patch + one call to the embed model.
    """
    service = EmbeddingService(db)
    return await service.create_embedding(org_id, current_user.id, payload)


@router.get("/{embedding_id}", response_model=EmbeddingRead)
async def get_embedding(
    embedding_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = EmbeddingService(db)
    return await service.get_embedding(org_id, embedding_id)


@router.post("/search", response_model=list[EmbeddingSearchMatch])
async def search_embeddings(
    payload: EmbeddingSearchRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Cosine nearest-neighbours over the embedding bank."""
    service = EmbeddingService(db)
    return await service.search(org_id, payload)


@router.post("/aoi-scan", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def aoi_scan(
    payload: AOIScanRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Scan an AOI for patches similar to a reference embedding (async job).

    Tiles the AOI at the reference embedding's own scale tier, embeds each
    tile not already cached in ``embedding_tiles`` from a prior scan, and
    ranks by similarity. Multi-tile work is HTTP-bound (one TiTiler crop +
    one model call per new tile), so this enqueues a Celery job instead of
    running inline — poll ``GET /api/jobs/{job_id}`` for
    ``config.result`` (matches) once ``status == "completed"``.
    """
    service = EmbeddingService(db)
    await service.get_embedding(org_id, payload.reference_embedding_id)
    await service._resolve_class_and_annotation(org_id, None, payload.output_class_id)  # noqa: SLF001

    rows = await db.scalars(
        select(DatasetItem).where(
            DatasetItem.id.in_(payload.dataset_item_ids),
            DatasetItem.organization_id == org_id,
            DatasetItem.is_active.is_(True),
        )
    )
    if len(rows.all()) != len(set(payload.dataset_item_ids)):
        raise HTTPException(status_code=404, detail="One or more dataset items not found")

    job = Job(
        organization_id=org_id,
        type=JobType.AOI_SCAN,
        status=JobStatus.QUEUED,
        config={"request": payload.model_dump(mode="json")},
        input_refs=[
            {"type": "dataset_item", "id": str(item_id)} for item_id in payload.dataset_item_ids
        ],
        created_by_user_id=current_user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from geoops.tasks import run_aoi_scan  # noqa: PLC0415

    run_aoi_scan.apply_async(args=[str(job.id)])
    return job


@router.post("/anomaly-scan", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def anomaly_scan(
    payload: AnomalyDetectionRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Find anomalous annotations of one class within a set of dataset items (async job).

    Only embeds annotations a model already produced (``AnnotationSet.source_type
    == 'model'``) — never tiles empty space like ``aoi-scan`` does. Each
    annotation is embedded once (reused across runs via the annotation+model
    cache), grouped by real-world scale tier, and ranked by cosine distance to
    its group's centroid — the annotations least like their peers surface
    first. Poll ``GET /api/jobs/{job_id}`` for ``config.result``.
    """
    service = EmbeddingService(db)
    # Reuses the same class-ownership and model/endpoint_url validation the
    # manual create/aoi-scan paths already do — fail fast here rather than
    # deep inside the worker.
    await service._resolve_class_and_annotation(org_id, None, payload.class_id)  # noqa: SLF001
    await service._resolve_class_and_annotation(org_id, None, payload.output_class_id)  # noqa: SLF001
    await service._get_model(org_id, payload.model_id)  # noqa: SLF001

    rows = await db.scalars(
        select(DatasetItem).where(
            DatasetItem.id.in_(payload.dataset_item_ids),
            DatasetItem.organization_id == org_id,
            DatasetItem.is_active.is_(True),
        )
    )
    if len(rows.all()) != len(set(payload.dataset_item_ids)):
        raise HTTPException(status_code=404, detail="One or more dataset items not found")

    job = Job(
        organization_id=org_id,
        type=JobType.ANOMALY_DETECTION,
        status=JobStatus.QUEUED,
        config={"request": payload.model_dump(mode="json")},
        input_refs=[
            {"type": "dataset_item", "id": str(item_id)} for item_id in payload.dataset_item_ids
        ]
        + [{"type": "annotation_class", "id": str(payload.class_id)}],
        created_by_user_id=current_user.id,
        model_id=payload.model_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from geoops.tasks import run_anomaly_detection  # noqa: PLC0415

    run_anomaly_detection.apply_async(args=[str(job.id)])
    return job
