"""Passkeys (WebAuthn): sign in with a fingerprint, face or device PIN instead of a password.

A passkey is bound to this site (RP ID = the host of SITE_URL), cannot be phished, and requires user verification
on the device, so a passkey sign-in counts as two factors: an admin who signs in with one skips the two-step code.
Challenges live in memory for a few minutes (the API runs as one process); a restart just means trying again.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from app.config import Settings, get_settings

CHALLENGE_TTL = 300.0
_pending: dict[str, tuple[float, bytes, int | None, str]] = {}   # state -> (expires, challenge, user_id, purpose)


def rp(s: Settings | None = None) -> tuple[str, list[str]]:
    """(RP ID, allowed origins) from SITE_URL; localhost works for development."""
    s = s or get_settings()
    u = urlparse(s.site_url)
    host = u.hostname or "localhost"
    origins = [f"{u.scheme}://{u.netloc}"]
    if host == "localhost":
        origins += ["http://localhost:3000", "http://127.0.0.1:3000"]
    return host, origins


def _remember(challenge: bytes, user_id: int | None, purpose: str) -> str:
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if v[0] < now]:
        _pending.pop(k, None)
    state = secrets.token_urlsafe(24)
    _pending[state] = (now + CHALLENGE_TTL, challenge, user_id, purpose)
    return state


def take(state: str, purpose: str) -> tuple[bytes, int | None] | None:
    """The challenge for `state`, once (and only for the purpose it was made for)."""
    hit = _pending.pop(state or "", None)
    if hit is None or hit[0] < time.monotonic() or hit[3] != purpose:
        return None
    return hit[1], hit[2]


def registration_options(user_id: int, email: str, existing: list[bytes]) -> tuple[dict[str, Any], str]:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)

    rp_id, _ = rp()
    opts = generate_registration_options(
        rp_id=rp_id, rp_name="Sirb", user_name=email, user_display_name=email,
        user_id=f"sirb-user-{user_id}".encode(),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c) for c in existing],
    )
    return json.loads(options_to_json(opts)), _remember(opts.challenge, user_id, "register")


def verify_registration(credential: dict[str, Any], challenge: bytes):
    from webauthn import verify_registration_response

    rp_id, origins = rp()
    return verify_registration_response(credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
                                        expected_origin=origins, require_user_verification=True)


def authentication_options() -> tuple[dict[str, Any], str]:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import UserVerificationRequirement

    rp_id, _ = rp()
    opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    return json.loads(options_to_json(opts)), _remember(opts.challenge, None, "login")


def verify_authentication(credential: dict[str, Any], challenge: bytes, public_key: bytes, sign_count: int):
    from webauthn import verify_authentication_response

    rp_id, origins = rp()
    return verify_authentication_response(credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
                                          expected_origin=origins, credential_public_key=public_key,
                                          credential_current_sign_count=sign_count, require_user_verification=True)
