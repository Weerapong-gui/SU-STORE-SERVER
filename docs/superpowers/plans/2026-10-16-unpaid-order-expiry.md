> หมายเหตุ (2026-10-21): คัดมาจาก repo `SU-STORE` เดิม path ในเอกสารยังเป็นโครงสร้างเก่า (`server/...`, `fp28/...`) ให้เทียบกับโครงสร้างปัจจุบันของ repo นี้ก่อนลงมือ

# Unpaid Order Expiry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store v2 `pending_payment` orders past an admin-set time limit become `expired` and give their stock back. Late slips re-reserve the stock or send the order to a refund queue. Customers see a countdown.

**Architecture:** Status rules and all-or-nothing stock moves live in a new `server/store/order_states.py`, which `db.update_order` and slip handling both use. Timer logic (settings, deadline, sweep, late slips) lives in a new `server/store/expiry.py`. A daemon thread in `order_api.py` runs the sweep every 60 s. The Next.js side adds three statuses, a countdown on `/order/[code]`, staff slip upload, and a settings card.

**Tech Stack:** Python 3 stdlib + SQLite (order-api), Next.js 14 / React / Tailwind, pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-10-16-unpaid-order-expiry-design.md`

## Global Constraints

- Only `store_*` tables are written; FP28 tables and `server/fp28/` behaviour are untouched.
- `store_orders.created_at` (and the new clock column) holds Bangkok time **with a `+07:00` offset** (`now_iso()`). Parse it with `datetime.fromisoformat`. Never add 7 hours.
- Settings defaults: `payment_expiry_enabled = "0"`, `payment_expiry_minutes = "60"`. The accepted range is 10–1440.
- New statuses and labels: `expired` = "หมดเวลาชำระ", `refund_pending` = "รอคืนเงิน", `refunded` = "คืนเงินแล้ว".
- Stock-holding statuses: `pending_payment`, `waiting_confirm`, `paid`, `ready`, `completed`. Every other status holds no stock.
- Expired-order copy (TH), verbatim: "หมดเวลาชำระแล้ว ถ้าโอนแล้วยังอัปสลิปได้ ทีมงานจะตรวจสอบให้ ผ่านทาง" followed by the link `https://www.instagram.com/su.mfu/`.
- Clean-code rules in `CLAUDE.md` §3.5:
  - errors are `StoreError(status, Thai message)`
  - new Python functions have type hints
  - no `any` in TS
  - user-facing strings are Thai (admin) or come from `storeI18n.ts` (storefront)
  - confirms use `useConfirm()`
- Per task, run that task's tests. Before finishing: `ruff check server tests` · `npm run lint` · `npx tsc --noEmit` · `npm test` · pytest for both `server/tests` and `tests/`.
- pytest needs the env vars from `.github/workflows/ci.yml`. Prefix every pytest command with:
  `ORDER_API_DB_PATH=/tmp/su-order-api/orders.db ORDER_API_SLIPS_DIR=/tmp/su-order-api/slips ORDER_API_SLIDES_DIR=/tmp/su-order-api/slides ORDER_API_ASSETS_DIR=/tmp/su-order-api/display2_assets`
  The steps below write it as `$ENV`.

## Review Focus

1. **A slip arrives seconds after the sweep flipped the order to `expired`.** The customer still gets a 200, and the order ends up in `waiting_confirm` or `refund_pending`, never stuck. Test: `test_receive_slip_on_expired_order_revives` (Task 3).
2. **The sweep and a staff status change hit the same order at once.** Stock comes back exactly once. Test: `test_sweep_skips_order_changed_after_read` (Task 2).
3. **Staff push `paid` back to `pending_payment` on a week-old order.** Its clock restarts, so the next sweep leaves it alone. Test: `test_back_to_pending_restarts_clock` (Task 1).
4. **A multi-line order where only one line has stock left.** Nothing is deducted and the order goes to `refund_pending`. Test: `test_partial_stock_goes_to_refund_and_keeps_stock` (Task 3).
5. **The browser clock is hours off.** The countdown follows `serverNow`, not `Date.now()` alone. Test: `paymentCountdown.test.tsx › uses server time offset` (Task 5).

