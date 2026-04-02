import uuid

from app.automation.registry import node, HandleDef


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
    from sqlalchemy import text

    pred_set = input_data.get("predictions", {})
    gt_set = input_data.get("ground_truth", {})
    pred_id = pred_set.get("id")
    gt_id = gt_set.get("id")

    if not pred_id or not gt_id:
        return {"matches": {"pairs": [], "summary": {}}}

    iou_threshold = config.get("iou_threshold", 0.5)
    match_labels = config.get("match_labels", True)

    # Compute pairwise IoU between prediction and ground truth geometries
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
        "pred_set_id": uuid.UUID(pred_id),
        "gt_set_id": uuid.UUID(gt_id),
    }).mappings().all()

    # Greedy matching: each prediction and GT used at most once
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
    fp = len(used_preds) - tp  # matched but below threshold (not really — unmatched preds)
    # Count total predictions and GTs for precision/recall
    from sqlalchemy import select, func
    from app.models.annotation import Annotation

    total_preds = session.execute(
        select(func.count()).where(
            Annotation.annotation_set_id == uuid.UUID(pred_id),
            Annotation.deleted_at.is_(None),
        )
    ).scalar()
    total_gts = session.execute(
        select(func.count()).where(
            Annotation.annotation_set_id == uuid.UUID(gt_id),
            Annotation.deleted_at.is_(None),
        )
    ).scalar()

    fn = total_gts - len(used_gts)
    fp_total = total_preds - tp

    precision = tp / (tp + fp_total) if (tp + fp_total) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    return {"matches": {
        "iou_threshold": iou_threshold,
        "pairs": pairs,
        "summary": {
            "true_positives": tp,
            "false_positives": fp_total,
            "false_negatives": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1_score": round(f1, 4),
        },
    }}


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
