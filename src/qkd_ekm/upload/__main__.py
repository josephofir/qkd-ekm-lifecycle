"""Entry point for the file upload server (console script ``qkd-ekm-upload``).

``UPLOAD_SINK_DIR`` swaps Cloud Storage for a local directory, which is how
the S2 workflow can be rehearsed without a GCP project.
"""

from __future__ import annotations

import uvicorn

from qkd_ekm.common.settings import env
from qkd_ekm.upload.app import DirSink, GcsSink, UploadSettings, create_app
from qkd_ekm.vpn.app import EkmClient


def build_app():
    settings = UploadSettings(
        bucket=env("GCS_BUCKET", required=True),
        kms_key_name=env("KMS_KEY_NAME", required=True),
        file_key_ttl_s=int(env("UPLOAD_KEY_TTL_S", 600)),
        peer_default=env("UPLOAD_PEER", "QKD2"),
    )
    sink_dir = env("UPLOAD_SINK_DIR")
    sink = DirSink(sink_dir, bucket=settings.bucket) if sink_dir else GcsSink(settings.bucket)
    ekm_client = EkmClient(
        base_url=env("EKM_URL", required=True),
        vpn_token=env("VPN_TOKEN", required=True),
        ca_file=env("EKM_CA_FILE"),
    )
    return create_app(ekm_client, sink, settings)


def main() -> None:
    # NOTE: plain HTTP, all interfaces -- the workload VM has no external
    # address and its firewall admits the VPN tunnel CIDR only.
    uvicorn.run(build_app(), host="0.0.0.0", port=int(env("UPLOAD_PORT", 8081)))


if __name__ == "__main__":
    main()
