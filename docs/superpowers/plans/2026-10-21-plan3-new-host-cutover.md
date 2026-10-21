# Plan 3: ตั้งเครื่องใหม่ ซ้อม และสลับ `sumfu.store` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ร้านใหม่รันบนเครื่องใหม่พร้อมข้อมูลจริง, `sumfu.store` ชี้มาที่นี่, merge แล้ว deploy เอง, มี backup รายวันส่งไปเครื่องเก่า และ FP28 ยังใช้ได้ที่ `fp28.sumfu.store` / `admin.sumfu.xyz`

**Architecture:**
- โค้ดใหม่มี 3 ชิ้น:
  - `ops/backup.sh` + systemd timer
  - สคริปต์ย้ายข้อมูล `ops/migrate/`
  - commit เดียวใน repo `SU-STORE` เดิมที่บล็อก `/v2` ด้วย 410
- งานบนเครื่องทำผ่าน SSH ตาม checklist ส่วนงานบน Cloudflare dashboard เจ้าของงานกดเอง
- ข้อมูลไหลผ่าน Tailscale ด้วย key ที่ถูกจำกัดด้วย `rrsync` ทั้งสองทิศ:
  - เครื่องเก่า → เครื่องใหม่: ย้ายข้อมูล ใช้ key ที่เขียนได้อย่างเดียว
  - เครื่องเก่า ← เครื่องใหม่: เครื่องเก่าดึง backup ใช้ key ที่อ่านได้อย่างเดียว

**Tech Stack:** bash, Python (ใน image ของ order-api), systemd timer, cron, rsync/rrsync, cloudflared, GitHub Actions self-hosted runner

**Spec:** `SU-STORE-clean/docs/superpowers/specs/2026-10-20-repo-split-and-server-move-design.md` ข้อ 4.4–4.6, 4.9 · plan 2 เสร็จแล้ว (image ทั้งสองตัวอยู่บน GHCR และ job deploy ยังปิดอยู่)

**ข้อมูลเครื่อง (ตรวจแบบอ่านอย่างเดียว เมื่อ 2026-10-21):**
- **เครื่องใหม่:**
  - Manjaro, Docker 29.7.2, Compose 5.5.1, ดิสก์เหลือ 29G (82%), timezone Asia/Bangkok
  - มี `flock`, `sqlite3`, `rsync`, `rrsync` (`/usr/bin/rrsync`), `crontab` (cronie เปิดอยู่) แต่ไม่มี `jq`
  - network ที่มีอยู่: `fullstack-network`, `su-loadtest-net`
  - ยังไม่มี `su-store-*`, ไม่มี user runner, ไม่มี `/srv/su-store`
- **เครื่องเก่า:**
  - รัน `su-store` + `su-order-api` (healthy), ดิสก์เหลือ 19G
  - สลิปทั้งหมด 912M, รูปสินค้า 36M
  - Tailscale SSH เป็นโหมด **check** (ต้องยืนยันตัวตนทาง browser) **การเชื่อมต่ออัตโนมัติที่เข้าเครื่องเก่าจึงใช้ไม่ได้** ต้องให้เครื่องเก่าเป็นฝ่ายเชื่อมออกไปเท่านั้น
- IP, tunnel ID และ path จริงของเครื่องเก่าอยู่ใน skill `deploy` (gitignore) เอกสารนี้ใช้ `<new-host>`, `<old-host>`, `<old-data-dir>`

## สิ่งที่ผู้วางแผนเลือกเอง (เจ้าของงานตรวจได้)

- **เครื่องเก่าเป็นฝ่ายดึง backup** แทนที่เครื่องใหม่จะ push ไป (spec ข้อ 4.5 เขียนว่า "ส่ง") · เหตุผล: Tailscale SSH ของเครื่องเก่าเป็นโหมด check ทำให้เครื่องใหม่เข้าเครื่องเก่าอัตโนมัติไม่ได้
- **ใช้ `/srv/su-store` แทน `/home/park/su-store`** · เหตุผล: runner รันเป็น user `su-runner` ที่ต้องเข้าถึงโฟลเดอร์ deploy ได้ แต่เข้า `/home/park` ไม่ได้ (spec ข้อ 4.7)
- **backup ทำด้วย systemd timer บน host** ไม่ใช้ service ใน compose (spec ข้อ 4.4 เขียนว่า "งาน backup" อยู่ใน compose ของ backend) · เหตุผล: ใช้ `docker exec` เข้า container `su-store-api` ที่มี Python อยู่แล้ว ไม่ต้องเพิ่ม container ที่รันค้างไว้ · ไฟล์ unit อยู่ใน repo (`ops/systemd/`)
- **tunnel ใหม่ใช้แบบ token-managed** (ตั้ง hostname ใน dashboard) จึงไม่มีไฟล์ credentials บนเครื่อง มีแค่ `TUNNEL_TOKEN` ใน `.env`
- **`next.sumfu.store` ต้องมี Cloudflare Access กั้นไว้** (อนุญาตเฉพาะอีเมลทีม) เพราะ `STAGING_HOST=next.sumfu.store` ทำให้ร้านบน host นั้นเปิดอยู่เสมอ ต่อให้ `site_closed=1` ถ้าไม่กั้น คนนอกจะสั่งซื้อผ่าน host นี้ได้ระหว่างซ้อม และหลังเปิดร้านจริง
- **ตรวจหลังย้ายด้วยการนับแถวและเช็กสลิป** โดยรัน `ops/migrate/counts.py` ด้วย Python ของ image order-api (เครื่องทั้งสองไม่มี `jq` และไม่อยากลง Python เพิ่มบน host)

