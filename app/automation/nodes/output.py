import statistics
import uuid
from datetime import datetime, UTC

import httpx

from app.automation.registry import node, HandleDef


_CONFIDENCE_BUCKETS = (
    ("0.00-0.50", 0.0, 0.5),
    ("0.50-0.70", 0.5, 0.7),
    ("0.70-0.85", 0.7, 0.85),
    ("0.85-1.00", 0.85, 1.0001),  # upper exclusive; 1.0001 catches 1.0 exactly
)


def _confidence_buckets(values: list[float]) -> dict[str, int]:
    """Histogram-style buckets for confidence values. Skips None entries."""
    counts = {label: 0 for label, _, _ in _CONFIDENCE_BUCKETS}
    for v in values:
        if v is None:
            continue
        f = float(v)
        for label, lo, hi in _CONFIDENCE_BUCKETS:
            if lo <= f < hi:
                counts[label] += 1
                break
    return counts


def _area_stats(areas: list[float]) -> dict[str, float | None]:
    """Min / max / median area in sq m. Returns Nones when the list is empty."""
    if not areas:
        return {"min_sqm": None, "max_sqm": None, "median_sqm": None}
    return {
        "min_sqm": round(min(areas), 4),
        "max_sqm": round(max(areas), 4),
        "median_sqm": round(statistics.median(areas), 4),
    }


@node(
    type="send_webhook",
    category="output",
    label="Send Webhook",
    description="POST results to an external webhook URL.",
    inputs=[HandleDef(handle="data", type="any")],
    config_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "title": "Webhook URL"},
            "headers": {"type": "object", "title": "Custom Headers", "default": {}},
        },
        "required": ["url"],
    },
    icon="send",
)
def execute_send_webhook(session, config, input_data, **kwargs):
    response = httpx.post(config["url"], json=input_data, headers=config.get("headers", {}), timeout=30)
    response.raise_for_status()
    return {"status_code": response.status_code}


@node(
    type="send_email",
    category="output",
    label="Send Email",
    description="Send an email notification via SES.",
    inputs=[HandleDef(handle="data", type="any")],
    config_schema={
        "type": "object",
        "properties": {
            "to": {"type": "array", "items": {"type": "string"}, "title": "Recipients"},
            "subject": {"type": "string", "title": "Subject"},
            "template": {"type": "string", "title": "Template Name", "default": "automation_result"},
        },
        "required": ["to", "subject"],
    },
    icon="mail",
    status="placeholder",
)
def execute_send_email(session, config, input_data, **kwargs):
    # Placeholder — full implementation uses boto3 SES
    return {"sent_to": config.get("to", [])}


