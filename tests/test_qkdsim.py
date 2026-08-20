import base64
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from qkd_ekm.qkd.etsi014 import Etsi014Client, QkdError, QkdUnavailable
from qkd_ekm.qkd.heqa import HeqaMonitor
from qkd_ekm.qkdsim.app import create_app
from qkd_ekm.qkdsim.store import KeyStore

# --- fixtures -----------------------------------------------------------


@pytest.fixture
def store():
    return KeyStore(seed=1)


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.delenv("SIM_TOKEN", raising=False)
    monkeypatch.setenv("SIM_SAES", "QKD1,QKD2")
    return create_app(store)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as c:
        yield c


async def _login(client) -> str:
    resp = await client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200
    return resp.json()["token"]


# --- ETSI-014 key delivery ------------------------------------------------


async def test_enc_keys_on_qkd1_for_slave_qkd2_returns_one_key(client):
    resp = await client.get("/api/v1/keys/QKD2/enc_keys")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert len(keys) == 1
    assert len(base64.b64decode(keys[0]["key"])) == 32
    assert keys[0]["key_ID"]


async def test_dec_keys_on_qkd2_returns_identical_bytes(client):
    enc = await client.get("/api/v1/keys/QKD2/enc_keys")
    issued = enc.json()["keys"][0]

    dec = await client.get("/api/v1/keys/QKD1/dec_keys", params={"key_ID": issued["key_ID"]})
    assert dec.status_code == 200
    got = dec.json()["keys"][0]
    assert got["key_ID"] == issued["key_ID"]
    assert got["key"] == issued["key"]


async def test_dec_keys_second_fetch_of_same_id_is_400_consumed(client):
    enc = await client.get("/api/v1/keys/QKD2/enc_keys")
    key_id = enc.json()["keys"][0]["key_ID"]

    first = await client.get("/api/v1/keys/QKD1/dec_keys", params={"key_ID": key_id})
    assert first.status_code == 200

    second = await client.get("/api/v1/keys/QKD1/dec_keys", params={"key_ID": key_id})
    assert second.status_code == 400
    assert second.json() == {"message": "key already consumed"}


async def test_dec_keys_unknown_id_is_404(client):
    resp = await client.get("/api/v1/keys/QKD1/dec_keys", params={"key_ID": "nope"})
    assert resp.status_code == 404
    assert resp.json() == {"message": "unknown key_ID"}


async def test_dec_keys_multi_post(client):
    enc = await client.get("/api/v1/keys/QKD2/enc_keys", params={"number": 2})
    ids = [k["key_ID"] for k in enc.json()["keys"]]

    resp = await client.post(
        "/api/v1/keys/QKD1/dec_keys",
        json={"key_IDs": [{"key_ID": kid} for kid in ids]},
    )
    assert resp.status_code == 200
    assert [k["key_ID"] for k in resp.json()["keys"]] == ids


@pytest.mark.parametrize("number", ["abc", "", "0", "-1", "101"])
async def test_enc_keys_rejects_a_number_the_status_block_does_not_advertise(client, number):
    resp = await client.get("/api/v1/keys/QKD2/enc_keys", params={"number": number})

    assert resp.status_code == 400
    assert resp.json() == {"message": "invalid number"}


async def test_enc_keys_accepts_the_advertised_maximum(client):
    status = await client.get("/api/v1/keys/QKD2/status")
    maximum = status.json()["max_key_per_request"]

    resp = await client.get("/api/v1/keys/QKD2/enc_keys", params={"number": maximum})

    assert resp.status_code == 200
    assert len(resp.json()["keys"]) == maximum


async def test_unknown_sae_is_404_on_status_enc_and_dec(client):
    assert (await client.get("/api/v1/keys/QKD9/status")).status_code == 404
    assert (await client.get("/api/v1/keys/QKD9/enc_keys")).status_code == 404
    assert (await client.get("/api/v1/keys/QKD9/dec_keys", params={"key_ID": "x"})).status_code == 404


async def test_source_unavailable_503_enc_keys_but_dec_keys_still_ok(client):
    enc = await client.get("/api/v1/keys/QKD2/enc_keys")
    key_id = enc.json()["keys"][0]["key_ID"]

    src = await client.post("/sim/source", json={"available": False})
    assert src.status_code == 200
    assert src.json() == {"source_available": False}

    blocked = await client.get("/api/v1/keys/QKD2/enc_keys")
    assert blocked.status_code == 503
    assert blocked.json() == {"message": "source unavailable"}

    dec = await client.get("/api/v1/keys/QKD1/dec_keys", params={"key_ID": key_id})
    assert dec.status_code == 200


async def test_sim_state_counters_increment(client):
    before = (await client.get("/sim/state")).json()
    enc = await client.get("/api/v1/keys/QKD2/enc_keys")
    key_id = enc.json()["keys"][0]["key_ID"]
    await client.get("/api/v1/keys/QKD1/dec_keys", params={"key_ID": key_id})

    after = (await client.get("/sim/state")).json()
    assert after["consumed_keys"] == before["consumed_keys"] + 1
    assert after["consumed_bits"] == before["consumed_bits"] + 256
    assert after["key_requests"] == before["key_requests"] + 1
    assert after["deleted_bits"] == before["deleted_bits"] + 256
    assert after["available_256bit_keys"] == before["available_256bit_keys"] - 1