## Global Constraints

- **ห้ามแตะข้อมูลจริงบนเครื่องเก่าแบบเขียน** สิ่งที่ทำได้มีแค่:
  - `sqlite3 .backup()` ผ่าน `docker exec su-order-api python`
  - ตั้ง `site_closed` (ตอน cutover)
  - deploy commit บล็อก `/v2` ด้วย `./deploy.sh api`
- **ทุกขั้นที่ SSH เข้าเครื่อง ต้องให้เจ้าของงานยืนยันก่อนรัน** ขั้นที่ติด [เจ้าของงาน] ต้องกดเองใน dashboard หรือ browser
- ห้าม `docker system prune` · ห้ามแตะ container, network หรือ volume ที่ไม่ได้ขึ้นต้นด้วย `su-store-` บนเครื่องใหม่
- ห้าม publish พอร์ตออก host · ทุก container มี `restart: unless-stopped`, logging `max-size 10m`/`max-file 3`
- `.env` อยู่บน host เท่านั้น (`chmod 600`, owner `su-runner`) ห้ามมีค่าลับใน repo, log หรือข้อความ commit · token ใหม่ทุกตัวสร้างด้วย `openssl rand -hex 32`
- DB เก็บ path สลิปเป็น path เต็มใน container (`/var/data/su-order-api/slips/...`) ฝั่งใหม่จึงต้อง mount `${SU_STORE_DATA_DIR}/order-api` → `/var/data` เหมือนเดิม
- ตอนซ้อม **ห้ามสร้างออเดอร์หรือออกใบเสร็จ** บนเครื่องใหม่
- สคริปต์ใน `ops/` ต้องรันได้บน bash 3.2 (macOS) และ Linux · ทุกตัวมีเทส pytest ที่ใช้ docker/ssh/rsync ปลอม

## Review Focus

1. **เลขใบเสร็จซ้ำข้ามเครื่อง:** ถ้าเครื่องเก่ายังรับ `/v2` ได้หลังสลับ เลข `SU-R2569-xxxx` จะออกซ้ำ จึงต้องบล็อก `/v2` ด้วย 410 ก่อนเปิดร้านใหม่ และ verify ต้องเทียบ `store_counters` ทุกแถว (Task 3, Task 8)
2. **สลิปที่ DB อ้างแต่ไฟล์ไม่มาด้วย:** verify ต้องเช็กทุก `slip_storage_path` ที่ไม่ใช่ NULL ว่ามีไฟล์จริงใน container ใหม่ ถ้าขาดแม้ไฟล์เดียวต้อง exit 1 (Task 2)
3. **backup เงียบหาย:** ถ้า timer ไม่รัน หรือ `LAST_OK` เก่าเกิน 26 ชม. การดึงของเครื่องเก่าต้อง exit ไม่เป็น 0 แล้วเขียนลง log ที่ดูได้ ห้ามสำเร็จแบบไม่มีไฟล์ใหม่ (Task 1, Task 6)
4. **import ทับ DB ตอน API ยังเขียนอยู่:** `import-new.sh` ต้องปฏิเสธถ้า `su-store-api` ยังรันอยู่ และย้าย DB เดิมไปไว้ข้างๆ ก่อนวางไฟล์ใหม่เสมอ (Task 2)
5. **`next.sumfu.store` เปิดให้คนนอก:** ก่อนเปิดใช้ hostname นี้ต้องตั้ง Access ให้เสร็จ และตรวจจาก browser ที่ไม่ได้ login ว่าเจอหน้า Access (Task 5)

---

### Task 1: backup รายวันบนเครื่องใหม่

**Files:** Create `ops/backup.sh`, `ops/systemd/su-store-backup.service`, `ops/systemd/su-store-backup.timer`, `tests/test_backup.py` · Modify `README.md` (หัวข้อ Backup ภาพรวม)

