import contextlib
import datetime as dt
import json
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

from qkd_ekm.client import cli
from qkd_ekm.client.wgclient import WGClient
from qkd_ekm.common.crypto import b64e
from qkd_ekm.qkdsim.app import create_app as create_sim
from qkd_ekm.qkdsim.store import KeyStore
from qkd_ekm.upload.app import DirSink, UploadSettings
from qkd_ekm.upload.app import create_app as create_upload
from qkd_ekm.vpn.app import EkmClient

CLIENT_PRIV = b64e(b"c" * 32)
CLIENT_PUB = b64e(b"p" * 32)
SERVER_PUB = b64e(b"s" * 32)
ENDPOINT = "203.0.113.7:51819"
ALLOWED_IPS = "10.10.0.0/24,10.20.0.0/24"
CLIENT_IP = "10.20.0.2"
BUCKET = "qkd-ekm-data"
KMS_KEY = "projects/p/locations/us/keyRings/r/cryptoKeys/external"
WG_SHOW = f"interface: wgqkd\n  public key: {CLIENT_PUB}\n  peer: {SERVER_PUB}\n"

#: The client half of the paper's S1 log trace, in order.
CONNECT_LINES = [
    "Client Started",
    "User is trying to connect to the vpn server",
    "Creating local vpn interface",
    "Generating public-private key pair",
    "WG Interface file created",
    "Initiating connection to VPN Server",
    "Fetching Key from QKD with keyId: {key_id}",
    "Configuring the Wireguard VPN Interface",
    "Connected",
]


# --- fakes ------------------------------------------------------------------


class FakeRunner:
    """subprocess.run stand-in: records argv, replays canned results."""

    def __init__(self, events=None, gcloud_ok: bool = True, gcloud_missing: bool = False):
        self.commands: list[list[str]] = []
        self.stdin: list[str | None] = []
        self.psk_files: list[tuple[str, str]] = []
        self.events = events if events is not None else []
        self.gcloud_ok = gcloud_ok
        self.gcloud_missing = gcloud_missing

    def __call__(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        cmd = list(cmd)
        self.commands.append(cmd)
        self.stdin.append(kwargs.get("input"))
        self.events.append(("run", cmd))
        if "preshared-key" in cmd:
            path = cmd[cmd.index("preshared-key") + 1]
            with open(path) as fh:
                self.psk_files.append((path, fh.read()))
        code, out = self._result(cmd)
        return subprocess.CompletedProcess(cmd, code, out, "")

    def _result(self, cmd) -> tuple[int, str]:
        if cmd[:2] == ["wg", "genkey"]:
            return 0, CLIENT_PRIV + "\n"
        if cmd[:2] == ["wg", "pubkey"]:
            return 0, CLIENT_PUB + "\n"
        if cmd[:2] == ["wg", "show"]:
            return 0, WG_SHOW
        if cmd[0].startswith("gcloud"):
            if self.gcloud_missing:
                raise FileNotFoundError(2, "No such file or directory: 'gcloud'")
            return (0, "gcloud-token\n") if self.gcloud_ok else (1, "")
        return 0, ""

    def find(self, *prefix) -> list[list[str]]:
        return [c for c in self.commands if c[: len(prefix)] == list(prefix)]


class FakeSleep:
    def __init__(self, events=None):
        self.calls: list[float] = []
        self.events = events if events is not None else []

    def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        self.events.append(("sleep", delay))


class FakeControl:
    """The VPN control API, reduced to what the client depends on.

    Every key it hands out is a real simulator key drawn for the QKD1<-->QKD2
    pair, exactly as the EKM would draw it, so the client's `dec_keys` fetch
    has to return the same bytes for the tunnel to key up.
    """

    def __init__(self, qkd_url: str, activation_delay_s: int = 5):
        self.qkd_url = qkd_url
        self.activation_delay_s = activation_delay_s
        self.issued: list[tuple[str, str]] = []
        self.auth: list[str | None] = []

    def allocate(self) -> str:
        resp = httpx.get(f"{self.qkd_url}/api/v1/keys/QKD2/enc_keys")
        key = resp.json()["keys"][0]
        self.issued.append((key["key_ID"], key["key"]))
        return key["key_ID"]

    def app(self) -> FastAPI:
        api = FastAPI()

        @api.post("/api/start_connection")
        def start_connection(body: dict, request: Request) -> dict:
            self.auth.append(request.headers.get("authorization"))
            return {
                "qkd_id": "QKD1",
                "server_public_key": SERVER_PUB,
                "endpoint": ENDPOINT,
                "preshared_key_id": self.allocate(),
                "client_ip": CLIENT_IP,
                "allowed_ips": ALLOWED_IPS,
                "effective_time": dt.datetime.now(dt.UTC).isoformat(),
                "refresh_after_s": 3600,
            }

        @api.post("/api/refresh_connection")
        def refresh_connection(body: dict, request: Request) -> dict:
            self.auth.append(request.headers.get("authorization"))
            effective = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self.activation_delay_s)
            return {
                "preshared_key_id": self.allocate(),
                "effective_time": effective.isoformat(),
            }

        return api


