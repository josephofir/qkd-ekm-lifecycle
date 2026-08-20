"""FileUploadServer: the workload VM's tunnel-only ingest endpoint.

The client encrypts a file with a QKD key *before* it leaves the laptop
(`aes_gcm_wrap`, filename as AAD) and posts the ciphertext here. This server
holds the same key -- it asked the EKM for one (`purpose=file`), and the EKM
handed the *id* to the client, which fetched the material from QKD2 over the
quantum link -- so it can unwrap the blob and hand the plaintext to Cloud
Storage, where the bucket's CMEK sends KMS back to the EKM for the wrap.
That second, cloud-side protection layer is the paper's S2 path; the AES-GCM
layer here is the client-side one.

No authentication on purpose: the workload VM has no external address, its
firewall admits only the VPN tunnel CIDR, and reaching :8081 already requires
having completed the QKD-keyed WireGuard handshake. Adding a bearer token
here would be a second secret guarding a network the caller is already
inside; the tunnel *is* the authentication.

File keys are cached by id for `file_key_ttl_s` so the upload that follows a
`/api/file_key` call can be unwrapped; the same id stays usable for that
window, so a client whose POST was cut off can simply retry instead of burning
a second QKD key. An id that is unknown or older than the TTL is a 404, and
material never touches disk.

Deployment caveats. Because the endpoint is unauthenticated, anything that can
reach the tunnel can call `/api/file_key` in a loop and drain the EKM pool --
tunnel access is the only thing rationing QKD keys here, so keep the workload
VM's firewall to the tunnel CIDR and watch the EKM pool depth. And an upload is
read whole into memory (multipart body, then plaintext), which is fine for the
paper's sample files but is not a bulk transfer path.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from qkd_ekm.common.auth import error_body
from qkd_ekm.common.crypto import aes_gcm_unwrap
from qkd_ekm.common.log import get_logger

logger = get_logger("FileUploadServer")


@dataclass
class UploadSettings:
    bucket: str
    kms_key_name: str
    file_key_ttl_s: int = 600
    peer_default: str = "QKD2"


class GcsSink:
    """Cloud Storage. The bucket's CMEK is what pulls KMS through to the EKM."""

    def __init__(self, bucket: str):
        from google.cloud import storage

        self.bucket = bucket
        self._bucket = storage.Client().bucket(bucket)

    def upload(self, object_name: str, data: bytes) -> str:
        self._bucket.blob(object_name).upload_from_string(data)
        return f"gs://{self.bucket}/{object_name}"


class DirSink:
    """Local directory stand-in for GCS: same gs:// URI shape, no cloud."""

    def __init__(self, path: str, bucket: str | None = None):
        self.path = path
        self.bucket = bucket or os.path.basename(os.path.abspath(path))

    def upload(self, object_name: str, data: bytes) -> str:
        if "/" in object_name or object_name in ("", ".", ".."):
            raise ValueError(f"invalid object name: {object_name!r}")
        os.makedirs(self.path, exist_ok=True)
        with open(os.path.join(self.path, object_name), "wb") as fh:
            fh.write(data)
        return f"gs://{self.bucket}/{object_name}"


class FileKeyRequest(BaseModel):
    peer: str | None = None


#: Module-level singletons: FastAPI's field markers cannot be called inline in
#: a signature default (ruff B008).
_FILE = File(...)
_KEY_ID = Form(...)
_FILENAME = Form(...)


def create_app(ekm_client, sink, settings: UploadSettings) -> FastAPI:
    app = FastAPI()
    app.state.clock = time.monotonic
    keys: dict[str, tuple[bytes, float]] = {}

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
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=400, content=error_body(400, "invalid request"))

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Class name only: an exception message can carry internal detail the
        # caller has no business seeing.
        logger.error(f"Unhandled {exc.__class__.__name__} on {request.url.path}")
        return JSONResponse(status_code=500, content=error_body(500, "internal error"))

    def prune_keys() -> None:
        now = app.state.clock()
        for expired in [kid for kid, (_, expiry) in keys.items() if expiry <= now]:
            del keys[expired]

    def get_key(key_id: str) -> bytes:
        """The cached key for `key_id`; reusable until its TTL so retries work."""
        prune_keys()
        entry = keys.get(key_id)
        if entry is None:
            raise HTTPException(404, detail=error_body(404, f"unknown key id {key_id}"))
        return entry[0]

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/api/file_key")
    async def file_key(body: FileKeyRequest | None = None) -> dict:
        peer = (body.peer if body else None) or settings.peer_default
        prune_keys()
        try:
            key_id, key = await run_in_threadpool(ekm_client.new, peer, "file")
        except RuntimeError as exc:
            raise HTTPException(503, detail=error_body(503, str(exc))) from exc
        keys[key_id] = (key, app.state.clock() + settings.file_key_ttl_s)
        logger.info(f"received key with Id {key_id} for qkds: {peer}<-->QKD1")
        return {"key_id": key_id, "qkd_name": peer}

    @app.post("/api/upload")
    async def upload(
        file: UploadFile = _FILE,
        key_id: str = _KEY_ID,
        filename: str = _FILENAME,
    ) -> dict:
        logger.info(f"retrieving key with id: {key_id}")
        key = get_key(key_id)
        try:
            plaintext = aes_gcm_unwrap(key, await file.read(), aad=filename.encode())
        except ValueError as exc:
            raise HTTPException(400, detail=error_body(400, "decryption failed")) from exc

        # uuid prefix: two clients uploading the same name must not overwrite
        # each other, and the object name is the paper's `<uuid>_<file>`.
        object_name = f"{uuid.uuid4()}_{os.path.basename(filename)[:255]}"
        logger.info(f"uploading file to gs://{settings.bucket}/{object_name}")
        logger.info(f"using encryption key: {settings.kms_key_name}")
        uri = await run_in_threadpool(sink.upload, object_name, plaintext)
        logger.info("file uploaded successfully")
        return {"object": uri, "size": len(plaintext)}

    return app