**Interfaces:**
- Produces: `ops/backup.sh`
  - env `SU_STORE_DIR` (บังคับ) และ `BACKUP_KEEP_DAYS` (default `14`)
  - exit: `0` สำเร็จ · `1` backup หรือ integrity ล้มเหลว · `64` ใช้ผิดวิธี
  - ไฟล์ backup: `${SU_STORE_DIR}/data/order-api/backups/orders-<YYYYmmdd-HHMM>.db` (ใน container คือ `/var/data/backups/`)
  - สำเร็จแล้วเขียน `backups/LAST_OK` เป็นเวลา ISO แบบมี offset
- คำสั่งที่สคริปต์ใช้: `docker exec su-store-api python -c '<sqlite3 backup + PRAGMA integrity_check>' <src> <dst>` ถ้าผลไม่ใช่ `ok` ให้ลบไฟล์ dst แล้ว exit 1
- ลบเฉพาะไฟล์ที่ตรงชื่อ `orders-*.db` ในโฟลเดอร์ `backups/` ที่เก่ากว่า `BACKUP_KEEP_DAYS` วัน

- [ ] **Step 1: เขียนเทส** `tests/test_backup.py` ใช้ `docker` ปลอมที่รันคำสั่ง `python -c` ต่อด้วย Python ในเครื่อง พร้อมแปลง `/var/data` → `tmp_path/data/order-api` และสร้าง DB จริงใน tmp:
  - `test_backup_creates_consistent_copy_and_last_ok`
  - `test_corrupt_copy_is_removed_and_fails` (docker ปลอมตอบ integrity เป็น `not ok`)
  - `test_retention_deletes_only_old_backup_files` (ไฟล์อื่นในโฟลเดอร์ เช่น `LAST_OK`, `note.txt` ต้องอยู่ครบ)
  - `test_missing_su_store_dir_exits_64`
- [ ] **Step 2:** รันแล้วต้อง FAIL
- [ ] **Step 3:** เขียน `backup.sh` และ systemd units
  - service เป็น `Type=oneshot`, `User=su-runner`, `Environment=SU_STORE_DIR=/srv/su-store`, `ExecStart=/srv/su-store/backend/ops/backup.sh`
  - timer: `OnCalendar=*-*-* 04:00:00 Asia/Bangkok`, `Persistent=true` (เครื่องดับตอน 04:00 แล้วเปิดขึ้นมาจะรันตามให้)
- [ ] **Step 4:** รันเทสให้ผ่าน แล้วรันชุดตรวจ backend
- [ ] **Step 5:** commit `feat(ops): daily SQLite backup with retention`

### Task 2: สคริปต์ย้ายข้อมูล

**Files:** Create `ops/migrate/counts.py`, `ops/migrate/export-old.sh`, `ops/migrate/import-new.sh`, `ops/migrate/verify.sh`, `tests/test_migrate.py`

**Interfaces:**
- `counts.py <db_path> [--slips-root <dir>]` พิมพ์ JSON (keys เรียงตามตัวอักษร) แล้ว exit 0:
  - `tables`: จำนวนแถวของ `store_orders`, `store_order_items`, `store_receipts`, `admin_users`, `store_products`, `store_variants`, `store_categories`
  - `counters`: ทุกแถวของ `store_counters` เป็น dict `period` → `last_seq` (รวมแถว `receipt`)
  - `slips`: จำนวน `slip_storage_path` ที่ไม่ใช่ NULL
  - ใส่ `missing_slips` (list ของ path ที่ไม่มีไฟล์) เมื่อส่ง `--slips-root` มา
  - ใช้แค่ stdlib และเปิด DB แบบอ่านอย่างเดียว (`file:...?mode=ro`)
- `export-old.sh <snapshot_dir>` รันบน **เครื่องเก่า**
  - `docker exec su-order-api python` ทำ `.backup()` ของ `/var/data/su-order-api/orders.db` → `<snapshot_dir>/orders.db`
  - รัน `counts.py` กับ snapshot → `<snapshot_dir>/counts.json`
  - ทำรายการสลิป SU จาก snapshot (`SELECT slip_storage_path FROM store_orders WHERE slip_storage_path IS NOT NULL`) โดยตัด prefix `/var/data/su-order-api/` → `<snapshot_dir>/slips.txt`
  - rsync ไปเครื่องใหม่ด้วย key `~/.ssh/su-store-migrate` ทั้ง `orders.db`, `counts.json` และไฟล์ตาม `slips.txt` (`--files-from`, `--ignore-existing` สำหรับสลิป) รวม `product-images/` ทั้งโฟลเดอร์
  - **ไม่มี `--delete` ทุกคำสั่ง**
  - ปลายทางคือ `su-runner@<new-host>:incoming/` ซึ่ง rrsync จำกัดไว้ที่ `/srv/su-store/data/incoming`
  - ค่า host อ่านจาก env `NEW_HOST` (บังคับ)
