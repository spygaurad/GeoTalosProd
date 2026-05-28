"""
Analysis pipeline nodes for GIS operations on annotations and datasets.

All nodes use PostGIS SQL for computation. Heavy raster-based variants
delegate to Celery analysis workers via DeferToJob pattern.
"""
import statistics
import uuid
from sqlalchemy import select, func, cast, text
from geoalchemy2 import Geography

from app.automation.registry import node, HandleDef, DeferToJob


# ============================================================================
# 1. AREA CALCULATION
# ============================================================================

@node(
    type="area_calculation",
    category="analysis",
    label="Area Calculation",
    description="Compute area in square metres for each annotation using PostGIS ST_Area(geography).",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set", label="Annotation Set", required=True)],
    outputs=[HandleDef(handle="areas", type="quality_metrics", label="Area Metrics")],
    config_schema={},
    icon="maximize-2",
    color="#0EA5E9",
)
def execute_area_calculation(session, config, input_data, **kwargs):
    from app.models.annotation import Annotation

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")
    if not aset_id:
        return {"areas": {"annotation_set_id": None, "metrics": [], "total_area_sqm": 0, "count": 0}}

    try:
        aset_uuid = uuid.UUID(aset_id)
    except (ValueError, TypeError):
        return {"areas": {"annotation_set_id": None, "metrics": [], "total_area_sqm": 0, "count": 0}}

    rows = session.execute(
        select(
            Annotation.id,
            func.ST_Area(cast(Annotation.geometry, Geography)).label("area_sqm"),
        ).where(
            Annotation.annotation_set_id == aset_uuid,
            Annotation.deleted_at.is_(None),
        )
    ).all()

    metrics = [{"annotation_id": str(r.id), "area_sqm": round(float(r.area_sqm or 0), 4)} for r in rows]
    total = sum(m["area_sqm"] for m in metrics)
    return {
        "areas": {
            "annotation_set_id": aset_id,
            "total_area_sqm": round(total, 4),
            "count": len(metrics),
            "metrics": metrics,
        }
    }


# ============================================================================
# 2. TIMESERIES ANALYSIS
# ============================================================================

@node(
    type="timeseries_analysis",
    category="analysis",
    label="Timeseries Analysis",
    description="Group annotations by time period and compute count, area, and confidence statistics.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set", label="Annotation Set", required=True)],
    outputs=[HandleDef(handle="timeseries_data", type="quality_metrics", label="Timeseries Data")],
    config_schema={
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "title": "Metric",
                "enum": ["count", "area", "confidence"],
                "default": "count",
            },
            "group_by": {
                "type": "string",
                "title": "Group By",
                "enum": ["day", "week", "month"],
                "default": "month",
            },
        },
    },
    icon="trending-up",
    color="#0EA5E9",
)
def execute_timeseries_analysis(session, config, input_data, **kwargs):
    from app.models.annotation import Annotation

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")
    if not aset_id:
        return {"timeseries_data": {"series": [], "annotation_set_id": None}}

    try:
        aset_uuid = uuid.UUID(aset_id)
    except (ValueError, TypeError):
        return {"timeseries_data": {"series": [], "annotation_set_id": None}}

    period = config.get("group_by", "month").lower()
    period_map = {"day": "day", "week": "week", "month": "month"}
    sql_period = period_map.get(period, "month")

    rows = session.execute(
        text(f"""
            SELECT
                date_trunc(:{sql_period}, a.created_at) AS period,
                COUNT(*) AS count,
                SUM(ST_Area(a.geometry::geography)) AS total_area_sqm,
                AVG(a.confidence) AS avg_confidence
            FROM annotations a
            WHERE a.annotation_set_id = :set_id
              AND a.deleted_at IS NULL
            GROUP BY 1
            ORDER BY 1
        """),
        {
            "set_id": aset_uuid,
            sql_period: True,  # dummy for f-string interpolation
        }
    ).all()

    series = []
    for row in rows:
        period_str = row.period.isoformat() if row.period else None
        series.append({
            "period": period_str,
            "count": int(row.count or 0),
            "area_sqm": round(float(row.total_area_sqm or 0), 4),
            "avg_confidence": round(float(row.avg_confidence or 0), 4) if row.avg_confidence else None,
        })

    return {
        "timeseries_data": {
            "annotation_set_id": aset_id,
            "period": period,
            "series": series,
        }
    }


