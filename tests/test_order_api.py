"""Unit tests for order_api.py — pure logic functions only (no DB, no HTTP)."""
import os
import sys
from datetime import datetime

import pytest

# Add server directory to path so we can import order_api
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub out OCR module so order_api imports cleanly even without Tesseract
from unittest.mock import MagicMock

sys.modules["ocr"] = MagicMock()

from fp28 import config, khantok, pages, site, stats, timeutil  # noqa: E402


class TestGetScheduleStatus:
    """Tests for get_schedule_status — schedule open/closed/warning logic."""

    def test_disabled_schedule_always_open(self):
        result = site.get_schedule_status(False, "warning")
        assert result["scheduleClosed"] is False
        assert result["scheduleWarning"] is False

    def test_disabled_schedule_passes_through_message(self):
        result = site.get_schedule_status(False, "custom message")
        assert result["scheduleWarningMessage"] == "custom message"

    def test_enabled_schedule_returns_required_keys(self):
        result = site.get_schedule_status(True, "msg")
        assert "scheduleClosed" in result
        assert "scheduleWarning" in result
        assert "scheduleWarningMessage" in result


class TestOrderStatuses:
    """Verify ORDER_STATUSES and PAYMENT_STATUS_BY_ORDER_STATUS are consistent."""

    def test_all_order_statuses_have_payment_mapping(self):
        for status in config.ORDER_STATUSES:
            assert status in config.PAYMENT_STATUS_BY_ORDER_STATUS, (
                f"Order status '{status}' has no payment status mapping"
            )

    def test_payment_statuses_are_valid_values(self):
        valid_payment_statuses = {
            "awaiting_payment", "waiting_confirm", "paid", "rejected",
            "refund_pending", "refunded",
        }
        for order_status, payment_status in config.PAYMENT_STATUS_BY_ORDER_STATUS.items():
            assert payment_status in valid_payment_statuses, (
                f"Order status '{order_status}' maps to unknown payment status '{payment_status}'"
            )


class TestProductCost:
    """Tests for PRODUCT_COST — used in profit calculations."""

    def test_all_product_slugs_have_cost(self):
        expected_slugs = {"single", "jacket", "headband"}
        assert set(stats.PRODUCT_COST.keys()) == expected_slugs

    def test_costs_are_positive(self):
        for slug, cost in stats.PRODUCT_COST.items():
            assert cost > 0, f"Cost for '{slug}' must be positive"


class TestNowIso:
    """Tests for now_iso helper."""

    def test_returns_iso_string(self):
        result = timeutil.now_iso()
        assert isinstance(result, str)
        assert "T" in result  # ISO 8601 format

    def test_ends_with_utc_marker(self):
        result = timeutil.now_iso()
        assert result.endswith("Z") or "+" in result


class TestKhantokPrefix:
    """Tests for khantok ticket eligibility prefix."""

    def test_prefix_is_string(self):
        assert isinstance(config.KHANTOK_STUDENT_CODE_PREFIX, str)
        assert len(config.KHANTOK_STUDENT_CODE_PREFIX) > 0

    def test_quotas_are_positive(self):
        assert config.KHANTOK_QUOTA_100 > 0
        assert config.KHANTOK_QUOTA_50 > 0


