"""Payment slip files and the background OCR check."""
from __future__ import annotations

import base64
import binascii
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config
from .config import ALLOWED_SLIP_TYPES, MAX_SLIP_SIZE_BYTES
from .database import log_audit, open_db
from .timeutil import now_iso


def sanitize_file_name(file_name: str) -> str:
    sanitized = re.sub(r"[^a-z0-9.-]+", "-", file_name.strip().lower())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized or "slip"


def resolve_slip_extension(file_name: str, mime_type: str) -> str:
    original_extension = Path(file_name).suffix.lower()
    if original_extension:
        return original_extension
    if mime_type == "image/jpeg":
        return ".jpg"
    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/webp":
        return ".webp"
    if mime_type == "application/pdf":
        return ".pdf"
    return ".bin"


def resolve_stored_slip_path(stored_name: str | None, stored_path: str | None = None) -> Path | None:
    if isinstance(stored_path, str) and stored_path.strip():
        candidate = Path(stored_path.strip())
        if not candidate.is_absolute():
            return config.SLIPS_DIR / candidate.name
        return candidate

    if not stored_name:
        return None

    target_name = Path(str(stored_name)).name
    if not target_name:
        return None
    return config.SLIPS_DIR / target_name


def delete_local_slip_file(stored_name: str | None, stored_path: str | None = None) -> None:
    target_path = resolve_stored_slip_path(stored_name, stored_path)
    if target_path is None:
        return
    try:
        target_path.unlink()
    except FileNotFoundError:
        return


def persist_slip_file(
    order_code: str,
    original_name: str,
    mime_type: str,
    uploaded_at: str,
    file_content: bytes,
) -> tuple[dict[str, Any] | None, str | None]:
    if mime_type not in ALLOWED_SLIP_TYPES:
        return None, "unsupported slip mime type"

    file_size = len(file_content)
    if file_size < 1 or file_size > MAX_SLIP_SIZE_BYTES:
        return None, "slip.size must be between 1 and 5242880"

    safe_name = sanitize_file_name(Path(original_name).stem)
    extension = resolve_slip_extension(original_name, mime_type)
    stored_name = f"{order_code}-{int(datetime.now(UTC).timestamp())}-{safe_name}{extension}"
    stored_path = config.SLIPS_DIR / stored_name
    stored_path.write_bytes(file_content)

    normalized_slip = {
        "originalName": original_name,
        "storedName": stored_name,
        "storedPath": str(stored_path),
        "mimeType": mime_type,
        "size": file_size,
        "uploadedAt": uploaded_at,
    }
    return normalized_slip, None


