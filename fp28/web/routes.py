"""Every FP28 route, in the order the old if-chains tested them: the first match wins."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import routes_admin, routes_claim_station, routes_display, routes_public

if TYPE_CHECKING:
    from .handler import OrderRequestHandler


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    handler: Callable[[OrderRequestHandler, str, re.Match[str] | None], None]
    regex: bool = False

    def match(self, path: str) -> re.Match[str] | bool | None:
        return re.fullmatch(self.pattern, path) if self.regex else path == self.pattern


ROUTES: dict[str, list[Route]] = {
    "GET": [
        Route("GET", "/health", routes_public.handle_get_health),
        Route("GET", "/", routes_public.handle_get_root),
        Route("GET", "/admin/users", routes_admin.handle_get_admin_users),
        Route("GET", "/admin", routes_admin.handle_get_admin),
        Route("GET", "/docs", routes_public.handle_get_docs),
        Route("GET", "/openapi.yaml", routes_public.handle_get_openapi_yaml),
        Route("GET", "/admin/receipt-template", routes_admin.handle_get_admin_receipt_template),
        Route("GET", "/orders", routes_public.handle_get_orders),
        Route("GET", "/claim-station", routes_claim_station.handle_get_claim_station),
        Route("GET", "/display1", routes_display.handle_get_display1),
        Route("GET", "/display2", routes_display.handle_get_display2),
        Route("GET", "/dis3", routes_display.handle_get_dis3),
        Route("GET", "/dis3/data", routes_display.handle_get_dis3_data),
        Route("GET", r"/assets/(slides/[a-zA-Z0-9_.\-]+|[a-zA-Z0-9_.\-]+)", routes_display.handle_get_assets_by_id, regex=True),
        Route("GET", "/fonts/SukhumvitSet.ttc", routes_display.handle_get_fonts_sukhumvitset_ttc),
        Route("GET", "/display1/users", routes_display.handle_get_display1_users),
        Route("GET", "/display1/state", routes_display.handle_get_display1_state),
        Route("GET", "/display1/slides", routes_display.handle_get_display1_slides),
        Route("GET", "/display2/queue", routes_display.handle_get_display2_queue),
        Route("GET", r"/display2/assets/([A-Za-z0-9_.-]+)", routes_display.handle_get_display2_assets_by_id, regex=True),
        Route("GET", "/display2/admin/assets", routes_display.handle_get_display2_admin_assets),
        Route("GET", "/display2/history", routes_display.handle_get_display2_history),
        Route("GET", "/claim-station/stats", routes_claim_station.handle_get_claim_station_stats),
        Route("GET", "/claim-station/orders", routes_claim_station.handle_get_claim_station_orders),
        Route("GET", "/claim-station/orders-pending", routes_claim_station.handle_get_claim_station_orders_pending),
        Route("GET", r"/claim-station/orders/([A-Z0-9-]+)/slip", routes_claim_station.handle_get_claim_station_orders_by_id_slip, regex=True),
        Route("GET", r"/claim-station/orders/([A-Z0-9-]+)", routes_claim_station.handle_get_claim_station_orders_by_id, regex=True),
        Route("GET", "/check-order", routes_public.handle_get_check_order),
        Route("GET", "/admin/stats-stream", routes_admin.handle_get_admin_stats_stream),
        Route("GET", "/admin/orders", routes_admin.handle_get_admin_orders),
        Route("GET", r"/admin/orders/([A-Z0-9-]+)/extra-slips/(\d+)", routes_admin.handle_get_admin_orders_by_id_extra_slips_by_id, regex=True),
        Route("GET", r"/admin/orders/([A-Z0-9-]+)/extra-slips", routes_admin.handle_get_admin_orders_by_id_extra_slips, regex=True),
        Route("GET", r"/admin/orders/([A-Z0-9-]+)/slip-check", routes_admin.handle_get_admin_orders_by_id_slip_check, regex=True),
        Route("GET", r"/admin/orders/([A-Z0-9-]+)/slip", routes_admin.handle_get_admin_orders_by_id_slip, regex=True),
        Route("GET", "/products", routes_public.handle_get_products),
        Route("GET", "/site-settings", routes_public.handle_get_site_settings),
        Route("GET", "/site-status", routes_public.handle_get_site_status),
        Route("GET", "/admin/orders/export.csv", routes_admin.handle_get_admin_orders_export_csv),
        Route("GET", "/admin/product-breakdown", routes_admin.handle_get_admin_product_breakdown),
        Route("GET", "/admin/analytics", routes_admin.handle_get_admin_analytics),
        Route("GET", "/admin/audit-log", routes_admin.handle_get_admin_audit_log),
        Route("GET", "/admin/feedback", routes_admin.handle_get_admin_feedback),
        Route("GET", "/admin/site-settings", routes_admin.handle_get_admin_site_settings),
        Route("GET", "/admin/backups", routes_admin.handle_get_admin_backups),
        Route("GET", r"/admin/backups/(\d+)", routes_admin.handle_get_admin_backups_by_id, regex=True),
        Route("GET", r"/product-images/([^/]+)", routes_public.handle_get_product_images_by_id, regex=True),
        Route("GET", r"/orders/([A-Z0-9-]+)", routes_public.handle_get_orders_by_id, regex=True),
    ],
    "POST": [
        Route("POST", "/admin/users", routes_admin.handle_post_admin_users),
        Route("POST", r"/admin/orders/([A-Z0-9-]+)/slip", routes_admin.handle_post_admin_orders_by_id_slip, regex=True),
        Route("POST", r"/admin/orders/([A-Z0-9-]+)/extra-slips", routes_admin.handle_post_admin_orders_by_id_extra_slips, regex=True),
        Route("POST", r"/orders/([A-Z0-9-]+)/feedback", routes_public.handle_post_orders_by_id_feedback, regex=True),
        Route("POST", "/display1/update", routes_display.handle_post_display1_update),
        Route("POST", "/display1/slides/upload", routes_display.handle_post_display1_slides_upload),
        Route("POST", "/display1/slides/delete", routes_display.handle_post_display1_slides_delete),
        Route("POST", "/display2/pick", routes_display.handle_post_display2_pick),
        Route("POST", "/display2/undo", routes_display.handle_post_display2_undo),
        Route("POST", "/claim-station/login", routes_claim_station.handle_post_claim_station_login),
        Route("POST", "/admin/superlogin", routes_admin.handle_post_admin_superlogin),
        Route("POST", "/admin/test-warning", routes_admin.handle_post_admin_test_warning),
        Route("POST", "/admin/stop-test-warning", routes_admin.handle_post_admin_stop_test_warning),
        Route("POST", "/admin/backups", routes_admin.handle_post_admin_backups),
        Route("POST", r"/admin/products/([a-z0-9-]+)/image", routes_admin.handle_post_admin_products_by_id_image, regex=True),
        Route("POST", "/display2/admin/assets", routes_display.handle_post_display2_admin_assets),
        Route("POST", "/orders", routes_public.handle_post_orders),
    ],
    "PUT": [
        Route("PUT", r"/admin/products/([a-z0-9-]+)", routes_admin.handle_put_admin_products_by_id, regex=True),
        Route("PUT", r"/admin/users/([^/]+)/password", routes_admin.handle_put_admin_users_by_id_password, regex=True),
        Route("PUT", r"/orders/([A-Z0-9-]+)", routes_public.handle_put_orders_by_id, regex=True),
    ],
    "PATCH": [
        Route("PATCH", r"/claim-station/orders/([A-Z0-9-]+)/received", routes_claim_station.handle_patch_claim_station_orders_by_id_received, regex=True),
        Route("PATCH", r"/claim-station/orders/([A-Z0-9-]+)/khantok-claimed", routes_claim_station.handle_patch_claim_station_orders_by_id_khantok_claimed, regex=True),
        Route("PATCH", r"/claim-station/orders/([A-Z0-9-]+)/name", routes_claim_station.handle_patch_claim_station_orders_by_id_name, regex=True),
        Route("PATCH", r"/admin/orders/([A-Z0-9-]+)/status", routes_admin.handle_patch_admin_orders_by_id_status, regex=True),
        Route("PATCH", r"/admin/orders/([A-Z0-9-]+)", routes_admin.handle_patch_admin_orders_by_id, regex=True),
        Route("PATCH", "/admin/orders/bulk-status", routes_admin.handle_patch_admin_orders_bulk_status),
        Route("PATCH", r"/admin/products/([a-z0-9-]+)/available", routes_admin.handle_patch_admin_products_by_id_available, regex=True),
        Route("PATCH", "/admin/site-settings", routes_admin.handle_patch_admin_site_settings),
        Route("PATCH", r"/orders/([A-Z0-9-]+)/slip", routes_public.handle_patch_orders_by_id_slip, regex=True),
    ],
    "DELETE": [
        Route("DELETE", r"/display2/admin/assets/(\d+)", routes_display.handle_delete_display2_admin_assets_by_id, regex=True),
        Route("DELETE", r"/admin/users/([^/]+)", routes_admin.handle_delete_admin_users_by_id, regex=True),
        Route("DELETE", r"/admin/orders/([A-Z0-9-]+)/slip", routes_admin.handle_delete_admin_orders_by_id_slip, regex=True),
        Route("DELETE", r"/admin/orders/([A-Z0-9-]+)/extra-slips/(\d+)", routes_admin.handle_delete_admin_orders_by_id_extra_slips_by_id, regex=True),
        Route("DELETE", r"/admin/backups/(\d+)", routes_admin.handle_delete_admin_backups_by_id, regex=True),
    ],
}