class TestOrdersSummaryCache:
    """create_orders_summary_cached must not rescan orders for every caller.

    The uncached summary scans the whole orders table and json.loads every row,
    so concurrent admin pages / stat streams recomputing it is what starved the
    server of CPU.
    """

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        stats._SUMMARY_CACHE.clear()
        monkeypatch.setattr(stats, "_SUMMARY_CACHE_TTL", 60.0)
        yield
        stats._SUMMARY_CACHE.clear()

    def test_second_call_within_ttl_does_not_recompute(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            stats,
            "create_orders_summary",
            lambda conn, round_filter=0: calls.append(round_filter) or {"total": 1},
        )
        assert stats.create_orders_summary_cached(None) == {"total": 1}
        assert stats.create_orders_summary_cached(None) == {"total": 1}
        assert len(calls) == 1

    def test_different_round_filters_cached_separately(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            stats,
            "create_orders_summary",
            lambda conn, round_filter=0: calls.append(round_filter) or {"total": round_filter},
        )
        stats.create_orders_summary_cached(None, 1)
        stats.create_orders_summary_cached(None, 2)
        stats.create_orders_summary_cached(None, 1)
        assert calls == [1, 2]

    def test_caller_cannot_mutate_cached_value(self, monkeypatch):
        monkeypatch.setattr(
            stats, "create_orders_summary", lambda conn, round_filter=0: {"total": 1}
        )
        first = stats.create_orders_summary_cached(None)
        first["total"] = 999
        assert stats.create_orders_summary_cached(None)["total"] == 1

    def test_zero_ttl_disables_cache(self, monkeypatch):
        monkeypatch.setattr(stats, "_SUMMARY_CACHE_TTL", 0.0)
        calls = []
        monkeypatch.setattr(
            stats,
            "create_orders_summary",
            lambda conn, round_filter=0: calls.append(1) or {"total": 1},
        )
        stats.create_orders_summary_cached(None)
        stats.create_orders_summary_cached(None)
        assert len(calls) == 2


class TestStatsSnapshot:
    """get_stats_snapshot is shared by every open /admin/stats-stream connection."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        stats._STATS_SNAPSHOT = None
        monkeypatch.setattr(stats, "_STATS_SNAPSHOT_TTL", 60.0)
        yield
        stats._STATS_SNAPSHOT = None

    def test_snapshot_computed_once_within_ttl(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            stats,
            "_compute_stats_snapshot",
            lambda: calls.append(1) or {"summary": {}, "analytics": {}},
        )
        stats.get_stats_snapshot()
        stats.get_stats_snapshot()
        stats.get_stats_snapshot()
        assert len(calls) == 1

    def test_snapshot_recomputed_after_ttl(self, monkeypatch):
        calls = []
        monkeypatch.setattr(stats, "_STATS_SNAPSHOT_TTL", -1.0)
        monkeypatch.setattr(
            stats,
            "_compute_stats_snapshot",
            lambda: calls.append(1) or {"summary": {}, "analytics": {}},
        )
        stats.get_stats_snapshot()
        stats.get_stats_snapshot()
        assert len(calls) == 2


class TestStatsStreamLimit:
    """Concurrent stat streams are capped so leaked threads cannot pile up again."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        stats._stats_stream_count = 0
        yield
        stats._stats_stream_count = 0

    def test_acquire_up_to_max_then_refuses(self, monkeypatch):
        monkeypatch.setattr(stats, "_STATS_STREAM_MAX", 2)
        assert stats.stats_stream_acquire() is True
        assert stats.stats_stream_acquire() is True
        assert stats.stats_stream_acquire() is False

    def test_release_frees_a_slot(self, monkeypatch):
        monkeypatch.setattr(stats, "_STATS_STREAM_MAX", 1)
        assert stats.stats_stream_acquire() is True
        assert stats.stats_stream_acquire() is False
        stats.stats_stream_release()
        assert stats.stats_stream_acquire() is True

    def test_release_never_goes_negative(self):
        stats.stats_stream_release()
        stats.stats_stream_release()
        assert stats._stats_stream_count == 0


