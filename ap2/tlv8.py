# SPDX-License-Identifier: GPL-3.0-only AND MIT
# Xiaomi Room Audio: Python adaptation and modifications, 2026-11-18.
# Adapted from pyatv (Copyright (c) 2020 Pierre Ståhl, MIT).
# New project work is GPL-3.0-only; upstream notices remain in ../NOTICE.
"""HomeKit TLV8 codec (one level, >255B fragmentation). Ported from pyatv."""

from enum import IntEnum


class TlvValue(IntEnum):
    Method = 0x00
    Identifier = 0x01
    Salt = 0x02
    PublicKey = 0x03
    Proof = 0x04
    EncryptedData = 0x05
    SeqNo = 0x06
    Error = 0x07
    BackOff = 0x08
    Certificate = 0x09
    Signature = 0x0A
    Permissions = 0x0B
    FragmentData = 0x0C
    FragmentLast = 0x0D
    Name = 0x11
    Flags = 0x13


class Flags(IntEnum):
    TransientPairing = 0x10


def read_tlv(data: bytes) -> dict:
    result: dict = {}
    pos = 0
    size = len(data)
    while pos < size:
        tag = data[pos]
        length = data[pos + 1]
        value = data[pos + 2:pos + 2 + length]
        if tag in result:
            result[tag] += value
        else:
            result[tag] = value
        pos += 2 + length
    return result


def write_tlv(data: dict) -> bytes:
    out = b""
    for key, value in data.items():
        tag = bytes([int(key)])
        pos = 0
        remaining = len(value)
        while remaining > 0:
            chunk = min(remaining, 255)
            out += tag + bytes([chunk]) + value[pos:pos + chunk]
            pos += chunk
            remaining -= chunk
    return out
