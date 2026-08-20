"""Entry point for the EKM service (console script ``qkd-ekm-ekm``)."""

from __future__ import annotations

import os

import uvicorn

from qkd_ekm.common.auth import GoogleJwtVerifier, StaticTokenVerifier
from qkd_ekm.common.log import get_logger
from qkd_ekm.common.settings import env
from qkd_ekm.ekm.app import EkmSettings, create_app
from qkd_ekm.ekm.pool import KeyPool
from qkd_ekm.ekm.store import Store, load_local_key
from qkd_ekm.qkd.etsi014 import Etsi014Client

logger = get_logger("EKM")


def ssl_options() -> dict:
    """uvicorn TLS kwargs, refusing to start plaintext unless told to.

    Cloud KMS and the VPN token both travel on this listener, so a missing
    certificate must be a startup failure rather than a silent downgrade to
    HTTP; `EKM_PLAINTEXT_HTTP=1` is the explicit local-dev opt-out.
    """
    cert, key = env("EKM_TLS_CERT"), env("EKM_TLS_KEY")
    if cert and key:
        return {"ssl_certfile": cert, "ssl_keyfile": key}
    if env("EKM_PLAINTEXT_HTTP") == "1":
        logger.warning("WARNING serving plaintext HTTP")
        return {}
    raise RuntimeError("EKM_TLS_CERT and EKM_TLS_KEY are required")


def build_app():
    db_path = env("EKM_DB", "/var/lib/qkd-ekm/ekm.db")
    local_key_path = env("EKM_LOCAL_KEY_FILE", "/var/lib/qkd-ekm/local.key")
    for path in (db_path, local_key_path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    store = Store(db_path, load_local_key(local_key_path))
    qkd = Etsi014Client(
        base_url=env("QKD1_URL", required=True),
        ca_file=env("QKD_CA_FILE"),
        token=env("QKD_TOKEN"),
    )
    peers = [p.strip() for p in env("EKM_PEERS", "QKD2").split(",") if p.strip()]
    pool = KeyPool(
        store,
        qkd,
        peers,
        target=int(env("EKM_POOL_TARGET", 50)),
        ttl_s=float(env("EKM_POOL_TTL", 600)),
    )
    audiences = {a.strip() for a in env("EKM_JWT_AUDIENCES", "").split(",") if a.strip()}
    verifier_kms = GoogleJwtVerifier(
        allowed_emails={env("EKMS_SA_EMAIL", required=True)},
        audiences=audiences or None,
    )
    verifier_vpn = StaticTokenVerifier(env("VPN_TOKEN", required=True))
    settings = EkmSettings(
        bind_peer=env("EKM_BIND_PEER", peers[0] if peers else "QKD2"),
        pull_interval_s=float(env("EKM_PULL_INTERVAL", 2)),
    )
    return create_app(store, pool, verifier_kms, verifier_vpn, settings)


def main() -> None:
    ssl_kwargs = ssl_options()  # fail before opening the database or a socket
    app = build_app()

    # NOTE: binds all interfaces intentionally -- Cloud KMS reaches this VM
    # through the EKM VPC connection; the terraform firewall is the boundary.
    uvicorn.run(app, host="0.0.0.0", port=int(env("EKM_PORT", 8443)), **ssl_kwargs)


if __name__ == "__main__":
    main()