- `import-new.sh` รันบน **เครื่องใหม่**
  - ปฏิเสธ (exit 1) ถ้า `docker inspect su-store-api` บอกว่ายังรันอยู่
  - ถ้ามี `orders.db` เดิมอยู่ ให้ย้ายไปเป็น `orders.db.replaced-<YYYYmmdd-HHMM>` รวม `-wal` และ `-shm` ด้วย
  - ย้าย `incoming/orders.db` ไป `data/order-api/su-order-api/orders.db` แล้ว rsync `incoming/slips/` กับ `incoming/product-images/` เข้าที่ (ไม่ลบไฟล์เดิม)
  - งานที่ต้องใช้ root (ไฟล์ของ container เป็นของ root) ให้ทำผ่าน `docker run --rm --entrypoint sh -v ${SU_STORE_DATA_DIR}:/data ghcr.io/weerapong-gui/su-store-server:main -c '...'`
- `verify.sh` รันบน **เครื่องใหม่** หลัง start API
  - เรียก `docker exec su-store-api python - /var/data/su-order-api/orders.db --slips-root / < counts.py`
  - เทียบผลกับ `incoming/counts.json` ทุก key ยกเว้น `missing_slips`
  - พิมพ์ตารางเก่ากับใหม่
  - exit 0 เมื่อตรงกันทุกค่าและ `missing_slips` ว่าง · exit 1 เมื่อไม่ตรงหรือมีสลิปขาด และบอกว่า key ไหนต่าง

- [ ] **Step 1: เขียนเทส** `tests/test_migrate.py`
  - `test_counts_reports_tables_counters_and_slips` (DB ที่สร้างผ่าน `ensure_db` + `create_order` + `attach_slip`)
  - `test_counts_lists_missing_slip_files`
  - `test_counts_opens_read_only` (ไฟล์ DB ต้องไม่เปลี่ยน mtime)
  - `test_verify_fails_on_counter_mismatch` และ `test_verify_fails_on_missing_slip` (docker ปลอมรัน `counts.py` ในเครื่อง)
  - `test_import_refuses_while_api_running`
  - `test_import_keeps_previous_db_beside_new_one`
  - `test_export_never_uses_delete` (ssh/rsync ปลอมบันทึก argv)
- [ ] **Step 2:** รันแล้วต้อง FAIL
- [ ] **Step 3:** เขียนสคริปต์ทั้ง 4 ตัวตาม Interfaces
- [ ] **Step 4:** รันเทสให้ผ่าน แล้วรันชุดตรวจ backend
- [ ] **Step 5:** commit `feat(ops): migration export/import/verify scripts`

### Task 3: บล็อก `/v2` บนเครื่องเก่า (repo `SU-STORE` เดิม, เตรียมไว้ก่อน deploy คืนจริง)

**Files (repo `/Users/park/Projects/SU-STORE`):** Modify `server/fp28/web/handler.py:192` · Test `server/tests/test_v2_gone.py`

- [ ] **Step 1:** สร้าง branch `cutover/block-v2` จาก `main` ของ repo เดิม **ห้ามแตะงานที่ยังไม่ได้ commit** (`DesignPicker.tsx`, `storeI18n.ts`, `designPicker.test.tsx` ค้างอยู่) ให้ `git stash` ก่อน แล้ว `stash pop` กลับหลังทำเสร็จ
- [ ] **Step 2: เขียนเทส** `test_v2_paths_answer_410` ใช้ fixture HTTP เดิมของ repo นั้น:
  - `GET /v2/products`, `POST /v2/orders`, `GET /v2/admin/me` ต้องตอบ 410 และ `{"message": "ร้านย้ายไปที่ https://sumfu.store แล้ว"}`
  - `GET /health` และ route FP28 ตัวหนึ่ง (`/check-order?studentCode=...`) ยังตอบเหมือนเดิม
- [ ] **Step 3:** ใน `handler.py` ที่ `if path.startswith("/v2/"):` เปลี่ยนให้ส่ง 410 แทนการ dispatch ไป store แล้วรันเทสของ repo เดิมทั้งชุดให้ผ่าน
- [ ] **Step 4:** commit บน branch (`feat: answer 410 for /v2 after the store moved`) **ยังไม่ merge และไม่ deploy** จะทำใน Task 8

### Task 4 [ผู้ควบคุม + เจ้าของงานยืนยัน]: เตรียมเครื่องใหม่

- [ ] **Step 1:** สร้าง user และโฟลเดอร์
  - `sudo useradd --system --create-home --home-dir /var/lib/su-runner --shell /bin/bash su-runner && sudo usermod -aG docker su-runner`
  - `sudo install -d -o su-runner -g su-runner -m 750 /srv/su-store /srv/su-store/{backend,frontend,data} /srv/su-store/data/{order-api,next-image-cache} /srv/su-store/data/order-api/incoming`
  - `sudo usermod -aG su-runner park` เพื่อให้เจ้าของงานอ่านได้
