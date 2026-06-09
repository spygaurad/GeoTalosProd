from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class OutputAdapter:
    name: str
    label: str
    description: str
    supported_formats: list[str]
    config_schema: dict[str, Any]
    convert_fn: Callable[[Any, dict[str, Any], dict[str, Any]], dict[str, Any]]
    request_enricher: (
        Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]] | None
    ) = None
    # Resolves the model's static prompt spec into a per-patch prompt_payload.
    # Args: (prompt_spec, patch_context, config). ``patch_context`` carries the
    # patch's geo ``bbox`` + pixel ``width``/``height`` so spatial prompts (e.g.
    # SAM3 bbox exemplars drawn on the map in 4326) can be clipped + reprojected
    # into that patch's pixel space. Returning ``None`` tells ModelManager to
    # SKIP the patch entirely (the spatial prompt doesn't overlap it). When the
    # adapter has no resolver, ModelManager sends the static prompt to every patch.
    prompt_resolver: (
        Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None
    ) = None
