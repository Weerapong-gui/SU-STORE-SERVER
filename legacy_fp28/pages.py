"""HTML/SVG templates served by the FP28 order-api, loaded once at import."""
from __future__ import annotations

from pathlib import Path

# legacy_fp28/templates/
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


ADMIN_HTML = (TEMPLATES_DIR / "admin.html").read_text(encoding="utf-8")


ORDER_VIEW_HTML = (TEMPLATES_DIR / "order_view.html").read_text(encoding="utf-8")


CLAIM_STATION_HTML = (TEMPLATES_DIR / "claim_station.html").read_text(encoding="utf-8")


DISPLAY_HTML = (TEMPLATES_DIR / "display.html").read_text(encoding="utf-8")


DISPLAY2_HTML = (TEMPLATES_DIR / "display2.html").read_text(encoding="utf-8")


DISPLAY3_HTML = (TEMPLATES_DIR / "display3.html").read_text(encoding="utf-8")


RECEIPT_SVG = (TEMPLATES_DIR / "receipt_template.svg").read_bytes()


FONT_PATH = TEMPLATES_DIR / "fonts" / "SukhumvitSet.ttc"
