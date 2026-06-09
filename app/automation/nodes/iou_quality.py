import uuid

from app.automation.registry import node, HandleDef


def _compute_set_iou_summary(session, pred_set_id: str, gt_set_id: str, iou_threshold: float, match_labels: bool):
    from sqlalchemy import func, select, text

    from app.models.annotation import Annotation

    label_join = "AND p_cls.name = g_cls.name" if match_labels else ""
    query = text(f"""
        SELECT
            p.id AS pred_id,
            g.id AS gt_id,
            p_cls.name AS pred_class,
            g_cls.name AS gt_class,
            CASE
                WHEN ST_Area(ST_Union(p.geometry, g.geometry)) > 0
                THEN ST_Area(ST_Intersection(p.geometry, g.geometry)) /
                     ST_Area(ST_Union(p.geometry, g.geometry))
                ELSE 0
            END AS iou
        FROM annotations p
        JOIN annotation_classes p_cls ON p.class_id = p_cls.id
        CROSS JOIN annotations g
        JOIN annotation_classes g_cls ON g.class_id = g_cls.id
        WHERE p.annotation_set_id = :pred_set_id
          AND g.annotation_set_id = :gt_set_id
          AND p.deleted_at IS NULL
          AND g.deleted_at IS NULL
          AND ST_Intersects(p.geometry, g.geometry)
          {label_join}
        ORDER BY iou DESC
    """)

    rows = session.execute(query, {
        "pred_set_id": uuid.UUID(pred_set_id),
        "gt_set_id": uuid.UUID(gt_set_id),
    }).mappings().all()

    used_preds, used_gts = set(), set()
    pairs = []
    for r in rows:
        if r["pred_id"] in used_preds or r["gt_id"] in used_gts:
            continue
        pairs.append({
            "pred_id": str(r["pred_id"]),
            "gt_id": str(r["gt_id"]),
            "pred_class": r["pred_class"],
            "gt_class": r["gt_class"],
            "iou": round(float(r["iou"]), 4),
        })
        used_preds.add(r["pred_id"])
        used_gts.add(r["gt_id"])

    tp = sum(1 for p in pairs if p["iou"] >= iou_threshold)
    total_preds = session.execute(
        select(func.count()).where(
            Annotation.annotation_set_id == uuid.UUID(pred_set_id),
            Annotation.deleted_at.is_(None),
        )
    ).scalar() or 0
    total_gts = session.execute(
        select(func.count()).where(
            Annotation.annotation_set_id == uuid.UUID(gt_set_id),
            Annotation.deleted_at.is_(None),
        )
    ).scalar() or 0

    fn = total_gts - len(used_gts)
    fp_total = total_preds - tp
    precision = tp / (tp + fp_total) if (tp + fp_total) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    mean_iou = sum(p["iou"] for p in pairs) / len(pairs) if pairs else 0

    return {
        "pairs": pairs,
        "summary": {
            "true_positives": tp,
            "false_positives": fp_total,
            "false_negatives": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1_score": round(f1, 4),
            "mean_iou": round(mean_iou, 4),
            "matched_pairs": len(pairs),
            "prediction_count": int(total_preds),
            "ground_truth_count": int(total_gts),
        },
    }


@node(
    type="ground_truth_comparison",
    category="iou_quality",
    label="Ground Truth Comparison",
    description="Compare predictions against a ground truth annotation set using IoU.",
    inputs=[
        HandleDef(handle="predictions", type="annotation_set", label="Predictions"),
        HandleDef(handle="ground_truth", type="annotation_set", label="Ground Truth"),
    ],
    outputs=[HandleDef(handle="matches", type="matched_pairs", label="Matched Pairs")],
    config_schema={
        "type": "object",
        "properties": {
            "iou_threshold": {"type": "number", "title": "IoU Threshold", "default": 0.5, "minimum": 0, "maximum": 1},
            "match_labels": {"type": "boolean", "title": "Require Label Match", "default": True},
        },
    },
    icon="git-compare",
    color="#F59E0B",
)
def execute_ground_truth_comparison(session, config, input_data, **kwargs):
    """Pairwise IoU comparison between prediction and ground truth annotation sets."""
    pred_set = input_data.get("predictions", {})
    gt_set = input_data.get("ground_truth", {})
    pred_id = pred_set.get("id")
    gt_id = gt_set.get("id")

    if not pred_id or not gt_id:
        return {"matches": {"pairs": [], "summary": {}}}

    iou_threshold = config.get("iou_threshold", 0.5)
    match_labels = config.get("match_labels", True)
    result = _compute_set_iou_summary(session, pred_id, gt_id, iou_threshold, match_labels)
    return {"matches": {"iou_threshold": iou_threshold, **result}}