class TestKhantokClaimWarning:
    """รับของไปคนละวันไทยกับวันนี้ = เตือน staff ว่าอาจเคยได้บัตรไปพร้อมของแล้ว

    หรือรับของไปแล้ว (status == 'received') แต่ received_at พิสูจน์ไม่ได้ว่าเป็นวันนี้
    (admin กดสถานะ 'received' ตรงๆ ไม่ผ่าน claim station จึงไม่มี timestamp)
    """

    def _now(self):
        return datetime(2026, 7, 27, 9, 0, tzinfo=timeutil.TZ_BANGKOK)

    def test_received_today_does_not_warn(self):
        # flow ปกติ: กดรับของแล้วกดรับบัตรต่อในนาทีเดียวกัน ต้องเงียบ
        now = datetime(2026, 7, 27, 14, 0, tzinfo=timeutil.TZ_BANGKOK)
        assert khantok.khantok_claim_warning("2026-07-27T09:30:00+07:00", now=now) is False

    def test_received_yesterday_warns(self):
        assert khantok.khantok_claim_warning("2026-07-26T18:00:00+07:00", now=self._now()) is True

    def test_missing_value_does_not_warn(self):
        assert khantok.khantok_claim_warning(None, now=self._now()) is False
        assert khantok.khantok_claim_warning("", now=self._now()) is False

    def test_naive_utc_row_is_read_as_utc(self):
        # 2026-07-26T17:30Z == 2026-07-27T00:30 +07 -> วันไทยเดียวกับ now -> ไม่เตือน
        assert khantok.khantok_claim_warning("2026-07-26T17:30:00Z", now=self._now()) is False

    def test_naive_space_separated_value_with_no_offset_is_read_as_utc(self):
        # ไม่มีทั้ง offset และ 'Z' ต้องยังถูกอ่านเป็น UTC เหมือนกัน (ใช้ branch
        # tzinfo is None ของ _parse_stored_datetime ซึ่งก่อนหน้านี้ไม่มีเทสต์คุม)
        # UTC 17:30 26 ก.ค. == ไทย 00:30 27 ก.ค. -> วันเดียวกับ now -> ไม่เตือน
        assert khantok.khantok_claim_warning("2026-07-26 17:30:00", now=self._now()) is False

    def test_space_separated_value_is_accepted(self):
        assert khantok.khantok_claim_warning("2026-07-26 18:00:00+07:00", now=self._now()) is True

    def test_unparseable_value_does_not_warn(self):
        assert khantok.khantok_claim_warning("not a date", now=self._now()) is False

    def test_naive_now_is_assumed_bangkok(self):
        naive_now = datetime(2026, 7, 27, 9, 0)  # noqa: DTZ001  (naive on purpose)
        assert khantok.khantok_claim_warning("2026-07-26T18:00:00+07:00", now=naive_now) is True

    # --- status-aware rules (Finding 2) ---

    def test_received_status_with_null_timestamp_warns(self):
        assert khantok.khantok_claim_warning(None, status="received", now=self._now()) is True

    def test_received_status_with_empty_timestamp_warns(self):
        assert khantok.khantok_claim_warning("", status="received", now=self._now()) is True

    def test_received_status_with_unparseable_timestamp_warns(self):
        assert khantok.khantok_claim_warning("not a date", status="received", now=self._now()) is True

    def test_received_status_with_todays_timestamp_does_not_warn(self):
        assert khantok.khantok_claim_warning(
            "2026-07-27T09:30:00+07:00", status="received", now=self._now()
        ) is False

    def test_not_received_status_with_null_timestamp_does_not_warn(self):
        assert khantok.khantok_claim_warning(None, status="shipped", now=self._now()) is False


class TestClaimStationTemplate:
    def test_phase_four_gate_is_removed(self):
        # ด่านเดิมคือ Number(order.roundNumber) === 4 ต้องไม่เหลืออยู่
        assert "roundNumber) === 4" not in pages.CLAIM_STATION_HTML

    def test_claim_button_passes_the_warning_flag(self):
        assert "khantokClaimWarning" in pages.CLAIM_STATION_HTML
        assert "doClaimKhantok(code, needConfirm)" in pages.CLAIM_STATION_HTML

    def test_warning_panel_uses_the_existing_warn_palette(self):
        # ต้องเลือกสีตาม warning state จริง ไม่ใช่แค่มีคำว่า var(--warn-bg) ลอยอยู่ที่ไหนก็ได้ในไฟล์
        # (ตัวแปรนี้มีอยู่แล้วใน CSS :root ก่อนหน้า task นี้ด้วย จึงต้องล็อกไปที่ ternary ที่เลือกสีจริง)
        assert "accent = warn ? 'var(--warn)' : 'var(--ok)'" in pages.CLAIM_STATION_HTML
        assert "accentBg = warn ? 'var(--warn-bg)' : 'var(--ok-bg)'" in pages.CLAIM_STATION_HTML

    def test_timestamp_helper_reads_utc_getters(self):
        # กันการกลับไปใช้ .getHours() ซึ่งบวก offset ซ้ำบนเครื่องที่ตั้งเวลาไทย
        assert "function _fmtBkkShort(" in pages.CLAIM_STATION_HTML
        assert "getUTCHours()" in pages.CLAIM_STATION_HTML


