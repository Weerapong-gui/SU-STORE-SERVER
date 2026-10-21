#!/usr/bin/env bash
# Run on the NEW host once su-store-api is up on the imported DB: compares it with the
# fingerprint taken on the old host (incoming/counts.json) and checks every slip file exists.
# exit: 0 everything matches · 1 something differs (stop the cutover)
set -euo pipefail

docker exec -i su-store-api python - /var/data/su-order-api/orders.db \
  --compare /var/data/incoming/counts.json --data-root /var/data < "$(dirname "$0")/counts.py"
