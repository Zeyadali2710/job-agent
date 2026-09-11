from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"[a-z0-9]{3,}")


def _shingles(text: str, k: int = 3) -> list[str]:
    words = _TOKEN.findall(text.lower())
    if len(words) < k:
        return words
    return [" ".join(words[i:i + k]) for i in range(len(words) - k + 1)]


def simhash(text: str | None, bits: int = 64) -> int | None:
    if not text:
        return None
    shingles = _shingles(text)
    if not shingles:
        return None
    v = [0] * bits
    for sh in shingles:
        h = int.from_bytes(hashlib.blake2b(sh.encode(), digest_size=8).digest(), "big")
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= 1 << i
    return out - (1 << 63) if out >= (1 << 63) else out   # fit signed BIGINT


def hamming(a: int | None, b: int | None) -> int:
    if a is None or b is None:
        return 64
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")
