"""Store metadata the storefront and admin load once: field labels, statuses, and the
current payment account, announcement and theme (accent colour, corner radius, font)."""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import ALWAYS_REQUIRED_FIELDS, BUYER_FIELDS, ORDER_STATUSES, PRODUCT_STATUSES, get_settings
from .home import published_theme


def meta(connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "buyerFields": [
            {"key": k, "label": v, "alwaysRequired": k in ALWAYS_REQUIRED_FIELDS} for k, v in BUYER_FIELDS.items()
        ],
        "orderStatuses": [{"key": k, "label": v} for k, v in ORDER_STATUSES.items()],
        "productStatuses": [{"key": k, "label": v} for k, v in PRODUCT_STATUSES.items()],
    }
    if connection is not None:
        settings = get_settings(connection)
        result["payment"] = settings["payment"]
        result["announcement"] = settings["announcement"]
        result.update(published_theme(connection))
    return result