class TestAdminKhantokStationCards:
    def test_both_cards_are_rendered(self):
        assert 'id="statKhantokClaimed"' in pages.ADMIN_HTML
        assert 'id="statKhantokOnly"' in pages.ADMIN_HTML

    def test_both_cards_are_wired_for_live_updates(self):
        # setOdom ถูกนิยามเฉพาะใน updateOrderStats() การเจอคู่นี้จึงพิสูจน์ว่า
        # การ์ดอัปเดตตอน stats-stream ส่งค่าใหม่ ไม่ใช่แค่ตอน render ครั้งแรก
        assert "setOdom('statKhantokClaimed'" in pages.ADMIN_HTML
        assert "setOdom('statKhantokOnly'" in pages.ADMIN_HTML


class TestAdminPaginationPlacement:
    def test_pagination_bar_sits_above_the_orders_table(self):
        html = pages.ADMIN_HTML
        assert html.index('id="paginationBar"') < html.index('id="ordersBody"')

    def test_page_info_is_not_duplicated_in_the_notice_line(self):
        assert 'setOrdersNotice("Page "' not in pages.ADMIN_HTML

    def test_pagination_bar_uses_the_new_label_format(self):
        # ต้องเป็นรูปแบบใหม่ที่ renderPagination ประกอบเอง ไม่ใช่ '(n orders)' แบบเดิม
        assert "'/' + totalPages + ' — ' + totalOrders + ' total orders'" in pages.ADMIN_HTML
        assert "' (' + totalOrders + ' orders)'" not in pages.ADMIN_HTML


class TestAdminSingleDarkTheme:
    """หน้า admin เหลือธีมเดียว — ร่องรอยของระบบสองธีมต้องไม่เหลือ"""

    def test_no_light_theme_machinery_remains(self):
        html = pages.ADMIN_HTML
        for leftover in ("prefers-color-scheme", 'data-theme', "suStoreTheme",
                         "theme-btn", "themeToggleBtn", "initTheme"):
            assert leftover not in html, "ยังเหลือร่องรอยระบบสองธีม: " + leftover

    def test_root_declares_dark_color_scheme(self):
        # ถ้าไม่ประกาศ scrollbar/dropdown/date picker ของเบราว์เซอร์จะเป็นกล่องขาว
        assert "color-scheme: dark" in pages.ADMIN_HTML
        assert "color-scheme: light dark" not in pages.ADMIN_HTML

    def test_new_tokens_are_declared(self):
        html = pages.ADMIN_HTML
        for token in ("--surface-2:", "--line-strong:", "--faint:", "--ok-bg:",
                      "--warn-bg:", "--danger-bg:", "--accent-bg:", "--purple-bg:",
                      "--neutral-bg:", "--r-sm:", "--r-md:", "--r-lg:", "--r-pill:",
                      "--shadow-1:", "--shadow-2:"):
            assert token in html, "ไม่พบ token: " + token

    def test_legacy_token_names_still_resolve(self):
        # โค้ดส่วนอื่นยังอ้างชื่อเดิมอยู่ ลบทิ้งแล้วสีจะหายเป็นช่วงๆ
        html = pages.ADMIN_HTML
        for legacy in ("--surface2:", "--header-bg:", "--badge-default:", "--badge-warn:",
                       "--badge-ok:", "--badge-danger:", "--badge-purple:",
                       "--editing-row:", "--toggle-track:", "--bar-track:", "--img-bg:"):
            assert legacy in html, "ลบ token เดิมที่ยังมีคนใช้: " + legacy


