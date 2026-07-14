"""Safe, atomic JSON artifact helpers."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (Path, UUID, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return json_safe(value.value)
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        return json_safe(value.item())
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def portable_path(path: Path, root: Path) -> str:
    absolute = path.resolve() if not path.is_absolute() else path
    try:
        return str(absolute.relative_to(root.resolve()))
    except ValueError:
        return str(absolute)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(json_safe(payload), handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
