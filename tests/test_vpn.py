import asyncio
import datetime as dt
import json
import logging
import os
import subprocess

import httpx
import pytest

from qkd_ekm.common.crypto import b64e
from qkd_ekm.vpn import __main__ as vpn_main
from qkd_ekm.vpn.app import EkmClient, VpnSettings, create_app
from qkd_ekm.vpn.wg import WG, ensure_private_key

CLIENT_PUB = b64e(bytes(range(32)))  # a real WireGuard-shaped key: 32 bytes, 44 chars
OTHER_PUB = b64e(bytes(range(32, 64)))
AUTH = {"Authorization": "Bearer id-token"}
NOW = dt.datetime(2026, 8, 19, 12, 0, 0, tzinfo=dt.UTC)
ROUTE_SHOW = "default via 10.10.0.1 dev ens4 proto dhcp src 10.10.0.5 metric 100\n"


# --- fakes ------------------------------------------------------------------


class FakeRunner:
    """subprocess.run stand-in: records argv, replays canned results."""

    def __init__(self, iface_exists: bool = True, masquerade_present: bool = False):
        self.commands: list[list[str]] = []
        self.psk_files: list[tuple[str, str]] = []
        self.iface_exists = iface_exists
        self.masquerade_present = masquerade_present
        self.set_peer_fails = False

    def __call__(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        cmd = list(cmd)
        self.commands.append(cmd)
        if "preshared-key" in cmd:
            path = cmd[cmd.index("preshared-key") + 1]
            with open(path) as fh:
                self.psk_files.append((path, fh.read()))
            if self.set_peer_fails:
                return subprocess.CompletedProcess(cmd, 1, "", "wg: Unable to modify interface")
        return subprocess.CompletedProcess(cmd, *self._result(cmd))

    def _result(self, cmd) -> tuple[int, str]:
        if cmd[:3] == ["ip", "link", "show"]:
            return (0 if self.iface_exists else 1), ""
        if cmd[:4] == ["wg", "show", "wg0", "public-key"]:
            return 0, "SERVER-PUBLIC-KEY=\n"
        if cmd[:3] == ["wg", "show", "wg0"]:
            return 0, "interface: wg0\n"
        if cmd[:2] == ["ip", "-o"]:
            return 0, ROUTE_SHOW
        if cmd[:1] == ["iptables"] and "-C" in cmd:
            return (0 if self.masquerade_present else 1), ""
        if cmd[:2] == ["wg", "genkey"]:
            return 0, "GENERATED-PRIVATE-KEY=\n"
        return 0, ""

    def find(self, *prefix) -> list[list[str]]:
        return [c for c in self.commands if c[: len(prefix)] == list(prefix)]


class FakeVerifier:
    def __init__(self, token: str = "id-token", email: str = "op@example.com"):
        self.token, self.email = token, email

    def verify(self, token: str) -> dict:
        if token != self.token:
            raise PermissionError("email not allowed")
        return {"email": self.email}


class FakeSleep:
    """Awaitable that parks every sleeper until the test releases them."""

    def __init__(self):
        self.calls: list[float] = []
        self._gate = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        await self._gate.wait()

    def release(self) -> None:
        self._gate.set()


class FakeEkm:
    """EKM `GET /api/{peer}/new` served through an httpx MockTransport."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.available = True

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        peer = request.url.path.split("/")[2]
        self.calls.append((peer, request.url.params.get("purpose")))
        if not self.available:
            return httpx.Response(
                503,
                json={"error": {"code": 503, "message": "no QKD key available", "status": "UNAVAILABLE"}},
            )
        n = len(self.calls)
        return httpx.Response(
            200,
            json={"key_id": f"key-{n}", "key": b64e(bytes([n]) * 32), "qkd_name": peer},
        )

    def key_bytes(self, n: int) -> bytes:
        return bytes([n]) * 32


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def wg(runner):
    return WG(iface="wg0", runner=runner)


@pytest.fixture
def ekm():
    return FakeEkm()


@pytest.fixture
def settings(tmp_path):
    return VpnSettings(
        public_endpoint="203.0.113.7:51819",
        peers_file=str(tmp_path / "state" / "peers.json"),
    )


@pytest.fixture
def app(wg, ekm, settings):
    app = create_app(
        wg=wg,
        ekm_client=EkmClient("http://ekm.internal", "vpn-token", transport=ekm.transport()),
        verifier=FakeVerifier(),
        settings=settings,
    )
    app.state.clock = lambda: NOW
    app.state.sleep = FakeSleep()
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://vpn") as c:
        yield c


async def drain(app) -> None:
    """Let every scheduled timer fire, then wait for the work it triggers.

    Activation re-arms the peer's rotation timer, so draining spawns tasks;
    cancelled results are expected (re-arming cancels the previous timer).
    """
    app.state.sleep.release()
    for _ in range(5):
        if not app.state.tasks:
            return
        await asyncio.gather(*list(app.state.tasks), return_exceptions=True)


def _read_peers(settings) -> dict:
    with open(settings.peers_file) as fh:
        return json.load(fh)


async def start(client, public_key: str = CLIENT_PUB, qkd_id: str = "QKD2"):
    return await client.post(
        "/api/start_connection", json={"qkd_id": qkd_id, "public_key": public_key}, headers=AUTH
    )


async def refresh(client, public_key: str = CLIENT_PUB):
    return await client.post(
        "/api/refresh_connection", json={"public_key": public_key}, headers=AUTH
    )


# --- wg manager -------------------------------------------------------------


def test_ensure_interface_creates_the_link_when_it_is_missing():
    runner = FakeRunner(iface_exists=False)
    WG(iface="wg0", runner=runner).ensure_interface(
        "/etc/qkd-ekm/wg_private.key", 51819, "10.20.0.1/24"
    )
    assert runner.commands == [
        ["ip", "link", "show", "wg0"],
        ["ip", "link", "add", "wg0", "type", "wireguard"],
        ["wg", "set", "wg0", "listen-port", "51819", "private-key", "/etc/qkd-ekm/wg_private.key"],
        ["ip", "addr", "add", "10.20.0.1/24", "dev", "wg0"],
        ["ip", "link", "set", "wg0", "up"],
    ]


def test_ensure_interface_does_not_recreate_an_existing_link(wg, runner):
    wg.ensure_interface("/etc/k", 51819, "10.20.0.1/24")
    assert runner.find("ip", "link", "add") == []
    assert runner.find("ip", "link", "set") == [["ip", "link", "set", "wg0", "up"]]


def test_public_key_reads_it_from_wg_show(wg):
    assert wg.public_key() == "SERVER-PUBLIC-KEY="


def test_set_peer_passes_the_psk_in_a_temp_file_that_is_deleted_afterwards(wg, runner):
    psk = bytes(range(32))
    wg.set_peer("PEER-PUB=", psk, "10.20.0.2/32")

    path, content = runner.psk_files[0]
    assert content == b64e(psk)
    assert not os.path.exists(path)
    assert runner.commands[-1] == [
        "wg", "set", "wg0", "peer", "PEER-PUB=",
        "preshared-key", path, "allowed-ips", "10.20.0.2/32",
    ]  # fmt: skip


def test_set_peer_deletes_the_psk_file_even_when_wg_fails():
    seen = {}

    def failing(cmd, **kwargs):
        seen["path"] = cmd[cmd.index("preshared-key") + 1]
        return subprocess.CompletedProcess(cmd, 1, "", "wg: bad peer")

    with pytest.raises(RuntimeError, match="bad peer"):
        WG(iface="wg0", runner=failing).set_peer("PEER-PUB=", b"\x00" * 32, "10.20.0.2/32")
    assert not os.path.exists(seen["path"])


def test_remove_peer_and_show(wg, runner):
    wg.remove_peer("PEER-PUB=")
    assert runner.commands[-1] == ["wg", "set", "wg0", "peer", "PEER-PUB=", "remove"]
    assert wg.show() == "interface: wg0"


def test_ensure_masquerade_appends_the_rule_when_it_is_absent(wg, runner):
    wg.ensure_masquerade("10.20.0.0/24")
    rule = ["POSTROUTING", "-s", "10.20.0.0/24", "-o", "ens4", "-j", "MASQUERADE"]
    assert runner.commands[-2:] == [
        ["iptables", "-t", "nat", "-C", *rule],
        ["iptables", "-t", "nat", "-A", *rule],
    ]


def test_ensure_masquerade_is_a_noop_when_the_rule_is_present():
    runner = FakeRunner(masquerade_present=True)
    WG(runner=runner).ensure_masquerade("10.20.0.0/24")
    assert runner.find("iptables", "-t", "nat", "-A") == []


def test_ensure_ip_forward(wg, runner):
    wg.ensure_ip_forward()
    assert runner.commands[-1] == ["sysctl", "-w", "net.ipv4.ip_forward=1"]


def test_ensure_private_key_generates_a_0600_key_only_when_missing(tmp_path):
    runner = FakeRunner()
    path = str(tmp_path / "keys" / "wg_private.key")
    ensure_private_key(path, runner=runner)

    with open(path) as fh:
        assert fh.read() == "GENERATED-PRIVATE-KEY=\n"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    ensure_private_key(path, runner=runner)
    assert runner.find("wg", "genkey") == [["wg", "genkey"]]


# --- start_connection -------------------------------------------------------


async def test_start_connection_returns_the_interface_details(client, ekm):
    resp = await start(client)
    assert resp.status_code == 200
    assert resp.json() == {
        "qkd_id": "QKD1",
        "server_public_key": "SERVER-PUBLIC-KEY=",
        "endpoint": "203.0.113.7:51819",
        "preshared_key_id": "key-1",
        "client_ip": "10.20.0.2",
        "allowed_ips": "10.10.0.0/24,10.20.0.0/24",
        "effective_time": NOW.isoformat(),
        "refresh_after_s": 3600,
    }
    assert ekm.calls == [("QKD2", "vpn")]


async def test_start_connection_applies_the_qkd_key_as_the_peer_psk(client, runner, ekm):
    await start(client)
    assert runner.psk_files[0][1] == b64e(ekm.key_bytes(1))
    assert runner.find("wg", "set") == [[
        "wg", "set", "wg0", "peer", CLIENT_PUB,
        "preshared-key", runner.psk_files[0][0], "allowed-ips", "10.20.0.2/32",
    ]]  # fmt: skip
    assert not os.path.exists(runner.psk_files[0][0])


async def test_client_ip_is_stable_per_public_key_and_distinct_across_peers(client):
    first = (await start(client)).json()["client_ip"]
    again = (await start(client)).json()["client_ip"]
    other = (await start(client, OTHER_PUB)).json()["client_ip"]
    assert first == again == "10.20.0.2"
    assert other == "10.20.0.3"


async def test_client_ip_survives_a_restart_via_the_peers_file(client, settings, wg, ekm):
    await start(client, OTHER_PUB)
    assert _read_peers(settings)[OTHER_PUB]["client_ip"] == "10.20.0.2"

    restarted = create_app(
        wg=wg,
        ekm_client=EkmClient("http://ekm.internal", "vpn-token", transport=ekm.transport()),
        verifier=FakeVerifier(),
        settings=settings,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://vpn"
    ) as c:
        assert (await start(c, CLIENT_PUB)).json()["client_ip"] == "10.20.0.3"
        assert (await start(c, OTHER_PUB)).json()["client_ip"] == "10.20.0.2"


async def test_start_connection_logs_the_client_and_the_key_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="VPNServer"):
        await start(client)
    assert [r.getMessage() for r in caplog.records] == [
        "Client op@example.com initiated VPN connection",
        "Using existing vpn interface",
        "Fetching new key for client QKD: QKD2",
        "Got new key with keyId: key-1",
        "Returning response to client with Interface details",
    ]


@pytest.mark.parametrize(
    "public_key",
    ["not-a-key", "", b64e(b"\x00" * 31) + "=", "A" * 44, f"{'A' * 42}/;"],
    ids=["junk", "empty", "short-key", "not-base64-padding", "illegal-chars"],
)
async def test_start_connection_rejects_a_public_key_that_is_not_a_wireguard_key(
    client, ekm, runner, settings, public_key
):
    resp = await start(client, public_key)
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert ekm.calls == []
    assert runner.find("wg", "set") == []
    assert not os.path.exists(settings.peers_file)


async def test_start_connection_rejects_a_qkd_id_that_could_traverse_the_ekm_path(client, ekm):
    resp = await start(client, qkd_id="../../api/state?")
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert ekm.calls == []


async def test_a_failed_wg_set_peer_is_a_500_envelope_and_persists_nothing(
    client, runner, settings
):
    runner.set_peer_fails = True
    resp = await start(client)
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {
            "code": 500,
            "message": "failed to configure WireGuard peer",
            "status": "INTERNAL",
        }
    }
    assert not os.path.exists(settings.peers_file)

    # The address the failed attempt held was released, not burned.
    runner.set_peer_fails = False
    assert (await start(client, OTHER_PUB)).json()["client_ip"] == "10.20.0.2"


async def test_start_connection_is_503_when_the_ekm_has_no_key(client, ekm):
    ekm.available = False
    resp = await start(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == "no QKD key available"


# --- refresh_connection -----------------------------------------------------


async def test_refresh_connection_schedules_activation_after_the_delay(client, app):
    await start(client)
    resp = await client.post("/api/refresh_connection", json={"public_key": CLIENT_PUB}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.json() == {
        "preshared_key_id": "key-2",
        "effective_time": (NOW + dt.timedelta(seconds=30)).isoformat(),
    }
    await asyncio.sleep(0)  # let the scheduled activation reach its sleep
    assert app.state.sleep.calls == [3600, 30]


async def test_refresh_activates_the_new_psk_when_the_delay_elapses(client, app, runner, ekm, caplog):
    await start(client)
    with caplog.at_level(logging.INFO, logger="VPNServer"):
        await client.post("/api/refresh_connection", json={"public_key": CLIENT_PUB}, headers=AUTH)
        assert runner.psk_files[-1][1] == b64e(ekm.key_bytes(1))  # old PSK still in place
        await drain(app)

    assert runner.psk_files[-1][1] == b64e(ekm.key_bytes(2))
    messages = [r.getMessage() for r in caplog.records]
    effective = (NOW + dt.timedelta(seconds=30)).isoformat()
    assert f"Scheduled PSK refresh for op@example.com with keyId: key-2 at {effective}" in messages
    assert "Activated PSK key-2" in messages


async def test_a_second_refresh_supersedes_the_pending_activation(client, app, runner, ekm, caplog):
    await start(client)
    with caplog.at_level(logging.INFO, logger="VPNServer"):
        await refresh(client)
        await refresh(client)
        await drain(app)

    messages = [r.getMessage() for r in caplog.records]
    assert runner.psk_files[-1][1] == b64e(ekm.key_bytes(3))
    assert "Activated PSK key-2" not in messages
    assert "Activated PSK key-3" in messages


async def test_a_failed_activation_is_logged_and_does_not_kill_the_timer(
    client, app, runner, caplog
):
    await start(client)
    await refresh(client)
    runner.set_peer_fails = True
    with caplog.at_level(logging.INFO, logger="VPNServer"):
        await drain(app)
    assert f"PSK activation failed for {CLIENT_PUB[:8]}: RuntimeError" in [
        r.getMessage() for r in caplog.records
    ]


async def test_refresh_connection_rejects_a_malformed_public_key(client, ekm):
    resp = await client.post("/api/refresh_connection", json={"public_key": "nope"}, headers=AUTH)
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert ekm.calls == []


async def test_refresh_connection_for_an_unknown_peer_is_404(client):
    resp = await client.post("/api/refresh_connection", json={"public_key": OTHER_PUB}, headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["status"] == "NOT_FOUND"


# --- status / auth ----------------------------------------------------------


async def test_status_lists_the_peers(client):
    await start(client)
    await start(client, OTHER_PUB)
    peers = (await client.get("/api/status", headers=AUTH)).json()["peers"]
    assert peers == [
        {
            "pubkey": CLIENT_PUB,
            "client_ip": "10.20.0.2",
            "preshared_key_id": "key-1",
            "next_refresh_at": (NOW + dt.timedelta(seconds=3600)).isoformat(),
            "refresh_due": False,
        },
        {
            "pubkey": OTHER_PUB,
            "client_ip": "10.20.0.3",
            "preshared_key_id": "key-2",
            "next_refresh_at": (NOW + dt.timedelta(seconds=3600)).isoformat(),
            "refresh_due": False,
        },
    ]


async def test_the_periodic_timer_marks_the_peer_refresh_due(client, app, caplog):
    await start(client)
    with caplog.at_level(logging.INFO, logger="VPNServer"):
        await drain(app)
    peers = (await client.get("/api/status", headers=AUTH)).json()["peers"]
    assert peers[0]["refresh_due"] is True
    assert f"Refresh due for peer {CLIENT_PUB[:8]}" in [r.getMessage() for r in caplog.records]
    assert 3600 in app.state.sleep.calls


async def test_control_endpoints_reject_a_missing_bearer_token(client):
    for path, body in (
        ("/api/start_connection", {"qkd_id": "QKD2", "public_key": CLIENT_PUB}),
        ("/api/refresh_connection", {"public_key": CLIENT_PUB}),
    ):
        resp = await client.post(path, json=body)
        assert resp.status_code == 401
        assert resp.json() == {
            "error": {"code": 401, "message": "missing bearer token", "status": "UNAUTHENTICATED"}
        }
    assert (await client.get("/api/status")).status_code == 401


async def test_a_rejected_identity_is_403_and_never_touches_wireguard(client, runner):
    resp = await start_with_token(client, "someone-elses-token")
    assert resp.status_code == 403
    assert resp.json()["error"]["status"] == "PERMISSION_DENIED"
    assert runner.find("wg", "set") == []


async def start_with_token(client, token: str):
    return await client.post(
        "/api/start_connection",
        json={"qkd_id": "QKD2", "public_key": CLIENT_PUB},
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_healthz_is_open(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- ekm client -------------------------------------------------------------


def test_ekm_client_returns_the_key_id_and_raw_key(ekm):
    key_id, key = EkmClient("http://ekm", "vpn-token", transport=ekm.transport()).new("QKD2")
    assert (key_id, key) == ("key-1", ekm.key_bytes(1))


def test_ekm_client_sends_the_vpn_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"key_id": "k", "key": b64e(b"\x00" * 32), "qkd_name": "QKD2"})

    EkmClient("http://ekm", "vpn-token", transport=httpx.MockTransport(handler)).new("QKD2")
    assert seen["auth"] == "Bearer vpn-token"


def test_ekm_client_escapes_the_peer_name_into_one_path_segment():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"key_id": "k", "key": b64e(b"\x00" * 32)})

    EkmClient("http://ekm", "t", transport=httpx.MockTransport(handler)).new("../../api/state?")
    assert seen["path"] == "/api/..%2F..%2Fapi%2Fstate%3F/new?purpose=vpn"


def test_ekm_client_raises_with_the_ekm_error_message(ekm):
    ekm.available = False
    client = EkmClient("http://ekm", "vpn-token", transport=ekm.transport())
    with pytest.raises(RuntimeError, match="no QKD key available"):
        client.new("QKD2")


# --- settings from the environment ------------------------------------------


def test_settings_from_env_takes_allowed_ips_from_the_environment(monkeypatch):
    monkeypatch.setenv("VPN_PUBLIC_ENDPOINT", "203.0.113.7:51819")
    monkeypatch.setenv("VPN_ALLOWED_IPS", "10.10.0.0/24,10.99.0.0/24")

    settings = vpn_main.settings_from_env("10.99.0.1/24", "10.99.0.0/24")

    assert settings.allowed_ips == "10.10.0.0/24,10.99.0.0/24"
    assert (settings.tunnel_cidr, settings.server_address) == ("10.99.0.0/24", "10.99.0.1/24")


def test_settings_from_env_keeps_the_default_allowed_ips_when_unset(monkeypatch):
    monkeypatch.setenv("VPN_PUBLIC_ENDPOINT", "203.0.113.7:51819")
    monkeypatch.delenv("VPN_ALLOWED_IPS", raising=False)

    settings = vpn_main.settings_from_env("10.20.0.1/24", "10.20.0.0/24")

    assert settings.allowed_ips == VpnSettings.allowed_ips