---

### Task 1: Status rules and stock moves

**Files:**
- Create: `server/store/order_states.py`
- Modify: `server/store/db.py`:
  - `ORDER_STATUSES` (≈line 49): add the 3 statuses after `completed`, before `cancelled`
  - schema migration (≈line 196): add column `payment_clock_started_at TEXT` to `store_orders`
  - `create_order` INSERT: set `payment_clock_started_at = now`
  - `update_order` (≈line 709): delegate status changes
- Test: `server/tests/test_store_expiry.py` (new; import helpers like `test_store_receipts.py` does: `from server.store import db, order_states` and `from .test_store_v2 import CUSTOMER, _call, _conn, _product`)

**Interfaces:**
- Produces in `order_states.py`:
  - `STOCK_HOLDING_STATUSES: frozenset[str]`
  - `NON_SALE_STATUSES: tuple[str, ...] = ("cancelled", "expired", "refund_pending", "refunded")`
  - `SYSTEM_ONLY_STATUSES = frozenset({"expired", "refund_pending"})`: PATCH may never target these
  - `RESTRICTED_FROM: dict[str, frozenset[str]] = {"expired": {"paid","cancelled"}, "refund_pending": {"refunded","waiting_confirm","cancelled"}, "refunded": frozenset()}`. Any target outside the set raises `StoreError(409, "เปลี่ยนสถานะนี้ไม่ได้")`. `refunded` is only reachable from `refund_pending`. `cancelled` stays final, as it already is.
  - `take_order_stock(conn, order_id: int) -> bool`: all-or-nothing. Do it inside `SAVEPOINT take_stock`, using the same `UPDATE … SET stock = stock - ? WHERE id = ? AND stock IS NOT NULL AND stock >= ?` as `create_order`. A NULL-stock variant always succeeds. If any line fails, run `ROLLBACK TO take_stock` and return False. In both cases finish with `RELEASE take_stock`.
  - `return_order_stock(conn, order_id: int) -> None`
  - `MIN_EXPIRY_MINUTES = 10`, `MAX_EXPIRY_MINUTES = 1440`, `DEFAULT_EXPIRY_MINUTES = 60`: kept here so `db.py` and `expiry.py` can both import them (`expiry` imports `db`, never the reverse).
  - `change_status(conn, row: sqlite3.Row, to: str, now: str) -> None`: the whole body runs inside `SAVEPOINT change_status`. On any exception it runs `ROLLBACK TO change_status`, `RELEASE` and re-raises, so a caller that catches the `StoreError` (the sweep, a late slip) is left with nothing changed. Steps:
    - validates the target against the rules above;
    - runs the conditional `UPDATE … WHERE id=? AND status=?` and raises `StoreError(409, "ออเดอร์ถูกแก้ไขไปแล้ว กรุณารีเฟรช")` when it updates 0 rows;
    - moves stock only when holding-ness changes: into holding calls `take_order_stock`, and a False result raises `StoreError(409, "สินค้าไม่พอสำหรับออเดอร์นี้")`, and the savepoint undoes the status change. Out of holding calls `return_order_stock`.
    - when `to == "pending_payment"`, sets `payment_clock_started_at = now`.
- `db.update_order` keeps its signature `(conn, order_code, payload) -> tuple[dict, str | None]`. It still validates `status in ORDER_STATUSES`, then calls `order_states.change_status`.

- [ ] **Step 1: Write the failing tests** in `server/tests/test_store_expiry.py`, class `TestTransitions`. Helper `_order(conn, stock=2, qty=1) -> tuple[str, int]` creates a product plus an order and returns `(code, variant_id)`. Helper `_stock(conn, vid)` reads the stock. Helper `_force(conn, code, status)` sets the status directly with SQL, for setup only.

