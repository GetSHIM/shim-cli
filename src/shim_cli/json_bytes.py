"""Serialize JSON to UTF-8, escaping surrogate code points only.

Ordinary Unicode retains its existing encoding. Surrogates become JSON
Unicode escapes instead of causing UTF-8 encoding to fail.
"""

from __future__ import annotations

import json
from typing import Any


def dumps(value: object, **options: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, **options).encode(
        "utf-8", errors="backslashreplace"
    )
