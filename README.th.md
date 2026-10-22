# SU STORE Order API

[English](README.md) · **ภาษาไทย**

## repo นี้คืออะไร

backend ของร้าน SU STORE ([sumfu.store](https://sumfu.store)) เป็น Python HTTP server ที่ใช้ `http.server` ของ standard library กับ SQLite ให้บริการ API ร้าน `/v2/*` และ public routes ได้แก่ `/health`, `/site-status` และ `/product-images/` หน้าร้าน Next.js อยู่ใน repo [SU-STORE-REMASTER](https://github.com/Weerapong-gui/SU-STORE-REMASTER) และเรียก API นี้ผ่าน proxy ของตัวเอง

## รันในเครื่อง

ต้องมี Docker แล้วรันจาก root ของ repo:

```sh
./dev.sh
```

API จะเปิดที่ `http://127.0.0.1:10000` กับฐานข้อมูลตัวอย่างใน `data/dev/` (ไม่ใช่ข้อมูลจริง) มีสินค้า หมวด หน้าแรก และออเดอร์ตัวอย่างให้

- login หลังร้าน: `staff` / `dev`
- API token: `dev-token`
- `./dev.sh reset` ล้างฐานข้อมูลตัวอย่างแล้วเริ่มใหม่

## ตรวจสอบ

ใช้ Python 3.11 ขึ้นไป สร้าง virtual environment ติดตั้ง `pytest`, `ruff==0.16.10` และ `pyyaml==6.0.2` แล้วรัน:

```sh
.venv/bin/ruff check .
ORDER_API_DB_PATH=/tmp/su-order-api/orders.db ORDER_API_SLIPS_DIR=/tmp/su-order-api/slips .venv/bin/python -m pytest tests/ -v
```

## โครงสร้าง

```text
order_api.py   จุดเริ่มต้น เปิด HTTP server
core/          config, ฐานข้อมูล, HTTP handler, public routes, สลิป
store/         API ร้าน /v2/*: สินค้า ออเดอร์ ใบเสร็จ staff หน้าแรก
contract/      สัญญาร่วมกับหน้าร้าน (ค่าคงที่, template ใบเสร็จ, OpenAPI)
ops/           สคริปต์ production: deploy, backup, ย้ายข้อมูล
tests/         pytest
```

## Contract

`contract/` เป็นแหล่งความจริงของสัญญาระหว่าง backend กับหน้าร้าน หน้าร้านดึงจาก branch `main` ของ repo นี้ไปเก็บสำเนาไว้ใน repo ของตัวเอง และเทสใน repo นี้ตรวจว่าโค้ด Python ใช้ค่าตรงกับ `contract/`

กฎความเข้ากันได้:

> backend ต้องรองรับหน้าร้านที่ deploy อยู่ปัจจุบันเสมอ เพิ่มของใหม่ได้ แต่จะลบหรือเปลี่ยนความหมายของ field/endpoint ได้ก็ต่อเมื่อหน้าร้านเลิกใช้แล้ว งานที่ต้องแก้สองฝั่งให้ merge backend ก่อน

## Deploy

merge เข้า `main` = deploy ขึ้น production โดย `.github/workflows/deploy.yml` รันเทส build image multi-arch ขึ้น GHCR (tag เป็น commit SHA) แล้ว `ops/deploy-service.sh` บนเครื่อง production ดึง image มารีสตาร์ตเฉพาะ API ถ้า container ใหม่ไม่ผ่าน health check ระบบจะถอยกลับเป็นเวอร์ชันเดิมเอง

ถอยกลับเวอร์ชันเก่า: `gh workflow run deploy.yml -f sha=<commit sha>`

รายละเอียดเครื่อง secret และ path ข้อมูลจริงไม่เก็บใน repo public นี้

## Backup

ทุกวันเวลา 04:00 (เวลาไทย) systemd timer `su-store-backup.timer` บนเครื่อง production จะรัน `ops/backup.sh` ซึ่งทำ `sqlite3 .backup()` ของฐานข้อมูลใน container `su-store-api` ไว้ที่ `data/order-api/backups/` แล้วตรวจด้วย `PRAGMA integrity_check` ถ้าผ่านจะเขียน `LAST_OK` และเก็บ backup ไว้ 14 วัน อีกเครื่องหนึ่งจะดึง backup สลิป และรูปสินค้าไปเก็บทุกวัน โดยไม่ลบไฟล์ใดๆ ฝั่งปลายทาง ถ้า backup ล้มเหลวให้ดูที่ `journalctl -u su-store-backup`
