"""HTTP contract checks for every documented SU STORE v2 endpoint."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from .schema_check import assert_matches
from .test_store_v2 import _call, server  # noqa: F401 (import pytest fixture)

CONTRACT_PATH = Path(__file__).parents[1] / "contract" / "openapi.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _seed_contract_data(base: str) -> tuple[dict[str, str], dict[str, str]]:
    """Create a category, product, paid order, receipt, and regular staff session via HTTP."""
    auth = {"Authorization": "Bearer test-token"}
    _, body = _call(base, "POST", "/v2/admin/categories", {"name": "Test category"}, auth)
    category = json.loads(body)
    _, body = _call(base, "POST", "/v2/admin/products", {
        "name": "Test product", "categoryId": category["id"], "status": "active",
        "variants": [{"size": "M", "color": "Black", "price": 100, "stock": 20}],
    }, auth)
    product = json.loads(body)
    _call(base, "POST", "/v2/admin/products", {
        "name": "Design product", "status": "active", "optionKind": "designs",
        "variants": [{"designCode": "D1", "designName": "Design one", "price": 120, "stock": 10}],
    }, auth)
    order_codes: dict[str, str] = {}
    for order_status in ("pending_payment", "waiting_confirm", "paid", "ready", "completed", "cancelled"):
        _, body = _call(base, "POST", "/v2/orders", {
            "items": [{"variantId": product["variants"][0]["id"], "quantity": 1}],
            "customer": {"name": "Test", "phone": "0812345678", "email": "test@example.com"},
        })
        order = json.loads(body)
        code = order["orderCode"]
        _call(base, "PATCH", f"/v2/admin/orders/{code}", {"status": order_status}, auth)
        order_codes[order_status] = code
    code = order_codes["paid"]
    _, body = _call(base, "POST", f"/v2/admin/orders/{code}/receipt", {"customer": {"name": "Test"}}, auth)
    receipt = json.loads(body)
    return {"category": category["slug"], "product": product["slug"], "product_id": str(product["id"]),
            "category_id": str(category["id"]), "order": code, "receipt_id": str(receipt["id"])}, auth


def test_openapi_contract_is_present_and_documents_store_types() -> None:
    spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    expected = {
        "StoreCategory", "StoreVariant", "StoreProduct", "StoreOrderItem", "StoreOrder",
        "StoreMeta", "StoreSettings", "Dashboard", "HeroSlide", "HomeBlock", "HomeLayout",
        "ResolvedHome", "HomeAdminState", "ReceiptLayout", "StoreReceipt", "IssuedReceipt",
        "ReceiptSettings", "ReceiptPage", "StaffMember",
    }
    assert expected <= schemas.keys()
    assert spec["openapi"].startswith("3.1.")


def test_schema_validator_reports_json_path() -> None:
    with pytest.raises(AssertionError, match=r"\$\.items\[0\]\.id"):
        assert_matches({"items": [{"id": "wrong"}]}, {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {
                "type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
            }}},
            "required": ["items"],
        }, {"components": {"schemas": {}}})


def test_documented_get_responses_match_their_schemas(request: pytest.FixtureRequest) -> None:
    base = request.getfixturevalue("server")
    spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    ids, auth = _seed_contract_data(base)
    for path, path_item in spec["paths"].items():
        operation = path_item.get("get")
        if operation is None:
            continue
        concrete_path = _concrete_path(path, ids)
        if path in ("/v2/admin/receipt-template", "/v2/admin/orders.csv", "/v2/admin/orders/{orderCode}/slip"):
            continue  # 403 for regular staff or non-JSON response; covered for routing below.
        status, raw = _call(base, "GET", concrete_path, headers=auth)
        assert status == 200, f"GET {concrete_path}: {status} {raw}"
        body = json.loads(raw)
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert_matches(body, schema, spec)


def test_every_documented_path_is_routable(request: pytest.FixtureRequest) -> None:
    base = request.getfixturevalue("server")
    spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    ids, auth = _seed_contract_data(base)
    for path, path_item in spec["paths"].items():
        concrete_path = _concrete_path(path, ids)
        for method in path_item.keys() & HTTP_METHODS:
            status, body = _call(base, method.upper(), concrete_path, body={}, headers=auth)
            if status == 404:
                response = json.loads(body)
                assert response.get("message") != "not found", f"{method.upper()} {concrete_path} is not routed"
            assert status != 501, f"{method.upper()} {concrete_path} has no handler for this method"


def _concrete_path(path: str, ids: dict[str, str]) -> str:
    slug = ids["category"] if path.startswith("/v2/categories/") else ids["product"]
    return (path.replace("{slug}", slug).replace("{categorySlug}", ids["category"])
            .replace("{id}", ids["product_id"]).replace("{categoryId}", ids["category_id"])
            .replace("{orderCode}", ids["order"]).replace("{receiptId}", ids["receipt_id"])
            .replace("{username}", "staff1"))
