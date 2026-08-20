"""Entry point for the VPN server (console script ``qkd-ekm-vpn``).

Brings the WireGuard interface, forwarding and NAT up *before* serving, so a
client that calls `/api/start_connection` immediately after boot finds a
working data plane.
"""

from __future__ import annotations

import uvicorn

from qkd_ekm.common.auth import GoogleJwtVerifier
from qkd_ekm.common.settings import env
from qkd_ekm.vpn.app import EkmClient, VpnSettings, create_app
from qkd_ekm.vpn.wg import WG, ensure_private_key

_DEFAULT_AUDIENCES = "32555940559.apps.googleusercontent.com,qkd-ekm-vpn"


def _split(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def settings_from_env(address: str, tunnel_cidr: str) -> VpnSettings:
    """VpnSettings from the environment (`/etc/qkd-ekm/env`, written by Terraform)."""
    return VpnSettings(
        public_endpoint=env("VPN_PUBLIC_ENDPOINT", required=True),
        tunnel_cidr=tunnel_cidr,
        server_address=address,
        # The routes the client installs on its side of the tunnel. Terraform
        # sets this to "<qkd subnet>,<tunnel cidr>"; the dataclass default is
        # the same pair for the local/dev stack.
        allowed_ips=env("VPN_ALLOWED_IPS", VpnSettings.allowed_ips),
        refresh_s=int(env("VPN_REFRESH_S", 3600)),
        activation_delay_s=int(env("VPN_ACTIVATION_DELAY_S", 30)),
        peers_file=env("VPN_PEERS_FILE", "/var/lib/qkd-ekm/peers.json"),
    )


def build_app():
    address = env("VPN_ADDRESS", "10.20.0.1/24")
    tunnel_cidr = env("VPN_TUNNEL_CIDR", "10.20.0.0/24")
    wg = WG(iface=env("VPN_IFACE", "wg0"))
    key_file = ensure_private_key(env("VPN_PRIVATE_KEY_FILE", "/etc/qkd-ekm/wg_private.key"))
    wg.ensure_interface(key_file, int(env("VPN_LISTEN_PORT", 51819)), address)
    wg.ensure_ip_forward()
    wg.ensure_masquerade(tunnel_cidr)

    settings = settings_from_env(address, tunnel_cidr)
    ekm_client = EkmClient(
        base_url=env("EKM_URL", required=True),
        vpn_token=env("VPN_TOKEN", required=True),
        ca_file=env("EKM_CA_FILE"),
    )
    verifier = GoogleJwtVerifier(
        allowed_emails=_split(env("VPN_ALLOWED_EMAILS", required=True)),
        audiences=_split(env("VPN_ALLOWED_AUDIENCES", _DEFAULT_AUDIENCES)) or None,
        component="VPNServer",
    )
    return create_app(wg, ekm_client, verifier, settings)


def main() -> None:
    # NOTE: plain HTTP on all interfaces -- operators reach the control API
    # through an IAP TCP tunnel, and the firewall allows :8080 from nowhere else.
    uvicorn.run(build_app(), host="0.0.0.0", port=int(env("VPN_PORT", 8080)))


if __name__ == "__main__":
    main()
