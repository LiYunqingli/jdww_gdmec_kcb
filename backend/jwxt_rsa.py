"""纯 Python 实现的 RSA/PKCS#1 v1.5 加密（用于教务本地登录）。

正方前端用 jsbn 的 `rsaKey.encrypt()` + `hex2b64()`，即标准 PKCS#1 v1.5 填充后 base64。
这里不引第三方库，直接用整数模幂。
"""
from __future__ import annotations

import base64
import secrets


def _b64_to_int(s: str) -> int:
    s = s.strip()
    s += "=" * (-len(s) % 4)
    return int.from_bytes(base64.b64decode(s), "big")


def rsa_encrypt(plain: str, modulus_b64: str, exponent_b64: str) -> str:
    n = _b64_to_int(modulus_b64)
    e = _b64_to_int(exponent_b64)
    k = (n.bit_length() + 7) // 8
    msg = plain.encode("utf-8")
    if len(msg) > k - 11:
        raise ValueError("密码过长，超出 RSA 可加密长度")
    ps_len = k - len(msg) - 3
    pad = bytearray()
    while len(pad) < ps_len:
        b = secrets.randbelow(255) + 1  # 1..255，不能为 0
        pad.append(b)
    block = b"\x00\x02" + bytes(pad) + b"\x00" + msg
    c = pow(int.from_bytes(block, "big"), e, n)
    return base64.b64encode(c.to_bytes(k, "big")).decode()
