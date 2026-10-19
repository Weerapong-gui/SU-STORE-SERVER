"""Tokens: staff (claim station) and superadmin logins, pickup QR codes, order access."""
from __future__ import annotations

import hmac
import secrets

from . import config


def compute_claim_token(username: str, password: str) -> str:
    return hmac.new(
        key=(username + ":" + password).encode("utf-8"),
        msg=b"claim-station-v1",
        digestmod="sha256",
    ).hexdigest()


def compute_qr_token(order_code: str) -> str:
    """Short HMAC token embedded in QR codes so staff can't forge scan codes."""
    if not config.ORDER_API_TOKEN:
        return ""
    return hmac.new(
        key=config.ORDER_API_TOKEN.encode("utf-8"),
        msg=f"qr-v1:{order_code}".encode(),
        digestmod="sha256",
    ).hexdigest()[:16]


def compute_super_token(username: str, password: str) -> str:
    return hmac.new(
        key=(username + ":" + password).encode("utf-8"),
        msg=b"superadmin-v1",
        digestmod="sha256",
    ).hexdigest()


def create_order_access_token() -> str:
    return secrets.token_hex(24)