def ekm_transport(qkd_url: str) -> httpx.MockTransport:
    """EKM `GET /api/{peer}/new`, backed by real simulator keys."""

    def handle(request: httpx.Request) -> httpx.Response:
        peer = request.url.path.split("/")[2]
        key = httpx.get(f"{qkd_url}/api/v1/keys/{peer}/enc_keys").json()["keys"][0]
        return httpx.Response(
            200, json={"key_id": key["key_ID"], "key": key["key"], "qkd_name": peer}
        )

    return httpx.MockTransport(handle)


# --- live servers -----------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@dataclass
class Stack:
    qkd_url: str
    control_url: str
    upload_url: str
    control: FakeControl
    objects: Path
    state_dir: str
    events: list = field(default_factory=list)

    @property
    def conf_path(self) -> Path:
        return Path(self.state_dir) / "wgqkd.conf"

    @property
    def session(self) -> dict:
        with open(Path(self.state_dir) / "session.json") as fh:
            return json.load(fh)

    def args(self, *extra: str) -> list[str]:
        return [
            "--control-url", self.control_url,
            "--qkd2-url", self.qkd_url,
            "--id-token", "test-id-token",
            "--state-dir", self.state_dir,
            *extra,
        ]  # fmt: skip


@pytest.fixture
def stack(tmp_path, monkeypatch):
    for name in ("SIM_TOKEN", "QKD2_URL", "QKD_CA_FILE", "QKD_TOKEN", "VPN_CONTROL_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SIM_SAES", "QKD1,QKD2")

    objects = tmp_path / "objects"
    with serve(create_sim(KeyStore(seed=11))) as qkd_url:
        control = FakeControl(qkd_url)
        upload_app = create_upload(
            ekm_client=EkmClient(
                "http://ekm.internal", "vpn-token", transport=ekm_transport(qkd_url)
            ),
            sink=DirSink(str(objects), bucket=BUCKET),
            settings=UploadSettings(bucket=BUCKET, kms_key_name=KMS_KEY),
        )
        with serve(control.app()) as control_url, serve(upload_app) as upload_url:
            yield Stack(
                qkd_url=qkd_url,
                control_url=control_url,
                upload_url=upload_url,
                control=control,
                objects=objects,
                state_dir=str(tmp_path / "state"),
            )


@pytest.fixture
def runner(stack):
    return FakeRunner(events=stack.events)


def messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "VPNClient"]


# --- WGClient ---------------------------------------------------------------


def test_genkey_pipes_the_private_key_into_wg_pubkey(tmp_path):
    runner = FakeRunner()
    wg = WGClient(runner=runner, state_dir=str(tmp_path))

    priv, pub = wg.genkey()

    assert (priv, pub) == (CLIENT_PRIV, CLIENT_PUB)
    assert runner.commands == [["wg", "genkey"], ["wg", "pubkey"]]
    assert runner.stdin[1] == CLIENT_PRIV


def test_write_conf_is_a_wg_quick_config_readable_only_by_the_owner(tmp_path):
    wg = WGClient(runner=FakeRunner(), state_dir=str(tmp_path))
    path = str(tmp_path / "wgqkd.conf")

    wg.write_conf(
        path,
        priv=CLIENT_PRIV,
        address=f"{CLIENT_IP}/32",
        server_pub=SERVER_PUB,
        endpoint=ENDPOINT,
        psk_b64=b64e(b"k" * 32),
        allowed_ips=ALLOWED_IPS,
    )

    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    with open(path) as fh:
        conf = fh.read()
    assert conf == (
        "[Interface]\n"
        f"PrivateKey = {CLIENT_PRIV}\n"
        f"Address = {CLIENT_IP}/32\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {SERVER_PUB}\n"
        f"PresharedKey = {b64e(b'k' * 32)}\n"
        f"Endpoint = {ENDPOINT}\n"
        f"AllowedIPs = {ALLOWED_IPS}\n"
        "PersistentKeepalive = 25\n"
    )


