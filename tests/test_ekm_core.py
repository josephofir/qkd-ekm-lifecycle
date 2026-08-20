import logging
import os
import stat
import threading

import pytest

from qkd_ekm.ekm.lifecycle import derive
from qkd_ekm.ekm.pool import KeyPool, PoolEmpty
from qkd_ekm.ekm.store import AlreadyBound, Store, load_local_key
from qkd_ekm.qkd.etsi014 import Key, QkdError, QkdUnavailable

# --- helpers -----------------------------------------------------------


class FakeQkd:
    """Stand-in for Etsi014Client: hands out sequentially-numbered keys."""

    def __init__(self):
        self.available = True
        self.calls: list[tuple[str, int]] = []
        self._counter = 0

    def enc_keys(self, slave_sae, number=1, size=256):
        self.calls.append((slave_sae, number))
        if not self.available:
            raise QkdUnavailable("source down")
        keys = []
        for _ in range(number):
            self._counter += 1
            keys.append(Key(f"k{self._counter}", bytes([self._counter % 256]) * 32))
        return keys


class FlakyQkd:
    """Raises QkdError (a non-QkdUnavailable failure) on its first call, then succeeds."""

    def __init__(self):
        self.calls = 0

    def enc_keys(self, slave_sae, number=1, size=256):
        self.calls += 1
        if self.calls == 1:
            raise QkdError("bad request")
        return [Key(f"flaky{self.calls}-{i}", bytes([i % 256]) * 32) for i in range(number)]


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(str(tmp_path / "ekm.db"), local_key=os.urandom(32))


# --- Store: bindings -----------------------------------------------------


def test_bind_then_lookup_object():
    s = Store(":memory:", local_key=os.urandom(32))
    s.bind("qkd-1", "ekm", "projects/p/cryptoKeys/k1", "QKD2")
    assert s.lookup_object("ekm", "projects/p/cryptoKeys/k1") == "qkd-1"


def test_lookup_object_returns_none_when_absent():
    s = Store(":memory:", local_key=os.urandom(32))
    assert s.lookup_object("ekm", "nope") is None


def test_bind_same_qkd_key_id_twice_raises_already_bound():
    s = Store(":memory:", local_key=os.urandom(32))
    s.bind("qkd-1", "ekm", "obj-a", "QKD2")
    with pytest.raises(AlreadyBound):
        s.bind("qkd-1", "ekm", "obj-b", "QKD2")


def test_bind_same_purpose_object_twice_raises_already_bound():
    s = Store(":memory:", local_key=os.urandom(32))
    s.bind("qkd-1", "ekm", "obj-a", "QKD2")
    with pytest.raises(AlreadyBound):
        s.bind("qkd-2", "ekm", "obj-a", "QKD2")


def test_bind_same_object_id_different_purpose_is_allowed():
    s = Store(":memory:", local_key=os.urandom(32))
    s.bind("qkd-1", "ekm", "shared-id", "QKD2")
    s.bind("qkd-2", "vpn", "shared-id", "QKD2")
    assert s.lookup_object("ekm", "shared-id") == "qkd-1"
    assert s.lookup_object("vpn", "shared-id") == "qkd-2"


def test_is_consumed_true_only_after_bind():
    s = Store(":memory:", local_key=os.urandom(32))
    assert s.is_consumed("qkd-1") is False
    s.bind("qkd-1", "file", "obj-a", "QKD1")
    assert s.is_consumed("qkd-1") is True


def test_count():
    s = Store(":memory:", local_key=os.urandom(32))
    assert s.count() == 0
    s.bind("qkd-1", "ekm", "obj-a", "QKD2")
    s.bind("qkd-2", "ekm", "obj-b", "QKD2")
    assert s.count() == 2


def test_bind_rejects_unknown_purpose():
    s = Store(":memory:", local_key=os.urandom(32))
    with pytest.raises(ValueError):
        s.bind("qkd-1", "bogus", "obj-a", "QKD2")


# --- Store: KEK at rest ----------------------------------------------------


