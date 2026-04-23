"""engine/backend_types.py - common backend return types.

Neutral module containing SendResult. Other modules can safely import from
here without risk of circular imports.
"""

from __future__ import annotations

from enum import Enum


class SendResult(Enum):
    OK = "ok"
    BUSY = "busy"
    FAIL = "fail"