```python
def test_patch_cannot_target_system_statuses(self):  # expired / refund_pending → 409
def test_expired_to_cancelled_does_not_return_stock_twice(self):
    # stock 2, order qty 1 → 1; _force expired + manually return stock (→2); PATCH cancelled → stock stays 2
def test_expired_to_paid_takes_stock_or_refuses(self):
    # ok case: stock back to 1 after paid; no-stock case: 409 "สินค้าไม่พอสำหรับออเดอร์นี้", status still expired, stock unchanged
def test_refund_pending_to_cancelled_keeps_stock(self):
def test_refunded_is_final(self):  # refunded → paid raises 409
def test_refund_pending_retry_reserves_all_or_nothing(self):
    # two-line order, one line out of stock → 409 and both stocks unchanged
def test_back_to_pending_restarts_clock(self):
    # set payment_clock_started_at to 2020-01-01T00:00:00+07:00, PATCH waiting_confirm→pending_payment,
    # column is now ≥ test start time
def test_create_order_sets_clock(self):  # payment_clock_started_at == created_at
```
Keep the existing `test_store_v2.py` cancel/stock tests unchanged. They must still pass.

- [ ] **Step 2:** Run `$ENV python3 -m pytest server/tests/test_store_expiry.py -v`. Expected: FAIL (`order_states` not found).
- [ ] **Step 3:** Implement `order_states.py` and the `db.py` edits. Move the inline stock return out of `update_order` into `return_order_stock`, so no duplicate code is left behind.
- [ ] **Step 4:** Run `$ENV python3 -m pytest server/tests/test_store_expiry.py server/tests/test_store_v2.py server/tests/test_store_receipts.py -v`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(store): expired/refund statuses with all-or-nothing stock moves`

---

### Task 2: Expiry settings and the sweep

**Files:**
- Create: `server/store/expiry.py`
- Modify:
  - `server/store/db.py`: `SETTING_DEFAULTS`, `get_settings`, `update_settings`
  - `server/store/dashboard.py`: use `NON_SALE_STATUSES` instead of the three `!= 'cancelled'` checks
- Test: `server/tests/test_store_expiry.py` (`TestSettings`, `TestSweep`), plus one dashboard test there

**Interfaces:**
- `get_settings` adds `"paymentExpiry": {"enabled": bool, "minutes": int}`.
- `update_settings` accepts `payload["paymentExpiry"]` as a dict with optional `enabled: bool` and `minutes`. It validates minutes via `as_int(..., "เวลาชำระเงิน (นาที)")` against `order_states.MIN_EXPIRY_MINUTES` / `MAX_EXPIRY_MINUTES` → `StoreError(400, "เวลาชำระเงินต้องอยู่ระหว่าง 10–1440 นาที")`.
- `expiry.py` produces:
  - `payment_deadline(row: sqlite3.Row, minutes: int) -> datetime`: `fromisoformat(row["payment_clock_started_at"] or row["created_at"]) + timedelta(minutes=minutes)`
  - `expire_unpaid_orders(conn, now: datetime | None = None) -> list[str]`:
    - returns `[]` when disabled;
    - otherwise runs `conn.commit()` and then `BEGIN IMMEDIATE`;
    - selects `pending_payment` rows, filters them in Python by deadline `<= now`, and calls `order_states.change_status(conn, row, "expired", now_iso())` for each. Catch `StoreError` per row and skip it, because another writer got there first;
    - commits and returns the codes.
  - `overdue_count(conn, minutes: int, now: datetime | None = None) -> int`: how many `pending_payment` orders would expire right now
- Dashboard: `orderCount`, `byDay.orders` and `byVariant` exclude every status in `NON_SALE_STATUSES`.

- [ ] **Step 1: Write the failing tests**

```python
class TestSettings:
    def test_defaults(self): assert db.get_settings(conn)["paymentExpiry"] == {"enabled": False, "minutes": 60}
    def test_update_and_bounds(self):  # 30 ok; 5 and 2000 → StoreError 400

class TestSweep:
    def test_disabled_does_nothing(self):
    def test_expires_only_overdue_pending_and_returns_stock(self):
        # orders: overdue pending (clock 2h ago), fresh pending, overdue waiting_confirm
        # → only the first becomes expired; its stock returns; others untouched
    def test_uses_clock_column_over_created_at(self):  # created 2h ago, clock 10 min ago → not expired
    def test_sweep_skips_order_changed_after_read(self, monkeypatch):
        # patch order_states.change_status to flip the row to waiting_confirm via SQL before the real call;
        # sweep returns [] for it, stock not returned
    def test_overdue_count(self):

