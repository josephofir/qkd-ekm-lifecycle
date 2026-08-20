"""Rotate a Cloud KMS external key onto the next EKM key path.

Cloud KMS binds each EXTERNAL_VPC crypto key *version* to one immutable
`ekm_connection_key_path`. Rotating therefore means creating version n+1
pointing at `api/keys/v{n+1}` and promoting it to primary, which is what
makes the EKM allocate and bind a fresh QKD unit on the next wrap. Older
versions stay enabled so data encrypted under them can still be decrypted.
"""

from __future__ import annotations

import time

from google.cloud import kms_v1

from qkd_ekm.common.log import get_logger
from qkd_ekm.common.settings import env

logger = get_logger("EKM")

_POLL_INTERVAL_S = 1.0
_POLL_ATTEMPTS = 60


def _version_number(name: str) -> int:
    return int(name.rsplit("/", 1)[-1])


def _state_name(state) -> str:
    return getattr(state, "name", str(state))


def rotate(client=None) -> str:
    kms_key = env("KMS_KEY", required=True)
    prefix = env("EKM_KEY_PATH_PREFIX", "api/keys/")
    client = client or kms_v1.KeyManagementServiceClient()

    versions = list(client.list_crypto_key_versions(parent=kms_key))
    next_number = max((_version_number(v.name) for v in versions), default=0) + 1
    key_path = f"{prefix}v{next_number}"

    version = client.create_crypto_key_version(
        parent=kms_key,
        crypto_key_version=kms_v1.CryptoKeyVersion(
            external_protection_level_options=kms_v1.ExternalProtectionLevelOptions(
                ekm_connection_key_path=key_path
            )
        ),
    )
    for _ in range(_POLL_ATTEMPTS):
        if _state_name(version.state) == "ENABLED":
            break
        time.sleep(_POLL_INTERVAL_S)
        version = client.get_crypto_key_version(name=version.name)
    else:
        raise RuntimeError(f"{version.name} did not become ENABLED")

    # Promote the id KMS actually assigned, not our max+1 guess: a version
    # created concurrently (or a destroyed-and-recreated one) would otherwise
    # make us promote somebody else's version.
    created_number = _version_number(version.name)
    if created_number != next_number:
        logger.warning(f"KMS assigned version {created_number} for key path {key_path}")
    client.update_crypto_key_primary_version(
        name=kms_key, crypto_key_version_id=str(created_number)
    )
    logger.info(f"Rotated external key to version v{next_number}")
    return f"v{next_number}"


def main(client=None) -> int:
    """Console-script entry point: `sys.exit(main())` must see 0, not the version label.

    (First cloud run: returning "v2" made the systemd unit exit 1 after a successful
    rotation.)
    """
    print(rotate(client))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
