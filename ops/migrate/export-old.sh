#!/usr/bin/env bash
# Run on the OLD host (copy ops/ there first). Takes a consistent snapshot of the live DB,
# fingerprints it, and sends the DB, the SU orders' slips and the product images to the new
# host's incoming/ dir. Slips already sent (rehearsal) are skipped, and nothing is ever deleted
# on either side; FP28 slips stay on the old host.
#
# usage: export-old.sh <snapshot_dir>
# env:   OLD_DATA_DIR  host path mounted at /var/data in su-order-api (see the deploy skill)
#        NEW_HOST      su-runner@<new-host>; its key only reaches incoming/ (rrsync -wo)
set -euo pipefail

fail() { echo "[export] $1" >&2; exit "$2"; }

[[ $# -eq 1 ]] || fail "วิธีใช้: $0 <snapshot_dir>" 64
[[ -n "${OLD_DATA_DIR:-}" && -n "${NEW_HOST:-}" ]] || fail "ต้องตั้ง OLD_DATA_DIR และ NEW_HOST" 64
snapshot_dir="$1"
here="$(cd "$(dirname "$0")" && pwd)"
container=su-order-api
snapshot=/var/data/su-store-migrate/orders.db
mkdir -p "$snapshot_dir"

echo "[export] snapshot DB"
docker exec -i "$container" python - /var/data/su-order-api/orders.db --snapshot "$snapshot" < "$here/../backup.py"
docker exec -i "$container" python - "$snapshot" < "$here/counts.py" > "$snapshot_dir/counts.json"
docker exec -i "$container" python - "$snapshot" --list-slips < "$here/counts.py" > "$snapshot_dir/slips.txt"
echo "[export] สลิป SU $(wc -l < "$snapshot_dir/slips.txt" | tr -d ' ') ไฟล์"

remote_shell="ssh -i $HOME/.ssh/su-store-migrate -o IdentitiesOnly=yes"
rsync -a -e "$remote_shell" "$OLD_DATA_DIR/su-store-migrate/orders.db" "$snapshot_dir/counts.json" "$NEW_HOST:"
rsync -a --ignore-existing -e "$remote_shell" --files-from="$snapshot_dir/slips.txt" "$OLD_DATA_DIR/" "$NEW_HOST:"
rsync -a --ignore-existing -e "$remote_shell" "$OLD_DATA_DIR/su-order-api/product-images" "$NEW_HOST:su-order-api/"
echo "[export] ส่งครบ: ต่อด้วย import-new.sh ที่เครื่องใหม่"
