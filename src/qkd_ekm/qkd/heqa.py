"""HEQA appliance monitoring client.

Talks to the HEQA public REST API (JWT-authenticated, distinct from the
ETSI-014 key-delivery API in :mod:`qkd_ekm.qkd.etsi014`) to pull the
dashboard values transcribed in the paper's Table 4.
"""

from __future__ import annotations

import httpx

from qkd_ekm.qkd.etsi014 import QkdUnavailable

# Table-4 fields sourced directly from a /monitoring/... "current" endpoint.
_RATE_PATHS = {
    "secure_bit_rate": "/monitoring/qkd-qtx/secure-bit-rate/current",
    "signal_qber": "/monitoring/qkd-qtx/signal-qber/current",
    "weak_decoy_qber": "/monitoring/qkd-qtx/decoy-qber/current",
    "signal_qber_per_state": "/monitoring/qkd-qtx/signal-states-qber/current",
    "weak_decoy_qber_per_state": "/monitoring/qkd-qtx/decoy-states-qber/current",
}

# Table-4 key-usage counters: the public API doesn't expose these per a
# dedicated monitoring endpoint. Best effort: discover a SAE id from
# /kms/key-servers, then read the ETSI-status `status_extension` block that
# the HEQA firmware attaches to /api/v1/keys/{sae}/status.
_COUNTER_FIELDS = {
    "available_secured_bits": "current_bits",
    "max_key_length": "maximum_key_length",
    "available_256bit_keys": "num_of_256_keys_available",
    "consumed_bits": "bits_consumed_from_the_beginning",
    "consumed_keys": "keys_consumed_from_the_beginning",
    "key_requests": "key_requests_from_the_beginning",
    "failed_key_requests": "num_failed_key_requests_from_the_beginning",
    "generated_bits": "total_generated_bits",
    "deleted_bits": "deleted_bits",
}

_TABLE4_KEYS = (
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
)


class HeqaMonitor:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify: bool | str = True,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._username = username
        self._password = password
        self.token: str | None = None
        self._client = httpx.Client(
            base_url=base_url, verify=verify, timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def login(self) -> str:
        try:
            resp = self._client.post(
                "/auth/login", json={"username": self._username, "password": self._password}
            )
        except httpx.TransportError as exc:
            raise QkdUnavailable(f"POST /auth/login: {exc}") from exc
        if resp.status_code == 503 or resp.status_code >= 500:
            raise QkdUnavailable(f"POST /auth/login: HTTP {resp.status_code}")
        resp.raise_for_status()
        self.token = resp.json()["token"]
        return self.token

    def get(self, path: str) -> dict:
        if self.token is None:
            self.login()
        try:
            resp = self._client.get(path, headers={"Authorization": f"Bearer {self.token}"})
        except httpx.TransportError as exc:
            raise QkdUnavailable(f"GET {path}: {exc}") from exc
        if resp.status_code == 503 or resp.status_code >= 500:
            raise QkdUnavailable(f"GET {path}: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def capture(self) -> dict:
        result: dict = dict.fromkeys(_TABLE4_KEYS)

        for field, path in _RATE_PATHS.items():
            try:
                result[field] = self.get(path)["value"]
            except Exception:  # noqa: BLE001, S110 - best effort per Table-4 field, missing -> None
                pass

        if result["secure_bit_rate"] is not None:
            result["secure_key_rate_256"] = result["secure_bit_rate"] / 256

        try:
            servers = self.get("/kms/key-servers")
            sae = servers[0]["slaveSAE"]
            ext = self.get(f"/api/v1/keys/{sae}/status")["status_extension"]
            for field, ext_key in _COUNTER_FIELDS.items():
                result[field] = ext.get(ext_key)
        except Exception:  # noqa: BLE001, S110 - counters are best effort, missing -> None
            pass

        return result