def test_up_and_down_use_wg_quick_with_the_conf_path(tmp_path):
    runner = FakeRunner()
    wg = WGClient(runner=runner, state_dir=str(tmp_path))
    wg.up("/etc/wireguard/wgqkd.conf")
    wg.down("/etc/wireguard/wgqkd.conf")
    assert runner.commands == [
        ["wg-quick", "up", "/etc/wireguard/wgqkd.conf"],
        ["wg-quick", "down", "/etc/wireguard/wgqkd.conf"],
    ]


def test_set_psk_passes_it_in_a_temp_file_that_is_deleted_afterwards(tmp_path):
    runner = FakeRunner()
    wg = WGClient(runner=runner, state_dir=str(tmp_path))
    psk = bytes(range(32))

    wg.set_psk(SERVER_PUB, psk)

    path, content = runner.psk_files[0]
    assert content == b64e(psk)
    assert not os.path.exists(path)
    assert runner.commands[-1] == [
        "wg", "set", "wgqkd", "peer", SERVER_PUB, "preshared-key", path,
    ]  # fmt: skip


def test_a_failing_wg_command_is_an_error_naming_the_command(tmp_path):
    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "wg-quick: `wgqkd' already exists")

    wg = WGClient(runner=failing, state_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="already exists"):
        wg.up("/etc/wireguard/wgqkd.conf")


# --- connect ----------------------------------------------------------------


def test_connect_writes_the_qkd_key_as_the_preshared_key(stack, runner):
    assert cli.run(stack.args("connect"), runner=runner) == 0

    key_id, key_b64 = stack.control.issued[0]
    with open(stack.conf_path) as fh:
        conf = fh.read()
    assert f"PresharedKey = {key_b64}\n" in conf
    assert f"PublicKey = {SERVER_PUB}\n" in conf
    assert f"Endpoint = {ENDPOINT}\n" in conf
    assert f"Address = {CLIENT_IP}/32\n" in conf
    assert f"AllowedIPs = {ALLOWED_IPS}\n" in conf
    assert f"PrivateKey = {CLIENT_PRIV}\n" in conf
    assert stack.session["preshared_key_id"] == key_id


def test_connect_brings_the_tunnel_up_with_wg_quick(stack, runner):
    cli.run(stack.args("connect"), runner=runner)
    assert runner.find("wg-quick") == [["wg-quick", "up", str(stack.conf_path)]]


def test_connect_logs_the_paper_client_sequence(stack, runner, caplog):
    with caplog.at_level(logging.INFO, logger="VPNClient"):
        cli.run(stack.args("connect"), runner=runner)

    key_id = stack.control.issued[0][0]
    assert messages(caplog) == [line.format(key_id=key_id) for line in CONNECT_LINES]


