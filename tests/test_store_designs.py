"""Design catalog validation and preservation of ordered options."""
import sqlite3

import pytest
from store import db
from store.designs import design_label

from .test_store_v2 import CUSTOMER, _conn, _product


def _designs() -> list[dict]:
    return [
        {"designCode": "scs", "designName": "วิทยาศาสตร์เครื่องสำอาง", "designNameEn": "Cosmetic Science", "image": "/product-images/scs.png", "price": 150, "size": "M", "color": "Black"},
        {"designCode": "AI", "designName": "ปัญญาประดิษฐ์", "price": 150},
    ]


def test_design_product_round_trip():
    conn = _conn()
    product = db.create_product(conn, {"name": "เสื้อ", "nameEn": "Shirt", "optionKind": "designs", "variants": _designs()})
    assert product["nameEn"] == "Shirt"
    assert product["optionKind"] == "designs"
    assert product["categoryId"] is None
    assert [v["designCode"] for v in product["variants"]] == ["SCS", "AI"]
    assert product["variants"][0]["label"] == "SCS วิทยาศาสตร์เครื่องสำอาง"
    assert product["variants"][0]["designNameEn"] == "Cosmetic Science"
    assert product["variants"][0]["image"] == "/product-images/scs.png"
    assert all(v["size"] == v["color"] == "" for v in product["variants"])
    updated = db.update_product(conn, product["id"], {"nameEn": "New Shirt", "variants": [{**product["variants"][0], "designName": "ลายใหม่"}]})
    assert updated["nameEn"] == "New Shirt"
    assert updated["variants"][0]["label"] == "SCS ลายใหม่"


def test_duplicate_design_code_rejected_case_insensitive():
    variants = _designs()
    variants[0]["designCode"], variants[1]["designCode"] = " scs ", "SCS"
    with pytest.raises(db.StoreError, match="ตัวย่อ SCS ซ้ำกัน") as error:
        db.create_product(_conn(), {"name": "เสื้อ", "optionKind": "designs", "variants": variants})
    assert error.value.status == 400


def test_design_requires_thai_name():
    with pytest.raises(db.StoreError) as error:
        db.create_product(_conn(), {"name": "เสื้อ", "optionKind": "designs", "variants": [{"designCode": "AI", "price": 150}]})
    assert error.value.status == 400


def test_designs_must_share_one_price():
    variants = _designs()
    variants[1]["price"] = 200
    with pytest.raises(db.StoreError, match="ทุกลายต้องราคาเดียวกัน") as error:
        db.create_product(_conn(), {"name": "เสื้อ", "optionKind": "designs", "variants": variants})
    assert error.value.status == 400


def test_switching_kind_deactivates_ordered_variants():
    conn = _conn()
    product = _product(conn)
    variant_id = product["variants"][0]["id"]
    order = db.create_order(conn, {"items": [{"variantId": variant_id, "quantity": 1}], "customer": CUSTOMER})
    updated = db.update_product(conn, product["id"], {"optionKind": "designs", "variants": _designs()})
    assert updated["optionKind"] == "designs"
    assert next(v for v in updated["variants"] if v["id"] == variant_id)["active"] is False
    assert all(v["active"] for v in updated["variants"] if v["id"] != variant_id)
    assert db.get_order(conn, order["orderCode"])["items"][0]["variantLabel"] == "Black"


def test_kind_change_without_variants_rejected():
    conn = _conn()
    product = _product(conn)
    with pytest.raises(db.StoreError, match="เปลี่ยนแบบตัวเลือกต้องส่งรายการตัวเลือกมาด้วย") as error:
        db.update_product(conn, product["id"], {"optionKind": "designs"})
    assert error.value.status == 400
    assert db.get_product(conn, product["id"], public=False) == product


