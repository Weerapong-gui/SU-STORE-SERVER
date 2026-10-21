#!/usr/bin/env bash
# Deploy one side of SU STORE on the production host: pull the image for a commit,
# restart only that service, wait for its healthcheck, roll back on failure.
# Both repos call this copy (the frontend deploy job runs it from $SU_STORE_DIR/backend/ops/).
#
# usage: deploy-service.sh <backend|frontend> <sha>
# exit:  0 ok · 1 unhealthy, rolled back · 2 unhealthy, nothing to roll back to
#        3 another deploy held the lock too long · 4 image not pullable · 64 bad usage
set -euo pipefail

part="${1:-}"
sha="${2:-}"
log() { echo "[deploy ${part:-?}] $*"; }
fail() { log "$1" >&2; exit "$2"; }

[[ $# -eq 2 ]] || fail "วิธีใช้: $0 <backend|frontend> <sha>" 64
case "$part" in
  backend) service=su-store-api; image=ghcr.io/weerapong-gui/su-store-server; tag_variable=API_IMAGE_TAG ;;
  frontend) service=su-store-web; image=ghcr.io/weerapong-gui/su-store-web; tag_variable=WEB_IMAGE_TAG ;;
  *) fail "part ต้องเป็น backend หรือ frontend" 64 ;;
esac
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail "sha ต้องเป็น hex ตัวเล็ก 40 ตัว" 64
[[ -n "${SU_STORE_DIR:-}" ]] || fail "ต้องตั้ง SU_STORE_DIR" 64

part_dir="$SU_STORE_DIR/$part"
compose_file="$part_dir/compose.yml"
env_file="$part_dir/.env"
revision_file="$part_dir/REVISION"
health_timeout="${DEPLOY_HEALTH_TIMEOUT:-90}"
poll_seconds="${DEPLOY_POLL_SECONDS:-3}"
lock_timeout="${DEPLOY_LOCK_TIMEOUT:-600}"
keep_tags=3

# One lock for both repos: GitHub's concurrency groups only serialize within a repo.
exec 9>"$SU_STORE_DIR/.deploy.lock"
flock -w "$lock_timeout" 9 || fail "deploy อื่นกำลังรันอยู่นานเกิน ${lock_timeout} วินาที" 3

previous=""
if [[ -f "$revision_file" ]]; then
  IFS= read -r previous < "$revision_file" || true
fi

start_service() {
  env "$tag_variable=$1" docker compose -f "$compose_file" --env-file "$env_file" up -d --no-deps "$service"
}

wait_healthy() {
  local started=$SECONDS status
  while (( SECONDS - started < health_timeout )); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$service" 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] && return 0
    sleep "$poll_seconds"
  done
  return 1
}

prune_old_tags() {
  local index=0 tag
  while IFS= read -r tag; do
    [[ -n "$tag" && "$tag" != "<none>" ]] || continue
    index=$((index + 1))
    (( index > keep_tags )) || continue
    [[ "$tag" == "$sha" || "$tag" == "$previous" ]] && continue
    # A failed cleanup must not turn a good deploy red; the next deploy retries it.
    docker image rm "$image:$tag" >/dev/null || log "ลบ image $tag ไม่ได้ ข้ามไปก่อน" >&2
  done <<< "$(docker image ls "$image" --format '{{.Tag}}')"
}

log "ดึง image $sha"
docker pull "$image:$sha" || fail "ดึง image $image:$sha ไม่ได้ (build ของ commit นี้เสร็จหรือยัง?)" 4

log "เริ่ม $service ด้วย $sha (เดิม: ${previous:-ไม่มี})"
start_service "$sha"
if wait_healthy; then
  printf '%s\n' "$sha" > "$revision_file"
  log "deploy สำเร็จ: $sha"
  prune_old_tags
  exit 0
fi

log "$service ไม่ healthy ภายใน ${health_timeout} วินาที" >&2
[[ -n "$previous" ]] || fail "ไม่มี tag ก่อนหน้าให้ย้อนกลับ" 2
log "ย้อนกลับไป $previous" >&2
if start_service "$previous" && wait_healthy; then
  fail "ย้อนกลับไป $previous สำเร็จ แต่ deploy $sha ล้มเหลว" 1
fi
fail "ย้อนกลับไป $previous แล้วแต่ยังไม่ healthy ต้องเข้าไปดูที่เครื่อง" 1
