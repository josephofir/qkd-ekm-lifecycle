import os

import pytest

from qkd_ekm.common.crypto import aes_gcm_unwrap, aes_gcm_wrap


def test_roundtrip_and_aad():
    kek, pt, aad = os.urandom(32), os.urandom(48), b"ctx"
    blob = aes_gcm_wrap(kek, pt, aad)
    assert aes_gcm_unwrap(kek, blob, aad) == pt
    with pytest.raises(ValueError):
        aes_gcm_unwrap(kek, blob, b"other")
    with pytest.raises(ValueError):
        aes_gcm_unwrap(os.urandom(32), blob, aad)
