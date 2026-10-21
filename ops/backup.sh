#!/usr/bin/env bash
# Daily backup of the production DB (run by su-store-backup.timer at 04:00 Bangkok time).
# The work happens in backup.py inside su-store-api, which owns the data files.
#
# env:  SU_STORE_DIR (required) · BACKUP_KEEP_DAYS (default 14)
# exit: 0 ok · 1 backup failed · 64 bad usage
set -euo pipefail

fail() { echo "[backup] $1" >&2; exit "$2"; }

[[ -n "${SU_STORE_DIR:-}" ]] || fail "ต้องตั้ง SU_STORE_DIR" 64
keep_days="${BACKUP_KEEP_DAYS:-14}"
[[ "$keep_days" =~ ^[0-9]+$ ]] || fail "BACKUP_KEEP_DAYS ต้องเป็นจำนวนวัน" 64

# Container paths: ${SU_STORE_DIR}/data/order-api is mounted at /var/data (compose.yml).
docker exec -i su-store-api python - /var/data/su-order-api/orders.db /var/data/backups "$keep_days" \
  < "$(dirname "$0")/backup.py"
