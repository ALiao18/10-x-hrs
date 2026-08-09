"""ULID generation, stdlib only.

26-character Crockford base32 encoding of 128 bits: a 48-bit millisecond
timestamp followed by 80 random bits. Lexicographic string order matches
creation order, which is what makes ULIDs a stable tie-breaker during replay.
"""

import os
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LENGTH = 26


def new_ulid(now_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Return a new ULID. Arguments exist so tests can pin the output."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if randomness is None:
        randomness = os.urandom(10)
    value = (now_ms << 80) | int.from_bytes(randomness, "big")
    out = []
    for _ in range(LENGTH):
        out.append(ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def is_ulid(text: str) -> bool:
    return len(text) == LENGTH and all(c in ALPHABET for c in text.upper())