@node(
    type="aggregate_model_runs",
    category="analysis",
    label="Aggregate Model Runs",
    description="Accumulate outputs from multiple model runs into a single structured JSON summary.",
    inputs=[HandleDef(handle="predictions", type="raw_predictions", label="Predictions", multiple=True)],
    outputs=[HandleDef(handle="summary", type="quality_metrics", label="Run Summary")],
    config_schema={},
    icon="table",
    color="#0EA5E9",
)
def execute_aggregate_model_runs(session, config, input_data, **kwargs):
    from app.models.ai_model import AIModel
    from app.models.job import Job

    predictions = input_data.get("predictions", [])
    if isinstance(predictions, dict):
        predictions = [predictions]
    predictions = [p for p in predictions if p]

    model_runs = []
    all_annotation_set_ids = []
    total_processed = 0
    total_failed = 0

    for pred in predictions:
        job_id = pred.get("job_id")
        job = session.get(Job, uuid.UUID(job_id)) if job_id else None
        model = session.get(AIModel, job.model_id) if job and job.model_id else None
        annotation_set_ids = pred.get("annotation_set_ids") or []
        all_annotation_set_ids.extend(annotation_set_ids)
        processed_items = int(pred.get("processed_items") or 0)
        failed_items = int(pred.get("failed_items") or 0)
        total_processed += processed_items
        total_failed += failed_items

        model_runs.append({
            "job_id": job_id,
            "model_id": pred.get("model_id") or (str(job.model_id) if job and job.model_id else None),
            "model_name": pred.get("model_name") or (model.name if model else None),
            "annotation_set_ids": annotation_set_ids,
            "processed_items": processed_items,
            "failed_items": failed_items,
        })

    return {
        "summary": {
            "model_run_count": len(model_runs),
            "model_runs": model_runs,
            "annotation_set_ids": all_annotation_set_ids,
            "total_annotation_set_count": len(all_annotation_set_ids),
            "total_processed_items": total_processed,
            "total_failed_items": total_failed,
        }
    }


# ============================================================================
# 3. ZONAL STATISTICS
# ============================================================================

