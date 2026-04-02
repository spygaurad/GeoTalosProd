from app.automation.registry import node, HandleDef


@node(
    type="multi_sensor_fusion",
    category="advanced",
    label="Multi-Sensor Fusion",
    description="Fuse data from multiple sensors (optical, SAR, lidar) for enhanced analysis.",
    inputs=[HandleDef(handle="datasets", type="dataset_items", multiple=True)],
    outputs=[HandleDef(handle="fused", type="dataset_items")],
    config_schema={
        "type": "object",
        "properties": {
            "fusion_method": {"type": "string", "enum": ["concatenate", "weighted_average", "pca"], "default": "concatenate"},
        },
    },
    icon="radio",
    color="#6366F1",
    status="placeholder",
)
def execute_multi_sensor_fusion(session, config, input_data, **kwargs):
    return {"fused": []}


@node(
    type="cloud_masking",
    category="advanced",
    label="Cloud Masking",
    description="Detect and mask cloud-covered pixels in optical imagery.",
    inputs=[HandleDef(handle="items", type="dataset_items")],
    outputs=[HandleDef(handle="items", type="dataset_items", label="Cloud-Free Items")],
    config_schema={
        "type": "object",
        "properties": {
            "max_cloud_pct": {"type": "number", "title": "Max Cloud %", "default": 20, "minimum": 0, "maximum": 100},
            "method": {"type": "string", "enum": ["scl", "fmask", "s2cloudless"], "default": "scl"},
        },
    },
    icon="cloud-off",
    color="#6366F1",
    status="placeholder",
)
def execute_cloud_masking(session, config, input_data, **kwargs):
    return {"items": input_data.get("items", [])}


@node(
    type="temporal_compositing",
    category="advanced",
    label="Temporal Compositing",
    description="Create cloud-free composites from multi-temporal imagery (median, max NDVI, etc.).",
    inputs=[HandleDef(handle="items", type="dataset_items")],
    outputs=[HandleDef(handle="composite", type="dataset_items")],
    config_schema={
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["median", "max_ndvi", "min_cloud", "mean"], "default": "median"},
            "time_window_days": {"type": "integer", "default": 30},
        },
    },
    icon="layers",
    color="#6366F1",
    status="placeholder",
)
def execute_temporal_compositing(session, config, input_data, **kwargs):
    return {"composite": []}


@node(
    type="compliance_check",
    category="advanced",
    label="Compliance Check",
    description="Verify annotations meet regulatory or organizational compliance rules.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[
        HandleDef(handle="compliant", type="annotation_set"),
        HandleDef(handle="violations", type="quality_metrics"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "rules": {"type": "array", "items": {"type": "string"}, "title": "Rule Names"},
        },
    },
    icon="shield",
    color="#6366F1",
    status="placeholder",
)
def execute_compliance_check(session, config, input_data, **kwargs):
    return {"compliant": {}, "violations": {}}


@node(
    type="self_improving_alert",
    category="advanced",
    label="Self-Improving Alert",
    description="Adaptive alerting that adjusts thresholds based on false-positive feedback.",
    inputs=[HandleDef(handle="tracked_object", type="tracked_objects")],
    outputs=[HandleDef(handle="alert", type="trigger_data")],
    config_schema={
        "type": "object",
        "properties": {
            "initial_threshold": {"type": "number", "default": 0.7},
            "learning_rate": {"type": "number", "default": 0.1},
        },
    },
    icon="zap",
    color="#6366F1",
    status="placeholder",
)
def execute_self_improving_alert(session, config, input_data, **kwargs):
    return {"alert": {}}


@node(
    type="domain_adaptation",
    category="advanced",
    label="Domain Adaptation",
    description="Fine-tune a pre-trained model on new geography/sensor data.",
    inputs=[
        HandleDef(handle="items", type="dataset_items"),
        HandleDef(handle="model", type="model"),
        HandleDef(handle="annotations", type="annotation_set"),
    ],
    outputs=[HandleDef(handle="model", type="model", label="Adapted Model")],
    config_schema={
        "type": "object",
        "properties": {
            "epochs": {"type": "integer", "default": 10},
            "learning_rate": {"type": "number", "default": 0.001},
        },
    },
    icon="refresh-cw",
    color="#6366F1",
    status="placeholder",
)
def execute_domain_adaptation(session, config, input_data, **kwargs):
    return {"model": input_data.get("model", {})}


@node(
    type="hypothesis_test",
    category="advanced",
    label="Hypothesis Test",
    description="Statistical test: is the observed change significant vs. baseline?",
    inputs=[HandleDef(handle="data", type="quality_metrics")],
    outputs=[HandleDef(handle="result", type="quality_metrics")],
    config_schema={
        "type": "object",
        "properties": {
            "test": {"type": "string", "enum": ["t_test", "chi_squared", "mann_whitney"], "default": "t_test"},
            "alpha": {"type": "number", "default": 0.05},
        },
    },
    icon="help-circle",
    color="#6366F1",
    status="placeholder",
)
def execute_hypothesis_test(session, config, input_data, **kwargs):
    return {"result": {}}