def test_dashboard_excludes_non_sale_statuses():  # expired/refund_pending/refunded not in orderCount or byVariant
```
Set ages by writing `payment_clock_started_at` directly with SQL. Pass `now=` explicitly.

- [ ] **Step 2:** Run `$ENV python3 -m pytest server/tests/test_store_expiry.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `$ENV python3 -m pytest server/tests/ -v`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(store): payment expiry settings and sweep`

---

### Task 3: Slips on any open order, staff upload, deadline in API, sweep thread

**Files:**
- Modify:
  - `server/store/expiry.py`: `receive_slip`, `deadline_fields`, `run_expiry_loop`
  - `server/store/db.py`: `attach_slip` also accepts `refund_pending`, which keeps its status
  - `server/store/api.py`:
    - customer slip route calls `expiry.receive_slip`
    - new `POST /v2/admin/orders/<code>/slip`
    - customer GET, admin GET and lookup add `deadline_fields`
    - new `GET /v2/admin/payment-expiry/preview?minutes=N`
    - settings PUT audits expiry changes
  - `server/order_api.py`: start the thread next to `run_scheduled_backup` (primary only)
- Test: `server/tests/test_store_expiry.py` (`TestLateSlip`, HTTP tests using the `server` fixture from `test_store_v2`)

**Interfaces:**
- `receive_slip(conn, order_code: str, slip: dict[str, Any]) -> tuple[dict[str, Any], str]` returns the order and an audit action:
  - `pending_payment` / `waiting_confirm` / `refund_pending`: calls `db.attach_slip` → `"v2_slip_uploaded"`
  - `expired`, inside `BEGIN IMMEDIATE`:
    - writes the slip columns;
    - `change_status(... "waiting_confirm")` → `"v2_order_revived"`;
    - if that raises the stock 409 (its savepoint has already undone it), sets the status to `refund_pending` with a conditional UPDATE and no stock change → `"v2_order_refund_needed"`; then commits.
  - any other status: `StoreError(409, "ออเดอร์นี้อัปโหลดสลิปไม่ได้แล้ว")`, which is the existing message
- `deadline_fields(conn, row: sqlite3.Row, now: datetime | None = None) -> dict[str, str | None]` returns `{"paymentDeadline": iso-with-offset | None, "serverNow": now_iso()}`. The deadline is set only when expiry is enabled and the status is `pending_payment`.
- `run_expiry_loop() -> None`:
  - loops forever;
  - in each `open_db()`, calls `expire_unpaid_orders` and `log_audit(conn, code, "v2_order_expired", "auto")` for each code;
  - on exception, prints and continues;
  - sleeps `EXPIRY_CHECK_SECONDS = 60`.
- Staff route: same size check and `read_multipart_slip` as the customer route. Calls `receive_slip`, then logs `"v2_slip_uploaded_by_staff"` with `f"{slip['storedName']} by {admin_user(handler)}"` plus the revived/refund action when one happened. Deletes the old slip file and triggers OCR exactly like the customer route. Extract a shared helper `_store_slip(handler, code) -> dict` in `api.py` so both routes share one code path.
- Preview route returns `{"overdueCount": int}`. `minutes` is validated with the same bounds.
- Settings PUT logs `v2_payment_expiry_changed` with `f"enabled={…} minutes={…} by {user}"` when the value changed.

- [ ] **Step 1: Write the failing tests**

```python
class TestLateSlip:
    def test_receive_slip_on_expired_order_revives(self):  # stock available → waiting_confirm, stock deducted, action v2_order_revived
    def test_partial_stock_goes_to_refund_and_keeps_stock(self):  # 2 lines, one sold out → refund_pending, both stocks unchanged
    def test_two_late_slips_race_for_last_unit(self, tmp_path):  # file DB, 2 threads → exactly one waiting_confirm, one refund_pending
    def test_refund_pending_accepts_replacement_slip(self):  # status stays refund_pending
    def test_cancelled_rejects_slip(self):

