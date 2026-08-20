"""EKM HTTP service: Cloud EKM wrap/unwrap, VPN key delivery, lifecycle state.

Two very different callers share one process:

* Cloud KMS calls `POST /api/keys/{key_id}:wrap|:unwrap` (Google OIDC JWT).
  The first wrap of an unseen `key_id` allocates one QKD unit from the pool
  and *persists* the binding before answering, so the external key is tied to
  a specific quantum-distributed unit for its whole life -- a later unwrap
  must find that same unit or fail closed.
* The VPN client and the upload server call `GET /api/{peer}/new` (shared
  bearer token) for a fresh, never-reused QKD key.

`GET /api/state` reports the paper's lifecycle mode, derived live from pool /
source / binding state by `lifecycle.derive`, plus the two operator-driven
inputs (`/api/authority`, `/api/recovery/ack`) that the continuity scenario
uses to exercise SUSPENDED and RECOVERY.
"""

from __future__ import annotations

import binascii
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from qkd_ekm.common.auth import bearer_dep, error_body
from qkd_ekm.common.crypto import aes_gcm_unwrap, aes_gcm_wrap, b64d, b64e
from qkd_ekm.common.log import get_logger
from qkd_ekm.ekm.lifecycle import derive
from qkd_ekm.ekm.pool import PoolEmpty
from qkd_ekm.ekm.store import AlreadyBound

logger = get_logger("EKM")

_PURPOSES = ("vpn", "file")


@dataclass
class EkmSettings:
    bind_peer: str = "QKD2"
    pull_interval_s: float = 2.0


class Authority(BaseModel):
    continuity_authority: bool


def _b64d(value: str | None) -> bytes:
    try:
        return b64d(value or "")
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, detail=error_body(400, "malformed base64")) from exc


