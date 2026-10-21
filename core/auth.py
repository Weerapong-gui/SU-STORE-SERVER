"""Tokens: staff (claim station) and superadmin logins, pickup QR codes, order access."""
from __future__ import annotations

import hmac


def compute_claim_token(username: str, password: str) -> str:
    return hmac.new(
        key=(username + ":" + password).encode("utf-8"),
        msg=b"claim-station-v1",
        digestmod="sha256",
    ).hexdigest()


def compute_super_token(username: str, password: str) -> str:
    return hmac.new(
        key=(username + ":" + password).encode("utf-8"),
        msg=b"superadmin-v1",
        digestmod="sha256",
    ).hexdigest()