def test_deadline_fields(self):  # disabled → None; enabled + pending → clock+minutes; waiting_confirm → None

def test_http_expiry_flow(server):
    # enable expiry via PUT /v2/admin/settings (Authorization: Bearer test-token);
    # create order, GET /v2/orders/<code>?token=… has paymentDeadline + serverNow;
    # force clock 2h back via sqlite on the tmp DB, call expiry.expire_unpaid_orders on it;
    # customer slip POST → 200 status waiting_confirm;
    # staff POST /v2/admin/orders/<code>/slip on a second expired order → 200;
    # GET /v2/admin/payment-expiry/preview?minutes=60 → {"overdueCount": n}; minutes=5 → 400
```
Copy the multipart body construction from the existing slip test in `test_store_v2.py`.

- [ ] **Step 2:** Run `$ENV python3 -m pytest server/tests/test_store_expiry.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement. In `order_api.py`:
  `threading.Thread(target=expiry.run_expiry_loop, daemon=True, name="payment-expiry").start()`
  inside the existing `if is_primary:`.
- [ ] **Step 4:** Run `$ENV python3 -m pytest server/tests/ -v && ruff check server tests`. Expected: PASS, including `test_fp28_routes_snapshot` and `test_module_layout`.
- [ ] **Step 5:** Commit: `feat(store): late slips revive or queue refund, staff slip upload, expiry thread`

---

### Task 4: Shared frontend types, labels and copy

**Files:**
- Modify:
  - `src/types/store.ts`:
    - `OrderStatus` gets the 3 statuses
    - `StoreOrder` gets `paymentDeadline?: string | null; serverNow?: string`
    - `StoreSettings` gets `paymentExpiry: { enabled: boolean; minutes: number }`
  - `src/components/admin/ui.tsx`: `ORDER_STATUS_STYLE` (expired `bg-zinc-200 text-zinc-700`, refund_pending `bg-orange-100 text-orange-800`, refunded `bg-zinc-100 text-zinc-500`) and `ORDER_STATUS_LABEL`
  - `src/lib/storeI18n.ts` (EN + TH):
    - `status.expired` / `status.refund_pending` / `status.refunded` with a label and a description
    - `payBy: (time: string) => string`
    - `timeLeft: (clock: string) => string`
    - `expiredNotice: string` (text before the link)
    - `refundPendingNotice: string`
    - `contactInstagram: string` (link text `@su.mfu`)
  - `src/components/Footer.tsx`: use the constant below
- Create: `src/lib/contact.ts`, exporting `INSTAGRAM_URL = "https://www.instagram.com/su.mfu/"`
- Test: `src/__tests__/storeI18nStatuses.test.ts`

Copy (TH, verbatim from the spec, plus my wording for the rest):
- `expiredNotice`: "หมดเวลาชำระแล้ว ถ้าโอนแล้วยังอัปสลิปได้ ทีมงานจะตรวจสอบให้ ผ่านทาง"
- EN: "Payment time is up. If you've already transferred, you can still upload your slip and our team will check it via"
- `refundPendingNotice`: TH "สินค้าหมดระหว่างรอชำระ ทีมงานจะติดต่อคืนเงินผ่านทาง" / EN "This item sold out before your payment arrived. Our team will contact you about a refund via"
- `payBy`: TH `ชำระภายใน ${time} น.` / EN `Pay by ${time}`
- `timeLeft`: TH `เหลือ ${clock}` / EN `${clock} left`

- [ ] **Step 1: Write the failing test.** For both languages, every `OrderStatus` key has a non-empty `label` and `description`, and `expiredNotice` is non-empty. Read the language tables the same way `languageProvider.test.tsx` does.
- [ ] **Step 2:** Run `npx vitest run src/__tests__/storeI18nStatuses.test.ts`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `npx tsc --noEmit && npx vitest run src/__tests__/storeI18nStatuses.test.ts`. Expected: PASS. Fix any `Record<OrderStatus, …>` that tsc reports as incomplete. That error is the checklist.
- [ ] **Step 5:** Commit: `feat(store): types, labels and copy for expiry statuses`

