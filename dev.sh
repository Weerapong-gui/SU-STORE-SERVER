#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT_DIR"

if [ "${1:-}" = "reset" ]; then
  printf 'ลบฐานข้อมูลตัวอย่างใน data/dev แล้วเริ่มใหม่หรือไม่? [y/N] '
  read -r answer
  case "$answer" in
    y|Y|yes|YES) rm -rf -- "$ROOT_DIR/data/dev" ;;
    *) printf 'ยกเลิกการ reset\n'; exit 0 ;;
  esac
elif [ "$#" -gt 0 ]; then
  printf 'วิธีใช้: ./dev.sh [reset]\n' >&2
  exit 64
fi

if ! command -v docker >/dev/null 2>&1; then
  printf 'ไม่พบ docker กรุณาติดตั้ง Docker Desktop หรือ OrbStack แล้วลองอีกครั้ง\n' >&2
  exit 1
fi

docker compose -f compose.dev.yml up --build --wait
printf '\nOrder API พร้อมใช้งานที่ http://127.0.0.1:10000/health\n'
