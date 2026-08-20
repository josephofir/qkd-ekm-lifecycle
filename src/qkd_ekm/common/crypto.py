import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


def aes_gcm_wrap(kek: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(kek).encrypt(nonce, plaintext, aad)
    return nonce + ct


def aes_gcm_unwrap(kek: bytes, blob: bytes, aad: bytes) -> bytes:
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    try:
        return AESGCM(kek).decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        raise ValueError("unwrap failed") from exc


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    # validate=True: without it, characters outside the base64 alphabet are
    # silently discarded, so malformed input can decode to something plausible
    # instead of being rejected.
    return base64.b64decode(data, validate=True)