def test_put_kek_then_get_kek_roundtrips():
    s = Store(":memory:", local_key=os.urandom(32))
    s.put_kek("projects/p/cryptoKeys/k1", "qkd-1", b"\x00" * 32)
    assert s.get_kek("projects/p/cryptoKeys/k1") == b"\x00" * 32


def test_get_kek_returns_none_when_absent():
    s = Store(":memory:", local_key=os.urandom(32))
    assert s.get_kek("nope") is None


def test_kek_material_is_wrapped_not_plaintext_on_disk(tmp_path):
    path = str(tmp_path / "ekm.db")
    s = Store(path, local_key=os.urandom(32))
    material = b"\x01" * 32
    s.put_kek("obj-a", "qkd-1", material)
    s.close()
    with open(path, "rb") as f:
        raw = f.read()
    assert material not in raw


def test_get_kek_fails_to_unwrap_with_wrong_local_key(tmp_path):
    path = str(tmp_path / "ekm.db")
    s1 = Store(path, local_key=b"\x11" * 32)
    s1.put_kek("obj-a", "qkd-1", b"\x02" * 32)
    s1.close()
    s2 = Store(path, local_key=b"\x22" * 32)
    with pytest.raises(ValueError):
        s2.get_kek("obj-a")


# --- load_local_key --------------------------------------------------------


def test_load_local_key_creates_32_bytes_mode_0600(tmp_path):
    path = str(tmp_path / "local.key")
    key = load_local_key(path)
    assert len(key) == 32
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_load_local_key_reuses_existing_file(tmp_path):
    path = str(tmp_path / "local.key")
    first = load_local_key(path)
    second = load_local_key(path)
    assert first == second


# --- KeyPool: pulling ------------------------------------------------------


def test_pull_once_fills_deque_to_target(store):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=5)
    pulled = pool.pull_once()
    assert pulled == 5
    assert pool.size("QKD2") == 5


def test_pull_once_tops_up_only_the_shortfall(store):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=5)
    pool.pull_once()
    pool.allocate("QKD2")
    pool.allocate("QKD2")
    pulled = pool.pull_once()
    assert pulled == 2
    assert pool.size("QKD2") == 5


def test_pull_once_never_exceeds_target():
    qkd = FakeQkd()
    s = Store(":memory:", local_key=os.urandom(32))
    pool = KeyPool(s, qkd, ["QKD2"], target=3)
    pool.pull_once()
    pool.pull_once()
    assert pool.size("QKD2") <= 3


def test_pull_once_caps_a_single_request_at_100(store):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=250)
    pool.pull_once()
    assert qkd.calls == [("QKD2", 100)]


def test_pull_once_covers_each_configured_peer(store):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD1", "QKD2"], target=4)
    pool.pull_once()
    assert pool.size("QKD1") == 4
    assert pool.size("QKD2") == 4


def test_pull_once_sets_source_available_false_on_qkd_unavailable(store):
    qkd = FakeQkd()
    qkd.available = False
    pool = KeyPool(store, qkd, ["QKD2"], target=5)
    assert pool.source_available is True
    pulled = pool.pull_once()
    assert pulled == 0
    assert pool.source_available is False


def test_pull_once_recovers_source_available_true(store):
    qkd = FakeQkd()
    qkd.available = False
    pool = KeyPool(store, qkd, ["QKD2"], target=5)
    pool.pull_once()
    assert pool.source_available is False
    qkd.available = True
    pool.pull_once()
    assert pool.source_available is True


# --- KeyPool: allocation ----------------------------------------------------


def test_allocate_pops_fifo(store):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=3)
    pool.pull_once()
    first = pool.allocate("QKD2")
    second = pool.allocate("QKD2")
    assert first.key_id == "k1"
    assert second.key_id == "k2"
    assert pool.size("QKD2") == 1


def test_allocate_raises_pool_empty_when_no_keys(store):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=3)
    with pytest.raises(PoolEmpty):
        pool.allocate("QKD2")