@node(
    type="multi_model_iou_comparison",
    category="iou_quality",
    label="Multi-Model IoU Comparison",
    description="Compare outputs from multiple model runs on the same dataset items and summarize pairwise IoU agreement.",
    inputs=[HandleDef(handle="annotation_sets", type="annotation_set", label="Annotation Sets", multiple=True)],
    outputs=[HandleDef(handle="comparison", type="quality_metrics", label="Comparison Summary")],
    config_schema={
        "type": "object",
        "properties": {
            "iou_threshold": {"type": "number", "title": "IoU Threshold", "default": 0.5, "minimum": 0, "maximum": 1},
            "match_labels": {"type": "boolean", "title": "Require Label Match", "default": True},
        },
    },
    icon="git-compare",
    color="#F59E0B",
)
def execute_multi_model_iou_comparison(session, config, input_data, **kwargs):
    from sqlalchemy import select

    from app.models.ai_model import AIModel
    from app.models.annotation_set import AnnotationSet
    from app.models.job import Job

    predictions = input_data.get("annotation_sets", [])
    if isinstance(predictions, dict):
        predictions = [predictions]
    predictions = [p for p in predictions if p]
    if len(predictions) < 2:
        return {"comparison": {"model_runs": predictions, "pairwise_comparisons": [], "summary": {"comparison_count": 0}}}

    iou_threshold = config.get("iou_threshold", 0.5)
    match_labels = config.get("match_labels", True)

    run_entries = []
    for pred in predictions:
        job_id = pred.get("job_id")
        annotation_set_ids = pred.get("annotation_set_ids") or []
        job = session.get(Job, uuid.UUID(job_id)) if job_id else None
        model = session.get(AIModel, job.model_id) if job and job.model_id else None
        rows = session.execute(
            select(AnnotationSet.id, AnnotationSet.dataset_item_id)
            .where(AnnotationSet.id.in_([uuid.UUID(sid) for sid in annotation_set_ids]))
        ).all() if annotation_set_ids else []
        by_item = {str(row.dataset_item_id): str(row.id) for row in rows if row.dataset_item_id is not None}
        run_entries.append({
            "job_id": job_id,
            "model_id": pred.get("model_id") or (str(job.model_id) if job and job.model_id else None),
            "model_name": pred.get("model_name") or (model.name if model else None),
            "annotation_set_ids": annotation_set_ids,
            "by_dataset_item": by_item,
            "processed_items": pred.get("processed_items"),
            "failed_items": pred.get("failed_items"),
        })

    pairwise = []
    for idx, left in enumerate(run_entries):
        for right in run_entries[idx + 1:]:
            shared_item_ids = sorted(set(left["by_dataset_item"]).intersection(right["by_dataset_item"]))
            item_comparisons = []
            for dataset_item_id in shared_item_ids:
                left_set_id = left["by_dataset_item"][dataset_item_id]
                right_set_id = right["by_dataset_item"][dataset_item_id]
                comp = _compute_set_iou_summary(
                    session,
                    left_set_id,
                    right_set_id,
                    iou_threshold,
                    match_labels,
                )
                item_comparisons.append({
                    "dataset_item_id": dataset_item_id,
                    "left_annotation_set_id": left_set_id,
                    "right_annotation_set_id": right_set_id,
                    **comp["summary"],
                })

            mean_iou = (
                round(sum(item["mean_iou"] for item in item_comparisons) / len(item_comparisons), 4)
                if item_comparisons else 0
            )
            pairwise.append({
                "left_model_id": left["model_id"],
                "left_model_name": left["model_name"],
                "left_job_id": left["job_id"],
                "right_model_id": right["model_id"],
                "right_model_name": right["model_name"],
                "right_job_id": right["job_id"],
                "shared_dataset_item_count": len(shared_item_ids),
                "mean_iou": mean_iou,
                "item_comparisons": item_comparisons,
            })

    return {
        "comparison": {
            "iou_threshold": iou_threshold,
            "match_labels": match_labels,
            "model_runs": run_entries,
            "pairwise_comparisons": pairwise,
            "summary": {
                "model_run_count": len(run_entries),
                "comparison_count": len(pairwise),
            },
        }
    }