- [ ] **Step 2:** `docker network create su-store-net` (ตรวจก่อนว่ายังไม่มี)
- [ ] **Step 3:** สร้าง `.env` (`chmod 600`, owner `su-runner`)
  - `backend/.env`: `ORDER_API_TOKEN` (สุ่มใหม่), `PICKUP_USERNAME=staff`, `PICKUP_PASSWORD=` (เว้นว่าง เพราะ DB จริงมีบัญชีแล้ว), `SU_STORE_DATA_DIR=/srv/su-store/data`, `TUNNEL_TOKEN=` (เติมใน Task 5)
  - `frontend/.env`: `BYPASS_TOKEN` (สุ่มใหม่), `ALWAYS_OPEN=0`, `STAGING_HOST=next.sumfu.store`, `SU_STORE_DATA_DIR=/srv/su-store/data`
  - เก็บค่าที่สุ่มไว้ใน skill `deploy` ของเจ้าของงานเท่านั้น
- [ ] **Step 4: key จำกัดสิทธิ์** ใน `/var/lib/su-runner/.ssh/authorized_keys` ที่สร้างบนเครื่องเก่าแล้วคัด public key มา:
  - `command="/usr/bin/rrsync -wo /srv/su-store/data/order-api/incoming",restrict <su-store-migrate.pub>`
  - `command="/usr/bin/rrsync -ro /srv/su-store/data/order-api",restrict <su-store-backup-pull.pub>`
  - ลองจากเครื่องเก่าว่า `rsync` ไปยัง incoming ได้, อ่าน `backups/` ได้, เขียนนอก incoming ไม่ได้ และ `ssh` เปิด shell ไม่ได้
- [ ] **Step 5: runner สองตัว** (หนึ่งตัวต่อ repo เพราะ repo อยู่ใต้ user ไม่ใช่ org)
  - ติดตั้ง actions-runner เวอร์ชันล่าสุดที่ออกมาแล้วอย่างน้อย 2 สัปดาห์ ลงใน `/var/lib/su-runner/runner-{web,api}`
  - `config.sh --unattended --url https://github.com/Weerapong-gui/<repo> --token <จาก gh api -X POST repos/Weerapong-gui/<repo>/actions/runners/registration-token> --labels su-store --name su-store-<web|api>`
  - ใส่ `SU_STORE_DIR=/srv/su-store` ในไฟล์ `.env` ของแต่ละ runner แล้ว `sudo ./svc.sh install su-runner && sudo ./svc.sh start`
  - ตรวจใน GitHub ว่า runner ทั้งสองขึ้นสถานะ Idle
- [ ] **Step 6:** ติดตั้ง systemd unit ของ Task 1 (คัดลอกจาก `ops/systemd/` ลง `/etc/systemd/system/`) แล้ว `systemctl enable --now su-store-backup.timer` และตรวจ `systemctl list-timers su-store-backup.timer`

### Task 5: tunnel ใหม่ + `next.sumfu.store`

**Files (backend):** Modify `compose.yml`

- [ ] **Step 1:** เพิ่ม service `su-store-cloudflared` ใน `compose.yml`
  - image `cloudflare/cloudflared:2026.9.3`, container `su-store-cloudflared`, `command: tunnel --no-autoupdate --metrics 127.0.0.1:2000 run`
  - env `TUNNEL_TOKEN: ${TUNNEL_TOKEN:?}`, network `su-store-net`, restart/logging ตาม Global Constraints
  - healthcheck: ถ้า image รองรับ `cloudflared tunnel ready` (ตรวจด้วย `--help` ของเวอร์ชันนี้) ให้ใช้ `["CMD", "cloudflared", "tunnel", "--metrics", "127.0.0.1:2000", "ready"]` ถ้าไม่รองรับให้คอมเมนต์ว่า image เป็น distroless ไม่มี healthcheck
  - เปิด PR แล้ว merge (deploy job deploy แค่ `su-store-api` ส่วน cloudflared เปิดเองใน Step 4)
- [ ] **Step 2 [เจ้าของงาน]:** ใน Cloudflare Zero Trust → Networks → Tunnels สร้าง tunnel `su-store-r` แบบ Cloudflared แล้วคัดลอก token มาใส่ `TUNNEL_TOKEN` ใน `backend/.env`
- [ ] **Step 3 [เจ้าของงาน]:** ใน tunnel `su-store-r` เพิ่ม public hostname `next.sumfu.store` → `http://su-store-web:3000` · ใน Zero Trust → Access → Applications สร้าง self-hosted app ครอบ `next.sumfu.store` และ policy Allow เฉพาะอีเมลทีม (One-time PIN)
- [ ] **Step 4:** `docker compose -f /srv/su-store/backend/compose.yml --env-file /srv/su-store/backend/.env up -d su-store-cloudflared` แล้วดูว่า tunnel ขึ้น Healthy ใน dashboard · เปิด `https://next.sumfu.store` จาก browser ที่ไม่ได้ login ต้องเจอหน้า Access

