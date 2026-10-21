"""Shared storefront values loaded from the backend contract files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contract"
CONTRACT: dict[str, Any] = json.loads((CONTRACT_DIR / "store-contract.json").read_text(encoding="utf-8"))
RECEIPT_TEMPLATE_PATH = CONTRACT_DIR / "receipt_template_v2.svg"
