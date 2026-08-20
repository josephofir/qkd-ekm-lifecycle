"""VPN control API: hands a client a QKD-derived WireGuard pre-shared key.

The server never sends key *material* to the client -- it returns the EKM's
`preshared_key_id`, and the client fetches the same key from QKD2 by that id
over the quantum link. Both ends therefore hold a PSK that never crossed the
public network.

Re-keying is coordinated, not unilateral: `/api/refresh_connection` allocates
the next key and announces an `effective_time` `VPN_ACTIVATION_DELAY_S` in the
future, so both ends install it at (roughly) the same moment instead of
blackholing the tunnel. The server-side periodic timer only *flags*
`refresh_due` -- it cannot re-key on its own, because a key the client was
never told about would be a key it cannot fetch from QKD2. The client CLI
polls `/api/status` (or just calls `/api/refresh_connection` every
`refresh_after_s`) and drives each rotation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import ipaddress
import json
import os
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from qkd_ekm.common.auth import bearer_dep, error_body
from qkd_ekm.common.crypto import b64d
from qkd_ekm.common.log import get_logger

logger = get_logger("VPNServer")


@dataclass
class VpnSettings:
    public_endpoint: str
    tunnel_cidr: str = "10.20.0.0/24"
    server_address: str = "10.20.0.1/24"
    allowed_ips: str = "10.10.0.0/24,10.20.0.0/24"
    refresh_s: int = 3600
    activation_delay_s: int = 30
    peers_file: str = "/var/lib/qkd-ekm/peers.json"
    server_peer: str = "QKD1"


class EkmClient:
    """`GET /api/{peer}/new` on the EKM, authenticated with the shared VPN token."""

    def __init__(
        self,
        base_url: str,
        vpn_token: str,
        ca_file: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ):
        self._client = httpx.Client(
            base_url=base_url,
            verify=ca_file or True,
            timeout=timeout,
            headers={"Authorization": f"Bearer {vpn_token}"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def new(self, peer: str, purpose: str = "vpn") -> tuple[str, bytes]:
        try:
            # quote(safe=""): the peer name is a single path segment, never a
            # way to reach a different EKM endpoint.
            resp = self._client.get(
                f"/api/{quote(peer, safe='')}/new", params={"purpose": purpose}
            )
        except httpx.TransportError as exc:
            raise RuntimeError(f"EKM unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(_ekm_error(resp))
        body = resp.json()
        return body["key_id"], b64d(body["key"])


def _ekm_error(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return f"EKM HTTP {resp.status_code}"


class PeerFile:
    """Peer records, persisted as JSON: ids and addresses only, never key material."""

    def __init__(self, path: str):
        self.path = path
        self.peers: dict[str, dict] = {}
        if os.path.exists(path):
            with open(path) as fh:
                self.peers = json.load(fh)

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(self.peers, fh, indent=2)
        os.replace(tmp, self.path)


def _wireguard_key(value: str) -> str:
    """A WireGuard public key: standard base64 of exactly 32 bytes, 44 chars.

    Rejecting anything else keeps attacker-controlled text out of the `wg`
    argv and out of the peers file, which is loaded again on restart.
    """
    if len(value) != 44:
        raise ValueError("public_key must be 44 base64 characters")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("public_key must be base64") from exc
    if len(raw) != 32:
        raise ValueError("public_key must decode to 32 bytes")
    return value


WireGuardKey = Annotated[str, AfterValidator(_wireguard_key)]
#: SAE names address an EKM path segment, so keep them to a boring alphabet.
QkdId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")]


class StartConnection(BaseModel):
    public_key: WireGuardKey
    qkd_id: QkdId = "QKD2"


class RefreshConnection(BaseModel):
    public_key: WireGuardKey


def create_app(wg, ekm_client, verifier, settings: VpnSettings) -> FastAPI:
    auth = Depends(bearer_dep(verifier))
    peers = PeerFile(settings.peers_file)
    timers: dict[str, asyncio.Task] = {}
    activations: dict[str, asyncio.Task] = {}

    app = FastAPI()
    app.state.clock = lambda: dt.datetime.now(dt.UTC)
    app.state.sleep = asyncio.sleep
    app.state.tasks = set()  # scheduled timers, kept referenced so they are not GC'd

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        body = detail if isinstance(detail, dict) and "error" in detail else None
        return JSONResponse(
            status_code=exc.status_code,
            content=body or error_body(exc.status_code, str(detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"][1:]) or "body"
        return JSONResponse(
            status_code=400, content=error_body(400, f"{field}: {first['msg']}")
        )

    def schedule(delay: float, work) -> asyncio.Task:
        async def run() -> None:
            await app.state.sleep(delay)
            await work()

        task = asyncio.ensure_future(run())
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)
        return task

    def arm_refresh_timer(pubkey: str) -> None:
        """(Re)start the peer's rotation clock; only one timer per peer runs."""
        running = timers.pop(pubkey, None)
        if running is not None:
            running.cancel()

        async def due() -> None:
            peers.peers[pubkey]["refresh_due"] = True
            peers.save()
            logger.info(f"Refresh due for peer {pubkey[:8]}")

        timers[pubkey] = schedule(settings.refresh_s, due)

    def allocate_ip() -> str:
        taken = {p["client_ip"] for p in peers.peers.values()}
        taken.add(str(ipaddress.ip_interface(settings.server_address).ip))
        for host in ipaddress.ip_network(settings.tunnel_cidr).hosts():
            if str(host) not in taken:
                return str(host)
        raise HTTPException(503, detail=error_body(503, "tunnel address pool exhausted"))

    def record_for(pubkey: str, qkd_id: str) -> tuple[dict, bool]:
        """The peer's record, plus whether this call invented it.

        A new record is only *held* here; it reaches peers.json once WireGuard
        has actually accepted the peer, so a failed `wg set` cannot burn a
        tunnel address or resurrect a half-configured peer after a restart.
        """
        record = peers.peers.get(pubkey)
        if record is not None:
            return record, False
        record = peers.peers[pubkey] = {
            "client_ip": allocate_ip(),
            "qkd_id": qkd_id,
            "preshared_key_id": None,
            "next_refresh_at": None,
            "refresh_due": False,
        }
        return record, True

    async def new_key(qkd_id: str) -> tuple[str, bytes]:
        try:
            return await run_in_threadpool(ekm_client.new, qkd_id)
        except RuntimeError as exc:
            raise HTTPException(503, detail=error_body(503, str(exc))) from exc

    def apply_key(pubkey: str, record: dict, key_id: str, key: bytes, new_record: bool) -> None:
        try:
            wg.set_peer(pubkey, key, f"{record['client_ip']}/32")
        except Exception:
            if new_record:
                peers.peers.pop(pubkey, None)  # release the address we just held
            raise
        record["preshared_key_id"] = key_id
        record["refresh_due"] = False
        record["next_refresh_at"] = (
            app.state.clock() + dt.timedelta(seconds=settings.refresh_s)
        ).isoformat()
        peers.save()
        arm_refresh_timer(pubkey)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/api/start_connection")
    async def start_connection(body: StartConnection, claims: dict = auth) -> dict:
        email = claims.get("email")
        logger.info(f"Client {email} initiated VPN connection")
        # The interface is brought up once at startup (see __main__), so by the
        # time a client can reach this endpoint it is always the existing one.
        logger.info("Using existing vpn interface")
        logger.info(f"Fetching new key for client QKD: {body.qkd_id}")
        key_id, key = await new_key(body.qkd_id)
        logger.info(f"Got new key with keyId: {key_id}")

        record, new_record = record_for(body.public_key, body.qkd_id)
        try:
            apply_key(body.public_key, record, key_id, key, new_record)
        except RuntimeError as exc:
            logger.warning(f"Failed to configure peer {body.public_key[:8]}: {exc}")
            raise HTTPException(
                500, detail=error_body(500, "failed to configure WireGuard peer", "INTERNAL")
            ) from exc
        logger.info("Returning response to client with Interface details")
        return {
            "qkd_id": settings.server_peer,
            "server_public_key": wg.public_key(),
            "endpoint": settings.public_endpoint,
            "preshared_key_id": key_id,
            "client_ip": record["client_ip"],
            "allowed_ips": settings.allowed_ips,
            "effective_time": app.state.clock().isoformat(),
            "refresh_after_s": settings.refresh_s,
        }

    @app.post("/api/refresh_connection")
    async def refresh_connection(body: RefreshConnection, claims: dict = auth) -> dict:
        pubkey = body.public_key
        record = peers.peers.get(pubkey)
        if record is None:
            raise HTTPException(404, detail=error_body(404, "unknown peer"))
        key_id, key = await new_key(record["qkd_id"])
        effective_time = app.state.clock() + dt.timedelta(seconds=settings.activation_delay_s)
        logger.info(
            f"Scheduled PSK refresh for {claims.get('email')} with keyId: {key_id} "
            f"at {effective_time.isoformat()}"
        )

        async def activate() -> None:
            try:
                apply_key(pubkey, record, key_id, key, new_record=False)
                logger.info(f"Activated PSK {key_id}")
            except Exception as exc:  # noqa: BLE001 - a timer must not die silently
                logger.error(f"PSK activation failed for {pubkey[:8]}: {exc.__class__.__name__}")

        # A second refresh supersedes an activation that has not fired yet:
        # installing the older key afterwards would desynchronise the tunnel.
        pending = activations.pop(pubkey, None)
        if pending is not None:
            pending.cancel()
        activations[pubkey] = schedule(settings.activation_delay_s, activate)
        return {"preshared_key_id": key_id, "effective_time": effective_time.isoformat()}

    @app.get("/api/status")
    async def status(claims: dict = auth) -> dict:
        return {
            "peers": [
                {
                    "pubkey": pubkey,
                    "client_ip": r["client_ip"],
                    "preshared_key_id": r["preshared_key_id"],
                    "next_refresh_at": r["next_refresh_at"],
                    "refresh_due": r["refresh_due"],
                }
                for pubkey, r in peers.peers.items()
            ]
        }

    return app