### Task 6: backup ไปเครื่องเก่า

**Files (เครื่องเก่า ไม่อยู่ใน repo):** `~/su-store-offsite/pull.sh`, crontab ของ `park`

- [ ] **Step 1:** สร้าง key บนเครื่องเก่า (`ssh-keygen -t ed25519 -N "" -f ~/.ssh/su-store-backup-pull` และ `~/.ssh/su-store-migrate`) แล้วส่ง public key ให้ Task 4 Step 4
- [ ] **Step 2:** เขียน `pull.sh` ไว้ใน skill `deploy` และบนเครื่องเก่า
  - `rsync -a` (ไม่มี `--delete`) ดึง `backups/`, `su-order-api/slips/`, `su-order-api/product-images/` ไปไว้ที่ `~/su-store-offsite/`
  - อ่าน `backups/LAST_OK` ที่ดึงมาแล้ว ถ้าเก่ากว่า 26 ชม. ให้ exit 1
  - เขียน log ต่อท้าย `~/su-store-offsite/pull.log` ทุกรอบ พร้อมเวลาและ exit code
- [ ] **Step 3:** crontab `30 5 * * * ~/su-store-offsite/pull.sh` (หลัง backup 04:00) แล้วรันมือหนึ่งรอบ ต้อง exit 0 และมีไฟล์ backup อยู่ใน `~/su-store-offsite/backups/`

### Task 7: deploy ครั้งแรก + ซ้อม (ร้านเปิดตามปกติที่เครื่องเก่า)

- [ ] **Step 1:** ตั้ง repo variable `DEPLOY_ENABLED=true` ทั้งสอง repo (`gh variable set DEPLOY_ENABLED --body true -R Weerapong-gui/<repo>`)
- [ ] **Step 2:** `gh workflow run deploy.yml -R Weerapong-gui/SU-STORE-SERVER -f sha=<sha ล่าสุดบน main>`
  - รอบแรกยังไม่มี `REVISION` ถ้า API ไม่ healthy สคริปต์จะ exit 2
  - DB ยังว่าง API สร้างตารางเองได้
  - ถัดไปรัน workflow เดียวกันของ `SU-STORE-REMASTER`
  - ทั้งสอง run ต้องเขียว และ `docker ps` ต้องเห็น `su-store-api`, `su-store-web`, `su-store-cloudflared` เป็น healthy
- [ ] **Step 3: ซ้อมย้ายข้อมูล**
  1. เครื่องเก่า: `OLD_DATA_DIR=<old-data-dir>/order-api NEW_HOST=su-runner@<new-host> ops/migrate/export-old.sh ~/su-store-snapshot` (ใช้ scp วาง `ops/` ทั้งโฟลเดอร์ลงเครื่องเก่าก่อน)
  2. เครื่องใหม่: `docker stop su-store-api` → `ops/migrate/import-new.sh` → start ด้วย `deploy-service.sh backend <sha เดิม>` → `ops/migrate/verify.sh` ต้อง exit 0
  3. บันทึกเวลาที่แต่ละขั้นใช้ไว้ใน Execution notes เพื่อวางแผนคืนจริง
- [ ] **Step 4: ตรวจผ่าน `next.sumfu.store`** (login Access ก่อน):
  - สินค้า หมวด และหน้าแรกแสดงครบ
  - ออเดอร์เก่าเปิดได้ และสลิปแสดง
  - login `/admin` ได้
  - หน้าใบเสร็จเดิมพิมพ์ซ้ำได้ (เปิดหน้าพิมพ์อย่างเดียว ห้ามกดออกใบใหม่)
  - `/admin/staff` แสดงรายชื่อ
  - **ห้ามสร้างออเดอร์หรือออกใบเสร็จ**
- [ ] **Step 5:** รอให้ backup 04:00 รอบแรกรันเสร็จ และ `pull.sh` ของเครื่องเก่าตอน 05:30 ได้ exit 0 ก่อนนัดคืนจริง
- [ ] **Step 6:** ทดสอบ rollback หนึ่งรอบ: `gh workflow run deploy.yml -f sha=<sha ก่อนหน้า>` ของหน้าร้าน แล้ว deploy sha ล่าสุดกลับ

### Task 8 [คืนจริง — เจ้าของงานอยู่ด้วยตลอด]: สลับ `sumfu.store`

ทำช่วงดึก ใช้เวลาไม่เกิน ~30 นาที ทุกขั้นทำตามลำดับ และหยุดทันทีเมื่อขั้นไหนไม่ผ่าน