def test_designs_to_options_keeps_order_label():
    conn = _conn()
    product = db.create_product(conn, {"name": "เสื้อ", "optionKind": "designs", "variants": _designs(), "status": "active"})
    design_id = product["variants"][0]["id"]
    order = db.create_order(conn, {"items": [{"variantId": design_id, "quantity": 1}], "customer": CUSTOMER})
    label = db.get_order(conn, order["orderCode"])["items"][0]["variantLabel"]
    updated = db.update_product(conn, product["id"], {"optionKind": "options", "variants": [{"size": "M", "color": "", "price": 150}]})
    assert updated["optionKind"] == "options"
    assert next(v for v in updated["variants"] if v["id"] == design_id)["active"] is False
    assert [v["size"] for v in updated["variants"] if v["active"]] == ["M"]
    assert db.get_order(conn, order["orderCode"])["items"][0]["variantLabel"] == label


def test_same_kind_without_variants_allowed():
    conn = _conn()
    product = _product(conn)
    updated = db.update_product(conn, product["id"], {"optionKind": "options", "nameEn": "Tote"})
    assert updated["nameEn"] == "Tote"


def test_options_product_unchanged():
    product = _product(_conn())
    assert product["optionKind"] == "options"
    assert product["nameEn"] == ""
    assert product["variants"][0]["label"] == "Black"
    assert product["variants"][0]["designCode"] == ""


def test_order_snapshots_both_languages() -> None:
    conn = _conn()
    product = db.create_product(conn, {
        "name": "พวงกุญแจ", "nameEn": "Keychain", "status": "active",
        "optionKind": "designs", "variants": _designs(),
    })
    order = db.create_order(conn, {
        "items": [{"variantId": v["id"], "quantity": 1} for v in product["variants"]],
        "customer": CUSTOMER,
    })
    item = order["items"][0]
    assert item["productNameEn"] == "Keychain"
    assert item["variantLabelEn"] == "SCS Cosmetic Science"
    assert item["productName"] == "พวงกุญแจ"
    assert item["variantLabel"] == "SCS วิทยาศาสตร์เครื่องสำอาง"
    assert order["items"][1]["variantLabelEn"] is None
    db.update_product(conn, product["id"], {
        "name": "ชื่อใหม่", "nameEn": "New Keychain",
        "variants": [{**v, "designName": "ลายใหม่", "designNameEn": "New Design"} for v in product["variants"]],
    })
    assert db.get_order(conn, order["orderCode"])["items"] == order["items"]


def test_old_order_item_has_null_english() -> None:
    conn = _conn()
    product = _product(conn)
    order = db.create_order(conn, {
        "items": [{"variantId": product["variants"][0]["id"], "quantity": 1}],
        "customer": CUSTOMER,
    })
    item = db.get_order(conn, order["orderCode"])["items"][0]
    assert item["productNameEn"] is None
    assert item["variantLabelEn"] is None


@pytest.mark.parametrize("code", ["", "A B", "ไทย", "A_", "A" * 13, 123])
def test_invalid_design_code(code):
    with pytest.raises(db.StoreError, match="ตัวย่อใช้ได้เฉพาะ A-Z, 0-9 และ -"):
        db.create_product(_conn(), {"name": "เสื้อ", "optionKind": "designs", "variants": [{"designCode": code, "designName": "ลาย", "price": 150}]})


@pytest.mark.parametrize("field,value", [("designName", "ก" * 81), ("designNameEn", "a" * 81), ("image", "https://example.com/a.png")])
def test_invalid_design_fields(field, value):
    variant = {**_designs()[0], field: value}
    with pytest.raises(db.StoreError):
        db.create_product(_conn(), {"name": "เสื้อ", "optionKind": "designs", "variants": [variant]})


@pytest.mark.parametrize("payload", [{"nameEn": "a" * 121}, {"optionKind": "other"}, {"optionKind": []}])
def test_invalid_product_fields(payload):
    with pytest.raises(db.StoreError):
        db.create_product(_conn(), {"name": "เสื้อ", **payload})


IMAGE_A = "/product-images/a.png"
IMAGE_B = "/product-images/b.png"
IMAGE_C = "/product-images/c.png"


def _image_product(conn: sqlite3.Connection, **fields: object) -> dict:
    return db.create_product(conn, {
        "name": "พวงกุญแจ", "optionKind": "designs", "status": "active",
        "variants": [{"designCode": "AGRO", "designName": "เกษตร", "price": 49, **fields}],
    })