@node(
    type="iou_threshold_gate",
    category="iou_quality",
    label="IoU Threshold Gate",
    description="Route annotations based on IoU: accept (high), review (mid), reject (low).",
    inputs=[HandleDef(handle="matches", type="matched_pairs")],
    outputs=[
        HandleDef(handle="accepted", type="annotation_set", label="Accepted"),
        HandleDef(handle="review", type="annotation_set", label="Needs Review"),
        HandleDef(handle="rejected", type="annotation_set", label="Rejected"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "accept_threshold": {"type": "number", "title": "Accept Threshold", "default": 0.85},
            "reject_threshold": {"type": "number", "title": "Reject Threshold", "default": 0.5},
        },
    },
    icon="git-branch",
    color="#F59E0B",
    frontend_preview=True,
)
def execute_iou_threshold_gate(session, config, input_data, **kwargs):
    """Route matched pairs into 3 buckets by IoU score."""
    matches = input_data.get("matches", {})
    pairs = matches.get("pairs", [])

    accept_t = config.get("accept_threshold", 0.85)
    reject_t = config.get("reject_threshold", 0.5)

    accepted_ids, review_ids, rejected_ids = [], [], []
    for pair in pairs:
        iou = pair.get("iou", 0)
        pred_id = pair.get("pred_id")
        if iou >= accept_t:
            accepted_ids.append(pred_id)
        elif iou >= reject_t:
            review_ids.append(pred_id)
        else:
            rejected_ids.append(pred_id)

    return {
        "accepted": {"annotation_ids": accepted_ids, "count": len(accepted_ids)},
        "review": {"annotation_ids": review_ids, "count": len(review_ids)},
        "rejected": {"annotation_ids": rejected_ids, "count": len(rejected_ids)},
    }


@node(
    type="inter_annotator_agreement",
    category="iou_quality",
    label="Inter-Annotator Agreement",
    description="Compare multiple annotation sets from different annotators. Compute Cohen's kappa / Fleiss' kappa.",
    inputs=[HandleDef(handle="annotation_sets", type="annotation_set", multiple=True)],
    outputs=[HandleDef(handle="agreement", type="quality_metrics")],
    config_schema={
        "type": "object",
        "properties": {
            "metric": {"type": "string", "enum": ["cohen_kappa", "fleiss_kappa", "iou_mean"], "default": "cohen_kappa"},
        },
    },
    icon="users",
    color="#F59E0B",
    status="placeholder",
)
def execute_inter_annotator_agreement(session, config, input_data, **kwargs):
    return {"agreement": {"metric": config.get("metric", "cohen_kappa")}}


@node(
    type="consensus_builder",
    category="iou_quality",
    label="Consensus Builder",
    description="Merge multiple annotation sets into a consensus set using majority voting or weighted merge.",
    inputs=[HandleDef(handle="annotation_sets", type="annotation_set", multiple=True)],
    outputs=[HandleDef(handle="consensus", type="annotation_set")],
    config_schema={
        "type": "object",
        "properties": {
            "strategy": {"type": "string", "enum": ["majority_vote", "weighted_merge", "union"], "default": "majority_vote"},
            "min_agreement": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1},
        },
    },
    icon="check-circle",
    color="#F59E0B",
    status="placeholder",
)
def execute_consensus_builder(session, config, input_data, **kwargs):
    return {"consensus": {}}


