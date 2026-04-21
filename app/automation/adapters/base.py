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
