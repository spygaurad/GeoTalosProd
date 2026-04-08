from __future__ import annotations

from app.automation.adapters.base import OutputAdapter
from app.automation.adapters import (
    coco_adapter,
    geojson_adapter,
    platform_adapter,
    sam3_adapter,
    yolo_adapter,
)


ADAPTER_REGISTRY: dict[str, OutputAdapter] = {
    "platform_passthrough": OutputAdapter(
        name="platform_passthrough",
        label="Platform Passthrough",
        description="Already in platform standard format.",
        supported_formats=["platform"],
        config_schema={"type": "object", "properties": {}},
        convert_fn=platform_adapter.convert,
    ),
    "geojson_to_platform": OutputAdapter(
        name="geojson_to_platform",
        label="GeoJSON",
        description="Converts GeoJSON FeatureCollection to platform standard.",
        supported_formats=["geojson"],
        config_schema={
            "type": "object",
            "properties": {
                "label_field": {"type": "string", "default": "label"},
                "score_field": {"type": "string", "default": "confidence"},
            },
        },
        convert_fn=geojson_adapter.convert,
    ),
    "yolo_to_platform": OutputAdapter(
        name="yolo_to_platform",
        label="YOLO",
        description="Converts YOLO [class,cx,cy,w,h,conf] outputs to platform standard.",
        supported_formats=["yolo"],
        config_schema={
            "type": "object",
            "properties": {
                "category_map": {"type": "object"},
                "min_score": {"type": "number", "default": 0.0},
            },
        },
        convert_fn=yolo_adapter.convert,
    ),
    "coco_to_platform": OutputAdapter(
        name="coco_to_platform",
        label="COCO",
        description="Converts COCO detection outputs to platform standard.",
        supported_formats=["coco"],
        config_schema={
            "type": "object",
            "properties": {
                "category_map": {"type": "object"},
                "min_score": {"type": "number", "default": 0.0},
            },
        },
        convert_fn=coco_adapter.convert,
    ),
    "sam3_to_platform": OutputAdapter(
        name="sam3_to_platform",
        label="SAM3",
        description="Converts SAM3 outputs (masks/polygons/bboxes) to platform standard.",
        supported_formats=["sam3"],
        config_schema={
            "type": "object",
            "properties": {
                "label_field": {"type": "string", "default": "label"},
                "score_field": {"type": "string", "default": "score"},
                "polygon_field": {"type": "string", "default": "polygon"},
                "bbox_field": {"type": "string", "default": "bbox"},
                "default_label": {"type": "string", "default": "object"},
                "min_score": {"type": "number", "default": 0.0},
            },
        },
        convert_fn=sam3_adapter.convert,
    ),
}


def get_adapter(name: str) -> OutputAdapter:
    adapter = ADAPTER_REGISTRY.get(name)
    if adapter is None:
        raise KeyError(f"Unknown adapter: {name}")
    return adapter
