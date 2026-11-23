"""Decode text returned by legacy switch consoles using common firmware encodings."""
from __future__ import annotations

import re


def decode_switch_text(raw: bytes) -> str:
    """Prefer UTF-8, then Simplified Chinese Windows encoding used by older VRP builds."""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def command_rejected(text: str) -> bool:
    """Recognize common VRP CLI rejection lines without treating ordinary descriptions as errors."""
    return bool(re.search(r"(?mi)^\s*(?:%\s*)?(?:Unrecognized command|Incomplete command|Too many parameters|Wrong parameter|Error:)", text))
