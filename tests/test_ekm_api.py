import asyncio
import logging
from collections import deque

import httpx
import pytest

from qkd_ekm.common.auth import AuthError, GoogleJwtVerifier, StaticTokenVerifier
from qkd_ekm.common.crypto import b64d, b64e
from qkd_ekm.ekm.app import EkmSettings, create_app
from qkd_ekm.ekm.pool import PoolEmpty
from qkd_ekm.ekm.store import Store, load_local_key
from qkd_ekm.qkd.etsi014 import Key

KMS_TOKEN = "kms-token"
VPN_TOKEN = "vpn-token"
KMS_AUTH = {"Authorization": f"Bearer {KMS_TOKEN}"}
VPN_AUTH = {"Authorization": f"Bearer {VPN_TOKEN}"}


# --- fakes ------------------------------------------------------------------


class FakeVerifier:
    """Accepts one token, or rejects everything with `error`."""

    def __init__(self, token: str, error: str | None = None, unauthenticated: bool = False):
        self.token = token
        self.error = error
        self.unauthenticated = unauthenticated

    def verify(self, token: str) -> dict:
        if self.error is not None:
            raise AuthError(self.error, unauthenticated=self.unauthenticated)
        if token != self.token:
            raise AuthError("bad token")
        return {"email": "caller@example.com"}


class FakePool:
    """KeyPool stand-in: buffered keys per peer, never touching a QKD source."""

    def __init__(self, store: Store, peers=("QKD2",), count: int = 4):
        self.store = store
        self.peers = list(peers)
        self.source_available = True
        self.background_started = 0
        self.tasks: list[asyncio.Task] = []
        self._keys = {p: deque(self._mint(p, count)) for p in self.peers}

    @staticmethod
    def _mint(peer: str, count: int, start: int = 0) -> list[Key]:
        return [Key(f"{peer}-k{i}", bytes([i % 256]) * 32) for i in range(start, start + count)]

    def refill(self, peer: str, count: int, start: int) -> None:
        self._keys[peer].extend(self._mint(peer, count, start))

    def drain(self, peer: str) -> None:
        self._keys[peer].clear()

    def size(self, peer: str) -> int:
        return len(self._keys[peer])

    def allocate(self, peer: str) -> Key:
        dq = self._keys[peer]  # KeyError for an unknown peer, like KeyPool
        while dq:
            key = dq.popleft()
            if not self.store.is_consumed(key.key_id):
                return key
        raise PoolEmpty(peer)

    def run_background(self, interval_s: float) -> list[asyncio.Task]:
        self.background_started += 1
        self.tasks = [asyncio.ensure_future(asyncio.sleep(3600))]
        return self.tasks


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "ekm.db")


@pytest.fixture
def store(tmp_path, db_path):
    s = Store(db_path, load_local_key(str(tmp_path / "local.key")))
    yield s
    s.close()


@pytest.fixture
def pool(store):
    return FakePool(store)


@pytest.fixture
def app(store, pool):
    return create_app(
        store=store,
        pool=pool,
        verifier_kms=FakeVerifier(KMS_TOKEN),
        verifier_vpn=FakeVerifier(VPN_TOKEN),
        settings=EkmSettings(bind_peer="QKD2"),
    )


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ekm") as c:
        yield c


async def _wrap(client, key_id: str, plaintext: bytes, aad: bytes | None = None):
    body = {"plaintext": b64e(plaintext)}
    if aad is not None:
        body["additionalAuthenticatedData"] = b64e(aad)
    return await client.post(f"/api/keys/{key_id}:wrap", json=body, headers=KMS_AUTH)


async def _unwrap(client, key_id: str, blob: str, aad: bytes | None = None):
    body = {"wrappedBlob": blob}
    if aad is not None:
        body["additionalAuthenticatedData"] = b64e(aad)
    return await client.post(f"/api/keys/{key_id}:unwrap", json=body, headers=KMS_AUTH)


# --- wrap / unwrap ----------------------------------------------------------


