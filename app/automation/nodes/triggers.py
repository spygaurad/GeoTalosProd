import uuid
from datetime import datetime, UTC

from app.automation.registry import node, HandleDef


@node(
    type="manual_trigger",
    category="triggers",
    label="Manual Trigger",
    description="User clicks Run from the UI. No configuration needed.",
    outputs=[HandleDef(handle="trigger", type="trigger_data", required=False, label="Trigger Data")],
    config_schema={},
    icon="play",
)
def execute_manual_trigger(session, config, input_data, **kwargs):
    return {"trigger": kwargs.get("trigger_data", {})}


@node(
    type="schedule_trigger",
    category="triggers",
    label="On Schedule (Cron)",
    description="Runs on a time schedule. Configure cron expression and timezone.",
    outputs=[HandleDef(handle="trigger", type="trigger_data", required=False, label="Trigger Data")],
    config_schema={
        "type": "object",
        "properties": {
            "cron_expression": {
                "type": "string",
                "title": "Cron Expression",
                "description": "e.g., '0 9 * * 1' for Mondays at 9am UTC",
                "default": "0 0 * * *",
            },
            "timezone": {"type": "string", "title": "Timezone", "default": "UTC"},
        },
        "required": ["cron_expression"],
    },
    icon="clock",
)
def execute_schedule_trigger(session, config, input_data, **kwargs):
    return {"trigger": {"scheduled_at": datetime.now(UTC).isoformat()}}


@node(
    type="dataset_ingested_trigger",
    category="triggers",
    label="On Dataset Ingested",
    description="Fires when a dataset upload and ingestion job completes.",
    outputs=[
        HandleDef(handle="dataset", type="dataset", label="Dataset"),
        HandleDef(handle="items", type="dataset_items", label="Dataset Items"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "dataset_id": {
                "type": "string",
                "format": "uuid",
                "title": "Dataset",
                "description": "Optional: only trigger for this specific dataset. Leave empty for any.",
                "x-picker": "dataset",
            },
        },
    },
    icon="database",
)
def execute_dataset_ingested_trigger(session, config, input_data, **kwargs):
    from app.models.dataset import Dataset
    from app.models.dataset_item import DatasetItem
    from sqlalchemy import select

    trigger_data = kwargs.get("trigger_data", {}) or {}
    dataset_id = config.get("dataset_id") or trigger_data.get("dataset_id")
    if not dataset_id:
        raise ValueError("No dataset_id provided in trigger data or config")

    dataset = session.get(Dataset, uuid.UUID(dataset_id))
    if not dataset:
        raise ValueError(f"Dataset {dataset_id} not found")

    items = session.execute(
        select(DatasetItem.id, DatasetItem.stac_item_id)
        .where(DatasetItem.dataset_id == dataset.id)
    ).all()

    return {
        "dataset": {"id": str(dataset.id), "name": dataset.name},
        "items": [{"id": str(i.id), "stac_item_id": i.stac_item_id} for i in items],
    }


@node(
    type="annotation_created_trigger",
    category="triggers",
    label="On Annotation Created",
    description="Fires when annotations are added to any set.",
    outputs=[HandleDef(handle="annotation_set", type="annotation_set", label="Annotation Set")],
    config_schema={
        "type": "object",
        "properties": {
            "schema_id": {
                "type": "string",
                "format": "uuid",
                "title": "Schema Filter",
                "description": "Optional: only trigger for annotation sets using this schema.",
                "x-picker": "annotation_schema",
            },
        },
    },
    icon="tag",
)
def execute_annotation_created_trigger(session, config, input_data, **kwargs):
    trigger_data = kwargs.get("trigger_data", {}) or {}
    return {"annotation_set": trigger_data.get("annotation_set", {})}


@node(
    type="threshold_breach_trigger",
    category="triggers",
    label="On Threshold Breach",
    description="Fires when a tracked object metric crosses a configured limit.",
    outputs=[
        HandleDef(handle="trigger", type="trigger_data", label="Trigger Data"),
        HandleDef(handle="tracked_object", type="tracked_objects", label="Tracked Object"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "metric": {"type": "string", "title": "Metric", "enum": ["area_change_pct", "observation_count", "confidence_score"]},
            "operator": {"type": "string", "title": "Operator", "enum": ["gt", "gte", "lt", "lte", "eq"]},
            "value": {"type": "number", "title": "Threshold Value"},
        },
        "required": ["metric", "operator", "value"],
    },
    icon="alert-triangle",
)
def execute_threshold_breach_trigger(session, config, input_data, **kwargs):
    trigger_data = kwargs.get("trigger_data", {}) or {}
    return {
        "trigger": trigger_data,
        "tracked_object": trigger_data.get("tracked_object", {}),
    }
