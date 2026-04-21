from __future__ import annotations

from typing import Any


def convert(raw: Any, _config: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or "predictions" not in raw:
        raise ValueError("platform adapter expects {'predictions': [...]}")
    return raw