@node(
    type="confusion_matrix",
    category="iou_quality",
    label="Confusion Matrix",
    description="Generate per-label confusion matrix from matched prediction/ground-truth pairs.",
    inputs=[HandleDef(handle="matches", type="matched_pairs")],
    outputs=[HandleDef(handle="matrix", type="quality_metrics")],
    config_schema={},
    icon="grid",
    color="#F59E0B",
    status="placeholder",
)
def execute_confusion_matrix(session, config, input_data, **kwargs):
    return {"matrix": {}}


@node(
    type="annotator_scoring",
    category="iou_quality",
    label="Annotator Scoring",
    description="Score individual annotators against consensus or ground truth.",
    inputs=[
        HandleDef(handle="annotation_set", type="annotation_set"),
        HandleDef(handle="ground_truth", type="annotation_set"),
    ],
    outputs=[HandleDef(handle="scores", type="quality_metrics")],
    config_schema={},
    icon="award",
    color="#F59E0B",
    status="placeholder",
)
def execute_annotator_scoring(session, config, input_data, **kwargs):
    return {"scores": {}}


@node(
    type="spatial_rule_checker",
    category="iou_quality",
    label="Spatial Rule Checker",
    description="Validate annotations against spatial rules (min area, max overlap, boundary constraints).",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[
        HandleDef(handle="valid", type="annotation_set", label="Valid"),
        HandleDef(handle="violations", type="annotation_set", label="Violations"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "min_area_sqm": {"type": "number", "default": 1},
            "max_overlap_pct": {"type": "number", "default": 0.1},
            "must_be_within": {"type": "object", "title": "Boundary GeoJSON"},
        },
    },
    icon="shield",
    color="#F59E0B",
    frontend_preview=True,
)
def execute_spatial_rule_checker(session, config, input_data, **kwargs):
    """Validate annotations against spatial rules: min area, boundary containment."""
    import json
    from sqlalchemy import select, func, cast
    from geoalchemy2 import Geography
    from app.models.annotation import Annotation

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")
    if not aset_id:
        return {"valid": aset, "violations": {"annotation_ids": []}}

    min_area = config.get("min_area_sqm", 1)
    boundary = config.get("must_be_within")

    # Get all annotations with their area
    stmt = select(
        Annotation.id,
        Annotation.geometry,
        func.ST_Area(cast(Annotation.geometry, Geography)).label("area_sqm"),
    ).where(
        Annotation.annotation_set_id == uuid.UUID(aset_id),
        Annotation.deleted_at.is_(None),
    )
    rows = session.execute(stmt).all()

    valid_ids, violation_ids = [], []
    violation_details = []

    for r in rows:
        violations = []
        if r.area_sqm < min_area:
            violations.append(f"area {r.area_sqm:.1f} < min {min_area}")

        if violations:
            violation_ids.append(str(r.id))
            violation_details.append({"id": str(r.id), "reasons": violations})
        else:
            valid_ids.append(str(r.id))

    # Boundary containment check (if configured) — batch via PostGIS
    if boundary and valid_ids:
        from sqlalchemy import text
        boundary_geojson = json.dumps(boundary)
        outside = session.execute(
            text("""
                SELECT id FROM annotations
                WHERE id = ANY(:ids)
                  AND NOT ST_Within(geometry, ST_GeomFromGeoJSON(:boundary))
            """),
            {"ids": [uuid.UUID(i) for i in valid_ids], "boundary": boundary_geojson},
        ).scalars().all()

        outside_set = {str(i) for i in outside}
        for oid in outside_set:
            valid_ids.remove(oid)
            violation_ids.append(oid)
            violation_details.append({"id": oid, "reasons": ["outside boundary"]})

    return {
        "valid": {**aset, "annotation_ids": valid_ids, "count": len(valid_ids)},
        "violations": {**aset, "annotation_ids": violation_ids, "count": len(violation_ids), "details": violation_details},
    }


