"""ECDH on the NIST B-163 binary curve, byte-compatible with Gadgetbridge's ``ECDH_B163`` (tiny-ECDH-c).

Keys: private 24 bytes, public 48 bytes (x then y, each 24 bytes little-endian), shared secret
48 bytes (the x and y of the shared point). Field elements are Python ints; coordinates use the
affine formulas of tiny-ECDH-c so the results match the Java port bit for bit.
"""

from __future__ import annotations

import os

DEGREE = 163
NBYTES = 24
PRIVATE_SIZE = NBYTES
PUBLIC_SIZE = 2 * NBYTES


def _words(ws: tuple[int, ...]) -> int:
    return sum((w & 0xFFFFFFFF) << (32 * i) for i, w in enumerate(ws))


POLY = _words((0x000000C9, 0, 0, 0, 0, 0x00000008))  # x^163 + x^7 + x^6 + x^3 + 1
COEFF_B = _words((0x4A3205FD, 0x512F7874, 0x1481EB10, 0xB8C953CA, 0x0A601907, 0x00000002))
BASE_X = _words((0xE8343E36, 0xD4994637, 0xA0991168, 0x86A2D57E, 0xF0EBA162, 0x00000003))
BASE_Y = _words((0x797324F1, 0xB11C5C0C, 0xA2CDD545, 0x71A0094F, 0xD51FBC6C, 0x00000000))
ORDER = _words((0xA4234C33, 0x77E70C12, 0x000292FE, 0, 0, 0x00000004))


def field_mul(x: int, y: int) -> int:
    z = x if y & 1 else 0
    t = x
    for i in range(1, DEGREE):
        t <<= 1
        if (t >> DEGREE) & 1:
            t ^= POLY
        if (y >> i) & 1:
            z ^= t
    return z


def field_inv(x: int) -> int:
    if x == 0:
        raise ZeroDivisionError("no inverse of 0 in GF(2^163)")
    u, v, g, z = x, POLY, 0, 1
    while u != 1:
        i = u.bit_length() - v.bit_length()
        if i < 0:
            u, v, g, z, i = v, u, z, g, -i
        u ^= v << i
        z ^= g << i
    return z


def point_double(x: int, y: int) -> tuple[int, int]:
    if x == 0:
        return 0, 0
    lam = field_mul(field_inv(x), y) ^ x
    x3 = field_mul(lam, lam) ^ lam ^ 1
    y3 = field_mul(x, x) ^ field_mul(lam ^ 1, x3)
    return x3, y3


def point_add(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int]:
    if x2 == 0 and y2 == 0:
        return x1, y1
    if x1 == 0 and y1 == 0:
        return x2, y2
    if x1 == x2:
        return point_double(x1, y1) if y1 == y2 else (0, 0)
    lam = field_mul(field_inv(x1 ^ x2), y1 ^ y2)
    x3 = field_mul(lam, lam) ^ lam ^ x1 ^ x2 ^ 1
    y3 = y1 ^ field_mul(x1 ^ x3, lam) ^ x3
    return x3, y3


def point_mul(x: int, y: int, k: int) -> tuple[int, int]:
    rx, ry = 0, 0
    for i in range(k.bit_length() - 1, -1, -1):
        rx, ry = point_double(rx, ry)
        if (k >> i) & 1:
            rx, ry = point_add(rx, ry, x, y)
    return rx, ry


def on_curve(x: int, y: int) -> bool:
    """y^2 + xy == x^3 + x^2 + b (the curve coefficient a is 1 for every NIST binary curve)."""
    if x == 0 and y == 0:
        return False
    x2 = field_mul(x, x)
    return x2 ^ field_mul(x2, x) ^ COEFF_B ^ field_mul(y, y) == field_mul(x, y)


def clip_private(private: bytes) -> int:
    """The scalar tiny-ECDH-c actually uses: bits at and above the order's degree cleared."""
    k = int.from_bytes(private, "little")
    return k & ((1 << (ORDER.bit_length() - 1)) - 1)


def new_private(rand=os.urandom) -> bytes:
    while True:
        p = rand(PRIVATE_SIZE)
        if int.from_bytes(p, "little").bit_length() >= DEGREE // 2:
            return p


def _encode_point(x: int, y: int) -> bytes:
    return x.to_bytes(NBYTES, "little") + y.to_bytes(NBYTES, "little")


def _decode_point(data: bytes) -> tuple[int, int]:
    if len(data) != PUBLIC_SIZE:
        raise ValueError(f"public key must be {PUBLIC_SIZE} bytes, got {len(data)}")
    return int.from_bytes(data[:NBYTES], "little"), int.from_bytes(data[NBYTES:], "little")


def generate_public(private: bytes) -> bytes:
    if int.from_bytes(private, "little").bit_length() < DEGREE // 2:
        raise ValueError("private key too small")
    return _encode_point(*point_mul(BASE_X, BASE_Y, clip_private(private)))


def shared_secret(private: bytes, remote_public: bytes) -> bytes:
    x, y = _decode_point(remote_public)
    if not on_curve(x, y):
        raise ValueError("remote public key is not on the curve")
    return _encode_point(*point_mul(x, y, clip_private(private)))
