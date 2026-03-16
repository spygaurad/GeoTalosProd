"""Helpers for PostgreSQL TSTZRANGE ↔ Python dict conversion."""
from __future__ import annotations

from typing import Any


def parse_tstzrange(value: dict | None) -> str | None:
    """Convert ``{"lower": ..., "upper": ..., "bounds": "[)"}`` to a PostgreSQL TSTZRANGE literal.

    asyncpg accepts the standard PostgreSQL range literal format, e.g. ``[2021-01-01T00:00:00+00:00,2022-01-01T00:00:00+00:00)``.
    """
    if value is None:
        return None
    lower = value.get("lower") or ""
    upper = value.get("upper") or ""
    bounds = value.get("bounds") or "[)"
    lb = bounds[0] if bounds else "["
    ub = bounds[1] if len(bounds) > 1 else ")"
    return f"{lb}{lower},{upper}{ub}"


def tstzrange_to_dict(value: Any) -> dict | None:
    """Convert an asyncpg ``Range`` object (or ``None``) to a JSON-serializable dict.

    Returns ``None`` when *value* is ``None``.
    Returns *value* unchanged when it is already a ``dict``.
    Otherwise assumes an asyncpg-style Range with ``.lower``, ``.upper``,
    ``.lower_inc``, ``.upper_inc`` attributes.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    lower = getattr(value, "lower", None)
    upper = getattr(value, "upper", None)
    lower_inc: bool = getattr(value, "lower_inc", True)
    upper_inc: bool = getattr(value, "upper_inc", False)
    lb = "[" if lower_inc else "("
    ub = "]" if upper_inc else ")"
    return {
        "lower": lower.isoformat() if lower is not None else None,
        "upper": upper.isoformat() if upper is not None else None,
        "bounds": f"{lb}{ub}",
    }
