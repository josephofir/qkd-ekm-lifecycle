"""ETSI GS QKD 014 key delivery client.

Talks to a QKD key management entity (KME) over the ETSI-014 REST API —
either a real HEQA appliance (TLS on :8200, optional bearer token) or the
in-repo simulator (``qkd_ekm.qkdsim``).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from qkd_ekm.common.crypto import b64d


class QkdUnavailable(RuntimeError):
    """The QKD source is unreachable: connection error, timeout, 5xx, or 503."""


class QkdError(RuntimeError):
    """The KME rejected the request (4xx)."""


@dataclass(frozen=True)
class Key:
    key_id: str
    key: bytes


class Etsi014Client:
    def __init__(
        self,
        base_url: str,
        ca_file: str | None = None,
        token: str | None = None,
        cert: tuple | None = None,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=base_url,
            verify=ca_file or True,
            cert=cert,
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise QkdUnavailable(f"{method} {path}: {exc}") from exc
        if resp.status_code == 503 or resp.status_code >= 500:
            raise QkdUnavailable(f"{method} {path}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise QkdError(f"{method} {path}: HTTP {resp.status_code}")
        return resp.json()

    def get_status(self, slave_sae: str) -> dict:
        return self._request("GET", f"/api/v1/keys/{slave_sae}/status")

    def enc_keys(self, slave_sae: str, number: int = 1, size: int = 256) -> list[Key]:
        data = self._request(
            "GET",
            f"/api/v1/keys/{slave_sae}/enc_keys",
            params={"number": number, "size": size},
        )
        return [Key(k["key_ID"], b64d(k["key"])) for k in data["keys"]]

    def dec_keys(self, master_sae: str, key_ids: list[str]) -> list[Key]:
        if len(key_ids) == 1:
            data = self._request(
                "GET",
                f"/api/v1/keys/{master_sae}/dec_keys",
                params={"key_ID": key_ids[0]},
            )
        else:
            data = self._request(
                "POST",
                f"/api/v1/keys/{master_sae}/dec_keys",
                json={"key_IDs": [{"key_ID": kid} for kid in key_ids]},
            )
        return [Key(k["key_ID"], b64d(k["key"])) for k in data["keys"]]
