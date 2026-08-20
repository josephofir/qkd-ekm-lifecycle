"""`qkd-ekm-client`: the client-side half of the paper's S1 and S2 workflows.

S1 (`connect`, `refresh`, `disconnect`): the VPN control API never sends key
material -- it answers with the *id* of a key the EKM drew for the QKD1<-->QKD2
pair. This CLI fetches that id from QKD2 over the quantum link
(`dec_keys?key_ID=`), so the WireGuard pre-shared key exists at both ends
without ever crossing the public network. `refresh` waits for the server's
announced `effective_time` before installing the next PSK, so both ends switch
together.

S2 (`upload`): a second QKD key, this one shared with the FileUploadServer,
encrypts the file on this machine (AES-GCM, filename as AAD) before it enters
the tunnel. Cloud-side CMEK wrapping through the EKM is the other protection
layer; this is the client-side one.

Calls to the control API carry a Google identity token. The GCE metadata
server is tried first (`format=full`, so the token carries the email claims
the VPN server pins its allow-list on); `gcloud auth print-identity-token`
is the fallback for a laptop, where there is no metadata server to answer.
Everything else is reachable only through the tunnel.

`run(argv, ...)` is the testable entry point: it takes the process boundaries
-- subprocess runner, HTTP transport, sleep, clock -- as injectable arguments
and returns an exit code, so the whole workflow can run in-process against the
simulator with no root, no network and no wall-clock waiting.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
import typer

from qkd_ekm.client.wgclient import WGClient
from qkd_ekm.common.crypto import aes_gcm_wrap, b64e
from qkd_ekm.common.log import get_logger
from qkd_ekm.qkd.etsi014 import Etsi014Client

logger = get_logger("VPNClient")

_AUDIENCE = "qkd-ekm-vpn"
DEFAULT_ID_TOKEN_CMD = f"gcloud auth print-identity-token --audiences={_AUDIENCE}"
_METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity"
)
_TIMEOUT = 30.0
#: The metadata server is either a hop away or not there at all; do not make a
#: laptop wait 30s to learn that gcloud was the only option.
_METADATA_TIMEOUT = 2.0


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Deps:
    """The process boundaries, injectable so tests can drive the whole CLI.

    Plain attributes, not a dataclass: a function stored as a *class* attribute
    would be bound as a method on access.
    """

    def __init__(self, http_transport=None, runner=None, sleep=None, clock=None):
        self.http_transport = http_transport
        self.runner = runner or subprocess.run
        self.sleep = sleep or time.sleep
        self.clock = clock or _utcnow


@dataclass
class Config:
    control_url: str
    qkd2_url: str
    qkd_ca: str | None
    qkd_token: str | None
    id_token: str | None
    id_token_cmd: str
    state_dir: str
    my_qkd: str
    peer_qkd: str
    deps: Deps


#: Replaced per `run()` call; typer builds the click Context itself, so there
#: is no earlier place to hand the injected boundaries to the callback.
_DEPS = Deps()

app = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    help="QKD-keyed WireGuard VPN client and encrypted upload tool.",
)


@app.callback()
def _global(
    ctx: typer.Context,
    control_url: Annotated[
        str, typer.Option("--control-url", envvar="VPN_CONTROL_URL")
    ] = "http://localhost:18080",
    qkd2_url: Annotated[
        str, typer.Option("--qkd2-url", envvar="QKD2_URL")
    ] = "https://localhost:8200",
    qkd_ca: Annotated[str | None, typer.Option("--qkd-ca", envvar="QKD_CA_FILE")] = None,
    qkd_token: Annotated[str | None, typer.Option("--qkd-token", envvar="QKD_TOKEN")] = None,
    id_token: Annotated[str | None, typer.Option("--id-token", envvar="VPN_ID_TOKEN")] = None,
    id_token_cmd: Annotated[str, typer.Option("--id-token-cmd")] = DEFAULT_ID_TOKEN_CMD,
    state_dir: Annotated[
        str, typer.Option("--state-dir", envvar="CLIENT_STATE_DIR")
    ] = "~/.qkd-ekm-client",
    my_qkd: Annotated[str, typer.Option("--my-qkd")] = "QKD2",
    peer_qkd: Annotated[str, typer.Option("--peer-qkd")] = "QKD1",
) -> None:
    ctx.obj = Config(
        control_url=control_url,
        qkd2_url=qkd2_url,
        qkd_ca=qkd_ca,
        qkd_token=qkd_token,
        id_token=id_token,
        id_token_cmd=id_token_cmd,
        state_dir=os.path.expanduser(state_dir),
        my_qkd=my_qkd,
        peer_qkd=peer_qkd,
        deps=_DEPS,
    )


# --- helpers ----------------------------------------------------------------


def _http(
    cfg: Config, base_url: str = "", headers: dict | None = None, timeout: float = _TIMEOUT
) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers=headers or {},
        timeout=timeout,
        transport=cfg.deps.http_transport,
    )


def _id_token(cfg: Config) -> str:
    if cfg.id_token:
        return cfg.id_token
    # On a GCE VM the metadata server is the authority: it mints an identity token
    # for the audience that carries email/email_verified (format=full), which the VPN
    # server pins its allow-list on. `gcloud auth print-identity-token` for a service
    # account omits those claims, so it is only the laptop fallback.
    metadata_error: str | None = None
    try:
        # Absolute URL, no base_url: httpx normalises a base_url to end in "/",
        # which would request `.../identity/` -- a 404 on the real metadata server.
        with _http(
            cfg, headers={"Metadata-Flavor": "Google"}, timeout=_METADATA_TIMEOUT
        ) as client:
            resp = client.get(_METADATA_URL, params={"audience": _AUDIENCE, "format": "full"})
            resp.raise_for_status()
            return resp.text.strip()
    except httpx.HTTPError as exc:
        metadata_error = str(exc) or exc.__class__.__name__
    try:
        result = cfg.deps.runner(
            shlex.split(cfg.id_token_cmd), capture_output=True, text=True, check=False
        )
    except OSError:
        result = None  # gcloud is not installed at all
    if result is not None and result.returncode == 0 and (result.stdout or "").strip():
        return result.stdout.strip()
    stderr = (result.stderr or "").strip() if result is not None else "not executable"
    raise RuntimeError(
        f"no identity token: the metadata server is unreachable ({metadata_error}) and "
        f"`{cfg.id_token_cmd}` failed ({stderr})"
    )


def _control(cfg: Config) -> httpx.Client:
    return _http(cfg, cfg.control_url, {"Authorization": f"Bearer {_id_token(cfg)}"})


def _wg(cfg: Config) -> WGClient:
    return WGClient(runner=cfg.deps.runner, state_dir=cfg.state_dir)


def _fetch_key(cfg: Config, key_id: str) -> bytes:
    """Retrieve the key material for `key_id` from QKD2, over the quantum link."""
    qkd = Etsi014Client(
        cfg.qkd2_url,
        ca_file=cfg.qkd_ca,
        token=cfg.qkd_token,
        transport=cfg.deps.http_transport,
    )
    try:
        return qkd.dec_keys(master_sae=cfg.peer_qkd, key_ids=[key_id])[0].key
    finally:
        qkd.close()


def _session_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "session.json")


def _save_session(cfg: Config, session: dict) -> None:
    os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
    # 0600: the session names the private key that keys the tunnel.
    fd = os.open(_session_path(cfg), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(session, fh, indent=2)


def _read_session(cfg: Config) -> dict | None:
    path = _session_path(cfg)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def _require_session(cfg: Config) -> dict:
    session = _read_session(cfg)
    if session is None:
        raise RuntimeError(f"no session at {_session_path(cfg)}: run `connect` first")
    return session


# --- commands ---------------------------------------------------------------


@app.command()
def connect(ctx: typer.Context) -> None:
    """Bring up the tunnel with a QKD-derived pre-shared key."""
    cfg: Config = ctx.obj
    logger.info("Client Started")
    logger.info("User is trying to connect to the vpn server")
    logger.info("Creating local vpn interface")
    logger.info("Generating public-private key pair")
    wg = _wg(cfg)
    priv, pub = wg.genkey()
    os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
    # NOTE: the paper's trace announces the interface file here, but its
    # Address and PSK only exist after the server answers -- the file itself is
    # written below, under "Configuring the Wireguard VPN Interface".
    logger.info("WG Interface file created")

    logger.info("Initiating connection to VPN Server")
    with _control(cfg) as client:
        resp = client.post(
            "/api/start_connection", json={"qkd_id": cfg.my_qkd, "public_key": pub}
        )
        resp.raise_for_status()
        body = resp.json()

    key_id = body["preshared_key_id"]
    logger.info(f"Fetching Key from QKD with keyId: {key_id}")
    psk = _fetch_key(cfg, key_id)

    logger.info("Configuring the Wireguard VPN Interface")
    wg.write_conf(
        wg.conf_path,
        priv=priv,
        address=f"{body['client_ip']}/32",
        server_pub=body["server_public_key"],
        endpoint=body["endpoint"],
        psk_b64=b64e(psk),
        allowed_ips=body["allowed_ips"],
    )
    wg.up(wg.conf_path)
    logger.info("Connected")

    # The private key stays in the wg-quick conf only: a second copy would be a
    # second thing to shred, and nothing reads it back.
    _save_session(
        cfg,
        {
            "public_key": pub,
            "server_public_key": body["server_public_key"],
            "endpoint": body["endpoint"],
            "client_ip": body["client_ip"],
            "preshared_key_id": key_id,
        },
    )


@app.command()
def refresh(ctx: typer.Context) -> None:
    """Install the next QKD pre-shared key at the server's effective time."""
    cfg: Config = ctx.obj
    session = _require_session(cfg)
    with _control(cfg) as client:
        resp = client.post(
            "/api/refresh_connection", json={"public_key": session["public_key"]}
        )
        resp.raise_for_status()
        body = resp.json()

    key_id, effective_time = body["preshared_key_id"], body["effective_time"]
    logger.info(f"Refreshing PSK with keyId: {key_id} effective at {effective_time}")
    psk = _fetch_key(cfg, key_id)

    # Both ends install the new PSK at the announced moment; installing it
    # early would blackhole the tunnel until the server caught up.
    delay = (dt.datetime.fromisoformat(effective_time) - cfg.deps.clock()).total_seconds()
    if delay > 0:
        cfg.deps.sleep(delay)
    _wg(cfg).set_psk(session["server_public_key"], psk)
    logger.info("PSK refreshed")

    session["preshared_key_id"] = key_id
    _save_session(cfg, session)