def create_app(store, pool, verifier_kms, verifier_vpn, settings: EkmSettings | None = None):
    settings = settings or EkmSettings()
    _verify_kms = bearer_dep(verifier_kms)
    seen_kms_token = False

    async def kms_claims(request: Request) -> dict:
        """Claims of an accepted Cloud KMS token, announced once per process.

        The `aud` Cloud KMS presents is not knowable before the first live
        call, and `ekm_jwt_audiences` cannot be populated without it. Logging
        it once -- claims only, never the token -- is what lets the operator
        turn the audience check on after the first apply.
        """
        nonlocal seen_kms_token
        claims = await _verify_kms(request)
        if not seen_kms_token:
            seen_kms_token = True
            logger.info(
                f"Accepted KMS token aud={claims.get('aud')} email={claims.get('email')}"
            )
        return claims

    kms_auth = Depends(kms_claims)
    vpn_auth = Depends(bearer_dep(verifier_vpn))
    state = {
        "continuity_authority": True,
        "recovery_pending": False,
        "source_seen": bool(pool.source_available),
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = pool.run_background(settings.pull_interval_s)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    app = FastAPI(lifespan=lifespan)

    # Registered on the Starlette base class so framework-raised 404/405s land
    # in the same envelope as ours (FastAPI's HTTPException is a subclass).
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        body = detail if isinstance(detail, dict) and "error" in detail else None
        return JSONResponse(
            status_code=exc.status_code,
            content=body or error_body(exc.status_code, str(detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content=error_body(400, "invalid request body"))

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Class name only: an exception message can carry internal detail the
        # caller has no business seeing.
        logger.error(f"Unhandled {exc.__class__.__name__} on {request.url.path}")
        return JSONResponse(status_code=500, content=error_body(500, "internal error"))

    def check_recovery() -> None:
        """A QKD source that came back needs an explicit ack before READY."""
        available = bool(pool.source_available)
        if available and not state["source_seen"]:
            state["recovery_pending"] = True
            logger.info("QKD source returned, recovery pending")
        state["source_seen"] = available

    def allocate(peer: str):
        try:
            return pool.allocate(peer)
        except KeyError as exc:
            raise HTTPException(404, detail=error_body(404, f"unknown peer {peer}")) from exc
        except PoolEmpty as exc:
            raise HTTPException(503, detail=error_body(503, "no QKD key available")) from exc

    def bind(qkd_key_id: str, purpose: str, object_id: str, peer: str) -> None:
        try:
            store.bind(qkd_key_id, purpose, object_id, peer)
        except AlreadyBound as exc:
            raise HTTPException(
                400, detail=error_body(400, "key already bound", "FAILED_PRECONDITION")
            ) from exc

    async def json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, detail=error_body(400, "malformed JSON body")) from exc
        if not isinstance(body, dict):
            raise HTTPException(400, detail=error_body(400, "body must be an object"))
        return body

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/api/keys/{key_id_op:path}")
    async def wrap_or_unwrap(key_id_op: str, request: Request, claims: dict = kms_auth) -> dict:
        key_id, _, operation = key_id_op.rpartition(":")
        if operation not in ("wrap", "unwrap") or not key_id:
            raise HTTPException(404, detail=error_body(404, f"unknown operation {key_id_op}"))
        body = await json_body(request)
        aad = _b64d(body.get("additionalAuthenticatedData"))
        if operation == "wrap":
            return await _wrap(key_id, body, aad)
        return await _unwrap(key_id, body, aad)

    # NOTE: insurance for the first cloud run. Cloud EKM is documented to use
    # POST :wrap / :unwrap only, but the connection-validation path may probe the
    # key path with a plain GET; answering it is harmless (no key material, no
    # allocation, same KMS-token check) and cheaper than a failed apply.
    @app.get("/api/keys/{key_id_op:path}")
    async def describe_key(key_id_op: str, claims: dict = kms_auth) -> dict:
        if ":" in key_id_op:
            raise HTTPException(404, detail=error_body(404, f"unknown operation {key_id_op}"))
        return {"name": key_id_op, "keyManagementMode": "MANUAL"}

    async def _wrap(key_id: str, body: dict, aad: bytes) -> dict:
        logger.info(f"Got Key Wrap request with KEKId: {key_id}")
        check_recovery()
        plaintext = _b64d(body.get("plaintext"))
        kek = store.get_kek(key_id)
        if kek is None:
            key = allocate(settings.bind_peer)
            # Material first, binding second. The reverse order is not
            # recoverable: a failure between the two burns the UNIQUE(purpose,
            # object_id) binding row for `key_id` with no KEK behind it, and
            # every later wrap of that id then fails 400 for ever. This way a
            # crash after put_kek leaves the KEK findable on retry, and a crash
            # before it leaves nothing at all. The QKD unit is never reused
            # either way: allocate() pops it out of the pool.
            store.put_kek(key_id, key.key_id, key.key)
            try:
                store.bind(key.key_id, "ekm", key_id, settings.bind_peer)
            except AlreadyBound:
                # An earlier attempt bound `key_id` and then lost its material
                # (get_kek was None above); the re-put is the recovery.
                logger.warning(f"Re-keyed external key {key_id} after a partial first use")
            kek = key.key
            logger.info(f"Bound external key {key_id} to QKD key {key.key_id}")
        return {"wrappedBlob": b64e(aes_gcm_wrap(kek, plaintext, aad))}

    async def _unwrap(key_id: str, body: dict, aad: bytes) -> dict:
        logger.info(f"Got Key Unwrap request with KEKId: {key_id}")
        kek = store.get_kek(key_id)
        if kek is None:
            raise HTTPException(404, detail=error_body(404, f"unknown key {key_id}"))
        try:
            plaintext = aes_gcm_unwrap(kek, _b64d(body.get("wrappedBlob")), aad)
        except ValueError as exc:
            raise HTTPException(400, detail=error_body(400, "unwrap failed")) from exc
        return {"plaintext": b64e(plaintext)}

    @app.get("/api/state")
    async def read_state(claims: dict = vpn_auth) -> dict:
        check_recovery()
        sizes = {peer: pool.size(peer) for peer in pool.peers}
        bindings_count = store.count()
        # NOTE: every binding in the EKM's own database was created by a
        # wrap or by /new, so a non-zero count *is* an authoritative binding;
        # Store exposes no per-purpose count and this task must not change it.
        modes = derive(
            source_available=bool(pool.source_available),
            ekm_reachable=True,
            pool_size=sum(sizes.values()),
            has_authoritative_binding=bindings_count > 0,
            continuity_authority=state["continuity_authority"],
            recovery_pending=state["recovery_pending"],
        )
        return {
            **modes,
            "pool": sizes,
            "bindings_count": bindings_count,
            "source_available": bool(pool.source_available),
            "continuity_authority": state["continuity_authority"],
            "recovery_pending": state["recovery_pending"],
        }

    @app.post("/api/authority")
    async def set_authority(body: Authority, claims: dict = vpn_auth) -> dict:
        state["continuity_authority"] = body.continuity_authority
        logger.info(f"Continuity authority set to {body.continuity_authority}")
        return {"continuity_authority": body.continuity_authority}

    @app.post("/api/recovery/ack")
    async def ack_recovery(claims: dict = vpn_auth) -> dict:
        state["recovery_pending"] = False
        logger.info("Recovery acknowledged")
        return {"recovery_pending": False}

    @app.get("/api/{peer}/new")
    async def new_key(peer: str, purpose: str = "vpn", claims: dict = vpn_auth) -> dict:
        logger.info(f"Got new key request for instance {peer}")
        if purpose not in _PURPOSES:
            raise HTTPException(400, detail=error_body(400, f"unknown purpose {purpose}"))
        logger.info(f"Retrieving 1 keys between QKD1 and {peer} from pool")
        key = allocate(peer)
        bind(key.key_id, purpose, f"{peer}:{key.key_id}", peer)
        return {"key_id": key.key_id, "key": b64e(key.key), "qkd_name": peer}

    return app
