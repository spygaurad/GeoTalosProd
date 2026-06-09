import uuid

from app.automation.registry import node, HandleDef


@node(
    type="filter_annotations",
    category="data_operations",
    label="Filter Annotations",
    description="Filter an annotation set by label, confidence, or area.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[
        HandleDef(handle="matched", type="annotation_set", label="Matched"),
        HandleDef(handle="unmatched", type="annotation_set", label="Unmatched"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "labels": {"type": "array", "items": {"type": "string"}, "title": "Class Names (include)"},
            "min_confidence": {"type": "number", "default": 0},
            "max_confidence": {"type": "number", "default": 1},
        },
    },
    icon="filter",
    color="#10B981",
    frontend_preview=True,
)
def execute_filter_annotations(session, config, input_data, **kwargs):
    """Filter annotations by class name and confidence range."""
    from sqlalchemy import select
    from app.models.annotation import Annotation
    from app.models.annotation_class import AnnotationClass

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")
    if not aset_id:
        return {"matched": aset, "unmatched": {"id": None, "annotation_ids": []}}

    stmt = (
        select(Annotation.id, AnnotationClass.name.label("class_name"), Annotation.confidence)
        .join(AnnotationClass, Annotation.class_id == AnnotationClass.id)
        .where(
            Annotation.annotation_set_id == uuid.UUID(aset_id),
            Annotation.deleted_at.is_(None),
        )
    )
    rows = session.execute(stmt).all()

    labels = config.get("labels")
    min_conf = config.get("min_confidence", 0)
    max_conf = config.get("max_confidence", 1)

    matched_ids, unmatched_ids = [], []
    for row in rows:
        conf = row.confidence if row.confidence is not None else 1.0
        label_ok = (not labels) or (row.class_name in labels)
        conf_ok = min_conf <= conf <= max_conf
        if label_ok and conf_ok:
            matched_ids.append(str(row.id))
        else:
            unmatched_ids.append(str(row.id))

    return {
        "matched": {**aset, "annotation_ids": matched_ids, "count": len(matched_ids)},
        "unmatched": {**aset, "annotation_ids": unmatched_ids, "count": len(unmatched_ids)},
    }


@node(
    type="merge_annotation_sets",
    category="data_operations",
    label="Merge Annotation Sets",
    description=(
        "Combine multiple annotation sets into one new set. The merged set "
        "inherits the schema of its sources and is auto-grouped into that "
        "schema's collection. To show it on a map, chain an Overlay on Map node."
    ),
    inputs=[HandleDef(handle="sets", type="annotation_set", multiple=True)],
    outputs=[HandleDef(handle="merged", type="annotation_set")],
    config_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "title": "Merged Set Name", "default": "Merged"},
        },
    },
    icon="git-merge",
    color="#10B981",
)
def execute_merge_annotation_sets(session, config, input_data, **kwargs):
    """Merge annotations from multiple sets into a new annotation set."""
    from sqlalchemy import select, text
    from app.models.annotation import Annotation
    from app.models.annotation_set import AnnotationSet

    sets = input_data.get("sets", [])
    if isinstance(sets, dict):
        sets = [sets]

    set_ids = [s.get("id") for s in sets if s.get("id")]
    if not set_ids:
        return {"merged": {}}

    source_uuids = [uuid.UUID(sid) for sid in set_ids]
    source_rows = session.execute(
        select(AnnotationSet.id, AnnotationSet.schema_id).where(
            AnnotationSet.id.in_(source_uuids)
        )
    ).all()
    # Inherit a schema from the first source that has one, so the merged set can
    # be auto-grouped into that schema's collection.
    merged_schema_id = next((r.schema_id for r in source_rows if r.schema_id), None)

    # Create a new merged annotation set under the run's organization (sourced
    # server-side from the pipeline, never the frontend).
    merged = AnnotationSet(
        organization_id=uuid.UUID(kwargs["organization_id"]),
        schema_id=merged_schema_id,
        source_type="merge",
        name=config.get("name") or "Merged",
    )
    session.add(merged)
    session.flush()

    from app.services.annotation_set_grouping import ensure_schema_collection_sync
    ensure_schema_collection_sync(session, merged)

    # Copy annotations from all source sets into the new set
    session.execute(text("""
        INSERT INTO annotations (annotation_set_id, class_id, geometry, confidence, properties)
        SELECT :new_set_id, class_id, geometry, confidence, properties
        FROM annotations
        WHERE annotation_set_id = ANY(:source_ids)
          AND deleted_at IS NULL
    """), {
        "new_set_id": merged.id,
        "source_ids": [uuid.UUID(sid) for sid in set_ids],
    })
    session.flush()

    # Count merged annotations
    count = session.execute(
        select(Annotation.id).where(
            Annotation.annotation_set_id == merged.id,
            Annotation.deleted_at.is_(None),
        )
    ).scalars().all()

    return {"merged": {
        "id": str(merged.id),
        "name": merged.name,
        "count": len(count),
        "source_set_ids": set_ids,
    }}


@node(
    type="review_queue",
    category="data_operations",
    label="Review Queue",
    description="Route annotations to a human review queue.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    config_schema={
        "type": "object",
        "properties": {
            "assign_to": {"type": "array", "items": {"type": "string", "format": "uuid"}, "title": "Assign to Users"},
            "priority": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
        },
    },
    icon="inbox",
    color="#10B981",
    status="placeholder",
)
def execute_review_queue(session, config, input_data, **kwargs):
    return {"annotation_set": input_data.get("annotation_set", {})}


@node(
    type="status_transition",
    category="data_operations",
    label="Status Transition",
    description="Bulk-update annotation status (e.g., draft → submitted → approved).",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    config_schema={
        "type": "object",
        "properties": {
            "new_status": {"type": "string", "enum": ["draft", "submitted", "approved", "rejected", "archived"], "title": "New Status"},
        },
        "required": ["new_status"],
    },
    icon="check-square",
    color="#10B981",
    status="placeholder",
)
def execute_status_transition(session, config, input_data, **kwargs):
    return {"annotation_set": input_data.get("annotation_set", {})}