@node(
    type="duplicate_detection",
    category="iou_quality",
    label="Duplicate Detection",
    description="Find and flag duplicate annotations based on spatial overlap.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[
        HandleDef(handle="unique", type="annotation_set", label="Unique"),
        HandleDef(handle="duplicates", type="annotation_set", label="Duplicates"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "iou_threshold": {"type": "number", "default": 0.9, "minimum": 0, "maximum": 1},
        },
    },
    icon="copy",
    color="#F59E0B",
)
def execute_duplicate_detection(session, config, input_data, **kwargs):
    """Detect duplicate annotations via pairwise IoU within the same set."""
    from sqlalchemy import text

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")
    if not aset_id:
        return {"unique": aset, "duplicates": {"annotation_ids": []}}

    iou_threshold = config.get("iou_threshold", 0.9)

    # Find all pairs with IoU above threshold within the same set
    query = text("""
        SELECT a.id AS id_a, b.id AS id_b,
               ST_Area(ST_Intersection(a.geometry, b.geometry)) /
               NULLIF(ST_Area(ST_Union(a.geometry, b.geometry)), 0) AS iou
        FROM annotations a
        JOIN annotations b ON a.id < b.id
            AND a.annotation_set_id = b.annotation_set_id
            AND ST_Intersects(a.geometry, b.geometry)
        WHERE a.annotation_set_id = :set_id
          AND a.deleted_at IS NULL
          AND b.deleted_at IS NULL
        HAVING ST_Area(ST_Intersection(a.geometry, b.geometry)) /
               NULLIF(ST_Area(ST_Union(a.geometry, b.geometry)), 0) >= :threshold
    """)

    rows = session.execute(query, {
        "set_id": uuid.UUID(aset_id),
        "threshold": iou_threshold,
    }).mappings().all()

    # Mark the second of each duplicate pair as a duplicate
    duplicate_ids = set()
    for r in rows:
        duplicate_ids.add(str(r["id_b"]))

    # Get all annotation IDs in the set
    from sqlalchemy import select
    from app.models.annotation import Annotation

    all_ids = session.execute(
        select(Annotation.id).where(
            Annotation.annotation_set_id == uuid.UUID(aset_id),
            Annotation.deleted_at.is_(None),
        )
    ).scalars().all()

    unique_ids = [str(aid) for aid in all_ids if str(aid) not in duplicate_ids]

    return {
        "unique": {**aset, "annotation_ids": unique_ids, "count": len(unique_ids)},
        "duplicates": {**aset, "annotation_ids": list(duplicate_ids), "count": len(duplicate_ids)},
    }


def _gdal_env_for_node():
    """rasterio Env options for reading COGs from object store inside a node."""
    from app.config import settings

    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    return {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if settings.AWS_ENDPOINT_URL.startswith("https://") else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    }


def _load_mask_dataset(session, dataset_id: str, organization_id: str | None):
    """Resolve a segmentation-mask dataset to ``(s3_uri, class_map)``.

    Reads the dataset's ``rendering_config.class_map`` (set when the mask was
    ingested / class-mapped) and the first active item's COG uri.
    """
    from sqlalchemy import select

    from app.models.dataset import Dataset
    from app.models.dataset_item import DatasetItem

    dataset = session.get(Dataset, uuid.UUID(str(dataset_id)))
    if dataset is None:
        raise ValueError(f"Dataset {dataset_id} not found")
    if organization_id and str(dataset.organization_id) != str(organization_id):
        raise ValueError("Dataset does not belong to this organization")

    class_map = ((dataset.metadata_ or {}).get("rendering_config") or {}).get("class_map")
    if not class_map or not class_map.get("value_class_map"):
        raise ValueError(
            f"Dataset '{dataset.name}' has no class map. Map its pixel values to "
            "classes (or generate it with Rasterize Annotation Set) before comparing."
        )

    item = session.execute(
        select(DatasetItem)
        .where(DatasetItem.dataset_id == dataset.id, DatasetItem.is_active.is_(True))
        .limit(1)
    ).scalar_one_or_none()
    if item is None:
        raise ValueError(f"Dataset '{dataset.name}' has no active raster item")

    return item.s3_uri, class_map, dataset.name


