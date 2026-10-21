# SU STORE Order API

## repo นี้คืออะไร

Backend API สำหรับร้าน SU STORE เป็น Python HTTP server ที่ใช้ `http.server` และ SQLite ให้บริการ API ร้าน `/v2/*` และ public routes เช่น `/health`, `/site-status` และ `/product-images/` หน้าร้าน Next.js อยู่ใน repo `SU-STORE-clean` และเรียก API นี้ผ่าน proxy ของตัวเอง

## รัน dev

เตรียม Docker และรันจาก root ของ repo:

```sh
./dev.sh
```

สคริปต์นี้เปิด API ที่ `http://127.0.0.1:10000` กับ DB ทดลองใน `data/dev/` (ไม่ใช่ข้อมูลจริง) พร้อมสินค้า หมวด หน้าแรก และออเดอร์ตัวอย่าง login หลังร้านด้วย `staff` / `dev` และ token `dev-token` · `./dev.sh reset` ล้าง DB ทดลองแล้วเริ่มใหม่

## ตรวจ

ใช้ Python 3.11 ขึ้นไป สร้าง virtual environment และติดตั้ง `pytest`, `ruff==0.16.10` และ `pyyaml==6.0.2` แล้วรัน:

```sh
.venv/bin/ruff check .
ORDER_API_DB_PATH=/tmp/su-order-api/orders.db ORDER_API_SLIPS_DIR=/tmp/su-order-api/slips .venv/bin/python -m pytest tests/ -v
```

## Contract

`contract/` ใน repo backend เป็นแหล่งความจริงของ contract ระหว่าง backend กับหน้าร้าน หน้าร้านดึง contract จาก branch `main` ของ repo backend แล้วเก็บสำเนาไว้ใน repo ของตัวเอง

กฎความเข้ากันได้:

> backend ต้องรองรับหน้าร้านที่ deploy อยู่ปัจจุบันเสมอ เพิ่มของใหม่ได้ แต่จะลบหรือเปลี่ยนความหมายของ field/endpoint ได้ก็ต่อเมื่อหน้าร้านเลิกใช้แล้ว งานที่ต้องแก้สองฝั่งให้ merge backend ก่อน

## Deploy

ภาพรวมคือ GitHub Actions สร้าง container image ของ backend และขั้น deploy บนเครื่องที่กำหนดจะดึง image ไปอัปเดต service รายละเอียดการตั้งค่าเครื่องและขั้นตอนปฏิบัติอยู่ใน skill `deploy` ของผู้ดูแลระบบ

## Backup

ทุกวันเวลา 04:00 (เวลาไทย) systemd timer `su-store-backup.timer` บนเครื่อง production จะรัน `ops/backup.sh` ซึ่งทำ `sqlite3 .backup()` ของ DB ภายใน container `su-store-api` ไว้ที่ `data/order-api/backups/` และตรวจด้วย `PRAGMA integrity_check` สำเร็จแล้วเขียน `LAST_OK` และเก็บ backup ไว้ 14 วัน ส่วนเครื่องสำรองจะดึง backup สลิป และรูปสินค้าไปเก็บเองทุกวัน (ไม่มีการลบไฟล์ฝั่งปลายทาง) ถ้าล้มเหลวให้ดูที่ `journalctl -u su-store-backup`

