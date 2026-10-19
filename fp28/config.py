"""Environment settings and constants for the FP28 order system."""
from __future__ import annotations

import hmac
import os
import re
from pathlib import Path

HOST = os.environ.get("ORDER_API_HOST", "0.0.0.0")


PORT = int(os.environ.get("ORDER_API_PORT", os.environ.get("PORT", "3010")))


DB_PATH = Path(os.environ.get("ORDER_API_DB_PATH", str(Path.home() / "su-order-api" / "data" / "orders.db")))


SLIPS_DIR = Path(os.environ.get("ORDER_API_SLIPS_DIR", str(DB_PATH.parent / "slips")))


PRODUCT_IMAGES_DIR = Path(os.environ.get("PRODUCT_IMAGES_DIR", str(DB_PATH.parent / "product-images")))


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


MAX_PRODUCT_IMAGE_SIZE_BYTES = 10 * 1024 * 1024


ORDER_PREFIX = os.environ.get("ORDER_PREFIX", "FP28")


ORDER_ROUND = int(os.environ.get("ORDER_ROUND", "1"))


ORDER_API_TOKEN = os.environ.get("ORDER_API_TOKEN", "")


BYPASS_TOKEN = os.environ.get("BYPASS_TOKEN", "")


PICKUP_USERNAME = os.environ.get("PICKUP_USERNAME", "staff")


PICKUP_PASSWORD = os.environ.get("PICKUP_PASSWORD", "")


# Deterministic session token derived from credentials (survives restarts)
SUPER_TOKEN = hmac.new(
    key=(PICKUP_USERNAME + ":" + PICKUP_PASSWORD).encode("utf-8"),
    msg=b"superadmin-v1",
    digestmod="sha256",
).hexdigest() if PICKUP_PASSWORD else ""


CLAIM_STATION_TOKEN = hmac.new(
    key=(PICKUP_USERNAME + ":" + PICKUP_PASSWORD).encode("utf-8"),
    msg=b"claim-station-v1",
    digestmod="sha256",
).hexdigest() if PICKUP_PASSWORD else ""


GOOGLE_SHEETS_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()


GOOGLE_SHEETS_WEBHOOK_TOKEN = os.environ.get("GOOGLE_SHEETS_WEBHOOK_TOKEN", "").strip()


KHANTOK_QUOTA_100 = int(os.environ.get("KHANTOK_QUOTA_100", "2000"))


KHANTOK_QUOTA_50 = int(os.environ.get("KHANTOK_QUOTA_50", "1000"))


KHANTOK_STUDENT_CODE_PREFIX = "693"


ORDER_ID_PATTERN = re.compile(r"^[A-Z0-9-]+$")


ALLOWED_SLIP_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


MAX_SLIP_SIZE_BYTES = 5 * 1024 * 1024


ORDER_STATUSES = {
    "pending_payment",
    "waiting_confirm",
    "paid",
    "preparing",
    "shipped",
    "received",
    "cancelled",
    "rejected",
    "refund",
    "refunded",
}


PAYMENT_STATUS_BY_ORDER_STATUS = {
    "pending_payment": "awaiting_payment",
    "waiting_confirm": "waiting_confirm",
    "paid": "paid",
    "preparing": "paid",
    "shipped": "paid",
    "received": "paid",
    "cancelled": "rejected",
    "rejected": "rejected",
    "refund": "refund_pending",
    "refunded": "refunded",
}


DEFAULT_PRODUCTS = [
    {
        "slug": "single-shirt",
        "name": "FRESHER POLO SHIRT",
        "short_name": "เสื้อเดี่ยว",
        "tagline": "Classic fresher polo for everyday campus wear.",
        "description": "FRESHER PACKAGE 28TH",
        "price": 399,
        "category": "single",
        "requires_size": 1,
        "requires_school": 1,
        "image_path": "/images/POLP_post/Artboard 4.png",
        "sort_order": 1,
    },
    {
        "slug": "fresh-jacket",
        "name": "FRESHER JACKET",
        "short_name": "แจ็คเก็ต",
        "tagline": "Layer up with a clean campus-ready jacket.",
        "description": "FRESHER PACKAGE 28TH",
        "price": 899,
        "category": "jacket",
        "requires_size": 1,
        "requires_school": 1,
        "image_path": "/images/Pr1.png",
        "sort_order": 3,
    },
    {
        "slug": "fresh-headband",
        "name": "FRESHER HEADBAND",
        "short_name": "ผ้าคาดสำนักวิชา",
        "tagline": "A lightweight accessory for sports day and activity looks.",
        "description": "FRESHER PACKAGE 28TH",
        "price": 35,
        "category": "headband",
        "requires_size": 0,
        "requires_school": 1,
        "image_path": "/images/Pr1.png",
        "sort_order": 4,
    },
]


SLIDES_DIR = Path(os.environ.get("ORDER_API_SLIDES_DIR", "/var/data/su-order-api/slides"))


SLIDES_DIR.mkdir(parents=True, exist_ok=True)


ASSETS_DIR = Path(os.environ.get("ORDER_API_ASSETS_DIR", "/var/data/su-order-api/display2_assets"))


ASSETS_DIR.mkdir(parents=True, exist_ok=True)


STATION_BY_SLUG = {
    "single": "polo",
    "single-shirt": "polo",
    "jacket": "jacket",
    "fresh-jacket": "jacket",
    "headband": "headband",
    "fresh-headband": "headband",
}