async def test_status_has_key_size_and_max_key_per_request(client):
    resp = await client.get("/api/v1/keys/QKD2/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_size"] == 256
    assert body["max_key_per_request"] == 100
    assert body["max_key_size"] == 8192
    assert body["min_key_size"] == 8
    assert body["slave_SAE_ID"] == "QKD2"
    ext = body["status_extension"]
    assert set(ext) == {
        "current_bits",
        "maximum_key_length",
        "num_of_256_keys_available",
        "bits_consumed_from_the_beginning",
        "keys_consumed_from_the_beginning",
        "key_requests_from_the_beginning",
        "num_failed_key_requests_from_the_beginning",
        "total_generated_bits",
        "deleted_bits",
    }


# --- auth ------------------------------------------------------------------


async def test_login_and_monitoring_returns_numeric_values(client):
    token = await _login(client)
    headers = {"Authorization": f"Bearer {token}"}

    rate = await client.get("/monitoring/qkd-qtx/secure-bit-rate/current", headers=headers)
    assert rate.status_code == 200
    assert rate.json()["value"] == pytest.approx(21220.0)

    sig = await client.get("/monitoring/qkd-qtx/signal-qber/current", headers=headers)
    assert sig.json()["value"] == pytest.approx(0.80)

    decoy = await client.get("/monitoring/qkd-qtx/decoy-qber/current", headers=headers)
    assert decoy.json()["value"] == pytest.approx(1.00)

    sig_states = await client.get("/monitoring/qkd-qtx/signal-states-qber/current", headers=headers)
    assert sig_states.json()["value"] == [0.19, 1.04, 1.45, 0.27, 0.83, 0.69]

    decoy_states = await client.get("/monitoring/qkd-qtx/decoy-states-qber/current", headers=headers)
    assert decoy_states.json()["value"] == [0.48, 1.53, 1.65, 0.69, 1.20, 0.48]


async def test_login_rejects_bad_credentials(client):
    resp = await client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


async def test_monitoring_requires_bearer(client):
    resp = await client.get("/monitoring/qkd-qtx/secure-bit-rate/current")
    assert resp.status_code == 401


async def test_kms_key_servers_names_saes(client):
    token = await _login(client)
    resp = await client.get(
        "/kms/key-servers", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    servers = resp.json()
    assert servers[0]["masterSAE"] == "QKD1"
    assert servers[0]["slaveSAE"] == "QKD2"


async def test_healthz_is_open(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200


async def test_etsi_routes_require_sim_token_when_set(store, monkeypatch):
    monkeypatch.setenv("SIM_SAES", "QKD1,QKD2")
    monkeypatch.setenv("SIM_TOKEN", "s3cr3t")
    app = create_app(store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as c:
        unauth = await c.get("/api/v1/keys/QKD2/enc_keys")
        assert unauth.status_code == 401

        ok = await c.get(
            "/api/v1/keys/QKD2/enc_keys", headers={"Authorization": "Bearer s3cr3t"}
        )
        assert ok.status_code == 200

        sim_unauth = await c.get("/sim/state")
        assert sim_unauth.status_code == 401
        sim_ok = await c.get("/sim/state", headers={"Authorization": "Bearer s3cr3t"})
        assert sim_ok.status_code == 200


async def test_log_line_reports_count_and_direction_no_material(client, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="QKDSim"):
        await client.get("/api/v1/keys/QKD2/enc_keys")
    assert caplog.records[-1].name == "QKDSim"
    message = caplog.records[-1].getMessage()
    assert message == "Issued 1 key(s) for QKD1->QKD2"


# --- live-server integration: real Etsi014Client + HeqaMonitor -------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.delenv("SIM_TOKEN", raising=False)
    monkeypatch.setenv("SIM_SAES", "QKD1,QKD2")
    port = _free_port()
    live_store = KeyStore(seed=7)
    live_app = create_app(live_store)
    config = uvicorn.Config(live_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_real_clients_round_trip_against_live_server(live_server):
    etsi = Etsi014Client(live_server)
    try:
        keys = etsi.enc_keys("QKD2", number=1)
        assert len(keys) == 1
        assert len(keys[0].key) == 32

        got = etsi.dec_keys("QKD1", [keys[0].key_id])
        assert got[0].key == keys[0].key

        with pytest.raises(QkdError):
            etsi.dec_keys("QKD1", [keys[0].key_id])
    finally:
        etsi.close()

    monitor = HeqaMonitor(live_server, "admin", "admin")
    try:
        result = monitor.capture()
        assert result["secure_bit_rate"] == pytest.approx(21220.0)
        assert result["signal_qber"] == pytest.approx(0.80)
        assert result["available_256bit_keys"] is not None
        assert result["consumed_keys"] >= 1
    finally:
        monitor.close()


def test_live_server_source_unavailable_raises_unavailable_for_enc_keys(live_server):
    control = httpx.Client(base_url=live_server)
    control.post("/sim/source", json={"available": False})
    control.close()

    etsi = Etsi014Client(live_server)
    try:
        with pytest.raises(QkdUnavailable):
            etsi.enc_keys("QKD2")
    finally:
        etsi.close()