def test_connect_persists_the_session_without_world_readable_state(stack, runner):
    cli.run(stack.args("connect"), runner=runner)

    session_path = Path(stack.state_dir) / "session.json"
    assert oct(os.stat(session_path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(stack.conf_path).st_mode & 0o777) == "0o600"
    assert stack.session == {
        "public_key": CLIENT_PUB,
        "server_public_key": SERVER_PUB,
        "endpoint": ENDPOINT,
        "client_ip": CLIENT_IP,
        "preshared_key_id": stack.control.issued[0][0],
    }


def test_connect_authenticates_to_the_control_api_with_the_identity_token(stack, runner):
    cli.run(stack.args("connect"), runner=runner)
    assert stack.control.auth == ["Bearer test-id-token"]


def test_connect_takes_the_identity_token_from_the_configured_command(stack, runner):
    args = [a for a in stack.args() if a not in ("--id-token", "test-id-token")]
    cli.run([*args, "--id-token-cmd", "gcloud auth print-identity-token", "connect"], runner=runner)

    assert stack.control.auth == ["Bearer gcloud-token"]
    assert runner.find("gcloud") == [["gcloud", "auth", "print-identity-token"]]


#: gcloud present but failing (not logged in), and gcloud absent entirely.
@pytest.mark.parametrize(
    "broken_token_command",
    [
        lambda: FakeRunner(gcloud_ok=False),
        lambda: FakeRunner(gcloud_missing=True),
    ],
)
def test_connect_falls_back_to_the_metadata_server_when_the_token_command_fails(
    tmp_path, broken_token_command
):
    seen = {}
    key = b"m" * 32

    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metadata.google.internal":
            seen["path"] = request.url.path
            seen["flavor"] = request.headers.get("metadata-flavor")
            seen["audience"] = request.url.params.get("audience")
            seen["format"] = request.url.params.get("format")
            return httpx.Response(200, text="metadata-token")
        if request.url.host == "control.local":
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "qkd_id": "QKD1",
                    "server_public_key": SERVER_PUB,
                    "endpoint": ENDPOINT,
                    "preshared_key_id": "key-1",
                    "client_ip": CLIENT_IP,
                    "allowed_ips": ALLOWED_IPS,
                    "effective_time": dt.datetime.now(dt.UTC).isoformat(),
                    "refresh_after_s": 3600,
                },
            )
        return httpx.Response(200, json={"keys": [{"key_ID": "key-1", "key": b64e(key)}]})

    runner = broken_token_command()
    code = cli.run(
        [
            "--control-url", "http://control.local",
            "--qkd2-url", "http://qkd.local",
            "--state-dir", str(tmp_path / "state"),
            "connect",
        ],
        runner=runner,
        http_transport=httpx.MockTransport(dispatch),
    )

    assert code == 0
    # A working gcloud must not win over the metadata server: on a VM its service-account
    # token lacks the email claims the VPN server pins on.
    seen.clear()
    assert (
        cli.run(
            [
                "--control-url", "http://control.local",
                "--qkd2-url", "http://qkd.local",
                "--state-dir", str(tmp_path / "state2"),
                "connect",
            ],
            runner=FakeRunner(),
            http_transport=httpx.MockTransport(dispatch),
        )
        == 0
    )
    assert seen["auth"] == "Bearer metadata-token"
    assert seen["path"] == "/computeMetadata/v1/instance/service-accounts/default/identity"
    assert seen["flavor"] == "Google"
    assert seen["audience"] == "qkd-ekm-vpn"
    # GCE omits email/email_verified unless format=full is requested.
    assert seen["format"] == "full"
    assert seen["auth"] == "Bearer metadata-token"
    with open(tmp_path / "state" / "wgqkd.conf") as fh:
        assert f"PresharedKey = {b64e(key)}\n" in fh.read()


def test_connect_falls_back_to_gcloud_when_the_metadata_server_is_unreachable(tmp_path):
    seen = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metadata.google.internal":
            raise httpx.ConnectError("no route to host")
        if request.url.host == "control.local":
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "qkd_id": "QKD1",
                    "server_public_key": SERVER_PUB,
                    "endpoint": ENDPOINT,
                    "preshared_key_id": "key-1",
                    "client_ip": CLIENT_IP,
                    "allowed_ips": ALLOWED_IPS,
                    "effective_time": dt.datetime.now(dt.UTC).isoformat(),
                    "refresh_after_s": 3600,
                },
            )
        return httpx.Response(200, json={"keys": [{"key_ID": "key-1", "key": b64e(b"m" * 32)}]})

    code = cli.run(
        [
            "--control-url", "http://control.local",
            "--qkd2-url", "http://qkd.local",
            "--state-dir", str(tmp_path / "state"),
            "connect",
        ],
        runner=FakeRunner(),
        http_transport=httpx.MockTransport(dispatch),
    )  # fmt: skip

    assert code == 0
    assert seen["auth"] == "Bearer gcloud-token"


# --- refresh ----------------------------------------------------------------


def test_refresh_installs_the_new_psk_only_after_the_effective_time(stack, runner, caplog):
    cli.run(stack.args("connect"), runner=runner)
    sleep = FakeSleep(events=stack.events)
    stack.events.clear()

    with caplog.at_level(logging.INFO, logger="VPNClient"):
        caplog.clear()
        assert cli.run(stack.args("refresh"), runner=runner, sleep=sleep) == 0

    key_id, key_b64 = stack.control.issued[1]
    assert runner.psk_files[-1][1] == key_b64
    assert stack.session["preshared_key_id"] == key_id

    kinds = [event[0] for event in stack.events]
    set_psk = next(
        i for i, event in enumerate(stack.events) if event[0] == "run" and "set" in event[1]
    )
    assert kinds.index("sleep") < set_psk
    assert 0 < sleep.calls[0] <= stack.control.activation_delay_s
    assert runner.commands[-1][:5] == ["wg", "set", "wgqkd", "peer", SERVER_PUB]

    effective = messages(caplog)[0].split("effective at ")[1]
    assert messages(caplog) == [
        f"Refreshing PSK with keyId: {key_id} effective at {effective}",
        "PSK refreshed",
    ]