def test_allocate_asserts_not_already_consumed(store):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=1)
    pool.pull_once()
    key = pool._deques["QKD2"][0][0]
    store.bind(key.key_id, "ekm", "obj-a", "QKD2")
    with pytest.raises(AssertionError):
        pool.allocate("QKD2")


# --- KeyPool: TTL expiry ----------------------------------------------------


def test_expire_once_removes_stale_unconsumed_entries(store):
    qkd = FakeQkd()
    clock = FakeClock()
    pool = KeyPool(store, qkd, ["QKD2"], target=3, ttl_s=10, clock=clock)
    pool.pull_once()
    assert pool.size("QKD2") == 3
    clock.advance(11)
    expired = pool.expire_once()
    assert expired == 3
    assert pool.size("QKD2") == 0


def test_expire_once_keeps_fresh_entries(store):
    qkd = FakeQkd()
    clock = FakeClock()
    pool = KeyPool(store, qkd, ["QKD2"], target=2, ttl_s=10, clock=clock)
    pool.pull_once()
    clock.advance(5)
    expired = pool.expire_once()
    assert expired == 0
    assert pool.size("QKD2") == 2


def test_expire_once_only_expires_the_stale_prefix(store):
    qkd = FakeQkd()
    clock = FakeClock()
    pool = KeyPool(store, qkd, ["QKD2"], target=2, ttl_s=10, clock=clock)
    pool.pull_once()  # 2 keys received at t=0
    clock.advance(11)
    pool.target = 4
    pool.pull_once()  # 2 more keys received at t=11
    expired = pool.expire_once()
    assert expired == 2
    assert pool.size("QKD2") == 2


# --- KeyPool: logging --------------------------------------------------------


def test_pull_logs_only_when_keys_pulled(store, caplog):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=2)
    with caplog.at_level(logging.INFO, logger="EKM"):
        pool.pull_once()  # pulls 2
        pool.pull_once()  # nothing to pull, deque already at target
    messages = [r.message for r in caplog.records]
    assert messages.count("Pulled 2 keys for peer QKD2") == 1


def test_unavailable_logged_once_per_transition(store, caplog):
    qkd = FakeQkd()
    qkd.available = False
    pool = KeyPool(store, qkd, ["QKD2"], target=2)
    with caplog.at_level(logging.WARNING, logger="EKM"):
        pool.pull_once()
        pool.pull_once()
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["QKD source unavailable for peer QKD2"]


def test_expire_logs_only_when_keys_expired(store, caplog):
    qkd = FakeQkd()
    clock = FakeClock()
    pool = KeyPool(store, qkd, ["QKD2"], target=2, ttl_s=10, clock=clock)
    pool.pull_once()
    with caplog.at_level(logging.INFO, logger="EKM"):
        pool.expire_once()  # nothing stale yet
        clock.advance(11)
        pool.expire_once()  # 2 stale
    messages = [r.message for r in caplog.records]
    assert messages.count("Expired 2 unused keys for peer QKD2") == 1


# --- KeyPool: fault tolerance ------------------------------------------------


def test_pull_once_survives_non_unavailable_error_and_keeps_working(store, caplog):
    qkd = FlakyQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=3)
    with caplog.at_level(logging.WARNING, logger="EKM"):
        first = pool.pull_once()
    assert first == 0
    assert pool.size("QKD2") == 0
    # QkdError is not a source-availability signal.
    assert pool.source_available is True
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["Key pull failed for peer QKD2: QkdError"]

    second = pool.pull_once()
    assert second == 3
    assert pool.size("QKD2") == 3


async def test_run_background_survives_non_unavailable_error_and_keeps_pulling(store):
    import asyncio

    qkd = FlakyQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=2)
    tasks = pool.run_background(interval_s=0.01)
    await asyncio.sleep(0.05)
    assert pool.size("QKD2") == 2  # loop survived the first QkdError and kept going
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- KeyPool: thread-safety ---------------------------------------------------


