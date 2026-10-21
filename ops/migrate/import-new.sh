#!/usr/bin/env bash
# Run on the NEW host after export-old.sh has filled ${SU_STORE_DIR}/data/order-api/incoming/.
# The API must be stopped first (docker stop su-store-api) so nothing writes the DB mid-swap.
#
# env:  SU_STORE_DIR (required)
# exit: 0 ok · 1 refused (API running, incoming incomplete, no REVISION) · 64 bad usage
set -euo pipefail

fail() { echo "[import] $1" >&2; exit "$2"; }

[[ -n "${SU_STORE_DIR:-}" ]] || fail "ต้องตั้ง SU_STORE_DIR" 64
data="$SU_STORE_DIR/data/order-api"
incoming="$data/incoming"

running="$(docker inspect --format '{{.State.Running}}' su-store-api 2>/dev/null || echo false)"
[[ "$running" != "true" ]] || fail "su-store-api ยังรันอยู่: docker stop su-store-api ก่อน" 1
[[ -f "$incoming/orders.db" && -f "$incoming/counts.json" ]] \
  || fail "ไม่พบ orders.db หรือ counts.json ใน $incoming (รัน export-old.sh ที่เครื่องเก่าก่อน)" 1
[[ -f "$SU_STORE_DIR/backend/REVISION" ]] || fail "ไม่พบ backend/REVISION: deploy backend อย่างน้อยหนึ่งครั้งก่อน" 1
IFS= read -r revision < "$SU_STORE_DIR/backend/REVISION"

# Same image as the running backend, so its Python writes the files as the container's root.
docker run --rm -i -v "$data:/var/data" --entrypoint python "ghcr.io/weerapong-gui/su-store-server:$revision" \
  - /var/data "$(date +%Y%m%d-%H%M)" < "$(dirname "$0")/import_files.py"
echo "[import] เสร็จ: start API ด้วย deploy-service.sh backend $revision แล้วรัน verify.sh"