async def test_wrap_then_unwrap_roundtrips_the_plaintext(client):
    resp = await _wrap(client, "v1", b"dek-material-0123")
    assert resp.status_code == 200
    blob = resp.json()["wrappedBlob"]

    resp = await _unwrap(client, "v1", blob)
    assert resp.status_code == 200
    assert b64d(resp.json()["plaintext"]) == b"dek-material-0123"


async def test_wrap_with_aad_roundtrips_when_aad_matches(client):
    resp = await _wrap(client, "v1", b"dek", aad=b"context")
    blob = resp.json()["wrappedBlob"]
    resp = await _unwrap(client, "v1", blob, aad=b"context")
    assert resp.status_code == 200
    assert b64d(resp.json()["plaintext"]) == b"dek"


async def test_unwrap_with_mismatched_aad_is_400_invalid_argument(client):
    blob = (await _wrap(client, "v1", b"dek", aad=b"context")).json()["wrappedBlob"]
    resp = await _unwrap(client, "v1", blob, aad=b"other")
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"


async def test_unwrap_of_unknown_key_id_is_404_not_found(client):
    resp = await _unwrap(client, "never-seen", b64e(b"\x00" * 32))
    assert resp.status_code == 404
    assert resp.json()["error"]["status"] == "NOT_FOUND"


