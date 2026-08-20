"""Per-peer buffer of unconsumed QKD keys.

Pulling from the QKD source (ETSI-014 `enc_keys`) is a network round trip
the allocation path should never have to wait on. `KeyPool` decouples the
two: a background loop keeps each peer's `deque` topped up to `target`
while `source_available` tracks whether the QKD source answered the last
pull, and `allocate` serves already-buffered keys FIFO with no I/O. A
second background loop evicts keys that have sat unused past `ttl_s`, so a
stale key is never handed out long after its symmetric-window peer likely
moved on.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable

from qkd_ekm.common.log import get_logger
from qkd_ekm.ekm.store import Store
from qkd_ekm.qkd.etsi014 import Etsi014Client, Key, QkdUnavailable

_LOG = get_logger("EKM")
_MAX_PER_REQUEST = 100


class PoolEmpty(RuntimeError):
    """No unconsumed keys are buffered for this peer."""


class KeyPool:
    def __init__(
        self,
        store: Store,
        qkd: Etsi014Client,
        peers: list[str],
        target: int = 50,
        ttl_s: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.qkd = qkd
        self.peers = list(peers)
        self.target = target
        self.ttl_s = ttl_s
        self.source_available = True
        self._clock = clock
        self._deques: dict[str, deque[tuple[Key, float]]] = {p: deque() for p in self.peers}
        # NOTE: one coarse lock over all peer deques. pull_once (which runs
        # off-thread via asyncio.to_thread), expire_once, and allocate() (called
        # from request handlers) all touch these deques concurrently; the lock
        # is only ever held for in-memory deque ops, never across the blocking
        # QKD HTTP call, so contention is negligible.
        self._lock = threading.Lock()

    def size(self, peer: str) -> int:
        with self._lock:
            return len(self._deques[peer])

    def pull_once(self) -> int:
        pulled = 0
        for peer in self.peers:
            with self._lock:
                n = min(self.target - len(self._deques[peer]), _MAX_PER_REQUEST)
            if n <= 0:
                continue
            try:
                keys = self.qkd.enc_keys(peer, number=n)
            except QkdUnavailable:
                if self.source_available:
                    _LOG.warning(f"QKD source unavailable for peer {peer}")
                self.source_available = False
                continue
            except Exception as exc:  # noqa: BLE001 - keep the pull loop alive
                _LOG.warning(f"Key pull failed for peer {peer}: {exc.__class__.__name__}")
                continue
            self.source_available = True
            received_at = self._clock()
            with self._lock:
                for key in keys:
                    self._deques[peer].append((key, received_at))
            if keys:
                _LOG.info(f"Pulled {len(keys)} keys for peer {peer}")
            pulled += len(keys)
        return pulled

    def expire_once(self) -> int:
        expired = 0
        for peer, dq in self._deques.items():
            cutoff = self._clock() - self.ttl_s
            n = 0
            with self._lock:
                while dq and dq[0][1] < cutoff:
                    dq.popleft()
                    n += 1
            if n:
                _LOG.info(f"Expired {n} unused keys for peer {peer}")
            expired += n
        return expired

    def allocate(self, peer: str) -> Key:
        with self._lock:
            dq = self._deques[peer]
            if not dq:
                raise PoolEmpty(peer)
            key, _received_at = dq.popleft()
        assert not self.store.is_consumed(key.key_id)
        return key

    def run_background(self, interval_s: float) -> list[asyncio.Task]:
        async def _pull_loop():
            while True:
                await asyncio.to_thread(self.pull_once)
                await asyncio.sleep(interval_s)

        async def _expire_loop():
            while True:
                self.expire_once()
                await asyncio.sleep(interval_s)

        return [asyncio.create_task(_pull_loop()), asyncio.create_task(_expire_loop())]