@node(
    type="generate_report",
    category="output",
    label="Generate Report",
    description="Build a JSON report with one section per annotation set (counts, per-class breakdown, optional per-annotation rows). Wire multiple Run Inference outputs in to compare sets side by side. Quality-metrics inputs (e.g. Raster Mask Metrics) add a metrics section.",
    inputs=[
        HandleDef(handle="annotation_sets", type="annotation_set", label="Annotation Sets", required=False, multiple=True),
        HandleDef(handle="metrics", type="quality_metrics", label="Metrics", required=False, multiple=True),
    ],
    outputs=[HandleDef(handle="report", type="any", label="Report")],
    config_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "title": "Report Title",
                "default": "Annotation Report",
            },
            "include_per_class_breakdown": {
                "type": "boolean",
                "title": "Per-Class Breakdown",
                "default": True,
            },
            "include_annotation_rows": {
                "type": "boolean",
                "title": "Include Annotation Rows",
                "description": "Adds one row per annotation. Can be very large for big sets.",
                "default": False,
            },
        },
    },
    icon="file-text",
    color="#06B6D4",
)
def execute_generate_report(session, config, input_data, **kwargs):
    """Build a structured JSON report from one or more upstream annotation sets.

    Accepts both single-set payloads (`{id, name, ...}`) and multi-set
    payloads (`{annotation_set_ids: [...]}`, as emitted by Run Inference).
    Each individual set becomes its own section in the report.
    """
    from sqlalchemy import select, func, cast
    from geoalchemy2 import Geography

    from app.models.annotation import Annotation
    from app.models.annotation_class import AnnotationClass
    from app.models.annotation_set import AnnotationSet
    from app.models.annotation_schema import AnnotationSchema
    from app.models.ai_model import AIModel

    # Normalize input: collect a flat list of (annotation_set_id, source_handle_label)
    raw = input_data.get("annotation_sets")
    if isinstance(raw, dict):
        raw = [raw]
    raw = list(raw or [])

    aset_uuids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for payload in raw:
        if not isinstance(payload, dict):
            continue
        ids = payload.get("annotation_set_ids") or []
        if not ids and payload.get("id"):
            ids = [payload["id"]]
        for sid in ids:
            try:
                u = uuid.UUID(str(sid))
            except (TypeError, ValueError):
                continue
            if u not in seen:
                seen.add(u)
                aset_uuids.append(u)

    include_per_class = bool(config.get("include_per_class_breakdown", True))
    include_rows = bool(config.get("include_annotation_rows", False))
    title = str(config.get("title") or "Annotation Report")

    sections: list[dict] = []
    total_annotations = 0
    total_area_sqm = 0.0

    for aset_id in aset_uuids:
        aset = session.get(AnnotationSet, aset_id)
        if aset is None or aset.deleted_at is not None:
            sections.append({
                "annotation_set_id": str(aset_id),
                "missing": True,
            })
            continue

        schema_name = None
        if aset.schema_id is not None:
            schema = session.get(AnnotationSchema, aset.schema_id)
            schema_name = schema.name if schema else None

        model_name = None
        if aset.model_id is not None:
            model = session.get(AIModel, aset.model_id)
            model_name = model.name if model else None

        # Single fetch per set — all rows with class, confidence and area.
        # Stats and per-class breakdowns are computed in Python so the
        # confidence buckets and area distribution can share the same data
        # without extra round-trips.
        all_rows = session.execute(
            select(
                Annotation.id.label("annotation_id"),
                AnnotationClass.name.label("class_name"),
                Annotation.confidence,
                func.ST_Area(cast(Annotation.geometry, Geography)).label("area_sqm"),
            )
            .join(AnnotationClass, Annotation.class_id == AnnotationClass.id)
            .where(
                Annotation.annotation_set_id == aset_id,
                Annotation.deleted_at.is_(None),
            )
        ).all()

        areas = [float(r.area_sqm or 0) for r in all_rows]
        confs = [float(r.confidence) for r in all_rows if r.confidence is not None]
        count = len(all_rows)
        area = sum(areas)
        avg_conf = statistics.mean(confs) if confs else None
        total_annotations += count
        total_area_sqm += area

        section: dict = {
            "annotation_set_id": str(aset_id),
            "name": aset.name,
            "schema": schema_name,
            "source_type": aset.source_type,
            "model": model_name,
            "created_at": aset.created_at.isoformat() if aset.created_at else None,
            "totals": {
                "annotation_count": count,
                "total_area_sqm": round(area, 4),
                "total_area_hectares": round(area / 10_000, 4),
                "avg_confidence": round(avg_conf, 4) if avg_conf is not None else None,
                "confidence_buckets": _confidence_buckets(confs),
                "area_stats": _area_stats(areas),
            },
        }

        if include_per_class:
            # Bucket rows by class
            by_class: dict[str, dict[str, list]] = {}
            for r in all_rows:
                bucket = by_class.setdefault(
                    r.class_name, {"areas": [], "confs": []}
                )
                bucket["areas"].append(float(r.area_sqm or 0))
                if r.confidence is not None:
                    bucket["confs"].append(float(r.confidence))

            per_class = []
            for class_name, b in by_class.items():
                cls_count = len(b["areas"])
                cls_area = sum(b["areas"])
                cls_avg_conf = statistics.mean(b["confs"]) if b["confs"] else None
                per_class.append({
                    "class": class_name,
                    "count": cls_count,
                    "area_sqm": round(cls_area, 4),
                    "area_hectares": round(cls_area / 10_000, 4),
                    "avg_confidence": round(cls_avg_conf, 4) if cls_avg_conf is not None else None,
                    "area_stats": _area_stats(b["areas"]),
                    "confidence_buckets": _confidence_buckets(b["confs"]),
                })
            per_class.sort(key=lambda c: c["count"], reverse=True)
            section["per_class"] = per_class

        if include_rows:
            ordered = sorted(
                all_rows,
                key=lambda r: (
                    -(r.confidence if r.confidence is not None else -1),
                    str(r.annotation_id),
                ),
            )
            section["rows"] = [
                {
                    "annotation_id": str(r.annotation_id),
                    "class": r.class_name,
                    "confidence": (
                        round(float(r.confidence), 4) if r.confidence is not None else None
                    ),
                    "area_sqm": round(float(r.area_sqm or 0), 4),
                }
                for r in ordered
            ]

        sections.append(section)

    # Optional quality-metrics inputs (e.g. Raster Mask Metrics). Each upstream
    # payload becomes one entry under `metrics`, untouched, so the report can
    # show per-class IoU / precision / recall alongside the set sections.
    raw_metrics = input_data.get("metrics")
    if isinstance(raw_metrics, dict):
        raw_metrics = [raw_metrics]
    metrics_sections = [m for m in (raw_metrics or []) if isinstance(m, dict)]

    report = {
        "title": title,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "annotation_set_count": len(sections),
            "total_annotations": total_annotations,
            "total_area_sqm": round(total_area_sqm, 4),
            "total_area_hectares": round(total_area_sqm / 10_000, 4),
            "metrics_count": len(metrics_sections),
        },
        "sections": sections,
    }
    if metrics_sections:
        report["metrics"] = metrics_sections
    return {"report": report}


@node(
    type="in_app_notification",
    category="output",
    label="In-App Notification",
    description="Create an in-app notification for specified users.",
    inputs=[HandleDef(handle="data", type="any")],
    config_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "title": "Notification Title"},
            "message": {"type": "string", "title": "Message"},
            "notify_creator": {"type": "boolean", "default": True},
        },
        "required": ["title"],
    },
    icon="bell",
    status="placeholder",
)
def execute_in_app_notification(session, config, input_data, **kwargs):
    return {"notified": True}