class TestAdminComponentLayer:
    """คอมโพเนนต์ที่ Task 3-5 จะใช้ ต้องมี rule รองรับก่อน"""

    def test_new_component_rules_exist(self):
        html = pages.ADMIN_HTML
        for rule in (".card {", ".card.tight {", ".chip {", ".pager {"):
            assert rule in html, "ไม่พบ rule: " + rule

    def test_active_chip_matches_the_class_js_actually_toggles(self):
        # JS สลับ active-phase ถ้า CSS รับแต่ .active ชิปที่เลือกจะไม่เปลี่ยนสี
        assert ".chip.active-phase" in pages.ADMIN_HTML

    def test_badge_status_modifiers_exist(self):
        html = pages.ADMIN_HTML
        for mod in (".badge.ok", ".badge.warn", ".badge.danger",
                    ".badge.accent", ".badge.purple", ".badge.neutral"):
            assert mod in html, "ไม่พบ badge modifier: " + mod

    def test_large_radius_is_used_for_cards(self):
        assert "border-radius: var(--r-lg)" in pages.ADMIN_HTML

    def test_stat_cards_wide_enough_for_hero_numerals(self):
        # Orders summary renders class="stats stats-row" — .stats-row is a
        # single-class selector declared after .stats with equal specificity,
        # so it wins on every property it sets, including display: flex. The
        # grid's minmax(190px, 1fr) floor is therefore inert on this element
        # (see Finding 2) — the width floor that keeps a 44px/800 hero
        # numeral from bleeding into the next card must come from a
        # flex-basis on the row's children instead. .stat > strong keeps
        # overflow-wrap as a second line of defence regardless of layout mode.
        html = pages.ADMIN_HTML
        idx = html.index(".stats-row > .stat {")
        rule = html[idx:html.index("}", idx)]
        assert "flex: 1 1 190px" in rule, "ไม่มี flex-basis 190px บน .stats-row > .stat"
        idx2 = html.index(".stat > strong {")
        rule2 = html[idx2:html.index("}", idx2)]
        assert "overflow-wrap: anywhere" in rule2, "ไม่มี overflow-wrap guard ใน .stat > strong"

    def test_stats_row_neutralises_the_grid_chrome(self):
        # .stats' background/border/border-radius/overflow were written for a
        # flush hairline grid (1px gaps). Orders renders class="stats
        # stats-row", and since only display is overridden by the later,
        # equally-specific .stats-row rule, those container-chrome
        # properties still apply — under 12px flex gaps that turns into a
        # visible grey plate behind unequal cards with clipped corners
        # (Finding 2). .stats-row must null the chrome out itself.
        html = pages.ADMIN_HTML
        idx = html.index(".stats-row {")
        rule = html[idx:html.index("}", idx)]
        for prop in ("background: none", "border: none", "border-radius: 0", "overflow: visible"):
            assert prop in rule, ".stats-row ขาด " + prop

    def test_hero_numeral_size_is_scoped_to_order_stats(self):
        # .stat > strong used to be 44px/800 unconditionally, so Analytics'
        # plain .stat cards — same class, ~158px track — inherited it too,
        # and baht() strings like "฿1,234,567" spilled into the next card
        # (Finding 3). The hero size must be scoped to #orderStats, and the
        # unscoped base rule must stay small enough to fit Analytics' track.
        html = pages.ADMIN_HTML
        idx = html.index(".stat > strong {")
        base_rule = html[idx:html.index("}", idx)]
        assert "44px" not in base_rule, "hero 44px ยังอยู่ใน .stat > strong ฐาน จะรั่วไปที่ Analytics"
        idx2 = html.index("#orderStats .stat")
        hero_rule = html[idx2:html.index("}", idx2)]
        assert "font-size: 44px" in hero_rule, "ไม่มี hero 44px ที่ scope ด้วย #orderStats"
        assert "font-weight: 800" in hero_rule, "hero rule ควรคง font-weight: 800 ไว้"

    def test_no_button_rule_pairs_status_token_background_with_white_foreground(self):
        # Task 1 replaced saturated --ok/--accent/--danger with pastels meant
        # to sit as *foreground* colour on --ok-bg/--accent-bg — not as a
        # background with white text on top, which measures as low as
        # 1.74:1. Any button rule that still backgrounds with a status token
        # and colours the text white is unreadable under venue lighting
        # (Finding 1).
        html = pages.ADMIN_HTML
        style_block = html[html.index("<style>"):html.index("</style>")]
        status_tokens = ("var(--ok)", "var(--accent)", "var(--danger)", "var(--warn)", "var(--purple)")
        for line in style_block.splitlines():
            stripped = line.strip()
            if not stripped.startswith("button"):
                continue
            if "color: #fff" not in stripped and "color:#fff" not in stripped:
                continue
            assert not any(tok in stripped for tok in status_tokens), (
                "button rule pairs a status token background with a white foreground: " + stripped
            )

    def test_pending_payment_badge_is_distinct_from_refunded(self):
        # pending_payment has no .badge.<status> rule of its own, so it fell
        # through to .badge's default — the same --neutral-bg/--muted pair
        # .badge.refunded uses. Scanning rows by colour no longer separated
        # "waiting for a slip" from "already refunded" (Finding 4).
        html = pages.ADMIN_HTML
        assert ".badge.pending_payment { background: var(--accent-bg); color: var(--accent); }" in html

    def test_needs_action_row_tint_is_visible_on_dark_surface(self):
        # rgba(180,83,9,.05) over --surface #1a1a1f moves the row by ~4 RGB
        # units — invisible. The left bar and the ⚠ marker still carry the
        # cue, but the row highlight itself needs a higher alpha to read on
        # a dark surface (Finding 5).
        html = pages.ADMIN_HTML
        assert "rgba(180,83,9,.05)" not in html, "alpha เดิม .05 ที่มองไม่เห็นบนพื้นมืดยังอยู่"
        assert "rgba(180,83,9,.14)" in html, "ไม่พบ alpha ใหม่ .14 สำหรับ tr.needs-action"