def materialize_slip_payload(order_code: str, slip: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    file_content_base64 = slip.get("fileContentBase64")
    if not isinstance(file_content_base64, str) or not file_content_base64.strip():
        stored_name = slip.get("storedName")
        stored_path = slip.get("storedPath")
        if (not isinstance(stored_name, str) or not stored_name.strip()) and isinstance(stored_path, str) and stored_path.strip():
            stored_name = Path(stored_path).name
        if not isinstance(stored_name, str) or not stored_name.strip():
            return None, "slip.storedName is required"
        return {
            "originalName": slip["originalName"],
            "storedName": Path(stored_name).name,
            "storedPath": str(resolve_stored_slip_path(stored_name, str(stored_path) if isinstance(stored_path, str) else None)),
            "mimeType": slip["mimeType"],
            "size": int(slip["size"]),
            "uploadedAt": slip["uploadedAt"],
        }, None

    mime_type = slip.get("mimeType")
    if not isinstance(mime_type, str) or mime_type not in ALLOWED_SLIP_TYPES:
        return None, "unsupported slip mime type"

    file_size = slip.get("size")
    if not isinstance(file_size, int) or file_size < 1 or file_size > MAX_SLIP_SIZE_BYTES:
        return None, "slip.size must be between 1 and 5242880"

    try:
        file_content = base64.b64decode(file_content_base64.encode("utf-8"), validate=True)
    except (binascii.Error, ValueError):
        return None, "slip.fileContentBase64 is invalid"

    if len(file_content) != file_size:
        return None, "slip.size does not match uploaded content"

    return persist_slip_file(
        order_code,
        str(slip["originalName"]),
        mime_type,
        str(slip["uploadedAt"]),
        file_content,
    )


def _run_ocr_for_slip(order_code: str, slip_path: str, expected_amount: float | None) -> None:
    try:
        with open_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO slip_ocr_results (order_code, status, checked_at) VALUES (?, 'pending', ?)",
                (order_code, now_iso()),
            )
            conn.commit()

        if slip_path.lower().endswith(".pdf"):
            with open_db() as conn:
                conn.execute(
                    "UPDATE slip_ocr_results SET status='skipped', raw_reason='PDF ไม่รองรับ OCR', checked_at=? WHERE order_code=?",
                    (now_iso(), order_code),
                )
                conn.commit()
            return

        import ocr as _ocr
        data = _ocr.extract_slip_data(slip_path)

        status = "rejected"
        raw_reason: str | None = None
        duplicate_of: str | None = None
        ref = data.get("ref_number")
        amount = data.get("amount")

        if not data.get("is_slip"):
            raw_reason = "ไม่พบข้อมูลสลิปในรูปภาพ"
        elif not ref:
            raw_reason = "ไม่พบเลขที่รายการในสลิป"
        else:
            with open_db() as conn:
                dup_row = conn.execute(
                    "SELECT order_code FROM slip_ocr_results WHERE ref_number = ? AND order_code != ? AND status NOT IN ('rejected','error','skipped','pending')",
                    (ref, order_code),
                ).fetchone()
            if dup_row:
                status = "duplicate"
                duplicate_of = dup_row["order_code"]
                raw_reason = f"ref ซ้ำกับออเดอร์ {duplicate_of}"
            elif expected_amount is not None and amount is not None and abs(amount - expected_amount) > 0.5:
                status = "amount_mismatch"
                raw_reason = f"จำนวนเงินไม่ตรง (สลิป: {amount:.2f}, ออเดอร์: {expected_amount:.2f})"
            else:
                status = "approved"

        with open_db() as conn:
            conn.execute(
                """UPDATE slip_ocr_results
                   SET status=?, ref_number=?, amount=?, date=?, sender=?, bank=?,
                       duplicate_of=?, raw_reason=?, checked_at=?
                   WHERE order_code=?""",
                (status, ref, amount, data.get("date"), data.get("sender"), data.get("bank"),
                 duplicate_of, raw_reason, now_iso(), order_code),
            )
            log_audit(conn, order_code, "slip_ocr_checked",
                      f"status={status}" + (f" ref={ref}" if ref else ""))
            conn.commit()
    except Exception as exc:
        import sys
        print(f"[OCR] ERROR order={order_code}: {exc}", file=sys.stderr)
        try:
            with open_db() as conn:
                conn.execute(
                    "UPDATE slip_ocr_results SET status='error', raw_reason=?, checked_at=? WHERE order_code=?",
                    (str(exc)[:300], now_iso(), order_code),
                )
                log_audit(conn, order_code, "slip_ocr_error", str(exc)[:200])
                conn.commit()
        except Exception as db_exc:
            print(f"[OCR] failed to persist error for order={order_code}: {db_exc}", file=sys.stderr)


def trigger_ocr_async(order_code: str, slip_path: str | None, expected_amount: float | None = None) -> None:
    import os
    import sys
    if not slip_path:
        return
    if not os.path.exists(slip_path):
        print(f"[OCR] slip not found on disk, skipping: {slip_path}", file=sys.stderr)
        return
    threading.Thread(
        target=_run_ocr_for_slip,
        args=(order_code, slip_path, expected_amount),
        daemon=True,
    ).start()
