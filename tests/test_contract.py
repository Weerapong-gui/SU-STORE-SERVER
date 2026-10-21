"""Checks that shared storefront values come from the backend contract."""
import copy
from pathlib import Path

import pytest

from store import db, designs, home, receipts
from store.common import StoreError
from store.contract import CONTRACT, RECEIPT_TEMPLATE_PATH

from .test_store_v2 import _call, server  # noqa: F401 (import pytest fixture)


def test_python_constants_match_contract():
    assert tuple(CONTRACT["homeBlockTypes"]) == home.HOME_BLOCK_TYPES
    assert CONTRACT["maxHomeBlocks"] == home.MAX_HOME_BLOCKS
    assert CONTRACT["maxHeroSlides"] == home.MAX_HERO_SLIDES
    assert CONTRACT["maxFeatured"] == home.MAX_FEATURED
    assert CONTRACT["maxHomeCategories"] == home.MAX_HOME_CATEGORIES
    assert tuple(CONTRACT["homeFonts"]) == home.HOME_FONTS
    assert CONTRACT["defaultRadius"] == home.DEFAULT_RADIUS
    assert CONTRACT["maxDesignImages"] == designs.MAX_DESIGN_IMAGES
    assert CONTRACT["maxDesignCodeLength"] == designs.MAX_CODE_LENGTH
    assert CONTRACT["maxProductImages"] == db.MAX_PRODUCT_IMAGES
    assert CONTRACT["maxItemsPerOrder"] == db.MAX_ITEMS_PER_ORDER
    assert CONTRACT["maxQtyPerItem"] == db.MAX_QTY_PER_ITEM
    assert tuple(CONTRACT["receiptIssuableStatuses"]) == receipts.RECEIPT_ISSUABLE_STATUSES
    assert CONTRACT["receiptDefaultLayout"] == receipts.DEFAULT_LAYOUT
    assert CONTRACT["receiptLayoutMax"] == receipts.LAYOUT_MAX
    assert CONTRACT["receiptFontSizeMin"] == receipts.FONT_SIZE_MIN
    assert CONTRACT["receiptFontSizeMax"] == receipts.FONT_SIZE_MAX


@pytest.mark.parametrize(("key", "value"), [("size", 30.5), ("size", 3.9), ("x", 1000.5)])
def test_layout_bounds_come_from_contract(key, value):
    layout = copy.deepcopy(receipts.DEFAULT_LAYOUT)
    layout["fields"]["date"][key] = value
    with pytest.raises(StoreError):
        receipts._validate_layout(layout)


def test_receipt_template_served_from_contract(server):  # noqa: F811 (pytest fixture shadows imported fixture name)
    status, body = _call(server, "GET", "/v2/admin/receipt-template", headers={"Authorization": "Bearer test-token"})
    assert status == 200
    assert body == RECEIPT_TEMPLATE_PATH.read_bytes()
    assert Path(__file__).parents[1] / "contract" / "receipt_template_v2.svg" == RECEIPT_TEMPLATE_PATH