class TestAdminSummaryZoneDeinlined:
    """โซนที่เห็นทันทีที่เปิดหน้าต้องใช้ class ไม่ใช่ inline"""

    def test_phase_chips_use_the_chip_class(self):
        assert 'class="chip active-phase phase-btn"' in pages.ADMIN_HTML

    def test_phase_button_renderer_paints_chip_not_ghost(self):
        # #phaseBtnContainer มี chip seed อยู่ในมาร์กอัปนิ่ง แต่ renderPhase() เขียนทับ
        # innerHTML ใหม่ทุกครั้งที่โหลด phase config — ถ้าฟังก์ชันนี้ยังปั้นปุ่มด้วย
        # "ghost phase-btn" ผู้ใช้จะเห็นปุ่มเก่าทันทีที่หน้าโหลดเสร็จ ไม่ใช่แค่ตอน seed
        assert 'fhtml = \'<button class="chip' in pages.ADMIN_HTML
        assert 'ghost phase-btn' not in pages.ADMIN_HTML
        assert 'style="min-height:30px;font-size:12px;padding:4px 12px"' not in pages.ADMIN_HTML

    def test_pagination_bar_has_no_inline_style(self):
        assert '<div id="paginationBar" class="pager"></div>' in pages.ADMIN_HTML
        assert 'id="paginationBar" style=' not in pages.ADMIN_HTML

    def test_bulk_bar_starts_hidden(self):
        # เดิมประกาศ display สองครั้งในสตริงเดียว ตัวหลังชนะ แถบเลยกางค้าง
        assert 'id="bulkBar" class="toolbar" hidden' in pages.ADMIN_HTML
        assert 'display:none;padding:10px 0;display:flex' not in pages.ADMIN_HTML

    def test_hidden_attribute_actually_hides(self):
        # .toolbar { display: flex } (author, normal) ชนะ [hidden]{display:none}
        # ของ UA stylesheet (user-agent, normal) เสมอ เพราะ origin ตัดสินก่อน specificity —
        # ถ้าไม่มี guard ของเราเอง #bulkBar จะกางค้างแม้ .hidden = true
        assert "[hidden] { display: none !important; }" in pages.ADMIN_HTML

    def test_khantok_ring_track_uses_a_token(self):
        assert "#e8e8e8" not in pages.ADMIN_HTML
        assert 'stroke="var(--surface-2)"' in pages.ADMIN_HTML

    def test_clickable_stat_card_uses_a_class(self):
        assert 'class="stat compact clickable"' in pages.ADMIN_HTML
        assert 'cursor:pointer;border-bottom:2px solid var(--accent)' not in pages.ADMIN_HTML

    def test_add_phase_button_uses_a_chip_class_not_inline_style(self):
        # #addPhaseBtn ต้องแยกจากกลุ่ม chip ด้วย class ไม่ใช่ inline style เกาะเดี่ยว
        # ที่เหลืออยู่ตัวเดียวในโซนที่ de-inline ไปแล้ว
        assert 'id="addPhaseBtn" class="chip chip-add"' in pages.ADMIN_HTML
        assert 'id="addPhaseBtn" class="ghost" style=' not in pages.ADMIN_HTML


