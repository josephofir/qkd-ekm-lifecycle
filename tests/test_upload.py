import logging
import os
import uuid

import httpx
import pytest

from qkd_ekm.common.crypto import aes_gcm_wrap, b64e
from qkd_ekm.upload.app import DirSink, UploadSettings, create_app
from qkd_ekm.vpn.app import EkmClient

BUCKET = "qkd-ekm-data"
KMS_KEY = "projects/p/locations/us/keyRings/r/cryptoKeys/external"


# --- fakes ------------------------------------------------------------------


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
                json={
                    "error": {
                        "code": 503,
                        "message": "no QKD key available",
                        "status": "UNAVAILABLE",
                    }
                },
            )
        n = len(self.calls)
        return httpx.Response(
            200,
            json={"key_id": f"key-{n}", "key": b64e(self.key_bytes(n)), "qkd_name": peer},
        )

    def key_bytes(self, n: int) -> bytes:
        return bytes([n]) * 32


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def ekm():
    return FakeEkm()


@pytest.fixture
def sink(tmp_path):
    return DirSink(str(tmp_path / "objects"), bucket=BUCKET)


@pytest.fixture
def settings():
    return UploadSettings(bucket=BUCKET, kms_key_name=KMS_KEY)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def app(ekm, sink, settings, clock):
    app = create_app(
        ekm_client=EkmClient("http://ekm.internal", "vpn-token", transport=ekm.transport()),
        sink=sink,
        settings=settings,
    )
    app.state.clock = clock
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://workload") as c:
        yield c


async def upload(client, key_id: str, filename: str, blob: bytes):
    return await client.post(
        "/api/upload",
        files={"file": (filename, blob, "application/octet-stream")},
        data={"key_id": key_id, "filename": filename},
    )


# --- file_key ---------------------------------------------------------------


async def test_file_key_allocates_a_file_purpose_key_for_the_client_qkd(client, ekm):
    resp = await client.post("/api/file_key", json={"peer": "QKD2"})
    assert resp.status_code == 200
    assert resp.json() == {"key_id": "key-1", "qkd_name": "QKD2"}
    assert ekm.calls == [("QKD2", "file")]


async def test_file_key_defaults_to_the_configured_peer(client, ekm):
    resp = await client.post("/api/file_key")
    assert resp.status_code == 200
    assert ekm.calls == [("QKD2", "file")]


async def test_file_key_logs_the_key_id_and_the_qkd_pair(client, caplog):
    with caplog.at_level(logging.INFO, logger="FileUploadServer"):
        await client.post("/api/file_key", json={"peer": "QKD2"})
    assert [r.getMessage() for r in caplog.records] == [
        "received key with Id key-1 for qkds: QKD2<-->QKD1"
    ]


async def test_file_key_is_503_when_the_ekm_has_no_key(client, ekm):
    ekm.available = False
    resp = await client.post("/api/file_key")
    assert resp.status_code == 503
    assert resp.json()["error"] == {
        "code": 503,
        "message": "no QKD key available",
        "status": "UNAVAILABLE",
    }


# --- upload -----------------------------------------------------------------


async def test_upload_decrypts_the_blob_and_writes_the_object(client, ekm, sink, tmp_path):
    key_id = (await client.post("/api/file_key")).json()["key_id"]
    plaintext = b"sensitive payload\n"
    blob = aes_gcm_wrap(ekm.key_bytes(1), plaintext, aad=b"sensitive.txt")

    resp = await upload(client, key_id, "sensitive.txt", blob)

    assert resp.status_code == 200
    body = resp.json()
    assert body["size"] == len(plaintext)
    assert body["object"].startswith(f"gs://{BUCKET}/")
    assert body["object"].endswith("_sensitive.txt")

    written = list((tmp_path / "objects").iterdir())
    assert len(written) == 1
    assert written[0].read_bytes() == plaintext
    assert written[0].name == body["object"].rsplit("/", 1)[1]


async def test_upload_logs_the_destination_the_kms_key_and_the_result(client, ekm, caplog):
    key_id = (await client.post("/api/file_key")).json()["key_id"]
    blob = aes_gcm_wrap(ekm.key_bytes(1), b"x", aad=b"sensitive.txt")

    with caplog.at_level(logging.INFO, logger="FileUploadServer"):
        caplog.clear()
        resp = await upload(client, key_id, "sensitive.txt", blob)

    object_name = resp.json()["object"].rsplit("/", 1)[1]
    assert [r.getMessage() for r in caplog.records] == [
        "retrieving key with id: key-1",
        f"uploading file to gs://{BUCKET}/{object_name}",
        f"using encryption key: {KMS_KEY}",
        "file uploaded successfully",
    ]


async def test_upload_with_an_unknown_key_id_is_404(client, ekm):
    blob = aes_gcm_wrap(ekm.key_bytes(1), b"x", aad=b"sensitive.txt")
    resp = await upload(client, "key-nope", "sensitive.txt", blob)
    assert resp.status_code == 404
    assert resp.json()["error"]["status"] == "NOT_FOUND"


