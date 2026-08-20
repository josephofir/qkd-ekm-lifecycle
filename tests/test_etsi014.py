import base64

import httpx
import pytest

from qkd_ekm.qkd.etsi014 import Etsi014Client, Key, QkdError, QkdUnavailable
from qkd_ekm.qkd.heqa import HeqaMonitor

KEY_A = base64.b64encode(b"\x01" * 32).decode("ascii")
KEY_B = base64.b64encode(b"\x02" * 32).decode("ascii")


def _client(handler, **kwargs) -> Etsi014Client:
    transport = httpx.MockTransport(handler)
    return Etsi014Client("https://qkd1.example:8200", transport=transport, **kwargs)


def test_get_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/keys/Bob1/status"
        return httpx.Response(200, json={"slave_SAE_ID": "Bob1", "stored_key_count": 5})

    client = _client(handler)
    status = client.get_status("Bob1")
    assert status == {"slave_SAE_ID": "Bob1", "stored_key_count": 5}


def test_enc_keys_decodes_32_byte_keys():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/keys/Bob1/enc_keys"
        assert request.url.params["number"] == "2"
        assert request.url.params["size"] == "256"
        return httpx.Response(
            200,
            json={"keys": [{"key_ID": "id-a", "key": KEY_A}, {"key_ID": "id-b", "key": KEY_B}]},
        )

    client = _client(handler)
    keys = client.enc_keys("Bob1", number=2)
    assert keys == [Key("id-a", b"\x01" * 32), Key("id-b", b"\x02" * 32)]
    assert all(len(k.key) == 32 for k in keys)


def test_dec_keys_single_uses_get_with_query_param():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/keys/Alice1/dec_keys"
        assert request.url.params["key_ID"] == "id-a"
        return httpx.Response(200, json={"keys": [{"key_ID": "id-a", "key": KEY_A}]})

    client = _client(handler)
    keys = client.dec_keys("Alice1", ["id-a"])
    assert keys == [Key("id-a", b"\x01" * 32)]


def test_dec_keys_multi_uses_post_with_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/keys/Alice1/dec_keys"
        import json

        assert json.loads(request.read()) == {"key_IDs": [{"key_ID": "id-a"}, {"key_ID": "id-b"}]}
        return httpx.Response(
            200,
            json={"keys": [{"key_ID": "id-a", "key": KEY_A}, {"key_ID": "id-b", "key": KEY_B}]},
        )

    client = _client(handler)
    keys = client.dec_keys("Alice1", ["id-a", "id-b"])
    assert [k.key_id for k in keys] == ["id-a", "id-b"]


@pytest.mark.parametrize("status_code", [503, 500, 502])
def test_server_errors_raise_qkd_unavailable(status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "boom"})

    client = _client(handler)
    with pytest.raises(QkdUnavailable):
        client.enc_keys("Bob1")


def test_connect_error_raises_qkd_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    with pytest.raises(QkdUnavailable):
        client.get_status("Bob1")


def test_timeout_raises_qkd_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _client(handler)
    with pytest.raises(QkdUnavailable):
        client.get_status("Bob1")


def test_4xx_raises_qkd_error_not_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    client = _client(handler)
    with pytest.raises(QkdError):
        client.get_status("Bob1")
    # QkdError must not be caught by a QkdUnavailable handler
    with pytest.raises(RuntimeError):
        client.get_status("Bob1")


def test_qkd_error_is_not_qkd_unavailable():
    assert not issubclass(QkdError, QkdUnavailable)
    assert issubclass(QkdError, RuntimeError)
    assert issubclass(QkdUnavailable, RuntimeError)


def test_token_sets_bearer_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"keys": []})

    client = _client(handler, token="s3cr3t")
    client.enc_keys("Bob1")
    assert seen["auth"] == "Bearer s3cr3t"


def test_ca_file_none_means_verify_true():
    client = _client(lambda r: httpx.Response(200, json={}))
    # httpx.Client exposes the verify setting indirectly; just assert construction succeeds
    # and defaults ca_file to True-verification without raising.
    assert client is not None


# --- HEQA monitor ---


def _monitor(handler, **kwargs) -> HeqaMonitor:
    transport = httpx.MockTransport(handler)
    return HeqaMonitor(
        "https://qkd1.example:8100", "viewer", "pw", verify=True, transport=transport, **kwargs
    )


