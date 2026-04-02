import httpx

from app.automation.registry import node, HandleDef


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
