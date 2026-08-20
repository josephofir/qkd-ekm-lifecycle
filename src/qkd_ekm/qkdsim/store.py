"""In-memory key store backing the ETSI-014 + HEQA-shaped simulator.

Models a single shared 256-bit-key pool between the two configured SAEs
(``QKD1``/``QKD2`` by default): :meth:`new_key` issues a fresh random
32-byte key -- as a real KME would draw one from its QKD-generated
inventory -- and holds it until the peer retrieves it exactly once via
``dec_keys``. Counters mirror the HEQA ``status_extension`` field names so
``HeqaMonitor.capture()`` (see ``qkd_ekm.qkd.heqa``) works unmodified
against this simulator.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

_KEY_BITS = 256
_KEY_BYTES = _KEY_BITS // 8


@dataclass(frozen=True)
class Key:
    key_id: str
    key: bytes
    pair: tuple[str, str]


class KeyStore:
    def __init__(
        self,
        rate_units_per_s: float = 82.890625,
        inventory: int = 3_275_971,
        seed: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.rate_units_per_s = rate_units_per_s
        self.inventory = inventory
        self.source_available = True

        self._rng = random.Random(seed)
        self._clock = clock
        self._start = clock()

        self._keys: dict[str, Key] = {}
        self._consumed_ids: set[str] = set()
        self._issued_count = 0

        self.consumed_keys = 0
        self.consumed_bits = 0
        self.key_requests = 0
        self.failed_key_requests = 0
        self.deleted_bits = 0

    @property
    def generated_bits(self) -> int:
        elapsed = self._clock() - self._start
        return int(elapsed * self.rate_units_per_s * _KEY_BITS)

    @property
    def available_256bit_keys(self) -> int:
        return self.inventory - self._issued_count

    @property
    def pending_count(self) -> int:
        """Keys issued but not yet retrieved via dec_keys."""
        return len(self._keys) - len(self._consumed_ids)

    def new_key(self, pair: tuple[str, str]) -> Key:
        key_id = str(uuid.uuid4())
        material = self._rng.randbytes(_KEY_BYTES)
        key = Key(key_id, material, pair)
        self._keys[key_id] = key
        self._issued_count += 1
        self.consumed_keys += 1
        self.consumed_bits += _KEY_BITS
        return key

    def get_by_id(self, key_id: str) -> Key | None:
        return self._keys.get(key_id)

    def is_consumed(self, key_id: str) -> bool:
        return key_id in self._consumed_ids

    def consume(self, key_id: str) -> Key | None:
        """Mark a pending key as retrieved. Returns None if unknown or already consumed."""
        if key_id not in self._keys or key_id in self._consumed_ids:
            return None
        self._consumed_ids.add(key_id)
        self.deleted_bits += _KEY_BITS
        return self._keys[key_id]
