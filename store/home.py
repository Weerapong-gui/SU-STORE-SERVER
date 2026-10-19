"""Home page layout for SU STORE v2.

Staff build the home page from a fixed set of block types in /admin/home. The draft and
the published layout are JSON in store_settings; customers only ever see home_published.
Until someone publishes, DEFAULT_HOME renders the original page.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .categories import list_categories
from .common import StoreError, clean_image_url, clean_text, now_iso
from .db import list_products

# Keep block types in sync with src/components/admin/home/blocks.ts.
HOME_BLOCK_TYPES = ("hero", "featured", "categories", "text", "imageText", "allProducts")
DEFAULT_ACCENT = "#0071e3"
# Corner roundness in percent of the original design: 0 = square, 100 = as designed.
DEFAULT_RADIUS = 100
# Storefront font ids; the families and Google Fonts URLs live in src/lib/theme/fonts.ts (keep in sync).
HOME_FONTS = ("sarabun", "prompt", "kanit", "noto-sans-thai", "ibm-plex-sans-thai", "anuphan", "mitr",
              "chakra-petch", "bai-jamjuree", "pridi")
DEFAULT_FONT = "sarabun"
MAX_HOME_BLOCKS = 20
MAX_HERO_SLIDES = 6
MAX_FEATURED = 12
# Must match MAX_HOME_CATEGORIES in src/components/admin/home/blocks.ts.
MAX_HOME_CATEGORIES = 12

# A hero slide with no image and no text renders the built-in bilingual store intro.
DEFAULT_HOME: dict[str, Any] = {
    "accent": DEFAULT_ACCENT,
    "radius": DEFAULT_RADIUS,
    "font": DEFAULT_FONT,
    "blocks": [
        {"id": "hero", "type": "hero", "hidden": False, "autoplay": True,
         "slides": [{"image": "", "eyebrow": "", "title": "", "subtitle": "", "buttonText": "", "buttonHref": ""}]},
        {"id": "all-products", "type": "allProducts", "hidden": False, "title": ""},
    ],
}

_BLOCK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _relative_luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_with_white(hex_color: str) -> float:
    return 1.05 / (_relative_luminance(hex_color) + 0.05)


def _clean_radius(value: Any, where: str) -> int:
    # bool is an int subclass: reject True/False explicitly
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise StoreError(400, f"ความโค้งของมุม{where}ต้องเป็นตัวเลข 0–100")
    return value


def _clean_link(text: Any, href: Any, where: str) -> tuple[str, str]:
    text = clean_text(text, f"ข้อความปุ่ม ({where})", max_len=30)
    href = clean_text(href, f"ลิงก์ปุ่ม ({where})", max_len=300)
    if text and not href:
        raise StoreError(400, f"กรุณาใส่ลิงก์ของปุ่ม ({where})")
    if href and not (re.fullmatch(r"/(?!/)\S*", href) or re.fullmatch(r"https://\S+", href)):
        raise StoreError(400, f"ลิงก์ปุ่ม ({where}) ต้องขึ้นต้นด้วย / หรือ https://")
    return text, href


def validate_home(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(400, "ข้อมูลหน้าแรกไม่ถูกต้อง")
    accent = payload.get("accent") or DEFAULT_ACCENT
    if not isinstance(accent, str) or not _HEX_RE.fullmatch(accent):
        raise StoreError(400, "สีหลักต้องเป็นรหัสสีแบบ #RRGGBB")
    accent = accent.lower()
    if contrast_with_white(accent) < 4.5:
        raise StoreError(400, "สีหลักอ่อนเกินไป ตัวหนังสือสีขาวบนปุ่มจะอ่านยาก กรุณาเลือกสีที่เข้มขึ้น")
    radius = _clean_radius(payload.get("radius", DEFAULT_RADIUS), "")
    font = payload.get("font", DEFAULT_FONT)
    if font not in HOME_FONTS:
        raise StoreError(400, "ไม่รู้จักฟอนต์นี้")

    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        raise StoreError(400, "ข้อมูลบล็อกไม่ถูกต้อง")
    if len(blocks) > MAX_HOME_BLOCKS:
        raise StoreError(400, f"ใส่ได้ไม่เกิน {MAX_HOME_BLOCKS} บล็อก")

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for n, block in enumerate(blocks, start=1):
        where = f"บล็อกที่ {n}"
        if not isinstance(block, dict) or block.get("type") not in HOME_BLOCK_TYPES:
            raise StoreError(400, f"{where}: ไม่รู้จักชนิดบล็อกนี้")
        block_id = block.get("id")
        if not isinstance(block_id, str) or not _BLOCK_ID_RE.fullmatch(block_id) or block_id in seen:
            raise StoreError(400, f"{where}: รหัสบล็อกไม่ถูกต้อง")
        seen.add(block_id)
        kind = block["type"]
        clean: dict[str, Any] = {"id": block_id, "type": kind, "hidden": bool(block.get("hidden"))}
        # No radius = follow the store-wide setting.
        if block.get("radius") is not None:
            clean["radius"] = _clean_radius(block["radius"], f" ({where})")

        if kind == "hero":
            slides = block.get("slides")
            if not isinstance(slides, list) or not 1 <= len(slides) <= MAX_HERO_SLIDES:
                raise StoreError(400, f"{where}: แบนเนอร์ต้องมี 1–{MAX_HERO_SLIDES} สไลด์")
            clean["autoplay"] = bool(block.get("autoplay", True))
            clean["slides"] = []
            for i, slide in enumerate(slides, start=1):
                if not isinstance(slide, dict):
                    raise StoreError(400, f"{where}: สไลด์ที่ {i} ไม่ถูกต้อง")
                label = f"{where} สไลด์ที่ {i}"
                button_text, button_href = _clean_link(slide.get("buttonText"), slide.get("buttonHref"), label)
                clean["slides"].append({
                    "image": clean_image_url(slide.get("image"), f"รูป ({label})"),
                    "eyebrow": clean_text(slide.get("eyebrow"), f"ข้อความเล็ก ({label})", max_len=60),
                    "title": clean_text(slide.get("title"), f"หัวข้อ ({label})", max_len=80),
                    "subtitle": clean_text(slide.get("subtitle"), f"คำโปรย ({label})", max_len=200),
                    "buttonText": button_text,
                    "buttonHref": button_href,
                })
        elif kind == "featured":
            ids = block.get("productIds") or []
            if not isinstance(ids, list) or len(ids) > MAX_FEATURED:
                raise StoreError(400, f"{where}: เลือกสินค้าแนะนำได้ไม่เกิน {MAX_FEATURED} ชิ้น")
            if any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
                raise StoreError(400, f"{where}: รายการสินค้าไม่ถูกต้อง")
            clean["title"] = clean_text(block.get("title"), f"หัวข้อ ({where})", max_len=80)
            clean["productIds"] = list(dict.fromkeys(ids))
        elif kind == "categories":
            ids = block.get("categoryIds", [])
            if not isinstance(ids, list) or len(ids) > MAX_HOME_CATEGORIES:
                raise StoreError(400, f"{where}: เลือกหมวดหมู่ได้ไม่เกิน {MAX_HOME_CATEGORIES} หมวดหมู่")
            if any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
                raise StoreError(400, f"{where}: รายการหมวดหมู่ไม่ถูกต้อง")
            clean["title"] = clean_text(block.get("title"), f"หัวข้อ ({where})", max_len=80)
            clean["categoryIds"] = list(dict.fromkeys(ids))
        elif kind == "text":
            align = block.get("align") or "center"
            if align not in ("left", "center"):
                raise StoreError(400, f"{where}: การจัดข้อความไม่ถูกต้อง")
            clean["title"] = clean_text(block.get("title"), f"หัวข้อ ({where})", max_len=80)
            clean["body"] = clean_text(block.get("body"), f"เนื้อความ ({where})", max_len=2000)
            clean["align"] = align
        elif kind == "imageText":
            side = block.get("imageSide") or "left"
            if side not in ("left", "right"):
                raise StoreError(400, f"{where}: ตำแหน่งรูปไม่ถูกต้อง")
            button_text, button_href = _clean_link(block.get("buttonText"), block.get("buttonHref"), where)
            clean.update({
                "image": clean_image_url(block.get("image"), f"รูป ({where})"),
                "title": clean_text(block.get("title"), f"หัวข้อ ({where})", max_len=80),
                "body": clean_text(block.get("body"), f"เนื้อความ ({where})", max_len=2000),
                "buttonText": button_text,
                "buttonHref": button_href,
                "imageSide": side,
            })
        else:  # allProducts
            clean["title"] = clean_text(block.get("title"), f"หัวข้อ ({where})", max_len=80)
        out.append(clean)
    return {"accent": accent, "radius": radius, "font": font, "blocks": out}


def _load_home(connection: sqlite3.Connection, key: str) -> tuple[dict[str, Any], str | None]:
    row = connection.execute("SELECT value, updated_at FROM store_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return json.loads(json.dumps(DEFAULT_HOME)), None
    layout = json.loads(row[0])
    # Layouts saved before these settings existed keep the original look.
    layout.setdefault("radius", DEFAULT_RADIUS)
    layout.setdefault("font", DEFAULT_FONT)
    return layout, row[1]


def _store_home(connection: sqlite3.Connection, key: str, layout: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO store_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(layout, ensure_ascii=False), now_iso()),
    )


def get_home_admin(connection: sqlite3.Connection) -> dict[str, Any]:
    published, published_at = _load_home(connection, "home_published")
    draft_row = connection.execute("SELECT 1 FROM store_settings WHERE key = 'home_draft'").fetchone()
    draft = _load_home(connection, "home_draft")[0] if draft_row else published
    return {"draft": draft, "published": published, "dirty": draft != published, "publishedAt": published_at}


def save_home_draft(connection: sqlite3.Connection, payload: Any) -> dict[str, Any]:
    _store_home(connection, "home_draft", validate_home(payload))
    return get_home_admin(connection)


def publish_home(connection: sqlite3.Connection) -> dict[str, Any]:
    state = get_home_admin(connection)
    # Re-validate: the draft may reference rules that changed since it was saved.
    _store_home(connection, "home_published", validate_home(state["draft"]))
    _store_home(connection, "home_draft", validate_home(state["draft"]))
    return get_home_admin(connection)


def discard_home_draft(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.execute("DELETE FROM store_settings WHERE key = 'home_draft'")
    return get_home_admin(connection)


def resolve_home(connection: sqlite3.Connection, layout: dict[str, Any]) -> dict[str, Any]:
    """What the storefront renders: hidden and empty blocks dropped, featured product ids
    replaced by the live public products (drafts/archived silently skipped).
    Category blocks include only visible categories in catalog sort order."""
    products = list_products(connection, public=True)
    categories = list_categories(connection, public=True)
    by_id = {p["id"]: p for p in products}
    blocks: list[dict[str, Any]] = []
    for block in layout.get("blocks", []):
        if block.get("hidden"):
            continue
        kind = block["type"]
        if kind == "featured":
            items = [by_id[i] for i in block.get("productIds", []) if i in by_id]
            if not items:
                continue
            block = {**block, "products": items}
        elif kind == "categories":
            ids = set(block.get("categoryIds", []))
            items = [category for category in categories if not ids or category["id"] in ids]
            if not items:
                continue
            block = {**block, "categories": items}
        elif (kind == "text" and not (block.get("title") or block.get("body"))) or (kind == "imageText" and not (block.get("image") or block.get("title") or block.get("body"))):
            continue
        blocks.append(block)
    return {**_theme(layout), "blocks": blocks, "products": products}


def _theme(layout: dict[str, Any]) -> dict[str, Any]:
    return {
        "accent": layout.get("accent") or DEFAULT_ACCENT,
        "radius": layout.get("radius", DEFAULT_RADIUS),
        "font": layout.get("font", DEFAULT_FONT),
    }


def published_theme(connection: sqlite3.Connection) -> dict[str, Any]:
    """Accent colour, corner radius and font of the published layout, for every storefront page."""
    return _theme(_load_home(connection, "home_published")[0])