---

### Task 5: Customer countdown on `/order/[code]`

**Files:**
- Create: `src/components/store/PaymentCountdown.tsx`
- Modify: `src/components/store/OrderView.tsx`
- Test: `src/__tests__/paymentCountdown.test.tsx`

**Interfaces:**
- `PaymentCountdown({ deadline, serverNow, onExpire }: { deadline: string; serverNow: string; onExpire: () => void })`:
  - the offset is `Date.parse(serverNow) - Date.now()`, computed once on mount;
  - it ticks every second;
  - it renders `t.payBy(HH:MM in Asia/Bangkok)` and `t.timeLeft(MM:SS, or H:MM:SS when ≥ 1 h)`;
  - it calls `onExpire` once when it reaches 0.
- `OrderView` changes:
  - `canUpload` becomes `pending_payment | waiting_confirm | expired | refund_pending`
  - the `FLOW` bar is hidden for `cancelled | expired | refund_pending | refunded`
  - the header pill turns red for those four
  - the countdown sits above the upload button when `order.paymentDeadline` is set
  - an expired notice block appears when the status is `expired` or after `onExpire` fires; it shows `t.expiredNotice` plus an `<a href={INSTAGRAM_URL} target="_blank" rel="noreferrer">{t.contactInstagram}</a>`
  - `refund_pending` shows `t.refundPendingNotice` with the same link
  - `onExpire` re-calls `load(token)` so the server status shows up after the next sweep

- [ ] **Step 1: Write the failing tests** using fake timers (`vi.useFakeTimers()`) and the render pattern from `designPicker.test.tsx`:

