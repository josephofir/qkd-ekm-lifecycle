"""Bearer-token verification for the EKM's two callers.

Cloud KMS reaches the EKM with a Google-issued OIDC identity token for the
EKM service agent (`service-<project-number>@gcp-sa-ekms.iam.gserviceaccount.com`);
`GoogleJwtVerifier` checks its signature against Google's certs and then the
claims that actually pin *who* is calling. The VPN/upload side authenticates
with a shared secret instead (`StaticTokenVerifier`), because those callers
are our own VMs, not Google.

Both expose the same `verify(token) -> claims` shape so `bearer_dep` -- and
the tests, which inject fakes -- treat them interchangeably. Verification is
never bypassable: there is no "skip auth" flag, only a different verifier
object passed into `create_app`.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from qkd_ekm.common.log import get_logger

logger = get_logger("EKM")

_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

#: Google API error status for each HTTP code the EKM returns.
ERROR_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    500: "INTERNAL",
    503: "UNAVAILABLE",
}


class AuthError(PermissionError):
    """Rejected credential.

    `unauthenticated` separates "this token is not a valid Google credential"
    (bad signature, expired, malformed -> 401 UNAUTHENTICATED) from "this is a
    genuine credential, but not one we accept" (issuer/email/audience -> 403
    PERMISSION_DENIED), which is the distinction Cloud KMS retries on.
    """

    def __init__(self, reason: str, unauthenticated: bool = False):
        super().__init__(reason)
        self.unauthenticated = unauthenticated


def error_body(code: int, message: str, status: str | None = None) -> dict:
    """Cloud EKM error envelope: `{"error": {"code", "message", "status"}}`."""
    return {
        "error": {
            "code": code,
            "message": message,
            "status": status or ERROR_STATUS.get(code, "UNKNOWN"),
        }
    }


class GoogleJwtVerifier:
    def __init__(
        self,
        allowed_emails: set[str],
        audiences: set[str] | None = None,
        component: str = "EKM",
    ):
        self.allowed_emails = set(allowed_emails)
        self.audiences = set(audiences) if audiences else None
        # The verifier is shared by the EKM and the VPN server; log under the
        # component that actually rejected the token.
        self._log = get_logger(component)

    def verify(self, token: str) -> dict:
        try:
            # audience=None: the audience is checked below against `audiences`,
            # so a deployment that leaves it unset still gets signature+expiry.
            claims = id_token.verify_token(token, google_requests.Request(), audience=None)
        except Exception as exc:  # noqa: BLE001 - any verification failure is a rejection
            self._reject(f"invalid token: {exc.__class__.__name__}", {}, unauthenticated=True)
        if claims.get("iss") not in _ISSUERS:
            self._reject("issuer not trusted", claims)
        if claims.get("email_verified") is not True:
            self._reject("email not verified", claims)
        if claims.get("email") not in self.allowed_emails:
            self._reject("email not allowed", claims)
        if self.audiences is not None and claims.get("aud") not in self.audiences:
            self._reject("audience not allowed", claims)
        return claims

    def _reject(self, reason: str, claims: dict, unauthenticated: bool = False) -> None:
        # Claims only -- logging the token itself would leak a live credential.
        self._log.warning(
            f"JWT rejected: {reason} aud={claims.get('aud')} email={claims.get('email')}"
        )
        raise AuthError(reason, unauthenticated=unauthenticated)


class StaticTokenVerifier:
    """Shared-secret bearer check for the VPN/upload callers."""

    def __init__(self, token: str):
        self._token = token

    def verify(self, token: str) -> dict:
        # compare_digest on str operands raises TypeError for non-ASCII input,
        # so compare the UTF-8 bytes: a garbage token must be a rejection, not
        # a 500.
        if not hmac.compare_digest(token.encode(), self._token.encode()):
            logger.warning("Bearer token rejected")
            raise AuthError("invalid token")
        return {"sub": "static"}


def bearer_dep(verifier):
    """FastAPI dependency returning the caller's claims, or raising 401/403."""

    async def dependency(request: Request) -> dict:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, detail=error_body(401, "missing bearer token"))
        try:
            return verifier.verify(authorization.removeprefix("Bearer ").strip())
        except PermissionError as exc:
            code = 401 if getattr(exc, "unauthenticated", False) else 403
            raise HTTPException(code, detail=error_body(code, str(exc))) from exc

    return dependency