def test_concurrent_pull_and_allocate_no_duplicates_or_errors(store):
    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=50)
    allocated_ids: list[str] = []
    allocated_lock = threading.Lock()
    errors: list[BaseException] = []
    stop = threading.Event()

    def puller():
        while not stop.is_set():
            pool.pull_once()

    def allocator():
        while not stop.is_set():
            try:
                key = pool.allocate("QKD2")
            except PoolEmpty:
                continue
            except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
                errors.append(exc)
                return
            with allocated_lock:
                allocated_ids.append(key.key_id)

    threads = [threading.Thread(target=puller) for _ in range(2)]
    threads += [threading.Thread(target=allocator) for _ in range(4)]
    for t in threads:
        t.start()
    stop_timer = threading.Timer(0.2, stop.set)
    stop_timer.start()
    for t in threads:
        t.join(timeout=5)
    stop_timer.cancel()

    assert errors == []
    assert len(allocated_ids) == len(set(allocated_ids))


# --- KeyPool: run_background -------------------------------------------------


async def test_run_background_returns_pull_and_expire_tasks(store):
    import asyncio

    qkd = FakeQkd()
    pool = KeyPool(store, qkd, ["QKD2"], target=2)
    tasks = pool.run_background(interval_s=0.01)
    assert len(tasks) == 2
    await asyncio.sleep(0.03)
    assert pool.size("QKD2") == 2
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- lifecycle.derive -------------------------------------------------------


@pytest.mark.parametrize(
    "source_available,ekm_reachable,pool_size,has_authoritative_binding,"
    "continuity_authority,recovery_pending,expected_mode",
    [
        (True, True, 5, False, True, False, "READY"),
        (False, True, 5, False, True, False, "BUFFERED"),
        (False, True, 0, True, True, False, "BINDING_HOLDOVER"),
        (False, True, 0, False, True, False, "EXHAUSTED"),
        (False, False, 0, False, False, False, "SUSPENDED"),
        (True, True, 5, False, False, False, "READY"),
        (True, True, 0, False, True, True, "RECOVERY"),
        (False, False, 0, True, True, True, "RECOVERY"),
    ],
)
def test_derive_mode_table(
    source_available,
    ekm_reachable,
    pool_size,
    has_authoritative_binding,
    continuity_authority,
    recovery_pending,
    expected_mode,
):
    result = derive(
        source_available,
        ekm_reachable,
        pool_size,
        has_authoritative_binding,
        continuity_authority,
        recovery_pending,
    )
    assert result["mode"] == expected_mode


@pytest.mark.parametrize("ekm_reachable", [True, False])
def test_derive_storage_matches_ekm_reachable(ekm_reachable):
    result = derive(True, ekm_reachable, 5, False, True, False)
    assert result["storage"] == ekm_reachable


def test_derive_fresh_allocation_true_when_ready_and_pool_nonempty():
    result = derive(True, True, 5, False, True, False)
    assert result["fresh_allocation"] is True


def test_derive_fresh_allocation_false_when_pool_empty():
    result = derive(True, True, 0, False, True, False)
    assert result["fresh_allocation"] is False


def test_derive_fresh_allocation_false_when_ekm_unreachable():
    result = derive(True, False, 5, False, True, False)
    assert result["fresh_allocation"] is False


def test_derive_fresh_allocation_false_when_suspended():
    result = derive(False, False, 0, False, False, False)
    assert result["mode"] == "SUSPENDED"
    assert result["fresh_allocation"] is False


def test_derive_fresh_allocation_false_when_recovery_even_with_pool():
    result = derive(True, True, 5, False, True, True)
    assert result["mode"] == "RECOVERY"
    assert result["fresh_allocation"] is False


def test_derive_established_vpn_is_distinct():
    result = derive(True, True, 5, False, True, False)
    assert result["established_vpn"] == "distinct"


def test_derive_recovery_pending_overrides_everything():
    result = derive(False, False, 0, False, False, True)
    assert result["mode"] == "RECOVERY"
