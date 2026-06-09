"""
Config validators for specific node types.
Called during graph validation to check node-specific config requirements.
"""
from uuid import UUID


def validate_config(node_type: str, config: dict) -> list[str]:
    """
    Validate node-specific config beyond JSON Schema basics.
    Returns list of error messages (empty = valid).
    """
    validators = {
        "run_inference": _validate_run_inference,
        "iou_threshold_gate": _validate_iou_gate,
        "trigger": _validate_trigger,
    }
    validator = validators.get(node_type)
    if validator:
        return validator(config)
    return []


def _validate_run_inference(config: dict) -> list[str]:
    errors = []
    threshold = config.get("confidence_threshold", 0.5)
    if not (0 <= threshold <= 1):
        errors.append("confidence_threshold must be between 0 and 1")
    return errors


def _validate_iou_gate(config: dict) -> list[str]:
    errors = []
    accept = config.get("accept_threshold", 0.85)
    reject = config.get("reject_threshold", 0.5)
    if reject >= accept:
        errors.append("reject_threshold must be less than accept_threshold")
    return errors


def _validate_trigger(config: dict) -> list[str]:
    errors = []
    mode = config.get("mode", "manual")
    if mode == "recurring":
        cron = config.get("cron_expression")
        if not cron:
            errors.append("cron_expression is required for a recurring trigger")
        elif len(cron.split()) != 5:
            errors.append("cron_expression must have exactly 5 fields (minute hour day month weekday)")
    elif mode == "once" and not config.get("run_at"):
        errors.append("run_at is required for a one-off trigger")
    return errors