@node(
    type="zonal_statistics",
    category="analysis",
    label="Zonal Statistics",
    description="Compute count, area, density, and mean confidence per zone (or whole set if no zones).",
    inputs=[
        HandleDef(handle="annotation_set", type="annotation_set", label="Annotations", required=True),
        HandleDef(handle="zones", type="annotation_set", label="Zones", required=False),
    ],
    outputs=[HandleDef(handle="stats", type="quality_metrics", label="Zonal Stats")],
    config_schema={
        "type": "object",
        "properties": {
            "statistics": {
                "type": "array",
                "title": "Statistics to Compute",
                "items": {"type": "string", "enum": ["count", "area", "density", "mean_confidence"]},
                "default": ["count", "area"],
            },
        },
    },
    icon="grid-3x3",
    color="#0EA5E9",
)
def execute_zonal_statistics(session, config, input_data, **kwargs):
    from app.models.annotation import Annotation

    aset = input_data.get("annotation_set", {})
    zones = input_data.get("zones", {})
    aset_id = aset.get("id")
    zone_set_id = zones.get("id") if zones else None

    if not aset_id:
        return {"stats": {"zones": [], "annotation_set_id": None}}

    try:
        aset_uuid = uuid.UUID(aset_id)
        zone_uuid = uuid.UUID(zone_set_id) if zone_set_id else None
    except (ValueError, TypeError):
        return {"stats": {"zones": [], "annotation_set_id": None}}

    # If zones provided: compute stats per zone
    if zone_uuid:
        rows = session.execute(
            text("""
                SELECT z.id AS zone_id,
                       COUNT(a.id) AS count,
                       SUM(ST_Area(a.geometry::geography)) AS total_area_sqm,
                       AVG(a.confidence) AS avg_confidence,
                       COUNT(a.id) / NULLIF(ST_Area(z.geometry::geography), 0) AS density_per_sqm
                FROM annotations z
                LEFT JOIN annotations a ON ST_Intersects(a.geometry, z.geometry)
                  AND a.annotation_set_id = :ann_set_id
                  AND a.deleted_at IS NULL
                WHERE z.annotation_set_id = :zone_set_id
                  AND z.deleted_at IS NULL
                GROUP BY z.id, z.geometry
                ORDER BY z.id
            """),
            {"ann_set_id": aset_uuid, "zone_set_id": zone_uuid}
        ).all()

        zones_data = []
        for row in rows:
            zones_data.append({
                "zone_id": str(row.zone_id),
                "count": int(row.count or 0),
                "area_sqm": round(float(row.total_area_sqm or 0), 4),
                "density_per_sqm": round(float(row.density_per_sqm or 0), 8) if row.density_per_sqm else 0,
                "avg_confidence": round(float(row.avg_confidence or 0), 4) if row.avg_confidence else None,
            })
        return {"stats": {"annotation_set_id": aset_id, "zones": zones_data}}

    # No zones: compute stats for whole set
    else:
        row = session.execute(
            select(
                func.count(Annotation.id).label("count"),
                func.sum(cast(Annotation.geometry, Geography).ST_Area()).label("total_area_sqm"),
                func.avg(Annotation.confidence).label("avg_confidence"),
            ).where(
                Annotation.annotation_set_id == aset_uuid,
                Annotation.deleted_at.is_(None),
            )
        ).one_or_none()

        if row:
            return {
                "stats": {
                    "annotation_set_id": aset_id,
                    "count": int(row.count or 0),
                    "area_sqm": round(float(row.total_area_sqm or 0), 4),
                    "avg_confidence": round(float(row.avg_confidence or 0), 4) if row.avg_confidence else None,
                    "zones": [],
                }
            }
        return {"stats": {"annotation_set_id": aset_id, "count": 0, "area_sqm": 0, "zones": []}}


# ============================================================================
# 4. ANOMALY DETECTION
# ============================================================================