async def test_wrap_with_malformed_base64_is_400(client):
    resp = await client.post(
        "/api/keys/v1:wrap", json={"plaintext": "not!base64!"}, headers=KMS_AUTH
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"


async def test_wrap_binds_one_qkd_key_and_persists_it(client, store, pool):
    before = pool.size("QKD2")
    await _wrap(client, "v1", b"dek")
    assert pool.size("QKD2") == before - 1
    qkd_id = store.lookup_object("ekm", "v1")
    assert qkd_id == "QKD2-k0"
    assert store.get_kek("v1") is not None


async def test_second_wrap_of_same_key_id_reuses_the_binding(client, store, pool):
    await _wrap(client, "v1", b"a")
    before = pool.size("QKD2")
    resp = await _wrap(client, "v1", b"b")
    assert resp.status_code == 200
    assert pool.size("QKD2") == before  # no second allocation
    assert store.count() == 1


async def test_wrap_logs_request_and_binding_without_key_material(client, caplog):
    with caplog.at_level(logging.INFO, logger="EKM"):
        await _wrap(client, "v1", b"dek")
    messages = [r.getMessage() for r in caplog.records]
    assert "Got Key Wrap request with KEKId: v1" in messages
    assert "Bound external key v1 to QKD key QKD2-k0" in messages


async def test_unwrap_logs_the_request(client, caplog):
    blob = (await _wrap(client, "v1", b"dek")).json()["wrappedBlob"]
    with caplog.at_level(logging.INFO, logger="EKM"):
        await _unwrap(client, "v1", blob)
    assert "Got Key Unwrap request with KEKId: v1" in [r.getMessage() for r in caplog.records]


async def test_wrap_is_503_unavailable_when_the_pool_is_empty(client, pool):
    pool.drain("QKD2")
    resp = await _wrap(client, "v1", b"dek")
    assert resp.status_code == 503
    assert resp.json()["error"]["status"] == "UNAVAILABLE"


async def test_nested_key_path_routes_to_wrap(client, store):
    resp = await _wrap(client, "proj/keys/v2", b"dek")
    assert resp.status_code == 200
    assert store.lookup_object("ekm", "proj/keys/v2") is not None


async def test_unknown_operation_suffix_is_404(client):
    resp = await client.post("/api/keys/v1:sign", json={}, headers=KMS_AUTH)
    assert resp.status_code == 404


# --- auth -------------------------------------------------------------------


async def test_wrap_without_authorization_is_401_unauthenticated(client):
    resp = await client.post("/api/keys/v1:wrap", json={"plaintext": b64e(b"dek")})
    assert resp.status_code == 401
    assert resp.json() == {
        "error": {"code": 401, "message": "missing bearer token", "status": "UNAUTHENTICATED"}
    }


async def test_verifier_rejection_is_403_permission_denied(store, pool):
    app = create_app(
        store=store,
        pool=pool,
        verifier_kms=FakeVerifier(KMS_TOKEN, error="email not allowed"),
        verifier_vpn=FakeVerifier(VPN_TOKEN),
        settings=EkmSettings(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ekm") as c:
        resp = await c.post("/api/keys/v1:wrap", json={"plaintext": b64e(b"d")}, headers=KMS_AUTH)
    assert resp.status_code == 403
    assert resp.json() == {
        "error": {"code": 403, "message": "email not allowed", "status": "PERMISSION_DENIED"}
    }


async def test_vpn_endpoints_reject_the_kms_token(client):
    resp = await client.get("/api/QKD2/new", headers=KMS_AUTH)
    assert resp.status_code == 403


async def test_healthz_needs_no_authorization(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- /api/{peer}/new --------------------------------------------------------


async def test_new_returns_32_byte_key_and_peer_name(client):
    resp = await client.get("/api/QKD2/new", headers=VPN_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["qkd_name"] == "QKD2"
    assert len(b64d(body["key"])) == 32
    assert body["key_id"] == "QKD2-k0"


async def test_new_returns_distinct_ids_and_persists_bindings(client, store):
    first = (await client.get("/api/QKD2/new", headers=VPN_AUTH)).json()
    second = (await client.get("/api/QKD2/new", headers=VPN_AUTH)).json()
    assert first["key_id"] != second["key_id"]
    assert store.count() == 2
    assert store.lookup_object("vpn", f"QKD2:{first['key_id']}") == first["key_id"]


async def test_new_logs_request_and_retrieval(client, caplog):
    with caplog.at_level(logging.INFO, logger="EKM"):
        await client.get("/api/QKD2/new", headers=VPN_AUTH)
    messages = [r.getMessage() for r in caplog.records]
    assert "Got new key request for instance QKD2" in messages
    assert "Retrieving 1 keys between QKD1 and QKD2 from pool" in messages


async def test_new_with_purpose_file_binds_a_file_purpose(client, store):
    body = (await client.get("/api/QKD2/new?purpose=file", headers=VPN_AUTH)).json()
    assert store.lookup_object("file", f"QKD2:{body['key_id']}") == body["key_id"]


async def test_new_with_unknown_purpose_is_400(client):
    resp = await client.get("/api/QKD2/new?purpose=bogus", headers=VPN_AUTH)
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"


async def test_new_for_unknown_peer_is_404(client):
    resp = await client.get("/api/QKD9/new", headers=VPN_AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["status"] == "NOT_FOUND"


async def test_new_is_503_when_pool_is_empty(client, pool):
    pool.drain("QKD2")
    resp = await client.get("/api/QKD2/new", headers=VPN_AUTH)
    assert resp.status_code == 503
    assert resp.json()["error"]["status"] == "UNAVAILABLE"


async def test_allocation_never_repeats_a_key_id_after_restart(db_path, tmp_path):
    local_key = load_local_key(str(tmp_path / "local.key"))
    issued = []
    for _restart in range(2):
        store = Store(db_path, local_key)
        pool = FakePool(store, count=2)  # same key ids offered again after restart
        app = create_app(
            store=store,
            pool=pool,
            verifier_kms=FakeVerifier(KMS_TOKEN),
            verifier_vpn=FakeVerifier(VPN_TOKEN),
            settings=EkmSettings(),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ekm") as c:
            pool.refill("QKD2", count=2, start=2)
            for _ in range(2):
                resp = await c.get("/api/QKD2/new", headers=VPN_AUTH)
                assert resp.status_code == 200
                issued.append(resp.json()["key_id"])
        store.close()
    assert len(set(issued)) == 4


# --- /api/state and continuity ----------------------------------------------


async def _state(client) -> dict:
    resp = await client.get("/api/state", headers=VPN_AUTH)
    assert resp.status_code == 200
    return resp.json()


async def test_state_is_ready_with_a_healthy_pool(client, pool):
    body = await _state(client)
    assert body["mode"] == "READY"
    assert body["pool"] == {"QKD2": pool.size("QKD2")}
    assert body["bindings_count"] == 0
    assert body["source_available"] is True
    assert body["continuity_authority"] is True
    assert body["recovery_pending"] is False


async def test_state_is_buffered_when_the_source_is_down_but_keys_remain(client, pool):
    pool.source_available = False
    body = await _state(client)
    assert body["mode"] == "BUFFERED"
    assert body["storage"] is True


async def test_state_is_binding_holdover_when_pool_empty_but_a_binding_exists(client, pool):
    await _wrap(client, "v1", b"dek")
    pool.source_available = False
    pool.drain("QKD2")
    body = await _state(client)
    assert body["mode"] == "BINDING_HOLDOVER"
    assert body["bindings_count"] == 1


async def test_state_is_exhausted_when_pool_empty_and_nothing_bound(client, pool):
    pool.source_available = False
    pool.drain("QKD2")
    assert (await _state(client))["mode"] == "EXHAUSTED"


async def test_withdrawing_authority_suspends(client, pool):
    pool.source_available = False
    pool.drain("QKD2")
    resp = await client.post(
        "/api/authority", json={"continuity_authority": False}, headers=VPN_AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["continuity_authority"] is False
    body = await _state(client)
    assert body["mode"] == "SUSPENDED"
    assert body["fresh_allocation"] is False


async def test_authority_requires_a_boolean(client):
    resp = await client.post("/api/authority", json={}, headers=VPN_AUTH)
    assert resp.status_code == 400


async def test_source_returning_sets_recovery_pending_until_acked(client, pool):
    pool.source_available = False
    await _state(client)
    pool.source_available = True
    body = await _state(client)
    assert body["mode"] == "RECOVERY"
    assert body["recovery_pending"] is True

    resp = await client.post("/api/recovery/ack", headers=VPN_AUTH)
    assert resp.status_code == 200
    assert resp.json()["recovery_pending"] is False
    assert (await _state(client))["mode"] == "READY"


async def test_recovery_is_also_detected_on_the_wrap_path(client, pool):
    pool.source_available = False
    await _state(client)
    pool.source_available = True
    await _wrap(client, "v1", b"dek")
    assert (await _state(client))["recovery_pending"] is True


# --- startup ----------------------------------------------------------------


async def test_lifespan_runs_and_cancels_the_pool_background_tasks(app, pool):
    async with app.router.lifespan_context(app):
        assert pool.background_started == 1
    await asyncio.sleep(0)
    assert pool.tasks[0].cancelled()


# --- verifiers --------------------------------------------------------------


def test_static_token_verifier_accepts_only_its_token():
    verifier = StaticTokenVerifier("s3cret")
    assert verifier.verify("s3cret")["sub"] == "static"
    with pytest.raises(PermissionError):
        verifier.verify("wrong")


def _google_claims(**overrides) -> dict:
    claims = {
        "iss": "https://accounts.google.com",
        "email": "service-1@gcp-sa-ekms.iam.gserviceaccount.com",
        "email_verified": True,
        "aud": "ekm-audience",
    }
    claims.update(overrides)
    return claims


@pytest.fixture
def verify_token(monkeypatch):
    box = {"claims": _google_claims(), "error": None}

    def fake(token, request, audience=None):
        if box["error"] is not None:
            raise box["error"]
        return box["claims"]

    monkeypatch.setattr("qkd_ekm.common.auth.id_token.verify_token", fake)
    return box


def _verifier() -> GoogleJwtVerifier:
    return GoogleJwtVerifier(
        allowed_emails={"service-1@gcp-sa-ekms.iam.gserviceaccount.com"},
        audiences={"ekm-audience"},
    )


def test_google_verifier_accepts_valid_claims(verify_token):
    assert _verifier().verify("tok")["email"].startswith("service-1@")


def test_google_verifier_rejects_bad_signature_as_unauthenticated(verify_token):
    verify_token["error"] = ValueError("Token expired")
    with pytest.raises(AuthError, match="invalid token") as excinfo:
        _verifier().verify("tok")
    assert excinfo.value.unauthenticated is True


@pytest.mark.parametrize(
    "claims",
    [
        _google_claims(iss="evil.example.com"),
        _google_claims(email="attacker@example.com"),
        _google_claims(aud="someone-else"),
    ],
)
def test_google_verifier_claim_rejections_are_permission_denied(verify_token, claims):
    verify_token["claims"] = claims
    with pytest.raises(AuthError) as excinfo:
        _verifier().verify("tok")
    assert excinfo.value.unauthenticated is False


def test_google_verifier_rejects_wrong_issuer(verify_token):
    verify_token["claims"] = _google_claims(iss="evil.example.com")
    with pytest.raises(PermissionError, match="issuer"):
        _verifier().verify("tok")


def test_google_verifier_rejects_unverified_email(verify_token):
    verify_token["claims"] = _google_claims()
    del verify_token["claims"]["email_verified"]
    with pytest.raises(PermissionError, match="email not verified"):
        _verifier().verify("tok")


def test_google_verifier_rejects_unknown_email(verify_token):
    verify_token["claims"] = _google_claims(email="attacker@example.com")
    with pytest.raises(PermissionError, match="email not allowed"):
        _verifier().verify("tok")


def test_google_verifier_rejects_wrong_audience(verify_token):
    verify_token["claims"] = _google_claims(aud="someone-else")
    with pytest.raises(PermissionError, match="audience"):
        _verifier().verify("tok")


def test_google_verifier_skips_audience_check_when_unset(verify_token):
    verify_token["claims"] = _google_claims(aud="anything")
    verifier = GoogleJwtVerifier(
        allowed_emails={"service-1@gcp-sa-ekms.iam.gserviceaccount.com"}, audiences=None
    )
    assert verifier.verify("tok")["aud"] == "anything"


def test_google_verifier_logs_claims_but_never_the_token(verify_token, caplog):
    verify_token["claims"] = _google_claims(email="attacker@example.com")
    with caplog.at_level(logging.WARNING, logger="EKM"), pytest.raises(PermissionError):
        _verifier().verify("super-secret-token")
    message = caplog.records[-1].getMessage()
    assert message == (
        "JWT rejected: email not allowed aud=ekm-audience email=attacker@example.com"
    )
    assert "super-secret-token" not in message


# --- rotate -----------------------------------------------------------------

KMS_KEY = "projects/p/locations/global/keyRings/r/cryptoKeys/ekm-key"


class FakeKmsClient:
    """Minimal stand-in for KeyManagementServiceClient (no network)."""

    def __init__(self, versions=(1, 2), states=("ENABLED",), assigns=None):
        self.versions = list(versions)
        self.states = list(states)
        self.assigns = assigns  # id KMS hands back, when not max+1
        self.created = None
        self.primary = None
        self.get_calls = 0

    def list_crypto_key_versions(self, parent):
        assert parent == KMS_KEY
        return [_FakeVersion(f"{parent}/cryptoKeyVersions/{n}", "ENABLED") for n in self.versions]

    def create_crypto_key_version(self, parent, crypto_key_version):
        self.created = crypto_key_version
        n = self.assigns if self.assigns is not None else max(self.versions) + 1
        return _FakeVersion(f"{parent}/cryptoKeyVersions/{n}", self.states[0])

    def get_crypto_key_version(self, name):
        self.get_calls += 1
        state = self.states[min(self.get_calls, len(self.states) - 1)]
        return _FakeVersion(name, state)

    def update_crypto_key_primary_version(self, name, crypto_key_version_id):
        self.primary = (name, crypto_key_version_id)
        return _FakeVersion(f"{name}/cryptoKeyVersions/{crypto_key_version_id}", "ENABLED")


class _FakeVersion:
    def __init__(self, name, state):
        self.name = name
        self.state = state


@pytest.fixture
def rotate_env(monkeypatch):
    monkeypatch.setenv("KMS_KEY", KMS_KEY)
    monkeypatch.delenv("EKM_KEY_PATH_PREFIX", raising=False)
    monkeypatch.setattr("qkd_ekm.ekm.rotate._POLL_INTERVAL_S", 0)


def test_rotate_creates_next_version_with_the_key_path(rotate_env):
    from qkd_ekm.ekm import rotate

    client = FakeKmsClient(versions=(1, 2))
    assert rotate.rotate(client=client) == "v3"
    assert rotate.main(client=FakeKmsClient(versions=(1, 2))) == 0  # console entry: exit code, not label
    options = client.created.external_protection_level_options
    assert options.ekm_connection_key_path == "api/keys/v3"
    assert client.primary == (KMS_KEY, "3")


def test_rotate_honours_a_custom_key_path_prefix(rotate_env, monkeypatch):
    from qkd_ekm.ekm import rotate

    monkeypatch.setenv("EKM_KEY_PATH_PREFIX", "api/ekm/")
    client = FakeKmsClient(versions=(7,))
    rotate.main(client=client)
    assert client.created.external_protection_level_options.ekm_connection_key_path == "api/ekm/v8"


def test_rotate_waits_for_the_version_to_become_enabled(rotate_env):
    from qkd_ekm.ekm import rotate

    client = FakeKmsClient(versions=(1,), states=("PENDING_GENERATION", "ENABLED"))
    rotate.main(client=client)
    assert client.get_calls == 1
    assert client.primary == (KMS_KEY, "2")


def test_rotate_raises_when_the_version_never_enables(rotate_env):
    from qkd_ekm.ekm import rotate

    client = FakeKmsClient(versions=(1,), states=("PENDING_GENERATION",))
    with pytest.raises(RuntimeError, match="did not become ENABLED"):
        rotate.main(client=client)
    assert client.primary is None


def test_rotate_logs_the_new_version(rotate_env, caplog):
    from qkd_ekm.ekm import rotate

    with caplog.at_level(logging.INFO, logger="EKM"):
        rotate.main(client=FakeKmsClient(versions=(1, 2)))
    assert "Rotated external key to version v3" in [r.getMessage() for r in caplog.records]


# --- no reuse / entry point -------------------------------------------------


class ReplayPool(FakePool):
    """Hands out the same key twice -- the store must refuse the second bind."""

    def allocate(self, peer: str) -> Key:
        return self._mint(peer, 1)[0]


async def test_rebinding_a_spent_key_fails_closed(store):
    app = create_app(
        store=store,
        pool=ReplayPool(store),
        verifier_kms=FakeVerifier(KMS_TOKEN),
        verifier_vpn=FakeVerifier(VPN_TOKEN),
        settings=EkmSettings(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ekm") as c:
        assert (await c.get("/api/QKD2/new", headers=VPN_AUTH)).status_code == 200
        resp = await c.get("/api/QKD2/new", headers=VPN_AUTH)
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "FAILED_PRECONDITION"
    assert store.count() == 1


def test_build_app_from_env_creates_parent_dirs_and_routes(tmp_path, monkeypatch):
    from qkd_ekm.ekm.__main__ import build_app

    monkeypatch.setenv("EKM_DB", str(tmp_path / "state" / "ekm.db"))
    monkeypatch.setenv("EKM_LOCAL_KEY_FILE", str(tmp_path / "state" / "local.key"))
    monkeypatch.setenv("QKD1_URL", "https://qkd1.invalid:8200")
    monkeypatch.setenv("EKMS_SA_EMAIL", "service-1@gcp-sa-ekms.iam.gserviceaccount.com")
    monkeypatch.setenv("VPN_TOKEN", "vpn-token")
    monkeypatch.setenv("EKM_JWT_AUDIENCES", "")
    monkeypatch.setenv("EKM_PEERS", "QKD2")

    app = build_app()
    paths = {route.path for route in app.routes}
    assert "/api/keys/{key_id_op:path}" in paths
    assert "/api/{peer}/new" in paths
    assert (tmp_path / "state" / "local.key").exists()


# --- fix round 1: auth mapping, envelopes, TLS, strict base64 ----------------


async def _app_client(store, pool, verifier_kms=None, verifier_vpn=None, **transport_kwargs):
    app = create_app(
        store=store,
        pool=pool,
        verifier_kms=verifier_kms or FakeVerifier(KMS_TOKEN),
        verifier_vpn=verifier_vpn or FakeVerifier(VPN_TOKEN),
        settings=EkmSettings(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, **transport_kwargs), base_url="http://ekm"
    )


async def test_unauthenticated_auth_error_is_401_not_403(store, pool):
    verifier = FakeVerifier(KMS_TOKEN, error="invalid token: ValueError", unauthenticated=True)
    async with await _app_client(store, pool, verifier_kms=verifier) as c:
        resp = await c.post("/api/keys/v1:wrap", json={"plaintext": b64e(b"d")}, headers=KMS_AUTH)
    assert resp.status_code == 401
    assert resp.json() == {
        "error": {
            "code": 401,
            "message": "invalid token: ValueError",
            "status": "UNAUTHENTICATED",
        }
    }


def test_static_token_verifier_rejects_non_ascii_without_raising_typeerror():
    with pytest.raises(AuthError):
        StaticTokenVerifier("s3cret").verify("tökén-שלום")


async def test_non_ascii_bearer_is_rejected_not_a_500(store, pool):
    async with await _app_client(store, pool, verifier_vpn=StaticTokenVerifier("vpn")) as c:
        # bytes: httpx (like HTTP itself) refuses non-ascii str header values,
        # but a raw non-ascii byte on the wire reaches the app as latin-1 text.
        resp = await c.get("/api/QKD2/new", headers={"Authorization": "Bearer tökén-שלום".encode()})
    assert resp.status_code == 403
    assert resp.json()["error"]["status"] == "PERMISSION_DENIED"


async def test_unknown_path_is_404_in_the_error_envelope(client):
    resp = await client.get("/nope")
    assert resp.status_code == 404
    assert resp.json()["error"] == {"code": 404, "message": "Not Found", "status": "NOT_FOUND"}


async def test_request_validation_error_is_400_invalid_argument(client):
    resp = await client.post(
        "/api/authority", json={"continuity_authority": "maybe"}, headers=VPN_AUTH
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"


async def test_auth_is_checked_before_body_validation(client):
    resp = await client.post("/api/authority", json={"continuity_authority": "maybe"})
    assert resp.status_code == 401


async def test_unhandled_exception_is_500_internal_without_details(store, caplog):
    class BrokenPool(FakePool):
        def size(self, peer: str) -> int:
            raise RuntimeError("secret internal detail")

    async with await _app_client(store, BrokenPool(store), raise_app_exceptions=False) as c:
        with caplog.at_level(logging.ERROR, logger="EKM"):
            resp = await c.get("/api/state", headers=VPN_AUTH)
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {"code": 500, "message": "internal error", "status": "INTERNAL"}
    }
    assert "secret internal detail" not in resp.text
    assert caplog.records[-1].getMessage() == "Unhandled RuntimeError on /api/state"


async def test_wrap_rejects_base64_with_stray_characters(client):
    resp = await client.post("/api/keys/v1:wrap", json={"plaintext": "AAA!A"}, headers=KMS_AUTH)
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"


def test_ssl_options_requires_both_cert_and_key(monkeypatch):
    from qkd_ekm.ekm.__main__ import main, ssl_options

    monkeypatch.delenv("EKM_TLS_CERT", raising=False)
    monkeypatch.setenv("EKM_TLS_KEY", "/etc/ssl/ekm.key")
    monkeypatch.delenv("EKM_PLAINTEXT_HTTP", raising=False)
    with pytest.raises(RuntimeError, match="EKM_TLS_CERT and EKM_TLS_KEY are required"):
        ssl_options()
    with pytest.raises(RuntimeError, match="EKM_TLS_CERT and EKM_TLS_KEY are required"):
        main()  # refuses to start before touching the database or a socket


def test_ssl_options_returns_uvicorn_kwargs(monkeypatch):
    from qkd_ekm.ekm.__main__ import ssl_options

    monkeypatch.setenv("EKM_TLS_CERT", "/etc/ssl/ekm.crt")
    monkeypatch.setenv("EKM_TLS_KEY", "/etc/ssl/ekm.key")
    assert ssl_options() == {"ssl_certfile": "/etc/ssl/ekm.crt", "ssl_keyfile": "/etc/ssl/ekm.key"}


def test_plaintext_http_needs_an_explicit_opt_in_and_warns(monkeypatch, caplog):
    from qkd_ekm.ekm.__main__ import ssl_options

    monkeypatch.delenv("EKM_TLS_CERT", raising=False)
    monkeypatch.delenv("EKM_TLS_KEY", raising=False)
    monkeypatch.setenv("EKM_PLAINTEXT_HTTP", "1")
    with caplog.at_level(logging.WARNING, logger="EKM"):
        assert ssl_options() == {}
    assert caplog.records[-1].getMessage() == "WARNING serving plaintext HTTP"


def test_rotate_promotes_the_id_kms_actually_assigned(rotate_env):
    from qkd_ekm.ekm import rotate

    client = FakeKmsClient(versions=(1, 2), assigns=9)
    assert rotate.rotate(client=client) == "v3"
    assert rotate.main(client=FakeKmsClient(versions=(1, 2))) == 0  # console entry: exit code, not label
    assert client.created.external_protection_level_options.ekm_connection_key_path == "api/keys/v3"
    assert client.primary == (KMS_KEY, "9")


# --- first-use binding is recoverable ---------------------------------------


class FlakyKekStore:
    """Store proxy whose first `put_kek` fails, like a full disk."""

    def __init__(self, store: Store):
        self._store = store
        self.fail_next_put = True

    def put_kek(self, object_id: str, qkd_key_id: str, material: bytes) -> None:
        if self.fail_next_put:
            self.fail_next_put = False
            raise RuntimeError("disk full")
        self._store.put_kek(object_id, qkd_key_id, material)

    def __getattr__(self, name):
        return getattr(self._store, name)


async def test_a_failed_kek_write_leaves_the_key_id_wrappable_on_retry(store, pool):
    flaky = FlakyKekStore(store)

    async with await _app_client(flaky, pool, raise_app_exceptions=False) as c:
        first = await _wrap(c, "v1", b"dek-material-0123")
        assert first.status_code == 500

        retry = await _wrap(c, "v1", b"dek-material-0123")
        assert retry.status_code == 200
        back = await _unwrap(c, "v1", retry.json()["wrappedBlob"])

    assert b64d(back.json()["plaintext"]) == b"dek-material-0123"
    # The first attempt's QKD unit was popped from the pool and never bound, so
    # exactly one binding exists and it names the unit that survived.
    assert store.count() == 1
    assert store.lookup_object("ekm", "v1") is not None


# --- KMS token audience disclosure ------------------------------------------


async def test_the_first_accepted_kms_token_logs_its_audience_once(store, pool, caplog):
    class AudVerifier(FakeVerifier):
        def verify(self, token: str) -> dict:
            return {**super().verify(token), "aud": "ekm-audience"}

    async with await _app_client(store, pool, verifier_kms=AudVerifier(KMS_TOKEN)) as c:
        with caplog.at_level(logging.INFO, logger="EKM"):
            await _wrap(c, "v1", b"dek-material-0123")
            await _wrap(c, "v2", b"dek-material-0123")

    announcements = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Accepted KMS token")
    ]
    assert announcements == [
        "Accepted KMS token aud=ekm-audience email=caller@example.com"
    ]


# --- GET on the key path (Task-13 insurance) --------------------------------


async def test_get_on_a_key_path_describes_the_key(client):
    resp = await client.get("/api/keys/v1", headers=KMS_AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"name": "v1", "keyManagementMode": "MANUAL"}


async def test_get_on_an_operation_suffix_is_404(client):
    resp = await client.get("/api/keys/v1:wrap", headers=KMS_AUTH)

    assert resp.status_code == 404


async def test_get_on_a_key_path_needs_the_kms_token(client):
    assert (await client.get("/api/keys/v1")).status_code == 401
    assert (await client.get("/api/keys/v1", headers=VPN_AUTH)).status_code == 403


def test_google_verifier_logs_under_the_configured_component(monkeypatch, capsys):
    from qkd_ekm.common import auth as auth_mod

    monkeypatch.setattr(
        auth_mod.id_token, "verify_token", lambda *a, **k: {"iss": "accounts.google.com"}
    )
    verifier = auth_mod.GoogleJwtVerifier({"x@example.com"}, component="VPNServer")
    with pytest.raises(PermissionError):
        verifier.verify("tok")
    assert "VPNServer: JWT rejected" in capsys.readouterr().out