def _class_names_for_maps(session, *class_maps) -> dict:
    """Build ``{class_id: name}`` for every class id referenced by the maps."""
    from sqlalchemy import select

    from app.models.annotation_class import AnnotationClass

    ids = set()
    for cm in class_maps:
        for cid in (cm or {}).get("value_class_map", {}).values():
            ids.add(str(cid))
    if not ids:
        return {}
    rows = session.execute(
        select(AnnotationClass.id, AnnotationClass.name).where(
            AnnotationClass.id.in_([uuid.UUID(c) for c in ids])
        )
    ).all()
    return {str(cid): name for cid, name in rows}


@node(
    type="raster_mask_metrics",
    category="iou_quality",
    label="Raster Mask Metrics",
    description=(
        "Compare a model-output mask (wired in from a previous node, e.g. "
        "Rasterize Annotation Set) against a ground-truth mask dataset, in the "
        "raster domain. The smaller-extent mask sets the evaluation grid; the "
        "larger is resampled onto it, so metrics cover only the overlap. Reports "
        "per-class IoU / precision / recall / F1 plus overall pixel accuracy and "
        "mean IoU."
    ),
    inputs=[HandleDef(handle="model_output", type="dataset", label="Model Output Mask")],
    outputs=[HandleDef(handle="metrics", type="quality_metrics", label="Metrics")],
    config_schema={
        "type": "object",
        "properties": {
            "ground_truth_dataset_id": {
                "type": "string",
                "format": "uuid",
                "title": "Ground Truth Dataset",
                "x-picker": "dataset",
                "description": "Pre-existing segmentation-mask dataset used as ground truth.",
            },
        },
        "required": ["ground_truth_dataset_id"],
    },
    icon="ruler",
    color="#F59E0B",
)
def execute_raster_mask_metrics(session, config, input_data, **kwargs):
    """Per-class raster metrics between an upstream model mask and a GT dataset.

    The model-output dataset arrives on the ``model_output`` input handle (the
    ``dataset`` produced by an upstream node such as Rasterize Annotation Set);
    the ground truth is a pre-existing dataset chosen in config.
    """
    from app.services.conversion import compare_raster_masks

    pred_payload = input_data.get("model_output")
    if isinstance(pred_payload, list):
        pred_payload = pred_payload[0] if pred_payload else None
    pred_id = pred_payload.get("id") if isinstance(pred_payload, dict) else None
    gt_id = config.get("ground_truth_dataset_id")
    if not pred_id:
        raise ValueError("No model-output dataset on the input — wire a dataset-producing node in")
    if not gt_id:
        raise ValueError("ground_truth_dataset_id is required")
    if str(gt_id) == str(pred_id):
        raise ValueError("Ground truth and model output must be different datasets")

    org_id = kwargs.get("organization_id")
    gt_uri, gt_class_map, gt_name = _load_mask_dataset(session, gt_id, org_id)
    pred_uri, pred_class_map, pred_name = _load_mask_dataset(session, pred_id, org_id)
    class_names = _class_names_for_maps(session, gt_class_map, pred_class_map)

    result = compare_raster_masks(
        gt_uri,
        pred_uri,
        _gdal_env_for_node(),
        gt_class_map=gt_class_map,
        pred_class_map=pred_class_map,
        class_names=class_names,
    )

    return {
        "metrics": {
            "type": "raster_mask_metrics",
            "ground_truth": {"dataset_id": str(gt_id), "name": gt_name},
            "prediction": {"dataset_id": str(pred_id), "name": pred_name},
            "per_class": result.per_class,
            "overall": result.overall,
            "grid": result.grid,
        }
    }