- [ ] **Step 1:** เครื่องเก่า ตั้ง `site_closed=1` จากหลังร้านเดิม แล้วรอ 2 นาทีให้ไม่มีคำขอเขียนค้าง (ดู log ของ `su-order-api`)
- [ ] **Step 2:** เครื่องเก่า รัน `export-old.sh` (สลิปส่วนใหญ่ถูกส่งไปแล้วตอนซ้อม รอบนี้ส่งแค่ส่วนที่เพิ่ม)
- [ ] **Step 3:** เครื่องใหม่ `docker stop su-store-api` → `import-new.sh` → start → `verify.sh`
  - **ถ้า exit ไม่เป็น 0:** ตั้ง `site_closed=0` ที่เครื่องเก่า แล้วจบคืนนี้ (ยังไม่มีอะไรเปลี่ยนสำหรับลูกค้า)
- [ ] **Step 4:** ตรวจผ่าน `next.sumfu.store` ตาม checklist ของ Task 7 Step 4 อีกรอบ
- [ ] **Step 5 [เจ้าของงาน]:** ใน Cloudflare
  1. tunnel `su-store-r` เพิ่ม public hostname `sumfu.store` และ `www.sumfu.store` → `http://su-store-web:3000` (DNS record เดิมที่ชี้ tunnel เก่าต้องเปลี่ยนให้ชี้ tunnel ใหม่ ซึ่ง dashboard จะถามให้ override)
  2. tunnel เก่า `su-store` (connector `archlinux`) **ใช้ config จาก dashboard แล้ว** (ดู Execution notes) ให้เพิ่ม route `fp28.sumfu.store` → `http://127.0.0.1:3000` ใน dashboard ห้ามแก้ `config-store.yml` เพราะไฟล์นี้ไม่มีผลแล้ว
  3. Redirect Rule บน `fp28.sumfu.store` ตาม spec ข้อ 4.6
- [ ] **Step 6:** เครื่องเก่า: เพิ่ม `STORE_V2_GONE=1` ใน `.env` แล้ว `./deploy.sh api` (commit `cutover/block-v2` ต้อง merge และ deploy ไว้ก่อนวันจริง ตาม Execution notes) ตรวจด้วย `curl` ว่า `/v2/products` ตอบ 410 จากนั้นตั้ง `site_closed=0` ที่เครื่องเก่าเพื่อเปิด `fp28.sumfu.store`
- [ ] **Step 7:** เครื่องใหม่ เปิดร้านจากหลังร้านใหม่ (`site_closed=0`) แล้วสั่งซื้อจริงหนึ่งออเดอร์ผ่าน `https://sumfu.store` อัปสลิป จากนั้นยกเลิกในหลังร้าน · ตรวจว่า `fp28.sumfu.store/check-order/fp28` ค้นด้วยรหัสนักศึกษาได้ และ `admin.sumfu.xyz` ยังใช้ได้
- [ ] **Step 8 ถอยกลับ (ใช้ได้ถึงก่อน Step 7 เท่านั้น):**
  1. ใน Cloudflare ย้าย `sumfu.store`/`www` กลับไป tunnel เก่า
  2. เครื่องเก่า: ลบ `STORE_V2_GONE=1` ออกจาก `.env` แล้ว `./deploy.sh api`
  3. ตั้ง `site_closed=0` ที่เครื่องเก่า
  - หลังเปิดร้านใหม่แล้ว จะถอยกลับต้องย้ายออเดอร์ใหม่ย้อนไปด้วย ซึ่งอยู่นอกขอบเขต (spec ข้อ 4.9)

### Task 9: หลัง cutover

- [ ] **Step 1:** เช้าวันรุ่งขึ้นตรวจว่า backup 04:00 กับ `pull.sh` 05:30 สำเร็จ และ `docker ps` ทุกตัวยัง healthy
- [ ] **Step 2:** อัปเดต skill `deploy`
  - host ใหม่, `/srv/su-store`, วิธีดู log (`docker logs su-store-api`, `journalctl -u su-store-backup`)
  - วิธี rollback (`gh workflow run deploy.yml -f sha=...`)
  - คำสั่ง restart cloudflared
  - เพิ่มหมายเหตุว่าเครื่องเก่าเหลือแค่ FP28
- [ ] **Step 3:** อัปเดต `CLAUDE.md` ของทั้งสอง repo (ในเครื่อง) ให้ตรงกับสภาพใหม่ แล้วลบ snapshot ที่ใช้ตอนซ้อมบนเครื่องเก่า (`~/su-store-snapshot`) หลังผ่านไป 7 วัน
- [ ] **Step 4:** ย้าย spec/plan ของ "ยกเลิกออเดอร์ที่ไม่จ่ายอัตโนมัติ" ขึ้นเป็นงานถัดไป (อยู่ใน `docs/superpowers/` ของ repo นี้แล้ว)

## Execution notes

