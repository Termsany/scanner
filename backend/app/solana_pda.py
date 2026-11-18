"""Minimal, dependency-free Solana helpers: base58 and associated-token-account (ATA) derivation.

ATA = find_program_address([owner, token_program, mint], ASSOCIATED_TOKEN_PROGRAM). A program-derived
address must NOT be a valid ed25519 point, so the bump search needs an on-curve test, done here with
the standard point-decompression check (no native crypto library required).
"""

from __future__ import annotations

import hashlib

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def pubkey_bytes(s: str) -> bytes:
    b = b58decode(s)
    if len(b) != 32:
        raise ValueError(f"not a 32-byte public key: {s}")
    return b


def is_on_curve(b: bytes) -> bool:
    """True if the 32 bytes decode to a point on ed25519 (RFC 8032 decoding, sign bit ignored)."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    prog = pubkey_bytes(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not is_on_curve(h):
            return b58encode(h), bump
    raise ValueError("no viable bump seed")


def associated_token_address(owner: str, mint: str, token_program: str = TOKEN_PROGRAM) -> str:
    return find_program_address(
        [pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)], ASSOCIATED_TOKEN_PROGRAM
    )[0]
