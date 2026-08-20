"""SQLite-backed EKM binding store.

Persists the mapping from a consumed QKD `qkd_key_id` to the (purpose,
object_id, peer) it was bound to, plus the wrapped KEK material derived
from an 'ekm'-purpose key. Bindings are the durable record of "this QKD
key has already been spent on this object" -- `is_consumed` and the
UNIQUE(purpose, object_id) constraint are what make re-delivery of the
same object idempotent and prevent two different objects from racing to
claim the same underlying key.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from qkd_ekm.common.crypto import aes_gcm_unwrap, aes_gcm_wrap

_PURPOSES = ("ekm", "vpn", "file")


class AlreadyBound(RuntimeError):
    """The qkd_key_id, or the (purpose, object_id) pair, is already bound."""


def load_local_key(path: str) -> bytes:
    """Return the 32-byte local wrapping key at `path`, creating it if absent.

    A freshly generated key is written with mode 0600 so only the owning
    process can read it.

    # NOTE: a production deployment would fetch this from an HSM or a
    # secret manager; a local file is enough for the reproducibility package.
    """
    if not os.path.exists(path):
        key = os.urandom(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key
    with open(path, "rb") as f:
        return f.read()


class Store:
    def __init__(self, path: str, local_key: bytes):
        self._local_key = local_key
        # NOTE: one lock around every use of the shared connection. A
        # single sqlite3.Connection isn't safe under concurrent access from
        # multiple threads (KeyPool.allocate calls is_consumed() from request
        # handlers while pull/expire run on other threads) -- SQLite itself
        # can raise "API misuse" if two threads touch it at once.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bindings (
                qkd_key_id TEXT PRIMARY KEY,
                purpose TEXT NOT NULL CHECK(purpose IN ('ekm', 'vpn', 'file')),
                object_id TEXT NOT NULL,
                peer TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(purpose, object_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kek (
                object_id TEXT PRIMARY KEY,
                qkd_key_id TEXT NOT NULL,
                enc_material BLOB NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def bind(self, qkd_key_id: str, purpose: str, object_id: str, peer: str) -> None:
        if purpose not in _PURPOSES:
            raise ValueError(f"invalid purpose: {purpose!r}")
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO bindings (qkd_key_id, purpose, object_id, peer, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (qkd_key_id, purpose, object_id, peer, time.time()),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise AlreadyBound(f"{qkd_key_id}/{purpose}/{object_id}") from exc

    def lookup_object(self, purpose: str, object_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT qkd_key_id FROM bindings WHERE purpose = ? AND object_id = ?",
                (purpose, object_id),
            ).fetchone()
        return row[0] if row else None

    def is_consumed(self, qkd_key_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM bindings WHERE qkd_key_id = ?", (qkd_key_id,)
            ).fetchone()
        return row is not None

    def put_kek(self, object_id: str, qkd_key_id: str, material: bytes) -> None:
        enc_material = aes_gcm_wrap(self._local_key, material, aad=object_id.encode())
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kek (object_id, qkd_key_id, enc_material) "
                "VALUES (?, ?, ?)",
                (object_id, qkd_key_id, enc_material),
            )
            self._conn.commit()

    def get_kek(self, object_id: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT enc_material FROM kek WHERE object_id = ?", (object_id,)
            ).fetchone()
        if row is None:
            return None
        return aes_gcm_unwrap(self._local_key, row[0], aad=object_id.encode())

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM bindings").fetchone()[0]