- **Task 1 (`82de8c0`):** งานทั้งหมดอยู่ใน `ops/backup.py` ที่รันใน `su-store-api` ผ่าน `docker exec -i ... python -` ส่วน `backup.sh` เป็นแค่ wrapper · เหตุผล: ไฟล์ใน `data/order-api` เป็นของ root ใน container ทำให้ `su-runner` บน host เขียน `LAST_OK` หรือลบไฟล์เก่าไม่ได้ · `backup.py --snapshot <target>` ใช้ร่วมกับ export
- **Task 2 (`c45e0d0`):**
  - `incoming/` อยู่ใต้ `data/order-api/` (ใน container คือ `/var/data/incoming`) แทน `data/incoming` เพื่อให้ `verify.sh` เทียบและเช็กสลิปด้วย Python ใน container ได้ (rrsync -wo ของ key migrate จึงชี้ไปที่ `data/order-api/incoming`)
  - import ใช้ `import_files.py` ใน image แทน `cp -n` เพราะ coreutils ของ Debian trixie เปลี่ยนพฤติกรรม `-n`
  - export ต้องตั้ง env `OLD_DATA_DIR`
- **Task 3 (`a2bdf23` บน branch `cutover/block-v2` ของ repo `SU-STORE`):** เปลี่ยนจากบล็อก `/v2` ตลอด เป็นสวิตช์ `STORE_V2_GONE=1` (ปิดไว้เป็นค่าเริ่มต้น และเพิ่มใน `docker-compose.yml` แล้ว)
  - **เหตุผล:** ถ้าบล็อกตลอด เทส HTTP `/v2` ของ repo เดิมจะพัง · ใช้สวิตช์แล้ว merge + deploy ไว้ก่อนวันจริงได้โดยไม่มีผลอะไร
  - **คืนจริง:** เพิ่มบรรทัดใน `.env` แล้ว `./deploy.sh api`
  - **ถอยกลับ:** ลบบรรทัดนั้นแล้ว deploy ใหม่ ไม่ต้อง revert
  - **ผลเทส:** repo เดิมผ่าน 266 + 36
- **Task 5:** tunnel เดิมของเครื่องเก่า (connector `archlinux`, route `www.sumfu.store`) **ชื่อ `su-store` อยู่แล้ว** ใน dashboard ชื่อจึงชนกับที่แผนตั้งไว้ tunnel ใหม่ของเครื่องใหม่จึงใช้ชื่อ **`su-store-v2`** · ห้ามแก้ route หรือ token ของ `su-store` จนกว่าจะถึง Task 8 Step 5
- **Task 7 Step 2 (2026-10-21):** deploy ครั้งแรกสำเร็จทั้งสองฝั่ง (backend `31dc25e`, หน้าร้าน `931118c`) `su-store-web` เรียก `su-store-api` ผ่าน `su-store-net` ได้ · merge PR #4 แล้ว deploy อัตโนมัติก็ทำงาน · พบว่า deploy job คัดลอกแค่ `deploy-service.sh` ไม่ได้คัดลอกทั้ง `ops/` จึงแก้ใน PR #5
- **2026-10-21 22:12–23:25 ร้านล่ม (`sumfu.store`, `www`) ระหว่างสร้าง tunnel ใหม่ใน dashboard:**
  - **ลำดับเหตุการณ์:**
    1. คำสั่งติดตั้ง cloudflared ที่ dashboard แสดงถูกรันบน Mac ทำให้ tunnel ใหม่ `su-store-r` มี connector อยู่บน Mac
    2. route ที่ใส่เป็น `www` และ apex แทน `next` เมื่อเพิ่ม route ระบบจะย้าย DNS ของชื่อนั้นไปชี้ที่ tunnel ใหม่ และเมื่อลบ route DNS record ก็ถูกลบตามไปด้วย
    3. มีการแก้ route ของ tunnel เดิม `su-store` ทำให้ tunnel นั้นเปลี่ยนไปใช้ config จาก dashboard และไม่อ่าน `~/.cloudflared/config-store.yml` อีก
  - **วิธีแก้:** เพิ่ม route `sumfu.store` และ `www.sumfu.store` → `http://127.0.0.1:3000` ใน dashboard ของ `su-store` ตรวจ config ที่ใช้อยู่จริงได้ด้วย `journalctl -u cloudflared-store | grep "Updated to new configuration"` บนเครื่องเก่า
  - **ผลต่อแผน:**
    - tunnel ของเครื่องใหม่คือ `su-store-r` (ID `3722edce-…`) ซึ่งต้องมีแค่ `next.sumfu.store` จนถึงคืนจริง
    - ต้องลบ connector บน Mac ออกก่อนเพิ่ม route ใดๆ
    - ทุกขั้นใน dashboard ต้องเขียนชื่อ tunnel พร้อม hostname ของ connector และระบุว่าห้ามรันคำสั่งติดตั้งที่ dashboard แสดง

