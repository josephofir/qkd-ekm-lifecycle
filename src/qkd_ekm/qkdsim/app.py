"""ETSI GS QKD 014 key delivery + HEQA-shaped monitoring simulator.

Stands in for the two HEQA Sceptre appliances (QKD1 = master/cloud-side,
QKD2 = slave/client-side) as a single FastAPI process, so the rest of the
pipeline -- EKM, VPN client, ``capture_qkd.py`` -- can run against a local
process instead of real hardware.

Two independent auth schemes, matching the real deployment:

* ETSI-014 routes (``/api/v1/keys/...``) and ``/sim/*`` stand in for HEQA's
  mTLS/client-cert story with a static bearer token (``SIM_TOKEN``, env);
  when unset, those routes are open (matches the local/dev default).
* Monitoring routes (``/monitoring/*``, ``/kms/*``) require a JWT obtained
  from ``POST /auth/login`` (``SIM_USER``/``SIM_PASSWORD``), signed with a
  random per-process secret -- independent of ``SIM_TOKEN``.
"""

from __future__ import annotations

import datetime as dt
import secrets

import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from qkd_ekm.common.crypto import b64e
from qkd_ekm.common.log import get_logger
from qkd_ekm.common.settings import env
from qkd_ekm.qkdsim.store import KeyStore

logger = get_logger("QKDSim")

_JWT_ALG = "HS256"
_MAX_KEY_PER_REQUEST = 100
_MAX_KEY_SIZE = 8192
_MIN_KEY_SIZE = 8

# Paper Table-4 defaults (overridable via env for the fault-injection scenarios).
_SIGNAL_STATES_QBER = [0.19, 1.04, 1.45, 0.27, 0.83, 0.69]
_DECOY_STATES_QBER = [0.48, 1.53, 1.65, 0.69, 1.20, 0.48]


