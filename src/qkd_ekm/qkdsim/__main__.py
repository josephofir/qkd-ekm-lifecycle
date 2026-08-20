"""Entry point for the QKD simulator service (console script ``qkd-ekm-qkdsim``)."""

from __future__ import annotations

import uvicorn

from qkd_ekm.common.settings import env
from qkd_ekm.qkdsim.app import create_app
from qkd_ekm.qkdsim.store import KeyStore


def main() -> None:
    store = KeyStore()
    app = create_app(store)

    port = int(env("SIM_PORT", 8200))
    ssl_kwargs = {}
    cert, key = env("SIM_TLS_CERT"), env("SIM_TLS_KEY")
    if cert and key:
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}

    # NOTE: binds all interfaces intentionally -- this is the VM's public
    # service (qkd-sim-vm), firewalled by the terraform config, not a laptop tool.
    uvicorn.run(app, host="0.0.0.0", port=port, **ssl_kwargs)


if __name__ == "__main__":
    main()