```tsx
it("shows the time left from the server clock")      // serverNow = deadline − 90 s → "01:30"
it("uses server time offset")                          // system time 3 h ahead of serverNow → still "01:30"
it("calls onExpire once at zero")                      // advance 91 s → called 1 time
```
- [ ] **Step 2:** Run `npx vitest run src/__tests__/paymentCountdown.test.tsx`. Expected: FAIL.
- [ ] **Step 3:** Implement the component and the `OrderView` changes.
- [ ] **Step 4:** Run `npx vitest run src/__tests__/paymentCountdown.test.tsx && npx tsc --noEmit`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(store): payment countdown and expired notice on order page`

---

### Task 6: Admin order page and settings card

**Files:**
- Create:
  - `src/lib/orderSteps.ts`: `NEXT_STEPS` and `canChangeFreely`, moved out of the page so they can be tested
  - `src/components/admin/order/SlipCard.tsx`: the existing slip card plus a staff upload button
  - `src/components/admin/settings/PaymentExpiryCard.tsx`
- Modify:
  - `src/app/admin/(panel)/orders/[code]/page.tsx`
  - `src/app/admin/(panel)/settings/page.tsx`
  - the admin GET route already returns `paymentDeadline` (Task 3)
- Test: `src/__tests__/orderSteps.test.ts`, `src/__tests__/paymentExpiryCard.test.tsx`

**Interfaces:**
- `NEXT_STEPS: Partial<Record<OrderStatus, { to: OrderStatus; label: string; tone?: "danger" | "secondary" }[]>>` keeps today's entries and adds:
  - `expired`: `{to:"paid", label:"ยืนยันว่าได้รับเงินแล้ว (ตรวจทาง IG)"}`, `{to:"cancelled", label:"ยกเลิกออเดอร์", tone:"danger"}`
  - `refund_pending`:
    - `{to:"refunded", label:"โอนเงินคืนแล้ว"}`
    - `{to:"waiting_confirm", label:"ลองจองสินค้าอีกครั้ง", tone:"secondary"}`
    - `{to:"cancelled", label:"สลิปไม่ถูกต้อง/ไม่ได้โอนจริง ยกเลิก", tone:"danger"}`
- `canChangeFreely(status: OrderStatus): boolean` is false for `cancelled | expired | refund_pending | refunded`. The free menu's target list excludes `expired | refund_pending | refunded`.
- Order page:
  - shows "หมดเวลาชำระ {formatThaiDateTime(paymentDeadline)}" under the header when the deadline is set;
  - the terminal text for `refunded` is "คืนเงินแล้ว";
  - `cancelled` from `expired`/`refund_pending` confirms with the message "ออเดอร์นี้คืนสต็อกไปแล้ว ยกเลิกแล้วเปลี่ยนกลับไม่ได้".
- `SlipCard({ order, onUploaded }: { order: StoreOrder; onUploaded: (o: StoreOrder) => void })`:
  - the upload button "อัปโหลดสลิปแทนลูกค้า" shows for `pending_payment | waiting_confirm | expired | refund_pending`;
  - it POSTs `FormData` field `slip` through `adminFetch("orders/<code>/slip", { method: "POST", body: form })`;
  - it toasts the new status label.
- `PaymentExpiryCard({ value, onSave }: { value: StoreSettings["paymentExpiry"]; onSave: (v: StoreSettings["paymentExpiry"]) => Promise<unknown> })`:
  - contains a switch and a minutes input (10–1440);
  - help text: "ระบบปิดเฉพาะออเดอร์ที่ยังไม่อัปสลิป สต็อกคืนทันที และลูกค้ายังอัปสลิปตามมาได้";
  - when saving a change that turns it on (or lowers the minutes while on), it first calls `adminFetch("payment-expiry/preview?minutes=N")`;
  - if `overdueCount > 0`, it asks `useConfirm()` with "มีออเดอร์รอโอน N รายการที่เกินเวลาแล้ว จะถูกปิดทันที".

- [ ] **Step 1: Write the failing tests**

```ts
// orderSteps.test.ts
expect(NEXT_STEPS.expired?.map((s) => s.to)).toEqual(["paid", "cancelled"]);
expect(NEXT_STEPS.refund_pending?.map((s) => s.to)).toEqual(["refunded", "waiting_confirm", "cancelled"]);
expect(["cancelled", "expired", "refund_pending", "refunded"].some(canChangeFreely)).toBe(false);
expect(canChangeFreely("paid")).toBe(true);
```
```tsx
// paymentExpiryCard.test.tsx — mock adminFetch + useConfirm
it("asks before enabling when orders are overdue")        // preview → 3; confirm called with "3"; onSave after confirm
it("saves without asking when nothing is overdue")
it("does not save when the confirm is declined")
```
- [ ] **Step 2:** Run `npx vitest run src/__tests__/orderSteps.test.ts src/__tests__/paymentExpiryCard.test.tsx`. Expected: FAIL.
- [ ] **Step 3:** Implement. The order page imports `NEXT_STEPS` / `canChangeFreely`. Delete the local copy.
- [ ] **Step 4:** Run `npm test && npx tsc --noEmit && npm run lint`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(admin): expiry-aware order actions, staff slip upload, expiry settings card`

---

### Task 7: Docs and full verification

**Files:**
- Modify: `CLAUDE.md`, store v2 section. Add one bullet on payment expiry:
  - the statuses
  - `order_states.py` / `expiry.py`
  - the settings keys
  - the sweep thread
  - "deploy api ก่อน store, backup DB ก่อนครั้งแรก"

- [ ] **Step 1:** Run the full suite: `ruff check server tests && npm run lint && npx tsc --noEmit && npm test && $ENV python3 -m pytest server/tests/ -v && $ENV python3 -m pytest tests/ -v`. Expected: all PASS.
- [ ] **Step 2:** Manual check with `./dev.sh`:
  1. Enable expiry at 10 min and create an order.
  2. Set `payment_clock_started_at` 11 min back in `data/dev/` via sqlite3.
  3. Wait ≤ 60 s: the order becomes `expired` and the stock is back.
  4. Upload a slip: the order goes to `waiting_confirm`.
  5. Repeat with stock 0: the order goes to `refund_pending`.
- [ ] **Step 3:** Commit: `docs: payment expiry in project guide`