@node(
    type="anomaly_detection",
    category="analysis",
    label="Anomaly Detection",
    description="Identify outlier annotations by area using z-score or IQR method.",
    inputs=[HandleDef(handle="areas", type="quality_metrics", label="Area Metrics", required=True)],
    outputs=[HandleDef(handle="anomalies", type="quality_metrics", label="Anomalies")],
    config_schema={
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "title": "Detection Method",
                "enum": ["zscore", "iqr"],
                "default": "zscore",
            },
            "threshold": {
                "type": "number",
                "title": "Threshold",
                "default": 2.0,
                "minimum": 0.1,
            },
        },
    },
    icon="alert-triangle",
    color="#0EA5E9",
)
def execute_anomaly_detection(session, config, input_data, **kwargs):
    areas_data = input_data.get("areas", {})
    metrics = areas_data.get("metrics", [])

    if len(metrics) < 3:
        return {
            "anomalies": {
                "method": config.get("method", "zscore"),
                "threshold": config.get("threshold", 2.0),
                "anomaly_ids": [],
                "count": 0,
            }
        }

    method = config.get("method", "zscore")
    threshold = config.get("threshold", 2.0)
    values = [m["area_sqm"] for m in metrics]

    anomaly_ids = []

    if method == "zscore":
        mean = statistics.mean(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0
        if stdev > 0:
            anomaly_ids = [
                m["annotation_id"]
                for m in metrics
                if abs((m["area_sqm"] - mean) / stdev) > threshold
            ]

    elif method == "iqr":
        sorted_vals = sorted(values)
        q1_idx = len(sorted_vals) // 4
        q3_idx = (3 * len(sorted_vals)) // 4
        q1 = sorted_vals[q1_idx] if q1_idx < len(sorted_vals) else sorted_vals[0]
        q3 = sorted_vals[q3_idx] if q3_idx < len(sorted_vals) else sorted_vals[-1]
        iqr = q3 - q1
        lower = q1 - threshold * iqr
        upper = q3 + threshold * iqr
        anomaly_ids = [
            m["annotation_id"]
            for m in metrics
            if not (lower <= m["area_sqm"] <= upper)
        ]

    return {
        "anomalies": {
            "method": method,
            "threshold": threshold,
            "anomaly_ids": anomaly_ids,
            "count": len(anomaly_ids),
        }
    }


# ============================================================================
# 5. CHANGE DETECTION
# ============================================================================

@node(
    type="change_detection",
    category="analysis",
    label="Change Detection",
    description="Detect changed areas between before and after annotation sets using geometry or raster methods.",
    inputs=[
        HandleDef(handle="before_items", type="annotation_set", label="Before Annotation Set", required=True),
        HandleDef(handle="after_items", type="annotation_set", label="After Annotation Set", required=True),
    ],
    outputs=[
        HandleDef(handle="changed_areas", type="annotation_set", label="Changed Areas"),
        HandleDef(handle="change_metrics", type="quality_metrics", label="Change Metrics"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "detection_method": {
                "type": "string",
                "title": "Detection Method",
                "enum": ["geometry_difference", "raster_threshold"],
                "default": "geometry_difference",
            },
            "threshold": {
                "type": "number",
                "title": "Overlap Threshold (0-1)",
                "default": 0.1,
                "minimum": 0,
                "maximum": 1,
            },
            "min_change_area_sqm": {
                "type": "number",
                "title": "Minimum Change Area (sq m)",
                "default": 10,
                "minimum": 0,
            },
        },
    },
    icon="git-compare",
    color="#0EA5E9",
)
def execute_change_detection(session, config, input_data, **kwargs):
    from app.models.annotation import Annotation
    from app.models.annotation_set import AnnotationSet

    before_set = input_data.get("before_items", {})
    after_set = input_data.get("after_items", {})
    before_id = before_set.get("id")
    after_id = after_set.get("id")

    if not before_id or not after_id:
        return {
            "changed_areas": {"id": None},
            "change_metrics": {"method": "geometry_difference", "total_changes": 0},
        }

    try:
        before_uuid = uuid.UUID(before_id)
        after_uuid = uuid.UUID(after_id)
    except (ValueError, TypeError):
        return {
            "changed_areas": {"id": None},
            "change_metrics": {"method": "geometry_difference", "total_changes": 0},
        }

    method = config.get("detection_method", "geometry_difference")
    threshold = config.get("threshold", 0.1)
    min_area = config.get("min_change_area_sqm", 10)

    if method == "raster_threshold":
        # Delegate to analysis worker via DeferToJob
        from app.models.job import Job

        job = Job(
            organization_id=uuid.UUID(kwargs["organization_id"]),
            type="change_detection",
            status="pending",
            config={
                "before_annotation_set_id": before_id,
                "after_annotation_set_id": after_id,
                "threshold": threshold,
                "min_change_area_sqm": min_area,
                "automation_run_id": kwargs.get("run_id"),
                "automation_step_id": kwargs.get("step_id"),
            },
        )
        session.add(job)
        session.flush()

        from app.workers.analysis.tasks import run_change_detection_job
        run_change_detection_job.delay(str(job.id))

        return DeferToJob(job_id=str(job.id))

    # geometry_difference method: inline PostGIS
    else:
        # Create output annotation set for changed areas
        output_set = AnnotationSet(
            name=f"Change Detection: {before_set.get('name', 'Before')} → {after_set.get('name', 'After')}",
        )
        session.add(output_set)
        session.flush()

        # Query annotations in after set that changed significantly
        # (have no matching overlap in before set, or low overlap)
        rows = session.execute(
            text("""
                SELECT a.id, a.geometry, a.class_id, a.confidence
                FROM annotations a
                WHERE a.annotation_set_id = :after_set_id
                  AND a.deleted_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM annotations b
                    WHERE b.annotation_set_id = :before_set_id
                      AND b.deleted_at IS NULL
                      AND ST_Intersects(a.geometry, b.geometry)
                      AND COALESCE(
                        ST_Area(ST_Intersection(a.geometry, b.geometry)::geography) /
                        NULLIF(ST_Area(a.geometry::geography), 0),
                        0
                      ) > :threshold
                  )
            """),
            {
                "after_set_id": after_uuid,
                "before_set_id": before_uuid,
                "threshold": threshold,
            }
        ).all()

        # Insert changed annotations into output set
        from geoalchemy2 import WKTElement

        change_count = 0
        for row in rows:
            ann = Annotation(
                annotation_set_id=output_set.id,
                class_id=row.class_id,
                geometry=row.geometry,
                confidence=row.confidence,
                created_by_job_id=None,
            )
            session.add(ann)
            change_count += 1

        if change_count > 0:
            session.flush()

        return {
            "changed_areas": {
                "id": str(output_set.id),
                "name": output_set.name,
                "count": change_count,
            },
            "change_metrics": {
                "method": method,
                "threshold": threshold,
                "min_change_area_sqm": min_area,
                "total_changes": change_count,
            },
        }


# ============================================================================
# 6. OBJECT STATE TRACKING
# ============================================================================

@node(
    type="object_state_tracking",
    category="analysis",
    label="Object State Tracking",
    description="Match annotations to tracked objects for temporal monitoring.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set", label="Annotation Set", required=True)],
    outputs=[HandleDef(handle="tracked_objects", type="quality_metrics", label="Tracked Objects")],
    config_schema={
        "type": "object",
        "properties": {
            "object_type": {
                "type": "string",
                "title": "Object Type",
                "enum": ["deforestation_front", "fire_perimeter", "building", "water_body", "custom"],
                "default": "deforestation_front",
            },
            "max_distance_m": {
                "type": "number",
                "title": "Max Distance (m)",
                "default": 100,
                "minimum": 0,
            },
            "max_gap_days": {
                "type": "number",
                "title": "Max Gap (days)",
                "default": 30,
                "minimum": 1,
            },
        },
    },
    icon="target",
    color="#0EA5E9",
)
def execute_object_state_tracking(session, config, input_data, **kwargs):
    from app.models.annotation import Annotation

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")

    if not aset_id:
        return {"tracked_objects": {"annotation_set_id": None, "enqueued": 0}}

    try:
        aset_uuid = uuid.UUID(aset_id)
    except (ValueError, TypeError):
        return {"tracked_objects": {"annotation_set_id": None, "enqueued": 0}}

    # Fire-and-forget: enqueue auto_match for each annotation in the set
    # This follows the existing pattern in discovery/tasks.py
    annotations = session.execute(
        select(Annotation.id).where(
            Annotation.annotation_set_id == aset_uuid,
            Annotation.deleted_at.is_(None),
        )
    ).scalars().all()

    org_id = kwargs.get("organization_id")
    enqueued = 0

    if annotations and org_id:
        try:
            from app.workers.discovery.tasks import auto_match_tracked_objects

            for ann_id in annotations:
                # Enqueue without waiting; errors are handled by Celery
                auto_match_tracked_objects.delay(None, str(ann_id), org_id)
                enqueued += 1
        except Exception:
            pass  # If import fails, silently skip

    return {
        "tracked_objects": {
            "annotation_set_id": aset_id,
            "enqueued": enqueued,
            "object_type": config.get("object_type", "deforestation_front"),
        }
    }
