"""Backbone registry.

Keying by a stable string id (not a DB row) because this registry describes
what the *service* can load, independent of which orgs/ai_models rows
reference it. Loading itself lives in app.dinov2_backbone.load_dinov2_raw,
cached process-wide so every class-head trained/served against the same
backbone_key shares one resident copy.
"""

BACKBONES: dict[str, dict] = {
    "dinov2-vits14": {
        "source": "huggingface",
        "model_id": "facebook/dinov2-small",
        "hidden_dim": 384,
        "patch_size": 14,
        "status": "loaded",
    },
    "dinov2-vitb14": {
        "source": "huggingface",
        "model_id": "facebook/dinov2-base",
        "hidden_dim": 768,
        "patch_size": 14,
        "status": "loaded",
    },
}