async def test_upload_with_an_expired_key_id_is_404(client, ekm, clock, settings):
    key_id = (await client.post("/api/file_key")).json()["key_id"]
    clock.now += settings.file_key_ttl_s + 1
    blob = aes_gcm_wrap(ekm.key_bytes(1), b"x", aad=b"sensitive.txt")

    resp = await upload(client, key_id, "sensitive.txt", blob)
    assert resp.status_code == 404


async def test_upload_of_a_tampered_ciphertext_is_400_and_writes_nothing(
    client, ekm, tmp_path
):
    key_id = (await client.post("/api/file_key")).json()["key_id"]
    blob = bytearray(aes_gcm_wrap(ekm.key_bytes(1), b"secret", aad=b"sensitive.txt"))
    blob[-1] ^= 0xFF

    resp = await upload(client, key_id, "sensitive.txt", bytes(blob))
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert not (tmp_path / "objects").exists() or list((tmp_path / "objects").iterdir()) == []


async def test_upload_rejects_a_filename_that_does_not_match_the_aad(client, ekm):
    key_id = (await client.post("/api/file_key")).json()["key_id"]
    blob = aes_gcm_wrap(ekm.key_bytes(1), b"secret", aad=b"sensitive.txt")

    resp = await upload(client, key_id, "other.txt", blob)
    assert resp.status_code == 400


async def test_two_uploads_get_distinct_object_names(client, ekm):
    key_id = (await client.post("/api/file_key")).json()["key_id"]
    blob = aes_gcm_wrap(ekm.key_bytes(1), b"x", aad=b"a.txt")

    first = await upload(client, key_id, "a.txt", blob)
    second = await upload(client, key_id, "a.txt", blob)
    assert first.json()["object"] != second.json()["object"]


async def test_upload_without_the_form_fields_is_400_invalid_argument(client):
    resp = await client.post("/api/upload", files={"file": ("a.txt", b"x")})
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"


async def test_an_unhandled_sink_failure_is_500_internal_without_details(
    ekm, settings, clock, caplog
):
    class BrokenSink:
        def upload(self, object_name: str, data: bytes) -> str:
            raise RuntimeError("service account has no bucket permission")

    app = create_app(
        ekm_client=EkmClient("http://ekm.internal", "vpn-token", transport=ekm.transport()),
        sink=BrokenSink(),
        settings=settings,
    )
    app.state.clock = clock
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://workload") as c:
        key_id = (await c.post("/api/file_key")).json()["key_id"]
        blob = aes_gcm_wrap(ekm.key_bytes(1), b"x", aad=b"a.txt")
        with caplog.at_level(logging.ERROR, logger="FileUploadServer"):
            resp = await upload(c, key_id, "a.txt", blob)

    assert resp.status_code == 500
    assert resp.json() == {
        "error": {"code": 500, "message": "internal error", "status": "INTERNAL"}
    }
    assert "bucket permission" not in resp.text
    assert caplog.records[-1].getMessage() == "Unhandled RuntimeError on /api/upload"


async def test_a_file_key_is_reusable_within_its_ttl_so_a_retry_works(client, ekm):
    key_id = (await client.post("/api/file_key")).json()["key_id"]
    blob = aes_gcm_wrap(ekm.key_bytes(1), b"retry me", aad=b"a.txt")

    assert (await upload(client, key_id, "a.txt", blob)).status_code == 200
    assert (await upload(client, key_id, "a.txt", blob)).status_code == 200


async def test_a_very_long_filename_is_truncated_in_the_object_name(ekm, settings, clock):
    """GCS caps object names at 1024 bytes; a client cannot spend them all on a name."""

    class RecordingSink:
        def __init__(self):
            self.names: list[str] = []

        def upload(self, object_name: str, data: bytes) -> str:
            self.names.append(object_name)
            return f"gs://{BUCKET}/{object_name}"

    sink = RecordingSink()
    app = create_app(
        ekm_client=EkmClient("http://ekm.internal", "vpn-token", transport=ekm.transport()),
        sink=sink,
        settings=settings,
    )
    app.state.clock = clock
    name = "x" * 400 + ".txt"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://workload") as c:
        key_id = (await c.post("/api/file_key")).json()["key_id"]
        blob = aes_gcm_wrap(ekm.key_bytes(1), b"x", aad=name.encode())
        resp = await upload(c, key_id, name, blob)

    assert resp.status_code == 200
    assert len(sink.names[0]) == len(str(uuid.uuid4())) + 1 + 255
    assert sink.names[0].endswith("x" * 255)


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- sinks ------------------------------------------------------------------


def test_dir_sink_writes_the_object_and_returns_a_gs_uri(tmp_path):
    sink = DirSink(str(tmp_path / "objects"), bucket=BUCKET)
    uri = sink.upload("abcd_report.txt", b"hello")
    assert uri == f"gs://{BUCKET}/abcd_report.txt"
    assert (tmp_path / "objects" / "abcd_report.txt").read_bytes() == b"hello"


def test_dir_sink_keeps_the_object_name_inside_its_directory(tmp_path):
    sink = DirSink(str(tmp_path / "objects"))
    with pytest.raises(ValueError, match="object name"):
        sink.upload("../escape.txt", b"hello")
    assert not os.path.exists(tmp_path / "escape.txt")