@app.command()
def upload(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    upload_url: Annotated[
        str, typer.Option("--upload-url", envvar="UPLOAD_URL")
    ] = "http://localhost:8081",
) -> None:
    """Encrypt a file with a QKD key and upload it through the tunnel."""
    cfg: Config = ctx.obj
    name = path.name
    logger.info(f"Getting key for file encryption shared with {cfg.peer_qkd}")
    with _http(cfg, upload_url) as client:
        resp = client.post("/api/file_key", json={"peer": cfg.my_qkd})
        resp.raise_for_status()
        key_id = resp.json()["key_id"]
        key = _fetch_key(cfg, key_id)

        logger.info(f"Encrypting the file with key: {key_id}")
        blob = aes_gcm_wrap(key, path.read_bytes(), aad=name.encode())

        logger.info(f"Uploading file {name}")
        resp = client.post(
            "/api/upload",
            files={"file": (name, blob, "application/octet-stream")},
            data={"key_id": key_id, "filename": name},
        )
        resp.raise_for_status()
    logger.info(f"File {name} uploaded successfully")
    typer.echo(resp.json()["object"])


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the interface and the stored session."""
    cfg: Config = ctx.obj
    typer.echo(_wg(cfg).show())
    session = _read_session(cfg)
    if session is None:
        typer.echo(f"no session at {_session_path(cfg)}")
        return
    typer.echo(f"endpoint: {session['endpoint']}")
    typer.echo(f"client ip: {session['client_ip']}")
    typer.echo(f"preshared key id: {session['preshared_key_id']}")


@app.command()
def disconnect(ctx: typer.Context) -> None:
    """Take the tunnel down, then delete the conf and the session."""
    cfg: Config = ctx.obj
    wg = _wg(cfg)
    try:
        wg.down(wg.conf_path)
    except RuntimeError as exc:
        # An interface that is already gone must not strand the key material
        # on disk -- the cleanup below is the point of this command.
        logger.warning(f"wg-quick down failed, cleaning up anyway: {exc}")
    for path in (wg.conf_path, _session_path(cfg)):
        if os.path.exists(path):
            os.unlink(path)
    logger.info("Disconnected")


# --- entry points -----------------------------------------------------------


def run(argv: list[str], *, http_transport=None, runner=None, sleep=None, clock=None) -> int:
    """Run one CLI invocation and return its exit code."""
    global _DEPS
    _DEPS = Deps(http_transport=http_transport, runner=runner, sleep=sleep, clock=clock)
    try:
        app(args=list(argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    except (RuntimeError, OSError, httpx.HTTPError) as exc:
        logger.error(str(exc))
        return 1
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))
