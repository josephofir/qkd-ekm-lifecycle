"""WireGuard interface manager: every `wg`/`ip`/`iptables` call the server makes.

`runner` is `subprocess.run` in production and a recording fake in tests, so
the exact argv this module produces is itself under test -- these commands are
the part of the system that cannot be exercised without root and a kernel
module.

Pre-shared keys never appear on a command line (`ps` is world-readable):
`wg set … preshared-key <file>` reads them from a 0600 temp file that is
deleted in a `finally`, whatever `wg` does.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from qkd_ekm.common.crypto import b64e
from qkd_ekm.common.log import get_logger

logger = get_logger("VPNServer")


class WG:
    def __init__(self, iface: str = "wg0", runner=subprocess.run):
        self.iface = iface
        self._runner = runner

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = self._runner(list(args), capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise RuntimeError(f"{' '.join(args)}: {(result.stderr or result.stdout).strip()}")
        return result

    def exists(self) -> bool:
        return self._run("ip", "link", "show", self.iface, check=False).returncode == 0

    def ensure_interface(self, private_key_file: str, listen_port: int, address: str) -> None:
        if not self.exists():
            self._run("ip", "link", "add", self.iface, "type", "wireguard")
            logger.info(f"Created vpn interface {self.iface}")
        self._run(
            "wg", "set", self.iface, "listen-port", str(listen_port),
            "private-key", private_key_file,
        )  # fmt: skip
        # Tolerated: re-adding the address after a restart is "RTNETLINK: File exists".
        self._run("ip", "addr", "add", address, "dev", self.iface, check=False)
        self._run("ip", "link", "set", self.iface, "up")

    def public_key(self) -> str:
        return self._run("wg", "show", self.iface, "public-key").stdout.strip()

    def set_peer(self, pubkey: str, psk: bytes, allowed_ips: str) -> None:
        fd, path = tempfile.mkstemp(prefix="wg-psk-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(b64e(psk))
            self._run(
                "wg", "set", self.iface, "peer", pubkey,
                "preshared-key", path, "allowed-ips", allowed_ips,
            )  # fmt: skip
        finally:
            os.unlink(path)

    def remove_peer(self, pubkey: str) -> None:
        self._run("wg", "set", self.iface, "peer", pubkey, "remove")

    def show(self) -> str:
        return self._run("wg", "show", self.iface).stdout.strip()

    def ensure_ip_forward(self) -> None:
        self._run("sysctl", "-w", "net.ipv4.ip_forward=1")

    def default_route_iface(self) -> str:
        out = self._run("ip", "-o", "-4", "route", "show", "to", "default").stdout.split()
        return out[out.index("dev") + 1]

    def ensure_masquerade(self, tunnel_cidr: str) -> None:
        """NAT tunnel traffic out of the VM's uplink, once."""
        rule = (
            "POSTROUTING", "-s", tunnel_cidr,
            "-o", self.default_route_iface(), "-j", "MASQUERADE",
        )  # fmt: skip
        if self._run("iptables", "-t", "nat", "-C", *rule, check=False).returncode != 0:
            self._run("iptables", "-t", "nat", "-A", *rule)
            logger.info(f"Added MASQUERADE rule for {tunnel_cidr}")


def ensure_private_key(path: str, runner=subprocess.run) -> str:
    """Generate the server's WireGuard private key on first boot (0600)."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        result = runner(["wg", "genkey"], capture_output=True, text=True, check=False)
        key = result.stdout.strip()
        if result.returncode != 0 or not key:
            raise RuntimeError(f"wg genkey failed: {(result.stderr or '').strip()}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key + "\n")
        logger.info(f"Generated WireGuard private key at {path}")
    return path