def create_app(store: KeyStore) -> FastAPI:
    app = FastAPI()

    saes = [s.strip() for s in env("SIM_SAES", "QKD1,QKD2").split(",") if s.strip()]
    username = env("SIM_USER", "admin")
    password = env("SIM_PASSWORD", "admin")
    sim_token = env("SIM_TOKEN")
    secure_bit_rate = float(env("SIM_SECURE_BIT_RATE", store.rate_units_per_s * 256))
    signal_qber = float(env("SIM_SIGNAL_QBER", 0.80))
    decoy_qber = float(env("SIM_DECOY_QBER", 1.00))
    jwt_secret = secrets.token_hex(32)

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"message": exc.detail})

    def _master_for(slave_sae: str) -> str:
        others = [s for s in saes if s != slave_sae]
        return others[0] if others else slave_sae

    def _require_sim_token(request: Request) -> None:
        if sim_token is None:
            return
        if request.headers.get("authorization") != f"Bearer {sim_token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    def _require_jwt(request: Request) -> None:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="unauthorized")
        token = authorization.removeprefix("Bearer ")
        try:
            jwt.decode(token, jwt_secret, algorithms=[_JWT_ALG])
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="unauthorized") from exc

    def _require_sae(sae: str) -> None:
        if sae not in saes:
            raise HTTPException(status_code=404, detail="unknown SAE")

    def _status_extension() -> dict:
        return {
            "current_bits": store.generated_bits - store.deleted_bits,
            "maximum_key_length": _MAX_KEY_SIZE,
            "num_of_256_keys_available": store.available_256bit_keys,
            "bits_consumed_from_the_beginning": store.consumed_bits,
            "keys_consumed_from_the_beginning": store.consumed_keys,
            "key_requests_from_the_beginning": store.key_requests,
            "num_failed_key_requests_from_the_beginning": store.failed_key_requests,
            "total_generated_bits": store.generated_bits,
            "deleted_bits": store.deleted_bits,
        }

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/auth/login")
    async def login(request: Request) -> dict:
        body = await request.json()
        if body.get("username") != username or body.get("password") != password:
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = jwt.encode(
            {"sub": username, "iat": int(dt.datetime.now(dt.UTC).timestamp())},
            jwt_secret,
            algorithm=_JWT_ALG,
        )
        return {"token": token}

    @app.get("/api/v1/keys/{sae}/status")
    async def status(sae: str, request: Request) -> dict:
        _require_sim_token(request)
        _require_sae(sae)
        return {
            "slave_SAE_ID": sae,
            "master_SAE_ID": _master_for(sae),
            "key_size": 256,
            "stored_key_count": store.pending_count,
            "max_key_count": store.inventory,
            "max_key_per_request": _MAX_KEY_PER_REQUEST,
            "max_key_size": _MAX_KEY_SIZE,
            "min_key_size": _MIN_KEY_SIZE,
            "status_extension": _status_extension(),
        }

    @app.get("/api/v1/keys/{slave_sae}/enc_keys")
    async def enc_keys(slave_sae: str, request: Request) -> dict:
        _require_sim_token(request)
        _require_sae(slave_sae)
        # A real appliance answers 400 for a malformed or oversized `number`
        # rather than 500; `max_key_per_request` is what /status advertises.
        try:
            number = int(request.query_params.get("number", "1"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid number") from exc
        if number < 1 or number > _MAX_KEY_PER_REQUEST:
            raise HTTPException(status_code=400, detail="invalid number")
        master_sae = _master_for(slave_sae)
        if not store.source_available:
            store.failed_key_requests += 1
            raise HTTPException(status_code=503, detail="source unavailable")
        store.key_requests += 1
        keys = [store.new_key((master_sae, slave_sae)) for _ in range(number)]
        logger.info(f"Issued {number} key(s) for {master_sae}->{slave_sae}")
        return {"keys": [{"key_ID": k.key_id, "key": b64e(k.key)} for k in keys]}

    async def _dec_key_ids(master_sae: str, request: Request) -> list[str]:
        if request.method == "POST":
            body = await request.json()
            return [item["key_ID"] for item in body.get("key_IDs", [])]
        key_id = request.query_params.get("key_ID")
        if key_id is None:
            raise HTTPException(status_code=400, detail="missing key_ID")
        return [key_id]

    @app.api_route("/api/v1/keys/{master_sae}/dec_keys", methods=["GET", "POST"])
    async def dec_keys(master_sae: str, request: Request) -> dict:
        _require_sim_token(request)
        _require_sae(master_sae)
        key_ids = await _dec_key_ids(master_sae, request)
        keys = []
        for key_id in key_ids:
            key = store.get_by_id(key_id)
            if key is None:
                raise HTTPException(status_code=404, detail="unknown key_ID")
            if store.is_consumed(key_id):
                raise HTTPException(status_code=400, detail="key already consumed")
            keys.append(store.consume(key_id))
        return {"keys": [{"key_ID": k.key_id, "key": b64e(k.key)} for k in keys]}

    @app.get("/kms/key-servers")
    async def key_servers(request: Request) -> list[dict]:
        _require_jwt(request)
        master = saes[0] if saes else ""
        slave = saes[1] if len(saes) > 1 else master
        return [{"id": f"{master}-{slave}", "masterSAE": master, "slaveSAE": slave}]

    @app.get("/monitoring/qkd-qtx/{metric}/current")
    async def monitoring(metric: str, request: Request) -> dict:
        _require_jwt(request)
        values = {
            "secure-bit-rate": secure_bit_rate,
            "signal-qber": signal_qber,
            "decoy-qber": decoy_qber,
            "signal-states-qber": _SIGNAL_STATES_QBER,
            "decoy-states-qber": _DECOY_STATES_QBER,
        }
        if metric not in values:
            raise HTTPException(status_code=404, detail="unknown metric")
        now = dt.datetime.now(dt.UTC).isoformat()
        return {"time": now, "value": values[metric]}

    @app.post("/sim/source")
    async def sim_source(request: Request) -> dict:
        _require_sim_token(request)
        body = await request.json()
        store.source_available = bool(body.get("available", True))
        return {"source_available": store.source_available}

    @app.get("/sim/state")
    async def sim_state(request: Request) -> dict:
        _require_sim_token(request)
        return {
            "source_available": store.source_available,
            "consumed_keys": store.consumed_keys,
            "consumed_bits": store.consumed_bits,
            "key_requests": store.key_requests,
            "failed_key_requests": store.failed_key_requests,
            "generated_bits": store.generated_bits,
            "deleted_bits": store.deleted_bits,
            "available_256bit_keys": store.available_256bit_keys,
        }

    return app
