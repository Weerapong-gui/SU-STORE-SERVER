"""Design row validation and catalog labels; independent of database wiring."""
import json

from .common import StoreError, as_int, clean_image_url, clean_text
from .contract import CONTRACT

MAX_DESIGN_IMAGES = CONTRACT["maxDesignImages"]
MAX_CODE_LENGTH = CONTRACT["maxDesignCodeLength"]
MAX_NAME_LENGTH = 80
CODE_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


def normalize_code(code: str) -> str:
    if isinstance(code, str):
        code = code.strip().upper()
        if code and len(code) <= MAX_CODE_LENGTH and all(c in CODE_CHARACTERS for c in code):
            return code
    raise StoreError(400, "ตัวย่อใช้ได้เฉพาะ A-Z, 0-9 และ -")


def design_label(code: str, name: str) -> str:
    return f"{code} {name}"


def clean_design(raw: dict, index: int) -> dict[str, str]:
    design = {
        "design_code": normalize_code(raw.get("designCode", "")),
        "design_name": clean_text(raw.get("designName"), f"ชื่อลาย ({index + 1})", required=True, max_len=MAX_NAME_LENGTH),
        "design_name_en": clean_text(raw.get("designNameEn"), "ชื่อลายภาษาอังกฤษ", max_len=MAX_NAME_LENGTH),
        "image": clean_image_url(None if "images" in raw else raw.get("image"), f"รูปลาย ({index + 1})"),
    }
    # Omission preserves the existing gallery for older admin clients.
    if "images" in raw:
        images = raw["images"]
        if not isinstance(images, list):
            raise StoreError(400, "รูปลายต้องเป็นรายการ URL")
        if len(images) > MAX_DESIGN_IMAGES:
            raise StoreError(400, f"รูปลายใส่ได้ไม่เกิน {MAX_DESIGN_IMAGES} รูป")
        images = [url for url in (clean_image_url(value, f"รูปลาย ({index + 1})") for value in images) if url]
        design["image"] = images[0] if images else ""
        design["extra_images_json"] = json.dumps(images[1:], ensure_ascii=False)
    return design


def clean_designs(variants: list) -> list[dict[str, str]]:
    designs = []
    codes: set[str] = set()
    prices: set[int] = set()
    for index, raw in enumerate(variants):
        if not isinstance(raw, dict):
            raise StoreError(400, "variants ต้องเป็น object")
        design = clean_design(raw, index)
        code = design["design_code"]
        if code in codes:
            raise StoreError(400, f"ตัวย่อ {code} ซ้ำกัน")
        codes.add(code)
        prices.add(as_int(raw.get("price"), f"ราคา ({design_label(code, design['design_name'])})"))
        designs.append(design)
    if len(prices) > 1:
        raise StoreError(400, "ทุกลายต้องราคาเดียวกัน")
    return designs
