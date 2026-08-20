"""Client-side WireGuard control: every `wg`/`wg-quick` call the CLI makes.

Mirrors `qkd_ekm.vpn.wg`: `runner` is `subprocess.run` in production and a
recording fake in tests, so the exact argv is under test even though the real
commands need root. Arguments are always a list -- never a shell string --
and pre-shared keys are handed to `wg` through a 0600 temp file rather than a
command line, because `ps` is world-readable.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from qkd_ekm.common.crypto import b64e

_CONF_TEMPLATE = """[Interface]
PrivateKey = {priv}
Address = {address}

[Peer]
PublicKey = {server_pub}
PresharedKey = {psk}
Endpoint = {endpoint}
AllowedIPs = {allowed_ips}
PersistentKeepalive = {keepalive}
"""


class WGClient:
    def __init__(
        self,
        iface: str = "wgqkd",
        runner=subprocess.run,
        state_dir: str = "~/.qkd-ekm-client",
    ):
        self.iface = iface
        self.state_dir = os.path.expanduser(state_dir)
        self._runner = runner

    @property
    def conf_path(self) -> str:
        return os.path.join(self.state_dir, f"{self.iface}.conf")

    def _run(self, *args: str, stdin: str | None = None, check: bool = True):
        result = self._runner(
            list(args), input=stdin, capture_output=True, text=True, check=False
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"{' '.join(args)}: {(result.stderr or result.stdout).strip()}")
        return result

    def genkey(self) -> tuple[str, str]:
        """A fresh keypair; the private key never leaves this machine."""
        priv = self._run("wg", "genkey").stdout.strip()
        pub = self._run("wg", "pubkey", stdin=priv).stdout.strip()
        return priv, pub

    def write_conf(
        self,
        path: str,
        priv: str,
        address: str,
        server_pub: str,
        endpoint: str,
        psk_b64: str,
        allowed_ips: str,
        keepalive: int = 25,
    ) -> str:
        """Write a wg-quick config, 0600: it holds both the private key and the PSK."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(
                _CONF_TEMPLATE.format(
                    priv=priv,
                    address=address,
                    server_pub=server_pub,
                    psk=psk_b64,
                    endpoint=endpoint,
                    allowed_ips=allowed_ips,
                    keepalive=keepalive,
                )
            )
        return path

    def up(self, conf_path: str) -> None:
        self._run("wg-quick", "up", conf_path)

    def down(self, conf_path: str) -> None:
        self._run("wg-quick", "down", conf_path)

    def set_psk(self, server_pub: str, psk: bytes) -> None:
        """Install a rotated PSK on the live interface, without a re-handshake."""
        fd, path = tempfile.mkstemp(prefix="wg-psk-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(b64e(psk))
            self._run("wg", "set", self.iface, "peer", server_pub, "preshared-key", path)
        finally:
            os.unlink(path)

    def show(self) -> str:
        return self._run("wg", "show", self.iface, check=False).stdout.strip()