class TestAdminOrderRowsDeinlined:
    """แถวตารางต้องไม่มีสีสว่างฝังตรงและใช้ class แทน inline"""

    def test_no_hardcoded_light_colours_remain(self):
        html = pages.ADMIN_HTML
        for hexcolour in ("#e5e7eb", "#fef9c3", "#ca8a04", "#e8e8e8"):
            assert hexcolour not in html, "ยังมีสีสว่างฝังตรง: " + hexcolour

    def test_item_separator_uses_a_class(self):
        assert '<hr class="item-sep">' in pages.ADMIN_HTML
        assert 'border-top:1px solid #e5e7eb' not in pages.ADMIN_HTML

    def test_edit_button_editing_state_uses_a_class(self):
        assert "editOrderBtn' + (isEditing ? ' editing' : '')" in pages.ADMIN_HTML

    def test_row_action_buttons_use_a_class(self):
        assert 'class="row-actions"' in pages.ADMIN_HTML
        assert 'font-size:12px;min-height:28px;flex:1' not in pages.ADMIN_HTML

    def test_slip_and_ticket_markers_use_status_classes(self):
        html = pages.ADMIN_HTML
        assert 'class="mark ok"' in html
        assert 'style="color:var(--ok)"' not in html

    def test_edit_button_is_exempt_from_the_row_actions_stretch(self):
        # เดิม editOrderBtn ไม่มี flex เลย ขนาดพอดีกับข้อความ "Edit" เหมือน receiptBtn
        # ถ้า .row-actions > button.receiptBtn ยกเว้นให้ตัวเดียว editOrderBtn จะรับ
        # flex:1 จาก .row-actions > button แล้วยืดเต็มแถว เปลี่ยน layout ของ action cell
        assert (
            ".row-actions > button.receiptBtn, .row-actions > button.editOrderBtn { flex: 0 0 auto; }"
            in pages.ADMIN_HTML
        )


class TestAdminPagerDeinlined:
    def test_pager_info_uses_a_class(self):
        assert 'class="pager-info"' in pages.ADMIN_HTML
        assert 'style="font-size:13px;color:var(--muted)">Page ' not in pages.ADMIN_HTML

    def test_pager_label_format_is_unchanged(self):
        # เทสต์เดิมของแถบนี้ต้องไม่ตก
        assert "'/' + totalPages + ' — ' + totalOrders + ' total orders'" in pages.ADMIN_HTML
