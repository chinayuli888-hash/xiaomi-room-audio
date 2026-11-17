"""HAP transient SRP-6a client (3072-bit group, SHA-512, PIN 3939).

Mirrors pyatv auth/hap_srp.py step1/step2/step4 behaviour for the transient
path: we only need the shared session key K and the client proof M1.
"""

import binascii
import hashlib
import os
from secrets import compare_digest

from srptools import SRPClientSession, SRPContext, constants

USERNAME = "Pair-Setup"
TRANSIENT_PIN = "3939"


class TransientSrp:
    def __init__(self, pin: str = TRANSIENT_PIN) -> None:
        self.pin = pin
        self.seed = os.urandom(32)
        self.context = SRPContext(
            USERNAME,
            str(pin),
            prime=constants.PRIME_3072,
            generator=constants.PRIME_3072_GEN,
            hash_func=hashlib.sha512,
        )
        self.session = SRPClientSession(
            self.context, private=binascii.hexlify(self.seed).decode()
        )
        self.shared: bytes = b""

    def process(self, server_pub: bytes, salt: bytes) -> tuple[bytes, bytes]:
        """Feed server B + salt; return (client public A, proof M1)."""
        self.session.process(
            binascii.hexlify(server_pub).decode(),
            binascii.hexlify(salt).decode(),
        )
        pub_a = binascii.unhexlify(self.session.public)
        proof = binascii.unhexlify(self.session.key_proof)
        self.shared = binascii.unhexlify(self.session.key)  # K = SHA512(S), 64B
        return pub_a, proof

    def verify_server_proof(self, server_m2: bytes) -> bool:
        expected = binascii.unhexlify(self.session.key_proof_hash)
        return compare_digest(server_m2, expected)
