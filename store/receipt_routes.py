"""HTTP routes for store v2 receipts (`/v2/admin/receipt*`, `/v2/admin/receipts/*`).

Called from `api._route` after the admin check; handlers only read the request, call
`receipts`, write the audit row and respond.
"""
from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any

from core.database import log_audit, open_db

from . import receipts
from .contract import RECEIPT_TEMPLATE_PATH
from .route_helpers import admin_user, json_body, require_superadmin

RECEIPT_SVG_V2 = RECEIPT_TEMPLATE_PATH.read_bytes()

_ORDER_RECEIPTS = re.compile(r"/v2/admin/orders/(SU\d{4}-\d{4,})/receipts?")
_RECEIPT = re.compile(r"/v2/admin/receipts/(\d+)")
_RECEIPT_CANCEL = re.compile(r"/v2/admin/receipts/(\d+)/cancel")


def route(handler: Any, method: str, path: str) -> bool:
    """Handle a receipt route; return False when the path is not one of ours."""
    send = handler.send_json

    if method == "GET" and path == "/v2/admin/receipt-template":
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        handler.send_header("Content-Length", str(len(RECEIPT_SVG_V2)))
        handler.end_headers()
        handler.wfile.write(RECEIPT_SVG_V2)
        return True

    if path == "/v2/admin/receipt-settings":
        if method == "GET":
            with open_db() as conn:
                settings = receipts.get_receipt_settings(conn)
                send(HTTPStatus.OK, {**settings, "nextReceiptNo": receipts.next_receipt_no(conn)})
            return True
        if method == "PUT":
            require_superadmin(handler)
            payload = json_body(handler)
            with open_db() as conn:
                settings = receipts.save_receipt_settings(conn, payload)
                log_audit(conn, "-", "v2_receipt_settings_changed", f"by {admin_user(handler)}")
            send(HTTPStatus.OK, settings)
            return True

    m = _ORDER_RECEIPTS.fullmatch(path)
    if m:
        code = m.group(1)
        if method == "GET" and path.endswith("/receipts"):
            with open_db() as conn:
                send(HTTPStatus.OK, {
                    "receipts": receipts.list_order_receipts(conn, code),
                    "warning": receipts.receipt_warning(conn, code),
                })
            return True
        if method == "POST" and path.endswith("/receipt"):
            payload = json_body(handler)
            with open_db() as conn:
                receipt, created = receipts.issue_receipt(
                    conn, code, issued_by=admin_user(handler), customer=payload.get("customer"))
                if created:
                    log_audit(conn, code, "v2_receipt_issued", f"{receipt['receiptNo']} by {admin_user(handler)}")
            send(HTTPStatus.CREATED if created else HTTPStatus.OK, {**receipt, "created": created})
            return True

    m = _RECEIPT.fullmatch(path)
    if method == "GET" and m:
        with open_db() as conn:
            send(HTTPStatus.OK, receipts.get_receipt(conn, int(m.group(1))))
        return True

    m = _RECEIPT_CANCEL.fullmatch(path)
    if method == "POST" and m:
        require_superadmin(handler)
        payload = json_body(handler)
        with open_db() as conn:
            receipt = receipts.cancel_receipt(conn, int(m.group(1)), cancelled_by=admin_user(handler),
                                              reason=payload.get("reason"))
            log_audit(conn, receipt["orderCode"], "v2_receipt_cancelled",
                      f"{receipt['receiptNo']}: {receipt['cancelReason']} by {admin_user(handler)}")
        send(HTTPStatus.OK, receipt)
        return True

    return False
