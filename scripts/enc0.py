#!/usr/bin/env python3
"""Evernote ENC0 payload parser / password verifier.

Layout (little-endian byte order of fields):
    b"ENC0"   4 bytes   magic
    salt      16 bytes  PBKDF2 salt for the AES key
    salthmac  16 bytes  PBKDF2 salt for the HMAC key
    iv        16 bytes  AES-CBC initialisation vector
    ciphertext  n*16 bytes
    hmac      32 bytes  HMAC-SHA256 over (magic|salt|salthmac|iv|ciphertext)

KDF: PBKDF2-HMAC-SHA256, 50000 iterations, 16-byte (128-bit) key.
"""
import base64
import hashlib
import hmac

MAGIC = b"ENC0"
ITERATIONS = 50000
KEYLEN = 16


class Enc0:
    def __init__(self, raw: bytes):
        if raw[:4] != MAGIC:
            raise ValueError("not an ENC0 payload")
        self.raw = raw
        self.salt = raw[4:20]
        self.salthmac = raw[20:36]
        self.iv = raw[36:52]
        self.ciphertext = raw[52:-32]
        self.digest = raw[-32:]
        self.body = raw[:-32]
        if len(self.ciphertext) % 16:
            raise ValueError("ciphertext not block aligned")

    @classmethod
    def from_b64(cls, text: str):
        return cls(base64.b64decode("".join(text.split())))

    def _key(self, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", self._pw, salt, ITERATIONS, KEYLEN)

    def verify(self, password) -> bool:
        """Return True when `password` reproduces the trailing HMAC."""
        pw = password.encode("utf-8") if isinstance(password, str) else password
        self._pw = pw
        keyhmac = self._key(self.salthmac)
        want = hmac.new(keyhmac, self.body, hashlib.sha256).digest()
        return hmac.compare_digest(want, self.digest)

    def decrypt(self, password) -> bytes:
        from Crypto.Cipher import AES

        pw = password.encode("utf-8") if isinstance(password, str) else password
        self._pw = pw
        if not self.verify(pw):
            raise ValueError("wrong password")
        key = self._key(self.salt)
        pt = AES.new(key, AES.MODE_CBC, self.iv).decrypt(self.ciphertext)
        # strip PKCS#7 padding (tolerate zero padding too)
        if pt:
            pad = pt[-1]
            if 1 <= pad <= 16 and pt.endswith(bytes([pad]) * pad):
                pt = pt[:-pad]
            else:
                pt = pt.rstrip(b"\x00")
        return pt
