"""Environment settings used by the store order API."""
from __future__ import annotations

import os
from pathlib import Path

HOST = os.environ.get("ORDER_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORDER_API_PORT", os.environ.get("PORT", "3010")))
DB_PATH = Path(os.environ.get("ORDER_API_DB_PATH", str(Path.home() / "su-order-api" / "data" / "orders.db")))
SLIPS_DIR = Path(os.environ.get("ORDER_API_SLIPS_DIR", str(DB_PATH.parent / "slips")))
PRODUCT_IMAGES_DIR = Path(os.environ.get("PRODUCT_IMAGES_DIR", str(DB_PATH.parent / "product-images")))
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_SLIP_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_PRODUCT_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
MAX_SLIP_SIZE_BYTES = 5 * 1024 * 1024
ORDER_API_TOKEN = os.environ.get("ORDER_API_TOKEN", "")
PICKUP_USERNAME = os.environ.get("PICKUP_USERNAME", "staff")
PICKUP_PASSWORD = os.environ.get("PICKUP_PASSWORD", "")
DEMO_SEED = os.environ.get("ORDER_API_DEMO_SEED", "") == "1"