def test_design_images_round_trip() -> None:
    conn = _conn()
    product = _image_product(conn, images=[IMAGE_A, IMAGE_B, IMAGE_C])
    for public in (True, False):
        variant = db.get_product(conn, product["id"], public=public)["variants"][0]
        assert variant["image"] == IMAGE_A
        assert variant["images"] == [IMAGE_A, IMAGE_B, IMAGE_C]


@pytest.mark.parametrize("image,expected", [(IMAGE_A, [IMAGE_A]), ("", [])])
def test_variant_images_from_legacy_row(image: str, expected: list[str]) -> None:
    product = _image_product(_conn(), image=image)
    assert product["variants"][0]["images"] == expected


def test_image_only_payload_keeps_extra_images() -> None:
    conn = _conn()
    product = _image_product(conn, images=[IMAGE_A, IMAGE_B])
    variant = {key: value for key, value in product["variants"][0].items() if key != "images"}
    updated = db.update_product(conn, product["id"], {"variants": [{**variant, "image": IMAGE_C}]})
    assert updated["variants"][0]["images"] == [IMAGE_C, IMAGE_B]


def test_image_only_payload_with_empty_image_promotes_first_extra() -> None:
    conn = _conn()
    product = _image_product(conn, images=[IMAGE_A, IMAGE_B, IMAGE_C])
    variant = {key: value for key, value in product["variants"][0].items() if key != "images"}
    updated = db.update_product(conn, product["id"], {"variants": [{**variant, "image": ""}]})
    assert updated["variants"][0]["image"] == IMAGE_B
    assert updated["variants"][0]["images"] == [IMAGE_B, IMAGE_C]


def test_too_many_design_images_rejected() -> None:
    conn = _conn()
    product = _image_product(conn, image=IMAGE_A)
    with pytest.raises(db.StoreError, match="รูปลายใส่ได้ไม่เกิน 8 รูป") as error:
        db.update_product(conn, product["id"], {"variants": [
            {**product["variants"][0], "images": [IMAGE_A] * 9},
        ]})
    assert error.value.status == 400
    assert db.get_product(conn, product["id"], public=False) == product


def test_bad_design_image_url_rejected() -> None:
    with pytest.raises(db.StoreError, match=r"รูปลาย \(1\)ต้องเป็นรูปที่อัปโหลดผ่านหลังร้าน") as error:
        _image_product(_conn(), images=["https://evil.example/x.png"])
    assert error.value.status == 400


@pytest.mark.parametrize("old_schema", [False, True])
def test_migration_adds_extra_images_column(old_schema: bool) -> None:
    conn = _conn()
    if old_schema:
        conn.execute("ALTER TABLE store_variants DROP COLUMN extra_images_json")
    db.ensure_store_db(conn)
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(store_variants)")}
    assert "extra_images_json" in columns
    assert columns["extra_images_json"][3:5] == (1, "'[]'")


@pytest.mark.parametrize("images", [[], [IMAGE_A] * 8])
def test_design_images_replace_entire_gallery(images: list[str]) -> None:
    conn = _conn()
    product = _image_product(conn, images=[IMAGE_A, IMAGE_B])
    updated = db.update_product(conn, product["id"], {"variants": [
        {**product["variants"][0], "images": images},
    ]})
    assert updated["variants"][0]["images"] == images
    assert updated["variants"][0]["image"] == (images[0] if images else "")


@pytest.mark.parametrize("images", [None, "bad", {}])
def test_design_images_must_be_list(images: object) -> None:
    with pytest.raises(db.StoreError) as error:
        _image_product(_conn(), images=images)
    assert error.value.status == 400


def test_options_have_empty_images() -> None:
    conn = _conn()
    product = _product(conn)
    assert product["variants"][0]["images"] == []
    assert conn.execute("SELECT extra_images_json FROM store_variants").fetchone()[0] == "[]"


def test_design_label_uses_space_separator():
    assert design_label("SCI", "School of Science") == "SCI School of Science"