def test_refresh_without_a_session_fails_without_touching_wireguard(stack, runner):
    assert cli.run(stack.args("refresh"), runner=runner) == 1
    assert runner.find("wg", "set") == []


# --- upload -----------------------------------------------------------------


def test_upload_encrypts_locally_and_the_server_stores_the_plaintext(
    stack, runner, tmp_path, capsys
):
    source = tmp_path / "sensitive.txt"
    source.write_bytes(b"quantum-safe payload\n")

    assert cli.run(stack.args("upload", str(source), "--upload-url", stack.upload_url),
                   runner=runner) == 0  # fmt: skip

    written = list(stack.objects.iterdir())
    assert len(written) == 1
    assert written[0].read_bytes() == source.read_bytes()
    assert written[0].name.endswith("_sensitive.txt")
    assert f"gs://{BUCKET}/{written[0].name}" in capsys.readouterr().out


def test_upload_logs_the_paper_client_sequence(stack, runner, tmp_path, caplog):
    source = tmp_path / "sensitive.txt"
    source.write_bytes(b"payload")

    with caplog.at_level(logging.INFO, logger="VPNClient"):
        cli.run(stack.args("upload", str(source), "--upload-url", stack.upload_url), runner=runner)

    logged = messages(caplog)
    key_id = logged[1].removeprefix("Encrypting the file with key: ")
    assert logged == [
        "Getting key for file encryption shared with QKD1",
        f"Encrypting the file with key: {key_id}",
        "Uploading file sensitive.txt",
        "File sensitive.txt uploaded successfully",
    ]


def test_upload_of_a_missing_file_is_an_error(stack, runner, tmp_path):
    assert cli.run(
        stack.args("upload", str(tmp_path / "nope.txt"), "--upload-url", stack.upload_url),
        runner=runner,
    ) == 2


# --- status / disconnect ----------------------------------------------------


def test_status_reports_the_interface_and_the_session(stack, runner, capsys):
    cli.run(stack.args("connect"), runner=runner)
    capsys.readouterr()

    assert cli.run(stack.args("status"), runner=runner) == 0
    out = capsys.readouterr().out
    assert "interface: wgqkd" in out
    assert stack.control.issued[0][0] in out
    assert CLIENT_IP in out


def test_status_without_a_session_still_shows_the_interface(stack, runner, capsys):
    assert cli.run(stack.args("status"), runner=runner) == 0
    assert "interface: wgqkd" in capsys.readouterr().out


def test_disconnect_takes_the_tunnel_down_and_shreds_the_key_material(stack, runner):
    cli.run(stack.args("connect"), runner=runner)

    assert cli.run(stack.args("disconnect"), runner=runner) == 0
    assert runner.find("wg-quick", "down") == [["wg-quick", "down", str(stack.conf_path)]]
    assert not (Path(stack.state_dir) / "session.json").exists()
    assert not stack.conf_path.exists()


def test_disconnect_cleans_up_even_when_the_interface_is_already_gone(stack, runner):
    cli.run(stack.args("connect"), runner=runner)

    def failing(cmd, **kwargs):
        if cmd[:2] == ["wg-quick", "down"]:
            return subprocess.CompletedProcess(cmd, 1, "", "wg-quick: `wgqkd' is not a WireGuard interface")
        return runner(cmd, **kwargs)

    assert cli.run(stack.args("disconnect"), runner=failing) == 0
    assert not stack.conf_path.exists()
    assert not (Path(stack.state_dir) / "session.json").exists()


def test_no_identity_token_anywhere_is_an_error_naming_the_command_and_its_stderr(
    tmp_path, caplog
):
    def failing(cmd, **kwargs):
        if cmd[0] == "gcloud":
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: (gcloud) not logged in\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nodename nor servname provided")

    with caplog.at_level(logging.ERROR, logger="VPNClient"):
        code = cli.run(
            ["--state-dir", str(tmp_path / "state"), "connect"],
            runner=failing,
            http_transport=httpx.MockTransport(unreachable),
        )

    assert code == 1
    message = caplog.records[-1].getMessage()
    assert "ERROR: (gcloud) not logged in" in message
    assert "metadata server is unreachable" in message