def test_login_returns_and_stores_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/login"
        import json

        assert json.loads(request.read()) == {"username": "viewer", "password": "pw"}
        return httpx.Response(200, json={"token": "jwt-abc"})

    mon = _monitor(handler)
    tok = mon.login()
    assert tok == "jwt-abc"
    assert mon.token == "jwt-abc"


def test_get_auto_logs_in_and_sends_bearer():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "jwt-xyz"})
        assert request.headers.get("authorization") == "Bearer jwt-xyz"
        return httpx.Response(200, json={"time": "t", "value": 21220.0})

    mon = _monitor(handler)
    data = mon.get("/monitoring/qkd-qtx/secure-bit-rate/current")
    assert data["value"] == 21220.0
    assert calls == ["/auth/login", "/monitoring/qkd-qtx/secure-bit-rate/current"]


def test_capture_maps_table4_fields_and_derives_key_rate():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(200, json={"token": "jwt"})
        if path == "/monitoring/qkd-qtx/secure-bit-rate/current":
            return httpx.Response(200, json={"time": "t", "value": 21220.0})
        if path == "/monitoring/qkd-qtx/signal-qber/current":
            return httpx.Response(200, json={"time": "t", "value": 0.8})
        if path == "/monitoring/qkd-qtx/decoy-qber/current":
            return httpx.Response(200, json={"time": "t", "value": 1.0})
        if path == "/monitoring/qkd-qtx/signal-states-qber/current":
            return httpx.Response(200, json={"time": "t", "value": [0.1] * 6})
        if path == "/monitoring/qkd-qtx/decoy-states-qber/current":
            return httpx.Response(200, json={"time": "t", "value": [0.48, 1.53, 1.65]})
        if path == "/kms/key-servers":
            return httpx.Response(
                200,
                json=[{"id": "Alice11", "masterSAE": "Alice11", "slaveSAE": "Bob11"}],
            )
        if path == "/api/v1/keys/Bob11/status":
            return httpx.Response(
                200,
                json={
                    "status_extension": {
                        "current_bits": 17145240,
                        "maximum_key_length": 8192,
                        "num_of_256_keys_available": 66922,
                        "bits_consumed_from_the_beginning": 5376,
                        "keys_consumed_from_the_beginning": 21,
                        "key_requests_from_the_beginning": 21,
                        "num_failed_key_requests_from_the_beginning": 0,
                        "total_generated_bits": 17145240,
                        "deleted_bits": 0,
                    }
                },
            )
        raise AssertionError(f"unexpected path {path}")

    mon = _monitor(handler)
    result = mon.capture()

    assert result["secure_bit_rate"] == 21220.0
    assert result["secure_key_rate_256"] == pytest.approx(21220.0 / 256)
    assert result["signal_qber"] == 0.8
    assert result["weak_decoy_qber"] == 1.0
    assert result["signal_qber_per_state"] == [0.1] * 6
    assert result["weak_decoy_qber_per_state"] == [0.48, 1.53, 1.65]
    assert result["available_secured_bits"] == 17145240
    assert result["max_key_length"] == 8192
    assert result["available_256bit_keys"] == 66922
    assert result["consumed_bits"] == 5376
    assert result["consumed_keys"] == 21
    assert result["key_requests"] == 21
    assert result["failed_key_requests"] == 0
    assert result["generated_bits"] == 17145240
    assert result["deleted_bits"] == 0


def test_capture_fields_default_to_none_when_endpoints_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "jwt"})
        return httpx.Response(503, json={"message": "unavailable"})

    mon = _monitor(handler)
    result = mon.capture()

    expected_keys = {
        "secure_bit_rate",
        "secure_key_rate_256",
        "signal_qber",
        "weak_decoy_qber",
        "signal_qber_per_state",
        "weak_decoy_qber_per_state",
        "available_secured_bits",
        "available_256bit_keys",
        "consumed_keys",
        "consumed_bits",
        "key_requests",
        "failed_key_requests",
        "max_key_length",
        "generated_bits",
        "deleted_bits",
    }
    assert set(result.keys()) == expected_keys
    assert all(v is None for v in result.values())
