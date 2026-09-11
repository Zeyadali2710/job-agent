from __future__ import annotations

import re

SUFFIXES = r"\b(pty|ltd|limited|inc|incorporated|group|holdings|australia|au|corp|plc)\b"


def normalise_company(name: str) -> str:
    n = name.lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(SUFFIXES, " ", n)
    return re.sub(r"\s+", " ", n).strip()
